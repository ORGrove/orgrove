"""Budget gate CLI used by every role run (#3).

    python -m orgrove.cli reserve --run-id R --role dev --issue owner/repo#12 [--ceiling 1.5]
        exit 0 = go, exit 3 = refused (a `refused` event is logged; the caller labels blocked/budget)
    python -m orgrove.cli settle --run-id R (--usd 0.42 | --cost RESULT_JSON) [--error "API Error: 400 ..."]
    python -m orgrove.cli status
    python -m orgrove.cli apply --role R --project P --issue N --outbox DIR [--cost RESULT_JSON --run-id R]
        validates DIR/result.json against the role's allowlist, performs the allowed side effects, then settles the
        run's cost. The only writers of the ledger are the dispatcher, `apply` and the Office meeting core (#41).
    python -m orgrove.cli config check [--root PATH]
        exit 0 and "ok" for a valid v2 config tree; every violation printed and exit 1 otherwise (#37)

The ledger checkout is $ORGROVE_LEDGER_DIR (a clone with the `ledger` branch).
"""
import argparse
import json
import math
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone

from orgrove import agent_result, budget, ticket_cost
from orgrove.ledger import GitLedger

REFUSED = 3
REJECTED = 4


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reserve(ledger, cfg, run_id, ceiling, role, issue, ts=None, kind="ticket", work_type=None):
    ts = ts or now()

    def step(lg):
        day = budget.local_day(ts, cfg["timezone"])
        events = lg.events(month=day[:7])
        if any(e["type"] == "reserve" and e["run_id"] == run_id for e in events):
            return day, None, budget.Decision(False, day, ceiling, *budget.spent(events, day), "run_id already reserved")
        d = budget.decide(events, ts, ceiling, cfg, kind, issue=issue, role=role)
        event = {"ts": ts, "day": day, "run_id": run_id, "role": role, "issue": issue,
                 "type": "reserve" if d.ok else "refused", "usd": ceiling if d.ok else 0.0,
                 "msg": d.reason} | ({"kind": kind} if kind != "ticket" else {}) \
            | ({"work_type": work_type} if work_type else {})
        return day, event, d

    return ledger.transact(step)


def valid_cost(usd):
    """A cost the ledger may record: a real, finite, non-negative number (never a bool)."""
    return isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd) and usd >= 0


def settle(ledger, cfg, run_id, usd, ts=None, error=None, usage=None, outbox_refused=None):
    if not valid_cost(usd):  # a negative or non-finite cost would lower or poison the day's spend
        raise SystemExit(f"refusing to settle run {run_id} at {usd!r}: not a finite, non-negative cost")
    ts = ts or now()

    def step(lg):
        res = next((e for e in reversed(lg.events()) if e["type"] == "reserve" and e["run_id"] == run_id), None)
        if res is None:
            raise SystemExit(f"no reservation for run {run_id}")
        event = {"ts": ts, "day": res["day"], "run_id": run_id, "role": res["role"],
                 "issue": res["issue"], "type": "cost", "usd": round(usd, 4), "msg": "settled"}
        if error:  # the AI provider refused the run; the dispatcher pauses on it (dispatch.provider_refusal)
            event["error"] = error[:300]
        if usage:  # tokens and turns (agent_result.usage): where the money went, not only how much
            event["usage"] = usage
        if outbox_refused:  # the run was paid for, its result was refused and changed nothing (dispatch.outbox_refusals)
            event["outbox_refused"] = outbox_refused[:200]
        return res["day"], event, event

    return ledger.transact(step)


def reported_cost(path):
    """total_cost_usd from the agent's --output-format json result, or None when absent or unreadable."""
    try:
        with open(path) as f:
            cost = json.load(f).get("total_cost_usd")
    except (OSError, ValueError, AttributeError):
        return None
    return float(cost) if valid_cost(cost) else None


def settle_run(a, ledger=None, cfg=None, outbox_refused=None):
    """Settle this job's own reservation at the reported cost. Without a cost the reservation stays at its ceiling."""
    cost = reported_cost(a.cost)
    if cost is None:
        print("no cost reported: the reservation stays at its full ceiling")
        ledger = ledger or GitLedger(os.environ.get("ORGROVE_LEDGER_DIR", ".ledger"))
        for line in budget.expired_log_lines(ledger.events(), now()):
            print(line)
        return
    with open(a.cost) as f:
        raw = f.read()
    cfg = cfg or budget.load()
    ledger = ledger or GitLedger(os.environ.get("ORGROVE_LEDGER_DIR", ".ledger"))
    error = agent_result.refusal(raw) or None
    # A provider refusal is the provider's fault and has its own pause: only a paid run whose result was refused counts.
    event = settle(ledger, cfg, a.run_id, cost, error=error, usage=agent_result.usage(raw),
                   outbox_refused=None if error else outbox_refused)
    cost_comment(ledger, event)
    print(f"settled run {a.run_id} at ${cost:g}")
    for line in budget.expired_log_lines(ledger.events(), now()):
        print(line)


