"""The owner's own Claude Code sessions on this laptop, for the Office (both editions; plan 2026-11-20, Phase 1).

Read-only: it follows the transcripts Claude Code already writes under ~/.claude/projects/<dir>/<session>.jsonl and
<session>/subagents/agent-<id>.jsonl. It never touches ~/.claude/settings.json and never sends transcript text to
the page: a session is only its project folder's name, a state and when that state began. The idea of reading the
transcripts this way, and the 7 s "may need you" guess, come from pixel-agents (MIT, see NOTICE).

States: working (a turn is running), idle (the turn ended; it waits for your next message), waiting (it needs you:
a question or a plan to approve is open, or, a guess, a tool that can be stopped at a permission prompt in the
session's permission mode has run 7 s with nothing new, which is how a prompt looks from the transcript). A session leaves the Office 2 min after its file last changed, or
30 min after, while a question is open.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from orgrove import editions

try:   # ORGrove Pro only (orgrove/editions.py, part "sessions_live"): what a session is doing, and its context gauge
    from orgrove import office_sessions_live as live
except ImportError:
    live = None
try:   # ... and the opt-in hook behind exact prompts (Phase 3)
    from orgrove import session_hooks
except ImportError:
    session_hooks = None

ROOT = Path(os.environ.get("ORGROVE_CLAUDE_PROJECTS", Path.home() / ".claude" / "projects"))
ACTIVE_S = 120            # a session whose file has not changed for 2 min has left
ASKING_S = 30 * 60        # ... unless it is asking you something: then it stays half an hour
PERMISSION_GUESS_S = 7    # a tool running this long with nothing new probably waits on a permission prompt
TAIL_BYTES = 256 * 1024   # first look at a file reads only its end: a resumed session can be 100+ MB
MODE_SCAN_BYTES = 16 * 1024 * 1024   # ... and looks back this far for the permission mode (screenshots fill the end)
ASKS = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}
LONG_TOOLS = {"Agent", "Task", "Workflow"}   # run for minutes by design; not a permission guess
NEVER_ASK = {"Read", "Grep", "Glob", "LS", "TodoWrite", "ToolSearch"}   # read-only tools: no permission prompt
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}             # allowed without asking in acceptEdits mode
QUIET_MODES = {"auto", "bypassPermissions", "dontAsk", "plan"}          # sessions that (almost) never stop to ask
# Phase 3 (ORGrove Pro, opt-in): what ORGrove's Claude Code hook writes, one small file per session (session_hook.py)
EVENTS = Path(os.environ.get("ORGROVE_SESSION_EVENTS", Path.home() / ".orgrove" / "session-events"))
EXACT = set(ASKS.values()) | {"permission"}  # waits that are known, not guessed: a session stays half an hour on them


_ROOTS = {}


def project_name(cwd):
    """The repository's top folder for a working directory (a session that cd'd into orgrove/office_static is still
    'orgrove'), else the folder itself. Cached: one stat walk per new directory."""
    if cwd not in _ROOTS:
        p = Path(cwd)
        top = next((q for q in (p, *p.parents) if (q / ".git").exists()), None)
        _ROOTS[cwd] = (top or p).name or str(p)
    return _ROOTS[cwd]


_MODE = re.compile(rb'"permissionMode"\s*:\s*"([A-Za-z]{1,40})"')


def last_mode(path, end):
    """The newest permissionMode before byte `end`, reading backwards in 1 MB steps, at most MODE_SCAN_BYTES."""
    step, pos = 1 << 20, end
    try:
        with open(path, "rb") as f:
            while pos > 0 and end - pos < MODE_SCAN_BYTES:
                start = max(0, pos - step)
                f.seek(start)
                found = _MODE.findall(f.read(min(end, pos + 80) - start))   # overlap: a key cut at a step's edge
                if found:
                    return found[-1].decode()
                pos = start
    except OSError:
        return None      # the file went away mid-read: the mode is simply unknown
    return None


def helper_kind(path):
    """A helper's type from its .meta.json ("Explore", "Plan"…), never its description: that is the owner's words."""
    try:
        kind = json.loads(path.with_suffix(".meta.json").read_text()).get("agentType")
    except (OSError, ValueError, AttributeError):
        return None
    return kind if isinstance(kind, str) else None


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


class Transcript:
    """One transcript file, read incrementally. Holds only state, never message text."""

    def __init__(self, path):
        self.path = path
        self.offset = None
        self.buf = b""
        self.cwd = None
        self.state = "idle"
        self.since = None          # when the current state began (epoch s)
        self.last = None           # time of the newest record
        self.pending = {}          # open tool_use id -> tool name
        self.ended = False         # a sub-agent's run is over
        self.mode = None           # the session's permission mode, from its newest prompt
        self.doing = {}            # open tool_use id -> (label, reading): Pro only, never the tool's input itself
        self.used = None           # context tokens of the newest request: Pro only

    def _set(self, state, t):
        if state != self.state or self.since is None:
            self.state, self.since = state, t

    def feed(self, rec):
        t = _ts(rec.get("timestamp")) or self.last
        if rec.get("cwd"):
            self.cwd = rec["cwd"]
        if rec.get("permissionMode"):
            self.mode = rec["permissionMode"]
        kind = rec.get("type")
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        if kind == "assistant":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    self.pending[b["id"]] = b.get("name", "")
                    if live:
                        self.doing[b["id"]] = live.activity(b.get("name", ""), b.get("input"))
            if live and (used := live.context_used(msg.get("usage"))):
                self.used = used
            if msg.get("stop_reason") == "end_turn":
                self.pending.clear()
                self.doing.clear()
                self.ended = True
                self._set("idle", t)
            else:
                self.ended = False
                self._set("working", t)
        elif kind == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self.pending.pop(b.get("tool_use_id"), None)
                        self.doing.pop(b.get("tool_use_id"), None)
            self.ended = False
            self._set("working", t)
        elif kind == "system" and rec.get("subtype") == "stop_hook_summary":
            self.pending.clear()
            self.doing.clear()
            self._set("idle", t)
        else:
            return                 # bookkeeping records (titles, snapshots, api requests) say nothing about state
        if t is not None:
            self.last = t

    def read(self):
        """Read what was appended since the last call. Partial last lines wait for the next call."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if self.offset is None or size < self.offset:       # first look, or the file was rewritten
            self.offset = max(0, size - TAIL_BYTES)
            self.buf, skip = b"", self.offset > 0
            if skip:
                self.mode = last_mode(self.path, self.offset)
        else:
            skip = False
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(size - self.offset)
        except OSError:
            return
        self.offset += len(data)
        data = self.buf + data
        if skip:                                             # began mid-line: drop the cut first line
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
        *lines, self.buf = data.split(b"\n")
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                self.feed(rec)

    def may_ask(self, tool):
        """Could this tool be stopped at a permission prompt in this session's mode? A long build in auto mode is
        not a prompt: guessing there put a "may need you" sign on every busy session (seen 2026-11-21)."""
        if tool in LONG_TOOLS or tool in NEVER_ASK or self.mode in QUIET_MODES:
            return False
        return not (self.mode == "acceptEdits" and tool in EDIT_TOOLS)

    def view(self, now, mtime, ev=None):
        """(state, why, since) as the Office shows it now. `ev`: the session's newest hook event (Phase 3), if any.
        A permission prompt the hook saw is exact while nothing newer reached the transcript (an answer to it, yes or
        no, writes the tool's result); a session with the hook never gets the 7 s guess."""
        asks = [ASKS[n] for n in self.pending.values() if n in ASKS]
        if asks:
            return "waiting", asks[0], self.since
        if ev and self.state == "working" and ev["t"] > (self.last or 0) and (
                ev["event"] == "PermissionRequest" or (ev["event"] == "Notification" and ev.get("type") == "permission_prompt")):
            return "waiting", "permission", ev["t"]
        quiet = now - max(self.last or 0, mtime)
        if (ev is None and self.state == "working" and quiet >= PERMISSION_GUESS_S
                and any(self.may_ask(n) for n in self.pending.values())):
            return "waiting", "maybe-permission", max(self.last or 0, mtime) + PERMISSION_GUESS_S
        return self.state, None, self.since

    def detail(self, state):
        """Pro: {"doing", "reading", "context"} for the page. The newest open tool is what the person is doing; a
        working turn with none open is thinking. Nothing here is the tool's input: see office_sessions_live."""
        label, reading = (next(reversed(self.doing.values())) if self.doing
                          else (live.THINKING if state == "working" else None, False))
        return {"doing": label if state != "idle" else None, "reading": reading and state == "working",
                "context": live.context(self.used)}


