"""Agent outbox: an agent writes orgrove-out/result.json, a privileged step validates and applies it (#40).

    python -m orgrove.cli apply result.json --role qa --project orgrove --issue 40 [--dry-run]

The result is untrusted model output. `validate` is pure and never raises on bad input; `apply` applies nothing
unless the whole result is clean. Only keys in the role's `outbox` allowlist may act.
"""
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from orgrove import stages, verdict

KEYS = ("verdict", "comment", "questions", "sub_items", "stage_request")
MAX_TEXT = 20000
MAX_ITEMS = 10
NEEDS_HUMAN = "needs/human"
_MENTION = re.compile(r"@(?=\w)")
_MARKER = re.compile(r"orgrove(?=\s*(?:dispatcher\s*)?:)", re.I)   # `orgrove:<marker>` and the prose `ORGrove dispatcher:` hold
VERDICT_KEYS = ("role", "issue", "branch", "head", "round", "decision")
FINDING_STRS = ("title", "severity", "file")
FINDING_INTS = ("line", "sub_issue")


@dataclass(frozen=True)
class Comment:
    body: str


@dataclass(frozen=True)
class AddLabel:
    label: str


@dataclass(frozen=True)
class RemoveLabel:
    label: str


@dataclass(frozen=True)
class SubIssue:
    title: str
    body: str


class Tracker(Protocol):
    def labels(self, issue): ...
    def comment(self, issue, body): ...
    def add_label(self, issue, label): ...
    def remove_label(self, issue, label): ...
    def create_sub_issue(self, issue, title, body): ...


def defang(text):
    """Model-written @mentions never notify anyone: only the profile owners are mentioned.
    Free text also can't carry a machine marker (`<!--`, `orgrove:verdict`, `orgrove:approve-protected`…):
    only `render_verdict` writes one, after `verdict.parse` accepted it."""
    text = _MENTION.sub("@​", text)
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return _MARKER.sub(lambda m: m.group(0) + "​", text)


def _text(v):
    return isinstance(v, str) and 0 < len(v.strip()) <= MAX_TEXT


def clean_verdict(v):
    """The VERDICT.md keys only, strings defanged, or None if any has the wrong type. Unknown keys are dropped."""
    if not isinstance(v, dict) or not isinstance(v.get("role"), str) or not isinstance(v.get("decision"), str):
        return None
    out = {}
    for k in VERDICT_KEYS:
        if k in v:
            if isinstance(v[k], bool) or not isinstance(v[k], str if k in ("role", "branch", "head", "decision") else (str, int)):
                return None
            out[k] = defang(v[k]) if isinstance(v[k], str) else v[k]
    findings = v.get("findings", [])
    if not isinstance(findings, list):
        return None
    out["findings"] = []
    for f in findings:
        if not isinstance(f, dict):
            return None
        g = {}
        for k in FINDING_STRS:
            if k in f:
                if not isinstance(f[k], str):
                    return None
                g[k] = defang(f[k])
        for k in FINDING_INTS:
            if f.get(k) is not None:
                if isinstance(f[k], bool) or not isinstance(f[k], int):
                    return None
                g[k] = f[k]
        out["findings"].append(g)
    return out


def render_verdict(v):
    """The block, with `<`, `>` and `@` escaped inside the JSON: it can't close the comment or mention anyone."""
    dumped = json.dumps(v).replace("<", "\\u003c").replace(">", "\\u003e").replace("@", "\\u0040")
    return "<!-- orgrove:verdict\n" + dumped + "\n-->"