def board_of(prof):
    """The project's board for the outbox: its owner, number and the board's own names for the fields, or None."""
    b = prof.get("board")
    if not b:
        return None
    names = prof.get("board_fields", {})
    return {"owner": b["owner"], "number": b["number"], "fields": {k: names[k] for k in outbox_fields() if k in names}}


def outbox_fields():
    from orgrove import outbox
    return outbox.BOARD_FIELDS


def process_result(project, role, issue, path, tracker=None, dry_run=False, out=print):
    """Read a role's result file, check it against its role and its ticket, and (unless dry_run) apply it. The one code
    path the apply job and the agent job's own check before finishing share, so a result the check passes is a result
    apply accepts. Returns (rc, why it was refused or "", the report lines)."""
    from orgrove import manifest, outbox  # lazy: only this path needs them

    prof = manifest.project(project)
    role_cfg = manifest.department(prof["departments"][0])["roles"][role]
    policy = {"flags": role_cfg.get("flags"), "close": role_cfg.get("close")}   # what this role may change
    try:
        with open(path) as f:
            result = json.load(f)
    except (OSError, ValueError) as e:     # no result, or not JSON: refused as "no result", nothing is applied
        out(f"REJECTED: cannot read result: {e}")
        return REJECTED, f"cannot read the result: {type(e).__name__}", [f"REJECTED: cannot read result: {e}"]
    _, report = outbox.apply(result, role, role_cfg["outbox"], tracker or outbox.GhTracker(prof["repo"], board=board_of(prof)), issue,
                             owners=prof.get("owners", []), dry_run=dry_run, policy=policy)
    out("\n".join(report))
    refused = next((line[len("REJECTED: "):] for line in report if line.startswith("REJECTED")), "")
    return (REJECTED if refused else 0), refused, report


REFUSED_MARKER = "orgrove:result-refused"    # on the ticket's note about a refused result (ticket_brief shows it to Dev and PM)


def refusal_note(role, reasons, corrections=0):
    """The ticket's plain-words note when a paid run's result was refused: what happened, why, what to do. No internal terms."""
    from orgrove import outbox
    told = (f"\n\nThe model was told and tried {corrections} time{'s' * (corrections != 1)} to fix it before the run finished."
            if corrections else "")
    why = "\n".join(f"- {outbox.defang(r)}" for r in reasons)
    return (f"**The {role} run was paid for, but its result was refused, so nothing it asked for was done.**\n\n"
            f"Why:\n{why}{told}\n\nThe next run of this role is told what to fix. Nothing here needs you unless it keeps happening."
            f"\n\n<!-- {REFUSED_MARKER} role={role} -->")


def apply_outbox(a, tracker=None, ledger=None):
    from orgrove import agent_result, manifest, outbox
    path = os.path.join(a.outbox, "result.json") if a.outbox else a.result
    rc, refused, report = process_result(a.project, a.role, a.issue, path, tracker=tracker, dry_run=a.dry_run)
    if refused and not a.dry_run:
        raw = open(a.cost).read() if a.cost and os.path.exists(a.cost) else ""
        if not agent_result.refusal(raw):    # the model's own result was refused (a provider refusal is not that)
            try:
                corrections = (json.loads(raw).get("selfcheck") or {}).get("corrections", 0) if raw else 0
            except ValueError:
                corrections = 0
            reasons = [line[len("REJECTED: "):] for line in report if line.startswith("REJECTED")]
            t = tracker or outbox.GhTracker(manifest.project(a.project)["repo"])
            try:
                t.comment(a.issue, refusal_note(a.role, reasons, corrections))
            except Exception as e:  # noqa: BLE001 (a note must never fail the settle)
                print(f"warning: the refusal note on #{a.issue} was not written: {e}", file=sys.stderr)
    if a.cost and a.run_id and not a.dry_run:   # the run cost money whatever the outbox held
        settle_run(a, ledger, outbox_refused=refused)
    return rc


