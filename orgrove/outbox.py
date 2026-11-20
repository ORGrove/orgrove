"""Agent outbox: an agent writes orgrove-out/result.json, a privileged step validates and applies it (#40).

    python -m orgrove.cli apply result.json --role qa --project orgrove --issue 40 [--dry-run]

The result is untrusted model output. `validate` is pure and never raises on bad input; `apply` applies nothing
unless the whole result is clean. Only keys in the role's `outbox` allowlist may act.
"""
import copy
import datetime
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from orgrove import stages, verdict

KEYS = ("verdict", "comment", "questions", "sub_items", "stage_request", "owner_question", "route",
        "flags", "waits_for", "close", "comment_on", "plan", "board", "milestone")
MAX_TEXT = 20000
MAX_ITEMS = 10
MAX_CLOSE = 5          # tickets one run may close
MAX_WAITS = 5
MAX_COMMENT_ON = 3
MAX_CRITERIA = 10
MAX_BOARD = 40         # a roadmap pass: up to 10 tickets, four fields each
MAX_MILESTONE = 10
BOARD_FIELDS = {"priority": "Priority", "size": "Size", "start": "Roadmap Start", "target": "Roadmap Target"}   # key -> default board name
BOARD_DATES = ("start", "target")
BOARD_KEEP_OWNERS = ("priority", "size")   # set only where empty: an owner's value is never changed
PLAN_HEADINGS = ("Acceptance criteria", "Security note", "Scope")   # the sections the PM writes into the body
CLOSE_REASONS = {"completed": "completed", "not planned": "not planned"}
CLOSE_MODES = ("sub_issues", "any_open")   # which tickets a role may close (departments/*.toml `close`)
NEEDS_HUMAN = "needs/human"
NEEDS_DELIVERY = "needs/delivery"
OWNER_QUESTION = "orgrove:owner-question"
DELIVERY_ROUTE = "orgrove:delivery-route"   # the Delivery Lead settled a hold without the owner; restarts the counts   # the marker on the Delivery Lead's question; the dispatcher checks for it
ROUTES = {"pm": "needs/plan", "dev": "needs/fix", "parked": "blocked/parked", "continue": None}   # where the Delivery Lead sends a non-question
QUESTION_STRS = ("decision", "why_now", "recommendation", "if_no_answer", "reply")
# The sections every owner question has, in render_owner_question's words; the dispatcher checks a question has them all
QUESTION_SECTIONS = ("Decision for you:**", "**Why now:**", "**Options**", "**Recommendation:**",
                     "**If you don't answer:**", "**Reply here with:**")
_MENTION = re.compile(r"@(?=\w)")
_MARKER = re.compile(r"orgrove(?=\s*(?:dispatcher\s*)?:)", re.I)   # `orgrove:<marker>` and the prose `ORGrove dispatcher:` hold
VERDICT_KEYS = ("role", "issue", "branch", "head", "round", "decision")
FINDING_STRS = ("title", "severity", "file")
FINDING_INTS = ("line", "sub_issue", "sub_item")   # sub_item: this finding's sub-issue is sub_items[n] of the same result


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


@dataclass(frozen=True)
class CommentOn:
    issue: int
    body: str


@dataclass(frozen=True)
class Close:
    issue: int
    reason: str
    comment: str


@dataclass(frozen=True)
class BoardSet:
    issue: int
    field: str      # priority | size | start | target
    value: str


@dataclass(frozen=True)
class SetMilestone:
    issue: int
    title: str


@dataclass(frozen=True)
class VerdictPost:
    """The verdict comment. A blocking finding says `sub_item: n` (its place in this result's sub_items) because the
    issue does not exist yet when the model writes: apply creates the sub-issues first and writes their real numbers
    in. `body` is the comment before that, with placeholder numbers, for the checks that read it."""
    prefix: str
    verdict: dict

    @property
    def body(self):
        return self.render(None)

    def render(self, numbers):
        v = copy.deepcopy(self.verdict)
        for f in v.get("findings", []):
            if "sub_item" in f:
                i = f.pop("sub_item")
                f["sub_issue"] = numbers[i] if numbers is not None else -(i + 1)
        return f"{self.prefix}{render_verdict(v)}"


@dataclass(frozen=True)
class SetPlan:
    """The PM's three body sections, written without touching the owner's text."""
    acceptance: tuple
    security: str
    scope: str


