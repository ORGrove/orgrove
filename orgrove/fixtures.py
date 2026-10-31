"""Golden decision fixtures (#31): the dispatcher's inputs and the actions it chose, as JSON.

    python -m orgrove.fixtures record --project orgrove [--out tests/golden/<name>.json]

Read-only: it reads the same state `dispatch.main()` reads and runs `board_sync()` + `decide()` on it. It never
calls `execute()`, `record_status()` or a `gh` write. `tests/test_golden.py` replays every fixture and compares.
A fixture holds only what `decide()` and `board_sync()` read: no comment bodies, no env, no ledger events.
"""
import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from orgrove import budget, dispatch, manifest, office, stages

GOLDEN = Path(__file__).resolve().parent.parent / "tests" / "golden"
CARD_KEYS = ("number", "title", "stage", "labels", "flags", "priority", "milestone", "target", "on_board", "board_status")


def trim_card(c):
    return {k: c.get(k) for k in CARD_KEYS}


def trim_review(review):
    out = {}
    for n, r in review.items():
        out[str(n)] = {
            "head": r["head"],
            "verdicts": {role: {"role": role, "decision": v["decision"], "head": v.get("head")}
                         for role, v in r["verdicts"].items()},
            "rounds": r["rounds"], "open_subs": r["open_subs"], "ci": r["ci"],
            "ci_failing": [list(x) for x in r["ci_failing"]], "ci_bounces": r["ci_bounces"],
            "ci_kicks": r["ci_kicks"], "head_age_min": round(r["head_age_min"], 1), "granted": r["granted"],
        }
    return out


def trim_dept(dept):
    return {"roles": {role: {"skill": r["skill"], "wip": r.get("wip", 1), "ceiling_usd": r["ceiling_usd"],
                             **({"projects": r["projects"]} if "projects" in r else {})}
                      for role, r in dept["roles"].items()}}


def trim_cfg(cfg):
    return {"max_review_rounds": cfg["max_review_rounds"],
            "stage": [{"id": s["id"], "board_status": s["board_status"]} for s in cfg["stage"]]}


def trim_row(row):
    return {"role": row["role"], "status": row["status"], "break_since": row["break_since"],
            "break_min": row["break_min"], "on": [{"number": o["number"]} for o in row["on"]]}


def trim_deploy(d):
    return {k: d[k] for k in ("state", "age_min", "url", "failures") if k in d}


def trim_awaiting(a):
    return {"head": a["head"], "ci": a["ci"], "ci_failing": [list(x) for x in a.get("ci_failing", [])],
            "ci_bounces": a.get("ci_bounces", 0), "url": a.get("url", "")}