def config_check(a):
    from orgrove import config_v2  # lazy: only this subcommand needs it

    errors = config_v2.check(a.root)
    if not errors:
        print("ok")
        return 0
    print("\n".join(errors))
    print(f"{len(errors)} violation(s)")
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="orgrove.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reserve")
    r.add_argument("--run-id", required=True)
    r.add_argument("--role", required=True)
    r.add_argument("--issue", required=True)
    r.add_argument("--ceiling", type=float)
    r.add_argument("--kind", choices=list(budget.KINDS), default="ticket")
    s = sub.add_parser("settle")
    s.add_argument("--run-id", required=True)
    s.add_argument("--usd", type=float, help="a cost to settle (finite, not negative)")
    s.add_argument("--cost", help="the agent's result.json: settled through reported_cost, else the ceiling stays")
    s.add_argument("--error", default="")
    s.add_argument("--result-file", default="", help="the run's Claude Code JSON result: its tokens and turns are recorded")
    sub.add_parser("status")
    ap = sub.add_parser("apply")
    ap.add_argument("result", nargs="?", help="a result.json path (or use --outbox)")
    ap.add_argument("--outbox", help="the directory holding result.json (orgrove-out/)")
    ap.add_argument("--cost", help="the agent's --output-format json result: its total_cost_usd is settled")
    ap.add_argument("--run-id", help="the run id reserved for this job (needed to settle)")
    ap.add_argument("--role", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    cf = sub.add_parser("config")
    cf.add_argument("action", choices=["check"])
    cf.add_argument("--root", default=".")
    a = p.parse_args(argv)

    if a.cmd == "config":
        return config_check(a)
    if a.cmd == "apply":
        if bool(a.outbox) == bool(a.result):
            p.error("give either a result path or --outbox DIR")
        if bool(a.cost) != bool(a.run_id):
            p.error("--cost and --run-id go together")
        return apply_outbox(a)
    cfg = budget.load()
    ledger = GitLedger(os.environ.get("ORGROVE_LEDGER_DIR", ".ledger"))
    if a.cmd == "reserve":
        # The ticket's kind of work, for Finance (ticket_cost); unreadable labels record nothing, never block.
        labels = ticket_cost.labels_of(a.issue) if a.kind == "ticket" and a.role not in ticket_cost.ROLE_TYPES else None
        d = reserve(ledger, cfg, a.run_id, a.ceiling or cfg["default_run_ceiling_usd"], a.role, a.issue,
                    kind=a.kind, work_type=ticket_cost.work_type(labels) if labels is not None else None)
        print(json.dumps(asdict(d)))
        return 0 if d.ok else REFUSED
    if a.cmd == "settle":
        if bool(a.cost) == (a.usd is not None):
            p.error("give either --usd or --cost")
        if a.cost:
            settle_run(a, ledger, cfg)
            return 0
        raw = open(a.result_file).read() if a.result_file and os.path.exists(a.result_file) else ""
        event = settle(ledger, cfg, a.run_id, a.usd, error=a.error or None, usage=agent_result.usage(raw) if raw else None)
        print(json.dumps(event))
        cost_comment(ledger, event)
        return 0
    ledger.sync()
    for line in budget.expired_log_lines(ledger.events(), now()):
        print(line, file=sys.stderr)
    day = budget.local_day(now(), cfg["timezone"])
    today, month = budget.spent(ledger.events(month=day[:7]), day)
    print(json.dumps({"day": day, "spent_today": today, "daily_cap": cfg["daily_usd"],
                      "spent_month": month, "monthly_cap": cfg["monthly_usd"]}))
    return 0


def cost_comment(ledger, event):
    """After a paid run on a ticket: edit the ticket's one cost comment (ticket_cost). A failure only warns: the
    run is settled either way, and the next settled run writes the comment again."""
    res = next((e for e in reversed(ledger.events()) if e["type"] == "reserve" and e["run_id"] == event["run_id"]), {})
    if event["usd"] <= 0 or res.get("kind") or res.get("role") in ticket_cost.ROLE_TYPES or "#" not in event["issue"]:
        return None
    try:
        summary = ticket_cost.ticket_summary(ledger.events(), event["issue"])
        return ticket_cost.upsert_comment(event["issue"], ticket_cost.comment_body(summary, ticket_cost.run_type(res)))
    except Exception as e:   # noqa: BLE001 (a comment must never fail a settle)
        print(f"warning: cost comment on {event['issue']} not written: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
