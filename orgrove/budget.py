"""Budget gate (#3): hard daily and monthly caps over the activity log.

Every run reserves its ceiling before it starts and settles its actual cost when
it ends. A reservation that is never settled (the run crashed or was cancelled)
keeps counting at its full ceiling, so a crash can never free up budget.
A run's cost belongs to the day it was reserved.
"""
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG = Path(__file__).resolve().parent.parent / "config" / "budget.toml"


def load(path=CONFIG, today=None):
    """The caps in force today. A [daily_override] (a temporary owner decision) replaces daily_usd up to and
    including its `until` local day, then ends by itself: nobody has to remember to put the cap back."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    o = cfg.get("daily_override")
    if o:
        today = today or datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
        if today <= o["until"]:
            cfg["daily_usd"] = o["usd"]
    return cfg


def validate(cfg):
    """Errors that would stop every run, found before any run tries (#159: a $1/day cap under a $2 ceiling)."""
    errors = []
    daily, monthly, run_max = cfg["daily_usd"], cfg["monthly_usd"], cfg["max_run_ceiling_usd"]
    if not 0 < daily <= monthly:
        errors.append("caps must be positive and monthly_usd >= daily_usd")
    if daily < run_max:
        errors.append(f"daily_usd ${daily:g} is below max_run_ceiling_usd ${run_max:g}: a run reserves up to its "
                      f"ceiling before it starts, so the largest roles could never start")
    try:
        ZoneInfo(cfg["timezone"])
    except (KeyError, ValueError):
        errors.append(f"timezone {cfg.get('timezone')!r} is not an IANA name such as Europe/London")
    reserve = cfg.get("github_api", {}).get("graphql_reserve", 0)
    if not 0 <= reserve < 5000:
        errors.append(f"github_api.graphql_reserve {reserve} must be from 0 to below 5000 (the App's hourly "
                      f"GraphQL allowance), or the dispatcher would never start anything")
    return errors


def local_day(ts, tz):
    return datetime.fromisoformat(ts).astimezone(ZoneInfo(tz)).date().isoformat()


def run_costs(events):
    """run_id -> (day, usd) using actual cost when settled, else the reserved ceiling."""
    reserved, settled = {}, {}
    for e in events:
        if e["type"] == "reserve":
            reserved[e["run_id"]] = (e["day"], e["usd"])
        elif e["type"] == "cost":
            settled[e["run_id"]] = settled.get(e["run_id"], 0.0) + e["usd"]
    return {
        run: (day, settled.get(run, ceiling))
        for run, (day, ceiling) in reserved.items()
    }


def free_time_today(events, day):
    """(runs, usd) of free-time runs reserved on this local day (#122); usd is actual when settled."""
    ids = {e["run_id"] for e in events if e["type"] == "reserve" and e.get("kind") == "free_time" and e["day"] == day}
    costs = run_costs(events)
    return len(ids), round(sum(costs[r][1] for r in ids), 4)


def spent(events, day):
    month = day[:7]
    today = this_month = 0.0
    for d, usd in run_costs(events).values():
        if d == day:
            today += usd
        if d[:7] == month:
            this_month += usd
    return round(today, 4), round(this_month, 4)


@dataclass
class Decision:
    ok: bool
    day: str
    ceiling: float
    spent_today: float
    spent_month: float
    reason: str


def decide(events, now, ceiling, cfg, kind="ticket"):
    day = local_day(now, cfg["timezone"])
    today, month = spent(events, day)

    def refuse(reason):
        return Decision(False, day, ceiling, today, month, reason)

    if kind == "free_time":  # its own small cap, checked here too so a dispatcher bug can't overspend (#122)
        ft = cfg.get("free_time", {})
        runs, usd = free_time_today(events, day)
        if not ft.get("enabled"):
            return refuse("free time is switched off ([free_time] enabled)")
        if runs >= ft["max_runs_per_day"]:
            return refuse(f"free time: {runs} run(s) today, limit {ft['max_runs_per_day']}")
        if usd + ceiling > ft["daily_usd"]:
            return refuse(f"free time: ${usd} spent + ${ceiling} > ${ft['daily_usd']} a day")

    if ceiling <= 0:
        return refuse("ceiling must be positive")
    if ceiling > cfg["max_run_ceiling_usd"]:
        return refuse(f"ceiling ${ceiling} exceeds the per-run maximum ${cfg['max_run_ceiling_usd']}")
    if today + ceiling > cfg["daily_usd"]:
        return refuse(f"daily cap: ${today} spent + ${ceiling} > ${cfg['daily_usd']}")
    if month + ceiling > cfg["monthly_usd"]:
        return refuse(f"monthly cap: ${month} spent + ${ceiling} > ${cfg['monthly_usd']}")
    return Decision(True, day, ceiling, today, month, "within caps")
