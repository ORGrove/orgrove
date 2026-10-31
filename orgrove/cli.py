"""Budget gate CLI used by every role run (#3).

    python -m orgrove.cli reserve --run-id R --role dev --issue owner/repo#12 [--ceiling 1.5]
        exit 0 = go, exit 3 = refused (a `refused` event is logged; the caller labels blocked/budget)
    python -m orgrove.cli settle --run-id R --usd 0.42 [--error "API Error: 400 ..."]
    python -m orgrove.cli status

The ledger checkout is $ORGROVE_LEDGER_DIR (a clone with the `ledger` branch).
"""
import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone

from orgrove import budget
from orgrove.ledger import GitLedger

REFUSED = 3
REJECTED = 4


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reserve(ledger, cfg, run_id, ceiling, role, issue, ts=None, kind="ticket"):
    ts = ts or now()

    def step(lg):
        day = budget.local_day(ts, cfg["timezone"])
        events = lg.events(month=day[:7])
        if any(e["type"] == "reserve" and e["run_id"] == run_id for e in events):
            return day, None, budget.Decision(False, day, ceiling, *budget.spent(events, day), "run_id already reserved")
        d = budget.decide(events, ts, ceiling, cfg, kind)
        event = {"ts": ts, "day": day, "run_id": run_id, "role": role, "issue": issue,
                 "type": "reserve" if d.ok else "refused", "usd": ceiling if d.ok else 0.0,
                 "msg": d.reason} | ({"kind": kind} if kind != "ticket" else {})
        return day, event, d

    return ledger.transact(step)


def settle(ledger, cfg, run_id, usd, ts=None, error=None):
    ts = ts or now()

    def step(lg):
        res = next((e for e in reversed(lg.events()) if e["type"] == "reserve" and e["run_id"] == run_id), None)
        if res is None:
            raise SystemExit(f"no reservation for run {run_id}")
        event = {"ts": ts, "day": res["day"], "run_id": run_id, "role": res["role"],
                 "issue": res["issue"], "type": "cost", "usd": round(usd, 4), "msg": "settled"}
        if error:  # the AI provider refused the run; the dispatcher pauses on it (dispatch.provider_refusal)
            event["error"] = error[:300]
        return res["day"], event, event

    return ledger.transact(step)


def apply_outbox(a):
    from orgrove import manifest, outbox  # lazy: only this subcommand needs them

    prof = manifest.project(a.project)
    allowlist = manifest.department(prof["departments"][0])["roles"][a.role]["outbox"]
    try:
        with open(a.result) as f:
            result = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"REJECTED: cannot read result: {e}")
        return REJECTED
    _, report = outbox.apply(result, a.role, allowlist, outbox.GhTracker(prof["repo"]), a.issue,
                             owners=prof.get("owners", []), dry_run=a.dry_run)
    print("\n".join(report))
    return REJECTED if any(line.startswith("REJECTED") for line in report) else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="orgrove.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reserve")
    r.add_argument("--run-id", required=True)
    r.add_argument("--role", required=True)
    r.add_argument("--issue", required=True)
    r.add_argument("--ceiling", type=float)
    r.add_argument("--kind", choices=["ticket", "free_time"], default="ticket")
    s = sub.add_parser("settle")
    s.add_argument("--run-id", required=True)
    s.add_argument("--usd", type=float, required=True)
    s.add_argument("--error", default="")
    sub.add_parser("status")
    ap = sub.add_parser("apply")
    ap.add_argument("result")
    ap.add_argument("--role", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "apply":
        return apply_outbox(a)
    cfg = budget.load()
    ledger = GitLedger(os.environ.get("ORGROVE_LEDGER_DIR", ".ledger"))
    if a.cmd == "reserve":
        d = reserve(ledger, cfg, a.run_id, a.ceiling or cfg["default_run_ceiling_usd"], a.role, a.issue,
                    kind=a.kind)
        print(json.dumps(asdict(d)))
        return 0 if d.ok else REFUSED
    if a.cmd == "settle":
        print(json.dumps(settle(ledger, cfg, a.run_id, a.usd, error=a.error or None)))
        return 0
    ledger.sync()
    day = budget.local_day(now(), cfg["timezone"])
    today, month = budget.spent(ledger.events(month=day[:7]), day)
    print(json.dumps({"day": day, "spent_today": today, "daily_cap": cfg["daily_usd"],
                      "spent_month": month, "monthly_cap": cfg["monthly_usd"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