@dataclass(frozen=True)
class WaitsFor:
    """The body's <!-- orgrove:after #a #b --> marker, which the dispatcher reads (dispatch.AFTER)."""
    numbers: tuple


class Tracker(Protocol):
    def labels(self, issue): ...
    def comment(self, issue, body): ...
    def add_label(self, issue, label): ...
    def remove_label(self, issue, label): ...
    def create_sub_issue(self, issue, title, body): ...
    def sub_issue_numbers(self, issue): ...
    def is_open(self, issue): ...
    def close(self, issue, reason, comment): ...
    def set_after(self, issue, numbers): ...
    def set_plan(self, issue, acceptance, security, scope): ...
    def board_options(self, field): ...
    def board_value(self, issue, field): ...
    def set_board(self, issue, field, value): ...
    def milestones(self): ...
    def set_milestone(self, issue, title): ...


def defang(text):
    """Model-written @mentions never notify anyone: only the profile owners are mentioned.
    Free text also can't carry a machine marker (`<!--`, `orgrove:verdict`, `orgrove:approve-protected`…):
    only `render_verdict` writes one, after `verdict.parse` accepted it."""
    text = _MENTION.sub("@​", text)
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return _MARKER.sub(lambda m: m.group(0) + "​", text)


def _iso_date(v):
    if not (isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)):
        return False
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _ticket(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


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


def validate(result, role, allowlist, owners=(), issue=None, stage_cfg=None, current_stage=None, policy=None, facts=None):
    """(actions, rejections). Any rejection means the caller must apply nothing.

    `policy` is the role's own limits (departments/*.toml: `flags`, `close`); `facts` answers what only the tracker
    knows (a ticket's sub-issues, whether one is open): the Tracker, or a fake of it."""
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
        subs = result.get("sub_items")
        n_subs = len(subs) if isinstance(subs, list) else 0
        checked, bad_ref = copy.deepcopy(clean), None
        for k, f in enumerate((checked or {}).get("findings", []), 1):
            if "sub_item" in f:
                i = f["sub_item"]
                if not 0 <= i < n_subs:
                    bad_ref = f"finding {k} points at sub_item {i}, but this result has {n_subs} sub_items (counting from 0)"
                f["sub_issue"] = -(i + 1)           # a stand-in number: the real one exists only once it is created
        body = render_verdict(checked) if checked is not None else None
        try:
            parsed = verdict.parse(body) if body else None
        except (AttributeError, TypeError, ValueError):
            parsed = None
        if clean is None:
            rej.append("verdict has a value of the wrong type (see skills/VERDICT.md)")
        elif bad_ref:
            rej.append(f"verdict: {bad_ref}")
        elif parsed is None:
            rej.append("verdict: " + (verdict.problem(checked) or "it is malformed (see skills/VERDICT.md)"))
        elif parsed["role"] != role:
            rej.append(f"verdict role {parsed['role']!r} is not {role!r}")
        elif issue is not None and parsed.get("issue") != issue:
            rej.append(f"verdict is for issue {parsed.get('issue')!r}, not {issue}")
        else:
            actions.append(VerdictPost(f"{defang(comment) + chr(10) * 2 if comment else ''}", clean))
            comment = None
    if comment:
        actions.append(Comment(defang(comment)))

    if "questions" in result:
        qs = result["questions"]
        if not isinstance(qs, list) or len(qs) > MAX_ITEMS or not all(_text(q) for q in qs):
            rej.append(f"questions must be a list of up to {MAX_ITEMS} non-empty strings")
        elif qs:
            # No agent asks the owner directly (2026-11-02): the Delivery Lead checks every question first, settles
            # what isn't the owner's, and asks the rest as one plain question (owner_question).
            actions += [Comment(f"Question for the owner, passed to the Delivery Lead first: {defang(q.strip())}") for q in qs]
            actions.append(AddLabel(NEEDS_DELIVERY))

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
        # The model sees labels ("stage/agent-review") on the ticket and sometimes sends one; the id is the part after
        # "stage/". Accepted when it names a real stage, then checked like an id (a rehearsal's Dev run was refused
        # on this and the ticket looped through 12 paid runs, 2026-11-10).
        if stage_cfg is not None and isinstance(to, str) and to.startswith("stage/") \
                and to[len("stage/"):] in stages.stage_ids(stage_cfg):
            to = to[len("stage/"):]
        if stage_cfg is None:
            rej.append("stage_request cannot be checked without the stage config")
        elif not isinstance(to, str) or (to not in stages.stage_ids(stage_cfg) and to != "backlog"):
            rej.append(f"stage_request {to!r} is not a stage in config/stages.toml")
        elif current_stage is None:
            rej.append("stage_request needs the issue's current stage")
        elif not stages.reachable(stage_cfg, current_stage, to, role):
            rej.append(f"role {role} may not move {current_stage} to {to}")
        else:
            by_id = {s["id"]: s["label"] for s in stage_cfg["stage"]}
            if current_stage in by_id:
                actions.append(RemoveLabel(by_id[current_stage]))
            if to in by_id:         # "backlog" is no stage: it is the absence of a stage label
                actions.append(AddLabel(by_id[to]))
    policy = policy or {}
    if "flags" in result:
        f = result["flags"]
        allowed = policy.get("flags") or {}
        ok = isinstance(f, dict) and set(f) <= {"add", "remove"} and f and all(
            isinstance(f[k], list) and 0 < len(f[k]) <= MAX_ITEMS and all(isinstance(x, str) for x in f[k]) for k in f)
        if not ok:
            rej.append('flags must be {"add": [labels], "remove": [labels]} (at least one, up to %d each)' % MAX_ITEMS)
        else:
            for k in ("add", "remove"):
                for label in f.get(k, []):
                    if label not in (allowed.get(k) or []):
                        rej.append(f"role {role} may not {k} the label {label!r} (stage moves go in stage_request)")
            if not rej:
                actions += [RemoveLabel(x) for x in f.get("remove", [])] + [AddLabel(x) for x in f.get("add", [])]
    if "waits_for" in result:
        w = result["waits_for"]
        if not (isinstance(w, list) and 0 < len(w) <= MAX_WAITS and all(_ticket(n) for n in w)):
            rej.append(f"waits_for must list 1 to {MAX_WAITS} ticket numbers")
        elif issue in w:
            rej.append("a ticket cannot wait for itself")
        elif facts is None or not all(facts.is_open(n) for n in w):
            rej.append("waits_for names a ticket that is not open in this project")
        else:
            actions += [WaitsFor(tuple(sorted(set(w)))), AddLabel("blocked/deps")]
    if "close" in result:
        c = result["close"]
        mode = policy.get("close")
        if not (isinstance(c, list) and 0 < len(c) <= MAX_CLOSE and all(
                isinstance(x, dict) and set(x) == {"issue", "reason", "comment"} and _ticket(x["issue"])
                and x["reason"] in CLOSE_REASONS and _text(x["comment"]) for x in c)):
            rej.append(f'close must be a list of up to {MAX_CLOSE} {{"issue", "reason": completed|not planned, "comment"}}')
        elif mode not in CLOSE_MODES:
            rej.append(f"role {role} may not close tickets")
        elif facts is None:
            rej.append("close cannot be checked without the tracker")
        else:
            subs = set(facts.sub_issue_numbers(issue)) if mode == "sub_issues" else set()
            for x in c:
                n = x["issue"]
                if n == issue and mode == "sub_issues":
                    rej.append(f"role {role} may not close the ticket it works on")
                elif mode == "sub_issues" and n not in subs:
                    rej.append(f"role {role} may close only a sub-issue of this ticket, and #{n} is not one")
                elif not facts.is_open(n):
                    rej.append(f"#{n} is not open")
                elif mode == "sub_issues" and x["reason"] != "completed":
                    rej.append(f"role {role} closes a sub-issue as completed only")
            if not rej:
                actions += [Close(x["issue"], CLOSE_REASONS[x["reason"]], defang(x["comment"].strip())) for x in c]
    if "comment_on" in result:
        c = result["comment_on"]
        if not (isinstance(c, list) and 0 < len(c) <= MAX_COMMENT_ON and all(
                isinstance(x, dict) and set(x) == {"issue", "body"} and _ticket(x["issue"]) and _text(x["body"]) for x in c)):
            rej.append(f'comment_on must be a list of up to {MAX_COMMENT_ON} {{"issue", "body"}}')
        elif facts is None or not all(facts.is_open(x["issue"]) for x in c):
            rej.append("comment_on names a ticket that is not open in this project")
        else:
            actions += [CommentOn(x["issue"], defang(x["body"].strip())) for x in c]
    if "plan" in result:
        pl = result["plan"]
        ok = (isinstance(pl, dict) and set(pl) == {"acceptance", "security", "scope"} and isinstance(pl["acceptance"], list)
              and 1 <= len(pl["acceptance"]) <= MAX_CRITERIA and all(_text(x) and len(x) <= 1200 for x in pl["acceptance"])
              and _text(pl["security"]) and _text(pl["scope"]))
        if not ok:
            rej.append('plan must be {"acceptance": [1 to %d criteria], "security": text, "scope": text}' % MAX_CRITERIA)
        else:
            actions.append(SetPlan(tuple(defang(x.strip()) for x in pl["acceptance"]), defang(pl["security"].strip()),
                                   defang(pl["scope"].strip())))
    if "board" in result:
        b = result["board"]
        if not (isinstance(b, list) and 0 < len(b) <= MAX_BOARD and all(
                isinstance(x, dict) and set(x) == {"issue", "field", "value"} and _ticket(x["issue"])
                and x["field"] in BOARD_FIELDS and isinstance(x["value"], str) for x in b)):
            rej.append(f'board must be a list of up to {MAX_BOARD} {{"issue", "field": {"|".join(BOARD_FIELDS)}, "value"}}')
        elif facts is None or not facts.has_board():
            rej.append("this project has no board to set fields on")
        else:
            for x in b:
                if x["field"] in BOARD_DATES:
                    if not _iso_date(x["value"]):
                        rej.append(f"board {x['field']} must be a date like 2026-12-31, not {x['value']!r}")
                else:
                    options = facts.board_options(x["field"])
                    if options is None:
                        rej.append(f"the board has no {x['field']} field")
                    elif x["value"] not in options:
                        rej.append(f"board {x['field']} must be one of {options}, not {x['value']!r}")
                if not facts.is_open(x["issue"]):
                    rej.append(f"#{x['issue']} is not open")
            if not rej:
                actions += [BoardSet(x["issue"], x["field"], x["value"]) for x in b]
    if "milestone" in result:
        m = result["milestone"]
        if not (isinstance(m, list) and 0 < len(m) <= MAX_MILESTONE and all(
                isinstance(x, dict) and set(x) == {"issue", "title"} and _ticket(x["issue"]) and _text(x["title"]) for x in m)):
            rej.append(f'milestone must be a list of up to {MAX_MILESTONE} {{"issue", "title"}}')
        elif facts is None:
            rej.append("milestone cannot be checked without the tracker")
        else:
            known = facts.milestones()
            for x in m:
                if x["title"] not in known:
                    rej.append(f"milestone {x['title']!r} does not exist (only an existing one may be used)")
                if not facts.is_open(x["issue"]):
                    rej.append(f"#{x['issue']} is not open")
            if not rej:
                actions += [SetMilestone(x["issue"], x["title"]) for x in m]
    if "owner_question" in result and "route" in result:
        rej.append("owner_question and route can't both be given: it is the owner's question, or it isn't")
    elif "owner_question" in result:
        q = result["owner_question"]
        problem = owner_question_problem(q)
        if problem:
            rej.append(f"owner_question: {problem}")
        elif not owners:
            rej.append("owner_question needs a profile owner to mention, and the project has none")
        else:
            actions += [Comment(render_owner_question(q, owners)), RemoveLabel(NEEDS_DELIVERY), AddLabel(NEEDS_HUMAN)]
    elif "route" in result:
        r = result["route"]
        if not (isinstance(r, dict) and set(r) <= {"to", "note"} and r.get("to") in ROUTES and _text(r.get("note"))):
            rej.append(f"route must be {{\"to\": one of {sorted(ROUTES)}, \"note\": text}}")
        else:
            opening = "Delivery Lead: parked for later." if r["to"] == "parked" else "Delivery Lead: this isn't a question for the owner."
            actions += [Comment(f"{opening} {defang(r['note'].strip())}"
                                f"\n\n<!-- {DELIVERY_ROUTE} to={r['to']} -->"),
                        RemoveLabel(NEEDS_DELIVERY)]
            if ROUTES[r["to"]]:
                actions.append(AddLabel(ROUTES[r["to"]]))
    if rej:
        return [], rej
    return actions, []


def owner_question_problem(q):
    """Why a Delivery Lead question isn't fit to send, or None. Every part is required: a question the owner can
    answer without opening another ticket (2026-11-02: "things waiting on me all of a sudden are confusing")."""
    if not isinstance(q, dict):
        return "must be an object"
    unknown = set(q) - set(QUESTION_STRS) - {"options", "urgency"}
    if unknown:
        return f"unknown key(s) {sorted(unknown)}"
    for k in QUESTION_STRS:
        if not _text(q.get(k)) or len(q[k]) > 1200:
            return f"{k} must be text of at most 1200 characters"
    opts = q.get("options")
    if not isinstance(opts, list) or not 2 <= len(opts) <= 4:
        return "options must list 2 to 4 choices"
    for o in opts:
        if not (isinstance(o, dict) and set(o) == {"choice", "effect", "cost"}
                and all(_text(o[k]) and len(o[k]) <= 600 for k in o)):
            return "each option needs choice, effect and cost (text)"
    if q.get("urgency", "now") not in ("now", "later"):
        return "urgency must be now or later"
    return None


def is_owner_question(body):
    """A Delivery Lead question: the marker and every section. Anything less goes back to the Delivery Lead."""
    return OWNER_QUESTION in body and all(x in body for x in QUESTION_SECTIONS)


def is_later(body):
    """A Delivery Lead question marked no rush: it waits on the owner, but not today."""
    return is_owner_question(body) and f"{OWNER_QUESTION} urgency=later" in body


def render_owner_question(q, owners):
    """The one format the owner sees: decision, why now, lettered options with cost, recommendation, what happens
    without an answer, and the exact reply. Defanged, so it can't mention anyone else or carry another marker."""
    later = q.get("urgency", "now") == "later"
    lines = [" ".join(f"@{o}" for o in owners) + f" **{'No rush. ' if later else ''}Decision for you:** {defang(q['decision'].strip())}",
             "", f"**Why now:** {defang(q['why_now'].strip())}", "", "**Options**"]
    for letter, o in zip("ABCD", q["options"]):
        lines.append(f"- **{letter}. {defang(o['choice'].strip())}**: {defang(o['effect'].strip())} Cost: {defang(o['cost'].strip())}")
    lines += ["", f"**Recommendation:** {defang(q['recommendation'].strip())}",
              f"**If you don't answer:** {defang(q['if_no_answer'].strip())}",
              f"**Reply here with:** {defang(q['reply'].strip())}",
              "", f"<!-- {OWNER_QUESTION} urgency={q.get('urgency', 'now')} -->"]
    return "\n".join(lines)


def apply(result, role, allowlist, tracker, issue, owners=(), stage_cfg=None, dry_run=False, policy=None):
    """(actions, report). Nothing reaches the tracker if anything is rejected, or on dry_run."""
    stage_cfg = stage_cfg or stages.load()
    current = None
    if isinstance(result, dict) and "stage_request" in result:
        try:
            current = stages.stage_of(stage_cfg, tracker.labels(issue))
        except ValueError as e:
            return [], [f"REJECTED: {e}"]
    actions, rej = validate(result, role, allowlist, owners, issue, stage_cfg, current, policy, tracker)
    if rej:
        return [], [f"REJECTED: {r}" for r in rej]
    report = [f"{'would apply' if dry_run else 'applied'}: {a}" for a in actions]
    if not dry_run:
        # the sub-issues first: a verdict's blocking findings carry their numbers, which exist only once created
        numbers = [tracker.create_sub_issue(issue, a.title, a.body) for a in actions if isinstance(a, SubIssue)]
        for i, a in enumerate(actions):
            if isinstance(a, SubIssue):
                continue
            if isinstance(a, VerdictPost):
                tracker.comment(issue, a.render(numbers))
            elif isinstance(a, Comment):
                tracker.comment(issue, a.body)
            elif isinstance(a, AddLabel):
                tracker.add_label(issue, a.label)
            elif isinstance(a, RemoveLabel):
                tracker.remove_label(issue, a.label)
            elif isinstance(a, CommentOn):
                tracker.comment(a.issue, a.body)
            elif isinstance(a, Close):
                tracker.close(a.issue, a.reason, a.comment)
            elif isinstance(a, WaitsFor):
                tracker.set_after(issue, a.numbers)
            elif isinstance(a, SetPlan):
                tracker.set_plan(issue, a.acceptance, a.security, a.scope)
            elif isinstance(a, BoardSet):
                kept = tracker.board_value(a.issue, a.field) if a.field in BOARD_KEEP_OWNERS else None
                if kept:
                    report[i] = f"skipped: #{a.issue} already has a {a.field} ({kept}): an owner's value is not changed"
                else:
                    tracker.set_board(a.issue, a.field, a.value)
            elif isinstance(a, SetMilestone):
                tracker.set_milestone(a.issue, a.title)
    return actions, report


class GhTracker:
    """The GitHub adapter. Calls: issue comment, issue edit labels, issue create, and the sub-issue link."""

    def __init__(self, repo, run=None, board=None):
        self.repo = repo
        self._run = run or self._gh
        self.board = board          # {"owner", "number", "fields": {key: board field name}} or None
        self._meta = None

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
        return made["number"]


    def sub_issue_numbers(self, issue):
        out = self._run("api", f"repos/{self.repo}/issues/{issue}/sub_issues", "--jq", ".[].number")
        return [int(x) for x in out.split()]

    def is_open(self, issue):
        try:
            out = self._run("issue", "view", str(issue), "-R", self.repo, "--json", "state", "-q", ".state")
        except subprocess.CalledProcessError:   # no such ticket here: not open
            return False
        return out.strip() == "OPEN"

    def close(self, issue, reason, comment):
        self._run("issue", "close", str(issue), "-R", self.repo, "--reason", reason, "--comment", comment)

    # --- the project board (A1b) -------------------------------------------------------------------------------

    def _graphql(self, query, **variables):
        args = ["api", "graphql", "-f", f"query={query}"]
        for k, v in variables.items():
            args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
        return json.loads(self._run(*args))["data"]

    def has_board(self):
        return bool(self.board)

    def _field_name(self, key):
        return (self.board.get("fields") or {}).get(key) or BOARD_FIELDS[key]

    def _board_meta(self):
        """The project's id and its fields: {name: {"id", "options": {name: id} or None}}. Read once."""
        if self._meta is None:
            fields = ("id fields(first: 50) { nodes { __typename ... on ProjectV2Field { id name } "
                      "... on ProjectV2SingleSelectField { id name options { id name } } } }")
            owner, number = self.board["owner"], self.board["number"]
            project = None
            for kind in ("organization", "user"):
                try:
                    data = self._graphql(f"query($login: String!, $number: Int!) {{ {kind}(login: $login) "
                                         f"{{ projectV2(number: $number) {{ {fields} }} }} }}", login=owner, number=number)
                except subprocess.CalledProcessError:
                    continue
                project = (data.get(kind) or {}).get("projectV2")
                if project:
                    break
            if not project:
                raise RuntimeError(f"board {owner}/{number} not found")
            self._meta = {"id": project["id"], "fields": {
                n["name"]: {"id": n["id"], "options": {o["name"]: o["id"] for o in n["options"]} if n.get("options") else None}
                for n in project["fields"]["nodes"] if n.get("name")}}
        return self._meta

    def board_options(self, field):
        f = self._board_meta()["fields"].get(self._field_name(field))
        return sorted(f["options"]) if f and f["options"] is not None else None

    def _item(self, issue):
        """(item id or None, {field name: value}) of the issue on this board."""
        owner, name = self.repo.split("/")
        names = " ".join(f'v{i}: fieldValueByName(name: "{self._field_name(k)}") {{ ... on ProjectV2ItemFieldSingleSelectValue '
                         f'{{ name }} ... on ProjectV2ItemFieldDateValue {{ date }} }}' for i, k in enumerate(BOARD_FIELDS))
        data = self._graphql("query($owner: String!, $name: String!, $n: Int!) { repository(owner: $owner, name: $name) "
                             "{ issue(number: $n) { id projectItems(first: 20) { nodes { id project { id } " + names + " } } } } }",
                             owner=owner, name=name, n=issue)
        issue_node = data["repository"]["issue"]
        pid = self._board_meta()["id"]
        for it in issue_node["projectItems"]["nodes"]:
            if it["project"]["id"] == pid:
                vals = {k: (it[f"v{i}"] or {}).get("name") or (it[f"v{i}"] or {}).get("date") for i, k in enumerate(BOARD_FIELDS)}
                return it["id"], vals, issue_node["id"]
        return None, {}, issue_node["id"]

    def board_value(self, issue, field):
        return self._item(issue)[1].get(field)

    def set_board(self, issue, field, value):
        meta = self._board_meta()
        item, _, node = self._item(issue)
        if item is None:       # not on the board yet: add it (this call is idempotent)
            item = self._graphql("mutation($p: ID!, $c: ID!) { addProjectV2ItemById(input: {projectId: $p, contentId: $c}) "
                                 "{ item { id } } }", p=meta["id"], c=node)["addProjectV2ItemById"]["item"]["id"]
        f = meta["fields"][self._field_name(field)]
        val = "{date: $v}" if field in BOARD_DATES else "{singleSelectOptionId: $v}"
        self._graphql("mutation($p: ID!, $i: ID!, $f: ID!, $v: " + ("Date!" if field in BOARD_DATES else "String!") + ") "
                      "{ updateProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i, fieldId: $f, value: " + val
                      + "}) { projectV2Item { id } } }", p=meta["id"], i=item, f=f["id"],
                      v=value if field in BOARD_DATES else f["options"][value])

    def milestones(self):
        out = self._run("api", f"repos/{self.repo}/milestones?state=open&per_page=100", "--jq", ".[].title")
        return [t for t in out.splitlines() if t]

    def set_milestone(self, issue, title):
        self._run("issue", "edit", str(issue), "-R", self.repo, "--milestone", title)

    def set_plan(self, issue, acceptance, security, scope):
        body = self._run("issue", "view", str(issue), "-R", self.repo, "--json", "body", "-q", ".body")
        self._run("issue", "edit", str(issue), "-R", self.repo, "--body", with_plan(body, acceptance, security, scope))

    def set_after(self, issue, numbers):
        """Put the one <!-- orgrove:after ... --> marker in the body, replacing an earlier one."""
        body = self._run("issue", "view", str(issue), "-R", self.repo, "--json", "body", "-q", ".body")
        self._run("issue", "edit", str(issue), "-R", self.repo, "--body", with_after(body, numbers))


def with_plan(body, acceptance, security, scope):
    """The body with the PM's three sections at the end, each replacing an earlier one of the same heading. The
    owner's own text, and any other section, is kept as it was."""
    text = body or ""
    for heading in PLAN_HEADINGS:
        text = re.sub(rf"(?ms)^## {re.escape(heading)}[ \t]*\n.*?(?=^## |\Z)", "", text)
    text = text.rstrip()
    sections = ("## Acceptance criteria\n" + "\n".join(f"- {c}" for c in acceptance)
                + f"\n\n## Security note\n{security}\n\n## Scope\n{scope}\n")
    return f"{text}\n\n{sections}" if text else sections


def with_after(body, numbers):
    marker = "<!-- orgrove:after " + " ".join(f"#{n}" for n in numbers) + " -->"
    stripped = re.sub(r"\s*<!--\s*orgrove:after\s+[#\d ,]+?\s*-->", "", body or "").rstrip()
    return f"{stripped}\n\n{marker}\n" if stripped else f"{marker}\n"


class FakeTracker:
    """In-memory Tracker for tests."""

    def __init__(self, labels=(), open_issues=(), subs=()):
        self._labels = set(labels)
        self.comments, self.sub_issues = [], []
        self.open_issues, self.subs = set(open_issues), list(subs)
        self.closed, self.after, self.plans = [], [], []
        self.board = None                       # {"priority": ["P0", ...], "size": [...]} when the project has a board
        self.board_values, self.board_sets = {}, []
        self.milestone_titles, self.milestone_sets = [], []

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
        number = 100 + len(self.sub_issues)
        self.subs.append(number)
        self.open_issues.add(number)
        return number

    def sub_issue_numbers(self, issue):
        return list(self.subs)

    def is_open(self, issue):
        return issue in self.open_issues

    def close(self, issue, reason, comment):
        self.closed.append((issue, reason, comment))
        self.open_issues.discard(issue)

    def set_after(self, issue, numbers):
        self.after.append((issue, tuple(numbers)))

    def set_plan(self, issue, acceptance, security, scope):
        self.plans.append((issue, tuple(acceptance), security, scope))

    def has_board(self):
        return self.board is not None

    def board_options(self, field):
        return self.board.get(field)

    def board_value(self, issue, field):
        return self.board_values.get((issue, field))

    def set_board(self, issue, field, value):
        self.board_sets.append((issue, field, value))

    def milestones(self):
        return list(self.milestone_titles)

    def set_milestone(self, issue, title):
        self.milestone_sets.append((issue, title))
