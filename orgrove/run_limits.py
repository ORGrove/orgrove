"""Right-size per-run limits from real spend (#77): a read-only report.

    python -m orgrove.run_limits              # last 14 days of the ledger
    python -m orgrove.run_limits --days 7 --ledger <checkout of the ledger branch>

Per role, over settled runs only: run count, mean, p95, max, the current `ceiling_usd` and a suggested one
(p95 plus HEADROOM, rounded up to $0.05, never below the largest run seen). Free-time runs are their own line.
A role with fewer than MIN_RUNS settled runs is reported as "too few runs", with no suggestion. It also counts
the past budget refusals that would have fit under the suggested limits.

Applying a suggestion is the owner's change (`ceiling_usd` is in the projects' list of things agents never do).
This module only reads: no ledger, config or GitHub write.
"""
import argparse
import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from orgrove import budget, manifest, office
from orgrove.ledger import GitLedger

MIN_RUNS = 5
HEADROOM = 1.25      # the suggestion is p95 x this, rounded up to STEP
STEP = 0.05
REFUSAL = re.compile(r"\$([\d.]+) spent \+ \$([\d.]+) > \$([\d.]+)")


def p95(values):
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)]


def round_up(usd):
    return round(math.ceil(round(usd / STEP, 6)) * STEP, 2)


def suggest(costs):
    """Suggested ceiling for a role's settled run costs, or None with too few runs."""
    if len(costs) < MIN_RUNS:
        return None
    return round_up(max(p95(costs) * HEADROOM, max(costs)))


def settled_runs(events):
    """(role line, usd) per settled run. Free-time runs get their own line. Not finance.ai_runs: that module is
    Pro-only and this report ships in both editions."""
    meta = {e["run_id"]: e for e in events if e["type"] == "reserve"}
    settled = {e["run_id"] for e in events if e["type"] == "cost"}
    out = []
    for run, (_, usd) in budget.run_costs(events).items():
        if run in settled:
            m = meta[run]
            role = manifest.resolve_role(m["role"], repo=(m.get("issue") or "").partition("#")[0]) if m.get("role") else "?"
            out.append((role + (" (free time)" if m.get("kind") == "free_time" else ""), usd))
    return out


def ceilings():
    """role -> current ceiling_usd, from every department file."""
    return {role: cfg["ceiling_usd"] for path in sorted((manifest.ROOT / "departments").glob("*.toml"))
            for role, cfg in manifest.department(path.stem).get("roles", {}).items() if "ceiling_usd" in cfg}


def in_window(events, today, days):
    """Events whose local day is within the last `days` days, today included."""
    first = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
    return [e for e in events if first <= e.get("day", "") <= today]


def rows(events, current=None):
    """One dict per role line, sorted by role: settled runs only."""
    current = ceilings() if current is None else current
    by_role = {}
    for role, usd in settled_runs(events):
        by_role.setdefault(role, []).append(usd)
    out = []
    for role, costs in sorted(by_role.items()):
        out.append({"role": role, "runs": len(costs), "mean": sum(costs) / len(costs), "p95": p95(costs), "max": max(costs),
                    "ceiling": current.get(role), "suggested": suggest(costs)})
    return out


def refusals(events, suggestions):
    """(total, would_fit): budget refusals in `events`, and those that fit under the suggested ceiling of their role.
    A refusal reads 'daily cap: $S spent + $C > $CAP'; it fits when S + suggestion <= CAP."""
    total = fit = 0
    for e in events:
        m = e["type"] == "refused" and REFUSAL.search(e.get("msg", ""))
        if not m:
            continue
        total += 1
        role = manifest.resolve_role(e["role"], repo=(e.get("issue") or "").partition("#")[0]) if e.get("role") else None
        spent, _, cap = float(m[1]), float(m[2]), float(m[3])
        if suggestions.get(role) is not None and spent + suggestions[role] <= cap:
            fit += 1
    return total, fit


def report(events, today, days=14, current=None):
    window = in_window(events, today, days)
    table = rows(window, current)
    lines = [f"Run limits from settled runs, {days} days to {today} (settled runs only; applying a limit is the owner's change)", "",
             f"{'role':<22}{'runs':>5}{'mean':>8}{'p95':>8}{'max':>8}{'ceiling':>9}{'suggested':>11}"]
    for r in table:
        ceiling = "-" if r["ceiling"] is None else f"${r['ceiling']:.2f}"
        sug = f"${r['suggested']:.2f}" if r["suggested"] is not None else f"too few runs (<{MIN_RUNS})"
        lines.append(f"{r['role']:<22}{r['runs']:>5}{r['mean']:>8.2f}{r['p95']:>8.2f}{r['max']:>8.2f}{ceiling:>9}  {sug}")
    total, fit = refusals(window, {r["role"]: r["suggested"] for r in table})
    lines += ["", f"Budget refusals in the window: {total}; would have fit under the suggested limits: {fit}"]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--ledger", help="a local checkout of the ledger branch (default: read it from GitHub)")
    args = ap.parse_args(argv)
    tz = ZoneInfo(budget.load()["timezone"])
    today = datetime.now(tz).date()
    if args.ledger:
        events = GitLedger(args.ledger).events()
    else:
        months = {today.isoformat()[:7], (today - timedelta(days=args.days - 1)).isoformat()[:7]}
        events = [e for m in sorted(months) for e in office.ledger_events(m)]
    print(report(events, today.isoformat(), args.days))


if __name__ == "__main__":
    main()