def gather(prof):
    """The inputs `dispatch.main()` builds for one project, in fixture (JSON) form. Reads only."""
    bcfg = budget.load()
    day = datetime.now(ZoneInfo(bcfg["timezone"])).date().isoformat()
    events = office.ledger_events(day[:7])
    spent_today, spent_month = budget.spent(events, day)
    budget_left = min(bcfg["daily_usd"], bcfg["monthly_usd"] - spent_month + spent_today) - spent_today
    budget_kind = "monthly" if bcfg["monthly_usd"] - spent_month < bcfg["daily_usd"] - spent_today else "daily"
    now = datetime.now(timezone.utc)
    dept = manifest.department(prof["departments"][0])
    cfg = stages.load(manifest.ROOT / dept["stages"])
    repo, owners = prof["repo"], prof.get("owners", [])
    board = office.board_items(prof)
    skip = set(prof.get("skip_issues", []))
    cards = office.pipeline(repo, cfg, board, skip)
    roadmap_request = None
    if prof.get("roadmap_issue"):
        labels = office.gh_json("issue", "view", str(prof["roadmap_issue"]), "-R", repo, "--json", "labels")["labels"]
        if any(label["name"] == "needs/roadmap" for label in labels):
            roadmap_request = prof["roadmap_issue"]
    merged = dispatch.merged_issues(repo, cards)
    env = (prof.get("environments") or {}).get("dev")
    deploys = dispatch.deploy_states(repo, env["deployed_by"], merged, now, known_failing=env.get("known_failing", ())) \
        if env and merged else {}
    return {
        "dept": trim_dept(dept), "cfg": trim_cfg(cfg),
        "cards": [trim_card(c) for c in cards],
        "board": {str(n): {"item_id": b["item_id"], "status": b["status"]} for n, b in board.items()},
        "open_numbers": sorted({c["number"] for c in cards if c["stage"] != "done"} | skip),
        "status_map": prof.get("status_map"),
        "roster_rows": [trim_row(r) for r in office.roster(dept, cards, office.runs(events))],
        "review": trim_review(dispatch.review_state(repo, cards, owners)),
        "merged": sorted(merged),
        "backlog": [c["number"] for c in cards if c["stage"] == "backlog"],
        "now": now.isoformat(timespec="seconds"), "spent_today": 0.0, "daily_cap": budget_left,
        "focus": prof.get("milestone_focus"), "roadmap_request": roadmap_request,
        "has_env": bool(prof.get("environments")),
        "answered": sorted(dispatch.answered_issues(repo, cards, owners)),
        "budget_kind": budget_kind,
        "sanity_passed": sorted(dispatch.sanity_passes(repo, cards)),
        "refused": dispatch.provider_refusal(events, now),
        "deploys": {str(n): trim_deploy(d) for n, d in deploys.items()},
        "awaiting_ci": {str(n): trim_awaiting(a) for n, a in dispatch.awaiting_pr_ci(repo, cards, owners).items()},
        "deps": {str(n): sorted(w) for n, w in dispatch.dependency_state(repo, cards).items()},
    }


def replay(inputs):
    """board_sync + decide on a deep copy of the inputs (decide mutates cards). Returns {"actions", "explain"}."""
    i = copy.deepcopy(inputs)
    cards = i["cards"]
    by_number = {c["number"]: c for c in cards}
    board = {int(n): b for n, b in i["board"].items()}
    explain = []
    actions = dispatch.board_sync(i["cfg"], cards, board, set(i["open_numbers"]), i["status_map"]) + dispatch.decide(
        i["dept"], i["cfg"], cards, i["roster_rows"], {int(n): r for n, r in i["review"].items()}, set(i["merged"]),
        [by_number[n] for n in i["backlog"]], datetime.fromisoformat(i["now"]), i["spent_today"], i["daily_cap"],
        focus=i["focus"], roadmap_request=i["roadmap_request"], has_env=i["has_env"], answered=set(i["answered"]),
        explain=explain, budget_kind=i["budget_kind"], sanity_passed=set(i["sanity_passed"]), refused=i["refused"],
        deploys={int(n): d for n, d in i["deploys"].items()},
        awaiting_ci={int(n): a for n, a in i["awaiting_ci"].items()},
        deps={int(n): w for n, w in i["deps"].items()}, project=i.get("project"))
    return {"actions": actions, "explain": explain}


def fixture(name, source, note, inputs):
    return {"name": name, "source": source, "note": note, "inputs": inputs, **replay(inputs)}


def write(path, fx):
    Path(path).write_text(json.dumps(fx, indent=1, sort_keys=True) + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(prog="orgrove.fixtures")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record the live inputs and the actions decide() chooses (read-only)")
    r.add_argument("--project", required=True)
    r.add_argument("--out", help="default: tests/golden/live-<project>.json")
    a = p.parse_args(argv)
    prof = manifest.project(a.project)
    inputs = gather(prof)
    out = Path(a.out) if a.out else GOLDEN / f"live-{a.project}.json"
    fx = fixture(out.stem, "live", f"recorded {inputs['now']} from {prof['repo']}", inputs)
    write(out, fx)
    print(f"{out}: {len(inputs['cards'])} cards, {len(fx['actions'])} action(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