class Sessions:
    """Every live session on this laptop. Thread-safe; cheap to call each second (it stats files, reads only appends)."""

    def __init__(self, root=None, clock=time.time):
        self.root = Path(root) if root is not None else ROOT
        self.clock = clock
        self.files = {}
        self.lock = threading.Lock()
        self.rich = False
        self.events = {}           # session id -> (mtime, newest hook event)
        self.swept = 0.0

    def _event(self, sid):
        """The session's newest hook event, read again only when its file changed. None without the hook."""
        p = EVENTS / f"{sid}.json"
        try:
            m = p.stat().st_mtime
        except OSError:
            self.events.pop(sid, None)
            return None
        if sid not in self.events or self.events[sid][0] != m:
            try:
                ev = json.loads(p.read_text())
                ev = ev if isinstance(ev, dict) and isinstance(ev.get("t"), (int, float)) else None
            except (OSError, ValueError):
                ev = None
            self.events[sid] = (m, ev)
        return self.events[sid][1]

    def _track(self, path):
        tr = self.files.get(path)
        if tr is None:
            tr = self.files[path] = Transcript(path)
        tr.read()
        return tr

    def _helpers(self, session_dir, now):
        out = []
        for p in sorted((session_dir / "subagents").glob("agent-*.jsonl")):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if now - mtime > ACTIVE_S:
                continue
            tr = self._track(p)
            if tr.ended:
                continue
            state, why, since = tr.view(now, mtime)
            kind = helper_kind(p)
            out.append({"id": p.stem.removeprefix("agent-")[:8], "kind": kind,
                        "state": state, "why": why, "since": since} | (tr.detail(state) if self.rich else {}))
        return out

    def snapshot(self):
        now = self.clock()
        self.rich = rich()
        sessions, seen = [], set()
        with self.lock:
            for p in self.root.glob("*/*.jsonl"):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if now - mtime > ASKING_S:
                    continue
                seen.add(p)
                tr = self._track(p)
                ev = self._event(p.stem) if self.rich else None
                if ev and ev["event"] == "SessionEnd" and ev["t"] >= (tr.last or 0):
                    continue                                 # the hook saw it close: it leaves now, not in 2 min
                state, why, since = tr.view(now, mtime, ev)
                if now - mtime > ACTIVE_S and why not in EXACT:
                    continue
                sessions.append({"id": p.stem[:8], "project": project_name(tr.cwd) if tr.cwd else p.parent.name,
                                 "state": state, "why": why, "since": since,
                                 "last": max(tr.last or 0, mtime), "helpers": self._helpers(p.with_suffix(""), now)}
                                | (tr.detail(state) | {"exact": ev is not None} if self.rich else {}))
            for gone in [p for p in self.files if p.parent.name != "subagents" and p not in seen]:
                del self.files[gone]                         # forget files that left, so memory stays flat
        sessions.sort(key=lambda s: (s["project"], s["id"]))
        for sid in [s for s in self.events if s not in {p.stem for p in seen}]:
            del self.events[sid]
        if self.rich and now - self.swept > 3600:
            self.swept = now
            sweep(now)
        return {"sessions": sessions, "enabled": True, "rich": self.rich, "now": now}


