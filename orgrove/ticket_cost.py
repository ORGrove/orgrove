"""What agent work costs, per ticket and per kind of work (owner, 2026-11-05).

The ledger already has every run's ticket, role and dollars. This adds the kind of work (the ticket's `type/` label),
recorded on the run's reservation so a later relabel doesn't rewrite past spending, and from it:
  - one cost comment per ticket, edited after every settled run (no model call; the dispatcher's prefix, so the
    PM's change check and the owner-reply check ignore it);
  - spend by work type and by role for today, yesterday, the week, the month and the year, with the average per
    ticket, for the Finance report and the Office's Finance page.
Runs reserved before this existed have no work type: they take the ticket's label of today, and a ticket with no
`type/` label is "unlabelled", never guessed.
"""

import json
import subprocess
from datetime import timedelta

# Label -> work type, in the order a ticket with two labels is counted (a bug fix that adds a feature is a bug).
LABELS = (("type/bug", "bug"), ("type/feature", "feature"), ("type/refactor", "refactor"), ("type/infra", "infra"),
          ("type/docs", "docs"))   # each one is in config/stages.toml [labels.project], so an install creates it
UNLABELLED = "unlabelled"
# Runs that are not ticket work have their own row: free-time looks, the retro, meetings.
KIND_TYPES = {"free_time": "free time"}
ROLE_TYPES = {"retro": "retro", "meeting": "meeting"}
ORDER = [t for _, t in LABELS] + [UNLABELLED, "free time", "retro", "meeting"]
MARKER = "<!-- ticket-cost -->"
PREFIX = "ORGrove dispatcher: cost so far"   # dispatch.pm_changes and the reply check skip "ORGrove dispatcher:" notes


def work_type(labels):
    names = set(labels)
    return next((t for label, t in LABELS if label in names), UNLABELLED)


def run_type(reserve_event, current=None):
    """The work type of one run: what its reservation recorded, else the ticket's label today, else unlabelled."""
    if reserve_event.get("work_type"):
        return reserve_event["work_type"]
    if reserve_event.get("kind") in KIND_TYPES:
        return KIND_TYPES[reserve_event["kind"]]
    if reserve_event.get("role") in ROLE_TYPES:
        return ROLE_TYPES[reserve_event["role"]]
    return (current or {}).get(reserve_event.get("issue") or "", UNLABELLED)


def _gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, timeout=60).stdout


def labels_of(issue, gh=_gh):
    """The ticket's label names (`owner/repo#12`); None when it can't be read, so the caller records nothing."""
    repo, _, n = (issue or "").partition("#")
    if not repo or not n.isdigit():
        return None
    try:
        return [x["name"] for x in json.loads(gh("issue", "view", n, "-R", repo, "--json", "labels"))["labels"]]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        return None


def current_types(repos, gh=_gh):
    """`owner/repo#n` -> work type from today's labels, for runs reserved before work types were recorded.
    One list call per repo; a repo that can't be read is left out (its runs show as unlabelled)."""
    out = {}
    for repo in sorted(repos):
        try:
            items = json.loads(gh("issue", "list", "-R", repo, "--state", "all", "--limit", "3000",
                                  "--json", "number,labels"))
        except (subprocess.SubprocessError, OSError, ValueError):
            continue
        for it in items:
            out[f"{repo}#{it['number']}"] = work_type(x["name"] for x in it.get("labels", []))
    return out


# ---------- one ticket ---------------------------------------------------------------------------

def ticket_summary(events, issue):
    """Settled spend on one ticket: total, paid runs, and by role."""
    roles = {e["run_id"]: e["role"] for e in events if e["type"] == "reserve" and e.get("issue") == issue}
    out = {"usd": 0.0, "runs": 0, "by_role": {}, "days": set()}
    for e in events:
        if e["type"] == "cost" and e["run_id"] in roles and float(e.get("usd") or 0) > 0:
            u = float(e["usd"])
            out["usd"] += u
            out["runs"] += 1
            out["by_role"][roles[e["run_id"]]] = out["by_role"].get(roles[e["run_id"]], 0.0) + u
            out["days"].add(e["day"])
    return out


def comment_body(s, wtype):
    parts = ", ".join(f"{r} ${u:.2f}" for r, u in sorted(s["by_role"].items(), key=lambda kv: -kv[1]))
    days = sorted(s["days"])
    span = f"{days[0]} to {days[-1]}" if len(days) > 1 else (days[0] if days else "")
    return (f"{PREFIX}: **${s['usd']:.2f}** over {s['runs']} paid agent run(s)"
            + (f" ({parts})" if parts else "") + f" · work type: {wtype}" + (f" · {span}" if span else "") + ".\n\n"
            "_Updated after every settled run, from the spend ledger; free and refused runs aren't counted._\n" + MARKER)


def upsert_comment(issue, body, gh=_gh, comments=None):
    """Edit this ticket's cost comment, or write it the first time. Returns "edited", "created" or "unchanged"."""
    from orgrove import office   # late: office imports half the package
    repo, _, n = issue.partition("#")
    mine = [c for c in (comments if comments is not None else office.issue_comments(repo, int(n)))
            if MARKER in (c.get("body") or "")]
    if mine:
        if mine[-1].get("body") == body:
            return "unchanged"
        gh("api", "-X", "PATCH", f"repos/{repo}/issues/comments/{mine[-1]['id']}", "-f", f"body={body}")
        return "edited"
    gh("issue", "comment", n, "-R", repo, "--body", body)
    return "created"


# ---------- by work type, for Finance ------------------------------------------------------------

def periods(today):
    return {"today": (today, today), "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
            "week": (today - timedelta(days=today.weekday()), today), "month": (today.replace(day=1), today),
            "year": (today.replace(month=1, day=1), today)}


def by_type(runs, today):
    """period -> work type -> {usd, runs, tickets, per_ticket, by_role}; runs are finance.ai_runs rows with work_type."""
    out = {}
    for name, (a, b) in periods(today).items():
        lo, hi, rows = a.isoformat(), b.isoformat(), {}
        for r in runs:
            if not lo <= r["day"] <= hi or r["usd"] <= 0:
                continue
            t = rows.setdefault(r.get("work_type") or UNLABELLED, {"usd": 0.0, "runs": 0, "tickets": set(), "by_role": {}})
            t["usd"] += r["usd"]
            t["runs"] += 1
            if r["issue"]:
                t["tickets"].add(r["issue"])
            role = r["role"].split(" (")[0]
            t["by_role"][role] = t["by_role"].get(role, 0.0) + r["usd"]
        for t in rows.values():
            t["per_ticket"] = t["usd"] / len(t["tickets"]) if t["tickets"] else None
            t["tickets"] = len(t["tickets"])
        out[name] = dict(sorted(rows.items(), key=lambda kv: ORDER.index(kv[0]) if kv[0] in ORDER else len(ORDER)))
    return out