def validate(result, role, allowlist, owners=(), issue=None, stage_cfg=None, current_stage=None):
    """(actions, rejections). Any rejection means the caller must apply nothing."""
    actions, rej = [], []
    if not isinstance(result, dict):
        return [], ["result must be a JSON object"]
    for key in result:
        if key not in KEYS:
            rej.append(f"unknown key {key!r}")
        elif key not in allowlist:
            rej.append(f"role {role} may not use {key!r}")
    if rej:
        return [], rej
    owners = list(owners)

    comment = result.get("comment")
    if comment is not None and not _text(comment):
        rej.append("comment must be a non-empty string")
        comment = None
    if "verdict" in result:
        v = result["verdict"]
        clean = clean_verdict(v)
        body = render_verdict(clean) if clean is not None else None
        try:
            parsed = verdict.parse(body) if body else None
        except (AttributeError, TypeError, ValueError):
            parsed = None
        if parsed is None:
            rej.append("verdict is malformed or breaks the rules in skills/VERDICT.md")
        elif parsed["role"] != role:
            rej.append(f"verdict role {parsed['role']!r} is not {role!r}")
        elif issue is not None and parsed.get("issue") != issue:
            rej.append(f"verdict is for issue {parsed.get('issue')!r}, not {issue}")
        else:
            actions.append(Comment(f"{defang(comment) + chr(10) * 2 if comment else ''}{body}"))
            comment = None
    if comment:
        actions.append(Comment(defang(comment)))

    if "questions" in result:
        qs = result["questions"]
        if not isinstance(qs, list) or len(qs) > MAX_ITEMS or not all(_text(q) for q in qs):
            rej.append(f"questions must be a list of up to {MAX_ITEMS} non-empty strings")
        elif qs:
            if not owners:
                rej.append("questions need a profile owner to mention, and the project has none")
            else:
                mention = " ".join(f"@{o}" for o in owners)
                actions += [Comment(f"{mention} {defang(q.strip())}") for q in qs]
                actions.append(AddLabel(NEEDS_HUMAN))

    if "sub_items" in result:
        subs = result["sub_items"]
        ok = isinstance(subs, list) and len(subs) <= MAX_ITEMS and all(
            isinstance(s, dict) and set(s) <= {"title", "body"} and _text(s.get("title")) and len(s["title"]) <= 256
            and (s.get("body") is None or _text(s["body"])) for s in subs)
        if not ok:
            rej.append(f"sub_items must be a list of up to {MAX_ITEMS} objects with a title (and optional body)")
        else:
            actions += [SubIssue(defang(s["title"].strip()), defang(s.get("body") or "")) for s in subs]

    if "stage_request" in result:
        to = result["stage_request"]
        if stage_cfg is None:
            rej.append("stage_request cannot be checked without the stage config")
        elif not isinstance(to, str) or to not in stages.stage_ids(stage_cfg):
            rej.append(f"stage_request {to!r} is not a stage in config/stages.toml")
        elif current_stage is None:
            rej.append("stage_request needs the issue's current stage")
        elif not stages.allowed(stage_cfg, current_stage, to, role):
            rej.append(f"role {role} may not move {current_stage} to {to}")
        else:
            by_id = {s["id"]: s["label"] for s in stage_cfg["stage"]}
            if current_stage in by_id:
                actions.append(RemoveLabel(by_id[current_stage]))
            actions.append(AddLabel(by_id[to]))
    if rej:
        return [], rej
    return actions, []


def apply(result, role, allowlist, tracker, issue, owners=(), stage_cfg=None, dry_run=False):
    """(actions, report). Nothing reaches the tracker if anything is rejected, or on dry_run."""
    stage_cfg = stage_cfg or stages.load()
    current = None
    if isinstance(result, dict) and "stage_request" in result:
        try:
            current = stages.stage_of(stage_cfg, tracker.labels(issue))
        except ValueError as e:
            return [], [f"REJECTED: {e}"]
    actions, rej = validate(result, role, allowlist, owners, issue, stage_cfg, current)
    if rej:
        return [], [f"REJECTED: {r}" for r in rej]
    report = [f"{'would apply' if dry_run else 'applied'}: {a}" for a in actions]
    if not dry_run:
        for a in actions:
            if isinstance(a, Comment):
                tracker.comment(issue, a.body)
            elif isinstance(a, AddLabel):
                tracker.add_label(issue, a.label)
            elif isinstance(a, RemoveLabel):
                tracker.remove_label(issue, a.label)
            elif isinstance(a, SubIssue):
                tracker.create_sub_issue(issue, a.title, a.body)
    return actions, report


class GhTracker:
    """The GitHub adapter. Calls: issue comment, issue edit labels, issue create, and the sub-issue link."""

    def __init__(self, repo, run=None):
        self.repo = repo
        self._run = run or self._gh

    @staticmethod
    def _gh(*args):
        return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout

    def labels(self, issue):
        out = self._run("issue", "view", str(issue), "-R", self.repo, "--json", "labels", "-q", ".labels[].name")
        return out.split()

    def comment(self, issue, body):
        self._run("issue", "comment", str(issue), "-R", self.repo, "--body", body)

    def add_label(self, issue, label):
        self._run("issue", "edit", str(issue), "-R", self.repo, "--add-label", label)

    def remove_label(self, issue, label):
        self._run("issue", "edit", str(issue), "-R", self.repo, "--remove-label", label)

    def create_sub_issue(self, issue, title, body):
        made = json.loads(self._run("api", f"repos/{self.repo}/issues", "-f", f"title={title}", "-f", f"body={body}"))
        self._run("api", f"repos/{self.repo}/issues/{issue}/sub_issues", "-F", f"sub_issue_id={made['id']}")


class FakeTracker:
    """In-memory Tracker for tests."""

    def __init__(self, labels=()):
        self._labels = set(labels)
        self.comments, self.sub_issues = [], []

    def labels(self, issue):
        return sorted(self._labels)

    def comment(self, issue, body):
        self.comments.append((issue, body))

    def add_label(self, issue, label):
        self._labels.add(label)

    def remove_label(self, issue, label):
        self._labels.discard(label)

    def create_sub_issue(self, issue, title, body):
        self.sub_issues.append((issue, title, body))