def sweep(now, root=None):
    """Delete hook files of sessions quiet for a day, so the folder never grows (one small file per session)."""
    for p in (root or EVENTS).glob("*.json"):
        try:
            if now - p.stat().st_mtime > 86400:
                p.unlink()
        except OSError:
            continue                                 # gone already, or not ours to delete: leave it


def rich():
    """Whether this copy shows what each session is doing (ORGrove Pro, licensed): the part's file is here and the
    licence grants it. Community has neither, and its sessions carry no "doing" or "context"."""
    return live is not None and editions.has("sessions_live")


SESSIONS = Sessions()


def snapshot():
    """What /api/sessions answers. Off with ORGROVE_SESSIONS=0; no ~/.claude means no sessions, not an error."""
    if os.environ.get("ORGROVE_SESSIONS", "1") == "0":
        return {"sessions": [], "enabled": False, "now": time.time()}
    snap = SESSIONS.snapshot()
    if snap["rich"] and session_hooks:
        snap["hooks"] = session_hooks.status()
    return snap


def set_hooks(on):
    """The Office's switch for exact prompts (Pro). Only ever called from the owner's click (office.py checks the
    request is this Office page's own, with its CSRF token). Raises HooksRefused when it can't be done safely."""
    if not (rich() and session_hooks):
        raise PermissionError("exact prompts are part of ORGrove Pro")
    try:
        return session_hooks.turn_on() if on else session_hooks.turn_off()
    except session_hooks.Refused as exc:
        raise HooksRefused(str(exc)) from None


class HooksRefused(Exception):
    """The settings file couldn't be changed safely; nothing was written."""
