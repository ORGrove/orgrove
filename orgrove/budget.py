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
    shares = cfg.get("project_daily", {})
    for repo, usd in shares.items():
        if 0 < usd < run_max:   # 0 is a parked project (parked()), not a share too small to use
            errors.append(f"project_daily {repo} ${usd:g} is below max_run_ceiling_usd ${run_max:g}: that project's "
                          f"largest roles could never start")
    if sum(shares.values()) > daily:
        errors.append(f"project_daily shares add up to ${sum(shares.values()):g}, more than daily_usd ${daily:g}")
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


RESERVATION_EXPIRY_MINUTES = 60   # a role run times out at 45 minutes (role-run.yml); the rest is margin


def expired_reservations(events, now, minutes=RESERVATION_EXPIRY_MINUTES):
    """Reserve events with no settle that are older than `minutes` (#41): the run crashed or was cancelled.
    They still count at their full ceiling in `run_costs`; this only lets a tick or apply say so."""
    settled = {e["run_id"] for e in events if e["type"] == "cost"}
    cutoff = datetime.fromisoformat(now).timestamp() - minutes * 60
    return [e for e in events if e["type"] == "reserve" and e["run_id"] not in settled
            and datetime.fromisoformat(e["ts"]).timestamp() < cutoff]


def expired_log_lines(events, now, minutes=RESERVATION_EXPIRY_MINUTES):
    return [f"expired reservation: run {e['run_id']} role {e['role']} issue {e['issue']} reserved ${e['usd']:g}, "
            f"still counted at its ceiling" for e in expired_reservations(events, now, minutes)]


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


def project_of(issue):
    """'owner/repo#12' -> 'owner/repo' (a run's issue names its project's repo)."""
    return (issue or "").rsplit("#", 1)[0]


# Roles that serve every project although their ledger rows name one repo's ticket: the retro reads the whole
# day's rework and writes lessons for all projects, so parking orgrove (where its thread lives) must not stop it.
PROJECT_WIDE = frozenset({"retro"})


def parked(cfg, repo):
    """True when the owner has parked this repo: its [project_daily] share is 0, so no agent works on it."""
    return cfg.get("project_daily", {}).get(repo) == 0


def spent_project(events, day, repo):
    """What runs on this repo's tickets cost on this local day (actual when settled, else the ceiling)."""
    repos = {e["run_id"]: project_of(e.get("issue")) for e in events if e["type"] == "reserve"}
    return round(sum(usd for run, (d, usd) in run_costs(events).items() if d == day and repos.get(run) == repo), 4)


def left(cfg, events, day, repo=None):
    """(dollars left today, the cap that binds: "daily" or "monthly"). With a repo, its [project_daily] share binds
    too (2026-11-01: one shared pot let one project's long queue spend the whole day before the other got a run)."""
    today, month = spent(events, day)
    caps = [(cfg["daily_usd"] - today, "daily"), (cfg["monthly_usd"] - month, "monthly")]
    share = cfg.get("project_daily", {}).get(repo)
    if share is not None:
        caps.append((share - spent_project(events, day, repo), "daily"))
    usd, kind = min(caps)
    return round(usd, 4), kind


@dataclass
class Decision:
    ok: bool
    day: str
    ceiling: float
    spent_today: float
    spent_month: float
    reason: str


KINDS = ("ticket", "free_time", "pr")   # pr: QA/Security on a PR ORGrove did not open (#82); the same caps as a ticket


def decide(events, now, ceiling, cfg, kind="ticket", issue=None, role=None):
    day = local_day(now, cfg["timezone"])
    today, month = spent(events, day)

    def refuse(reason):
        return Decision(False, day, ceiling, today, month, reason)

    if kind not in KINDS:
        return refuse(f"unknown run kind {kind!r}")
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
    repo = project_of(issue)
    share = None if role in PROJECT_WIDE else cfg.get("project_daily", {}).get(repo)
    if share == 0:
        return refuse(f"{repo} is parked ([project_daily] share 0): its work is done in the owner's session")
    if share is not None:
        p = spent_project(events, day, repo)
        if p + ceiling > share:
            return refuse(f"{repo} daily share: ${p} spent + ${ceiling} > ${share}")
    if month + ceiling > cfg["monthly_usd"]:
        return refuse(f"monthly cap: ${month} spent + ${ceiling} > ${cfg['monthly_usd']}")
    return Decision(True, day, ceiling, today, month, "within caps")
