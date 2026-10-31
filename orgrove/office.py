"""ORGrove Office, local 2D MVP (#8).

    bin/orgrove-office [--project <id>] [--port 8730]   (the next free port if 8730 is taken)

Serves one page on 127.0.0.1 that shows the agent organisation working. All data comes
from GitHub through the `gh` CLI, using your existing login: issues and labels, the bot's
PRs, role-run workflow runs, and the `ledger` branch. Nothing is simulated, and every item
links to the issue, PR, run or log entry behind it.
"""
import argparse
import re
import base64
import html
import json
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from orgrove import brand, budget, ci, editions, manifest, office_actions, org, stages

try:   # the publisher's own website panel: left out of a client's copy (bin/package, #159)
    from orgrove import office_waitlist
except ImportError:
    office_waitlist = None
try:   # ORGrove Pro only (orgrove/editions.py): ORGrove Community ships without meetings, voice or news chats
    from orgrove import meeting, voice
except ImportError:
    meeting = voice = None
try:
    from orgrove import office_news
except ImportError:
    office_news = None
FINANCE_ON = editions.has("finance")   # imported lazily below (it imports office)


def waitlist_on():
    return office_waitlist is not None and office_waitlist.configured()

ORGROVE_REPO = org.REPO
STATIC = Path(__file__).resolve().parent / "office_static"


def edition():
    """The optional parts this copy has (orgrove/editions.py). The pages hide what is absent (edition.js)."""
    return editions.present()


def pro_only(route):
    """A route of a part this copy doesn't have: (status, body, content type) saying where to get it, else None.
    A person's page gets the Pro page; a call from a page gets a short JSON note. Never a bare 404 (2026-10-31)."""
    if (name := editions.missing_part(route, edition())) is None:
        return None
    part = editions.PARTS[name]
    if route in part.pages:
        page = (STATIC / "pro.html").read_text()
        for key, value in (("__PART_NAME__", part.name), ("__PART_ABOUT__", part.about), ("__PRO_URL__", editions.PRO_URL)):
            page = page.replace(key, html.escape(value))
        return 200, page, "text/html; charset=utf-8"
    note = {"error": f"{part.name}: part of ORGrove Pro, which this copy (ORGrove Community) doesn't have",
            "pro": editions.PRO_URL}
    return 404, json.dumps(note), "application/json"


def people():
    """Role -> person name (config/people.toml, #137), for the Office in every edition."""
    return org.people()
CACHE_SECONDS = 300         # everything is re-read from GitHub at least every 5 min (owner, 2026-10-25)
# ...and at once when the dispatcher has run since: every change that matters (a ticket, a merge, an agent finishing)
# ends in a dispatcher run, so one small REST call every PROBE_SECONDS tells the Office when to re-read (#159).
# A full read costs ~9 GraphQL/REST calls (25 with two projects) and takes ~7 s, so reads are at least
# MIN_READ_SECONDS apart: the shared 5,000-an-hour allowance stays far away while the room is ~20 s behind.
PROBE_SECONDS = 10
MIN_READ_SECONDS = 20
BOARD_CACHE_SECONDS = 300   # the project board is the expensive GraphQL read; dates change slowly
_board_cache = {}


def gh_json(*args):
    try:
        out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as e:
        # gh's own message (502, rate limit, not found) is otherwise lost in CI tracebacks (#125).
        e.add_note(f"gh said: {(e.stderr or '').strip()[:500] or '(nothing on stderr)'}")
        raise
    return json.loads(out) if out.strip() else []


def ledger_events(month):
    """Every event in this month's ledger files."""
    try:
        files = gh_json("api", f"repos/{ORGROVE_REPO}/contents/events?ref=ledger")
    except subprocess.CalledProcessError:
        return []
    events = []
    for f in files:
        if f["name"].startswith(month) and f["name"].endswith(".jsonl"):
            blob = gh_json("api", f"repos/{ORGROVE_REPO}/contents/{f['path']}?ref=ledger")
            text = base64.b64decode(blob["content"]).decode()
            events += [json.loads(line) for line in text.splitlines() if line.strip()]
    return events


# A page of `gh project item-list` costs 101 GraphQL points: it asks for every field of every item. Each dispatcher
# tick read two boards (3 pages), and on 2026-10-30 the App's 5,000-an-hour allowance ran out twice
# (docs/incidents/2026-10-30-api-burst.md). This asks only for what is used, with no nested lists: 1 point a page.
BOARD_QUERY = """query($login: String!, $number: Int!, $endCursor: String%s) {
  organization(login: $login) { projectV2(number: $number) { items(first: 100, after: $endCursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id content { __typename ... on Issue { number repository { nameWithOwner } milestone { title } } }%s }
  } } }
}"""
BOARD_FIELD = """
      %s: fieldValueByName(name: $%s) {
        ... on ProjectV2ItemFieldSingleSelectValue { name } ... on ProjectV2ItemFieldDateValue { date } }"""


def board_query(names):
    """The board query for these field names (status, start, target), and its -f arguments. Names go in as
    variables, never into the query text."""
    keys = [k for k in ("status", "start", "target") if names.get(k)]
    query = BOARD_QUERY % ("".join(f", ${k}: String!" for k in keys), "".join(BOARD_FIELD % (k, k) for k in keys))
    return query, [a for k in keys for a in ("-f", f"{k}={names[k]}")]


def board_items(prof):
    """issue number -> its card on the project board (item id, Status, roadmap dates, milestone).
    A project without a board (optional, #159) has no cards: the board is never synced, the roadmap stays empty."""
    board, names = prof.get("board"), {"status": "Status"} | prof.get("board_fields", {})
    if not board:
        return {}
    query, fields = board_query(names)
    pages = gh_json("api", "graphql", "--paginate", "--slurp", "-f", f"query={query}",
                    "-f", f"login={board['owner']}", "-F", f"number={board['number']}", *fields)
    out = {}
    for page in pages:
        for it in page["data"]["organization"]["projectV2"]["items"]["nodes"]:
            c = it.get("content") or {}
            if c.get("__typename") != "Issue" or c["repository"]["nameWithOwner"].lower() != prof["repo"].lower():
                continue
            value = lambda k: (it.get(k) or {}).get("name") or (it.get(k) or {}).get("date")  # noqa: E731
            out[c["number"]] = {
                "item_id": it["id"], "status": value("status"), "start": value("start"), "target": value("target"),
                "milestone": (c.get("milestone") or {}).get("title"),
            }
    return out


def graphql_allowance():
    """(points left, reset time) of this token's hourly GraphQL allowance, or None when it can't be read.
    GET /rate_limit itself costs nothing."""
    try:
        g = gh_json("api", "rate_limit")["resources"]["graphql"]
        return g["remaining"], datetime.fromtimestamp(g["reset"], timezone.utc)
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError):
        return None


def cached_board(prof):
    """board_items, re-read at most every BOARD_CACHE_SECONDS; keeps the last good copy on errors."""
    key = prof["project"]
    hit = _board_cache.get(key)
    if hit and time.time() - hit[0] < BOARD_CACHE_SECONDS:
        return hit[1]
    try:
        items = board_items(prof)
    except subprocess.CalledProcessError:
        if hit:
            return hit[1]
        raise
    _board_cache[key] = (time.time(), items)
    return items


def pipeline(repo, cfg, board=None, skip=()):
    """Every open issue (no stage label = backlog) plus staged issues closed in the last 7 days."""
    board = board or {}
    stage_labels = [s["label"] for s in cfg["stage"]] + [stages.DONE_LABEL]
    since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    fields = "number,title,labels,state,url,updatedAt,closedAt,milestone"
    active = gh_json("issue", "list", "-R", repo, "--state", "open", "--limit", "500", "--json", fields)
    done = gh_json("issue", "list", "-R", repo, "--state", "closed", "--limit", "50", "--json", fields,
                   "--search", f"label:{','.join(stage_labels)} closed:>={since}")
    cards = []
    for i in active + done:
        if i["number"] in skip:
            continue
        names = [label["name"] for label in i["labels"]]
        try:
            stage = stages.stage_of(cfg, names, closed=i["state"] == "CLOSED")
        except ValueError:
            stage = "conflict"
        b = board.get(i["number"], {})
        cards.append({
            "number": i["number"], "title": i["title"], "url": i["url"], "stage": stage,
            "labels": names,
            "agents": [n.split("/", 1)[1] for n in names if n.startswith("agent/")],
            "flags": [n for n in names if n.split("/")[0] in ("needs", "blocked")],
            "priority": next((n.split("/")[1] for n in names if n.startswith("priority/")), ""),
            "updated": i["closedAt"] or i["updatedAt"],
            "milestone": (i.get("milestone") or {}).get("title"),
            "target": b.get("target"), "start": b.get("start"), "board_status": b.get("status"),
            "on_board": bool(b),
        })
    return cards


def roadmap(cards, today):
    """Open cards grouped by target month; overdue and undated called out (they're what the PM fixes)."""
    groups = {}
    for c in cards:
        if c["stage"] == "done":
            continue
        t = c.get("target")
        key = "No date" if not t else ("Overdue" if t < today else t[:7])
        groups.setdefault(key, []).append(c)
    rank = lambda k: (0, "") if k == "Overdue" else (2, "") if k == "No date" else (1, k)  # noqa: E731
    return [{"month": k, "count": len(v), "issues": sorted(v, key=lambda c: (c.get("target") or "", c["number"]))}
            for k, v in sorted(groups.items(), key=lambda kv: rank(kv[0]))]


RUN_NAME = re.compile(r"^(\w+) · #(\d+)(?: \(([\w.-]+)\))?")


def runs(events):
    """Recent role-run runs, each with its role and issue (from run-name, or the ledger for older runs)."""
    rows = gh_json("run", "list", "-R", ORGROVE_REPO, "--workflow", "role-run.yml", "--limit", "60",
                   "--json", "databaseId,displayTitle,status,conclusion,createdAt,updatedAt,url")
    by_run = {e["run_id"].split("-")[0]: e for e in events if e["type"] in ("reserve", "refused")}
    out = []
    for r in rows:
        role = issue = None
        m = RUN_NAME.match(r["displayTitle"])
        if m:
            role, issue = m.group(1), int(m.group(2))
            role = manifest.resolve_role(role, project_id=m.group(3))
        elif (e := by_run.get(str(r["databaseId"]))):
            repo, _, num = e["issue"].partition("#")
            role, issue = manifest.resolve_role(e["role"], repo=repo), int(num)
        title = f"{role} · #{issue}" if role else "gate or earlier run"
        out.append({"id": r["databaseId"], "title": title, "role": role, "issue": issue,
                    "status": r["status"], "conclusion": r["conclusion"], "created": r["createdAt"],
                    "updated": r["updatedAt"], "url": r["url"], "free_time": r["displayTitle"].endswith("· free time")})
    return out


def inbox(repo):
    prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--author", org.BOT_AUTHOR, "--json",
                  "number,title,url,createdAt,statusCheckRollup")
    items = []
    for p in prs:
        state = ci.ci_state(p.get("statusCheckRollup") or [])
        items.append({"kind": "merge", "number": p["number"], "title": p["title"], "url": p["url"], "repo": repo,
                      "ci": {"red": "failing", "none": "pending"}.get(state, state)})
    for i in gh_json("issue", "list", "-R", repo, "--state", "open", "--label", "needs/approval",
                     "--json", "number,title,url", "--limit", "50"):
        items.append({"kind": "approval", "number": i["number"], "title": i["title"], "url": i["url"], "repo": repo})
    for i in gh_json("issue", "list", "-R", repo, "--state", "open", "--search", "label:needs/human,blocked/budget",
                     "--json", "number,title,url,labels", "--limit", "50"):
        kind = "budget" if any(label["name"] == "blocked/budget" for label in i["labels"]) else "decision"
        comments = gh_json("api", f"repos/{repo}/issues/{i['number']}/comments?per_page=100")
        asks = [c for c in comments if c["user"]["login"] == org.BOT_LOGIN]
        items.append({"kind": kind, "number": i["number"], "title": i["title"], "url": i["url"], "repo": repo,
                      "question": (asks[-1]["body"][:600] if asks else ""),
                      "answered": bool(comments) and comments[-1]["user"]["login"] != org.BOT_LOGIN})
    return items


def roster(dept, cards, run_rows, project=None):
    """Each role is working (a run in progress) or on a coffee break since its last run finished.

    Derived only from GitHub runs and agent/<role> labels, the same data the dispatcher (#7) uses.
    """
    default_break = dept.get("break_min", 0)
    out = []
    for role, r in dept["roles"].items():
        live = [x for x in run_rows if x["role"] == role and x["status"] in ("in_progress", "queued", "waiting")]
        # agent/<skill> labels name the skill; the card's project says which role holds it (#112)
        labelled = [c for c in cards if r["skill"] in c["agents"]
                    and manifest.role_for(dept, r["skill"], c.get("project") or project) == role]
        finished = [x["updated"] for x in run_rows if x["role"] == role and x["status"] == "completed"]
        on = {(c.get("project"), c["number"]): {"number": c["number"], "url": c["url"], "title": c["title"],
                                                "project": c.get("project")} for c in labelled}
        for x in live:
            if not any(k[1] == x["issue"] for k in on):
                on[(None, x["issue"])] = {"number": x["issue"], "url": x["url"],
                                          "title": "free time · Dependabot triage" if x.get("free_time") else "run in progress"}
        out.append({"role": role, "model": r["model"], "ceiling": r["ceiling_usd"],
                    "status": "working" if live or labelled else "break",
                    "break_since": None if live or labelled else (max(finished) if finished else None),
                    "break_min": r.get("break_min", default_break),
                    "on": list(on.values())})
    return out


def spend_today_by_role(events, day):
    by = {}
    for run_id, (d, usd) in budget.run_costs(events).items():
        if d == day:
            e = next(e for e in events if e.get("run_id") == run_id)
            role = manifest.resolve_role(e["role"], repo=(e.get("issue") or "").partition("#")[0])
            by[role] = round(by.get(role, 0.0) + usd, 4)
    return by


STALE_STATUS_MIN = 75   # hourly wake-up plus slack: older than this, the dispatcher itself is the problem
REASON_TEXT = {
    "working": "working", "starting": "starting work",
    "on_break": "coffee break", "waiting_wakeup": "waiting for the hourly wake-up",
    "budget_daily": "daily budget cap reached", "budget_monthly": "monthly budget cap reached",
    "waiting_owner": "waiting on you", "waiting_agent": "waiting on another agent",
    "nothing_to_do": "nothing planned for this role", "unknown": "no status recorded yet",
    "provider_refused": "the AI provider is refusing runs",
}
# When the whole team is between tasks, the status leads with what the CTO can act on or should know first.
IMPACT = ["provider_refused", "budget_monthly", "budget_daily", "waiting_owner", "stale", "waiting_wakeup", "on_break",
          "waiting_agent", "nothing_to_do", "unknown"]


def why(events, now, roster_rows):
    """Who is doing what and why the others are idle, from the dispatcher's own last status (#98).

    Two things only time can tell are added here: a break that has ended since the dispatcher last
    looked means "waiting for the wake-up", and a status older than STALE_STATUS_MIN means the
    dispatcher itself isn't running. A run in progress (from the ledger) always wins over the status."""
    statuses = [e for e in events if e["type"] == "status"]
    last = max(statuses, key=lambda e: e["ts"]) if statuses else None
    age = (now - datetime.fromisoformat(last["ts"])).total_seconds() / 60 if last else None
    roles = {}
    for r in roster_rows:
        st = dict((last or {}).get("roles", {}).get(r["role"]) or {"reason": "unknown", "issues": [], "detail": ""})
        if st["reason"] == "on_break" and st.get("until") and datetime.fromisoformat(st["until"]) <= now:
            ago = int((now - datetime.fromisoformat(st["until"])).total_seconds() // 60)
            st |= {"reason": "waiting_wakeup",
                   "detail": f"break ended {ago} min ago; starts at the next hourly wake-up or when a run finishes"}
        if r["status"] == "working":
            st = {"reason": "working", "issues": [o["number"] for o in r.get("on", [])], "detail": "run in progress"}
        roles[r["role"]] = st | {"text": REASON_TEXT.get(st["reason"], st["reason"])}
    busy = [k for k, v in roles.items() if v["reason"] in ("working", "starting")]
    stale = age is not None and age > STALE_STATUS_MIN
    headline = None
    if not busy:
        counts = {}
        for v in roles.values():
            counts[v["reason"]] = counts.get(v["reason"], 0) + 1
        if stale:
            counts["stale"] = 1
        ranked = sorted(counts, key=lambda k: IMPACT.index(k) if k in IMPACT else len(IMPACT))
        text = REASON_TEXT | ({"stale": f"the dispatcher hasn't run for {int(age)} min"} if stale else {})
        headline = "The team is between tasks: " + "; ".join(
            text[k] + (f" ({counts[k]} agents)" if k != "stale" and counts[k] > 1 else "") for k in ranked)
    return {"as_of": last["ts"] if last else None, "age_min": round(age) if age is not None else None, "stale": stale,
            "roles": roles, "owner_waiting": (last or {}).get("owner_waiting", []), "headline": headline,
            "budget_left": (last or {}).get("budget_left"), "budget_kind": (last or {}).get("budget_kind")}


def snapshot(project=None):
    """Office state for one project, or for every project (manifest order) when project is None.

    Agents and budget are shared across projects, so the roster, spend and runs are org-wide;
    cards, roadmap and inbox items carry the project they belong to.
    """
    profiles = [manifest.project(project)] if project else manifest.projects()
    bcfg = budget.load()
    day = datetime.now(ZoneInfo(bcfg["timezone"])).date().isoformat()
    events = ledger_events(day[:7])
    today, month = budget.spent(events, day)
    run_rows = runs(events)
    feed = sorted((e for e in events if e["type"] != "status"), key=lambda e: e["ts"], reverse=True)[:40]
    all_cards, inbox_items, projects, dept, cfg = [], [], [], None, None
    for prof in profiles:
        dept = manifest.department(prof["departments"][0])
        cfg = stages.load(manifest.ROOT / dept["stages"])
        cards = pipeline(prof["repo"], cfg, cached_board(prof), set(prof.get("skip_issues", [])))
        for c in cards:
            c["project"] = prof["project"]
        all_cards += cards
        inbox_items += [i | {"project": prof["project"]} for i in inbox(prof["repo"])]
        projects.append({
            "id": prof["project"], "name": prof.get("name", prof["project"]), "repo": prof["repo"],
            "roadmap": roadmap(cards, day),
            "roadmap_url": f"https://github.com/{prof['repo']}/issues/{prof['roadmap_issue']}" if prof.get("roadmap_issue") else None,
        })
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": project or "all", "department": dept["department"], "projects": projects,
        "enabled": gh_json("api", f"repos/{ORGROVE_REPO}/actions/variables/ORGROVE_ENABLED").get("value"),
        "columns": [{"id": "backlog", "label": "Backlog"}]
                   + [{"id": s["id"], "label": s["id"].replace("-", " ").title()} for s in cfg["stage"]]
                   + [{"id": "done", "label": "Done (7d)"}],
        "cards": all_cards,
        "roster": (team := roster(dept, all_cards, run_rows)),
        "why": why(events, datetime.now(timezone.utc), team),
        "spend": {"day": day, "today": today, "daily_cap": bcfg["daily_usd"], "month": month,
                  "monthly_cap": bcfg["monthly_usd"], "by_role": spend_today_by_role(events, day)},
        "inbox": inbox_items,
        "runs": run_rows,
        "feed": feed,
        "ledger_url": f"https://github.com/{ORGROVE_REPO}/tree/ledger/events",
    }


CHANGES_KEPT = 50


def changes(prev, cur):
    """What really happened between two snapshots (#121): the office celebrates only these.

    Pure: compares two /api/state snapshots, no GitHub calls. A first snapshot (prev None) or an
    errored one yields nothing, so opening the office never replays a fake party. Something that
    appeared and finished between two snapshots counts only if it is newer than the previous one."""
    if not prev or not cur or prev.get("error") or cur.get("error"):
        return []
    at, since = cur["generated"], datetime.fromisoformat(prev["generated"].replace("Z", "+00:00"))
    newer = lambda ts: bool(ts) and datetime.fromisoformat(ts.replace("Z", "+00:00")) > since  # noqa: E731
    out = []
    before = {(c.get("project"), c["number"]): c for c in prev.get("cards", [])}
    for c in cur.get("cards", []):
        old = before.get((c.get("project"), c["number"]))
        roles = sorted(set(c.get("agents", [])) | set((old or {}).get("agents", [])))
        base = {"project": c.get("project"), "issue": c["number"], "title": c["title"], "roles": roles, "at": at}
        for stage, kind in (("done", "shipped"), ("deployed-dev", "deployed")):
            if c["stage"] == stage and (old["stage"] != stage if old else newer(c.get("updated"))):
                out.append({"kind": kind} | base)
    failed_before = {r["id"] for r in prev.get("runs", []) if r.get("conclusion") == "failure"}
    seen_before = {r["id"] for r in prev.get("runs", [])}
    for r in cur.get("runs", []):
        if r.get("conclusion") == "failure" and r["id"] not in failed_before \
                and (r["id"] in seen_before or newer(r.get("created"))):
            out.append({"kind": "run_failed", "project": None, "issue": r.get("issue"), "title": r["title"],
                        "roles": [r["role"]] if r.get("role") else [], "at": at})
    ci_before = {(i["repo"], i["number"]): i.get("ci") for i in prev.get("inbox", []) if i["kind"] == "merge"}
    for i in cur.get("inbox", []):
        if i["kind"] == "merge" and i.get("ci") == "failing" and ci_before.get((i["repo"], i["number"])) != "failing":
            out.append({"kind": "ci_failing", "project": i.get("project"), "issue": i["number"], "title": i["title"],
                        "roles": ["qa"], "at": at})
    return out


def last_tick():
    """The dispatcher's latest run, as (id, status, updated): it changes whenever a tick starts or ends."""
    r = gh_json("api", f"repos/{ORGROVE_REPO}/actions/workflows/dispatch.yml/runs?per_page=1",
                "--jq", ".workflow_runs[0] // {} | {id, status, updated_at}")
    return r.get("id"), r.get("status"), r.get("updated_at")


class Cache:
    def __init__(self, project, probe=last_tick, take=None):
        self.project, self.data, self.at, self.lock = project, None, 0.0, threading.Lock()
        self.log = []   # the last CHANGES_KEPT changes, oldest first
        self.probe, self.take = probe, take or (lambda: snapshot(self.project))
        self.mark, self.probed = None, 0.0

    def due(self, now):
        """A full re-read is due: nothing yet, older than CACHE_SECONDS, or the dispatcher has run since."""
        if self.data is None or now - self.at > CACHE_SECONDS:
            return True
        if now - self.at < MIN_READ_SECONDS or now - self.probed < PROBE_SECONDS:
            return False
        self.probed = now
        try:
            return self.probe() != self.mark
        except Exception:  # noqa: BLE001  a failed probe keeps the picture; the 5-minute read still comes
            return False

    def get(self):
        with self.lock:
            now = time.time()
            if self.due(now):
                try:
                    try:
                        self.mark, self.probed = self.probe(), now   # before the read: a tick during it shows next time
                    except Exception:  # noqa: BLE001
                        self.mark = None   # unknown: the next probe that answers triggers a read
                    fresh = self.take()
                    self.log = (self.log + changes(self.data, fresh))[-CHANGES_KEPT:]
                    self.data, self.at = fresh | {"changes": self.log}, now
                except Exception as exc:
                    err = f"{type(exc).__name__}: {getattr(exc, 'stderr', '') or exc}".strip()
                    if self.data is None:
                        return {"error": err}
                    # Keep the last good picture, marked stale, rather than blanking the page.
                    self.at = now
                    return self.data | {"stale": err}
            return self.data


# Pages filled in per request: CSRF token, client header, accent (#159).
PAGES = {"/": "index.html", "/index.html": "index.html", "/office3d": "office3d.html", "/office3d.html": "office3d.html",
         "/finance": "finance.html", "/finance.html": "finance.html", "/meeting": "meeting.html",
         "/meeting.html": "meeting.html", "/waitlist": "waitlist.html", "/waitlist.html": "waitlist.html",
         "/preview3d": "preview3d.html", "/preview3d.html": "preview3d.html"}
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".png": "image/png", "": "text/plain; charset=utf-8",
                ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".bin": "application/octet-stream"}   # 3D models (Office3D preview)


LIGHT_PAGES = ("office3d.html", "preview3d.html")


def static_file(url_path):
    """Serve a file from office_static only: no traversal, no hidden files, known types only."""
    rel = url_path.split("?", 1)[0].lstrip("/")
    if not rel or any(part.startswith(".") for part in rel.split("/")):
        return None
    target = (STATIC / rel).resolve()
    if STATIC.resolve() not in target.parents or not target.is_file():
        return None
    ctype = STATIC_TYPES.get(target.suffix)
    if ctype is None:
        return None
    return target.read_bytes(), ctype


MEETING = meeting.Meeting() if meeting else None


class FinanceCache:
    """The Finance report (#78), recomputed at most every CACHE_SECONDS; keeps the last good copy."""
    def __init__(self):
        self.data, self.at, self.lock = None, 0.0, threading.Lock()

    def get(self):
        from orgrove import finance  # imports office; loaded lazily to avoid a cycle
        with self.lock:
            if self.data is None or time.time() - self.at > CACHE_SECONDS:
                try:
                    self.data, self.at = finance.gather(), time.time()
                except Exception as exc:
                    self.at = time.time()
                    err = f"{type(exc).__name__}: {getattr(exc, 'stderr', '') or exc}".strip()
                    return {"error": err} if self.data is None else self.data | {"stale": err}
            return self.data


FINANCE = FinanceCache()


def make_handler(cache, brand_cfg=None):
    brand_cfg = brand.load() if brand_cfg is None else brand_cfg

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split("?")[0]
            if "waitlist" in route and not waitlist_on():
                self.send_error(404)
                return
            if (pro := pro_only(route)) is not None:
                return self._send(*pro)
            if self.path.startswith("/api/state"):
                live = {"meeting": MEETING.public()} if MEETING else {}
                body, ctype = json.dumps(cache.get() | live | {"people": people()}).encode(), "application/json"
            elif self.path.startswith("/api/edition"):
                body, ctype = json.dumps(edition()).encode(), "application/json"
            elif self.path.startswith("/api/meeting"):
                body, ctype = json.dumps(MEETING.public() | {"voice": voice.available(), "types": meeting.Meeting.types(),
                                                         "people": meeting.people()}).encode(), "application/json"
            elif self.path.startswith("/api/news"):          # break-room small talk: cached, refreshed every 3 hours
                body, ctype = json.dumps(office_news.headlines()).encode(), "application/json"
            elif self.path.startswith("/api/finance"):
                body, ctype = json.dumps(FINANCE.get(), default=str).encode(), "application/json"
            elif self.path.startswith("/api/waitlist"):
                body, ctype = json.dumps(office_waitlist.PANEL.get()).encode(), "application/json"
            elif (page := PAGES.get(route)) is not None and (STATIC / page).is_file():
                # the 3D strips are always light glass, so they keep the light client logo (#159)
                html = brand.render((STATIC / page).read_text(), brand_cfg, office_actions.TOKEN, dark=page not in LIGHT_PAGES)
                body, ctype = html.encode(), "text/html; charset=utf-8"
            elif self.path in ("/brand/client-light.png", "/brand/client-dark.png"):
                if (logo := brand.logo(brand_cfg, self.path[7:-4].replace("client-", "logo_"))) is None:
                    self.send_error(404)
                    return
                body, ctype = logo.read_bytes(), "image/png"
            elif (f := static_file(self.path)) is not None:
                body, ctype = f
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if (pro := pro_only(self.path)) is not None:
                self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536))   # answered unread, the client sees a reset
                return self._send(*pro)
            if self.path == "/api/meeting":
                return self._meeting()
            if self.path == "/api/transcribe":
                return self._transcribe()
            if self.path == "/api/waitlist" and waitlist_on():
                return self._waitlist()
            if self.path != "/api/act":
                self.send_error(404)
                return
            status, out = 200, {}
            try:
                office_actions.check_request(self.headers, self.server.server_address[1])
                length = int(self.headers.get("Content-Length") or 0)
                if length > 16384:
                    raise office_actions.Refused("request too large")
                act = office_actions.validate(json.loads(self.rfile.read(length) or b"{}"),
                                              office_actions.project_repos())
                out = {"ok": True, "message": office_actions.perform(act)}
                cache.at = 0  # show the result on the next refresh
            except office_actions.Refused as exc:
                status, out = 403, {"ok": False, "message": str(exc)}
            except (ValueError, subprocess.CalledProcessError) as exc:
                status, out = 400, {"ok": False, "message": (getattr(exc, "stderr", "") or str(exc))[:300]}
            body = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status, body, ctype):
            body = brand.render(body, brand_cfg, office_actions.TOKEN).encode() if ctype.startswith("text/html") else body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _waitlist(self):
            """Waitlist actions (u2-waitlist WF4); same CSRF/Host/Origin checks as /api/act."""
            status, out = 200, {}
            try:
                office_actions.check_request(self.headers, self.server.server_address[1])
                length = int(self.headers.get("Content-Length") or 0)
                if length > 4096:
                    raise office_actions.Refused("request too large")
                out = office_waitlist.act(json.loads(self.rfile.read(length) or b"{}"))
                office_waitlist.PANEL.invalidate()
            except office_actions.Refused as exc:   # "error" lets csrf.js retry once on a stale token
                status, out = 403, {"ok": False, "error": str(exc), "message": str(exc)}
            except (ValueError, OSError, subprocess.SubprocessError, office_waitlist.WaitlistError) as exc:
                status, out = 400, {"ok": False, "message": (getattr(exc, "stderr", "") or str(exc))[:300]}
            body = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _transcribe(self):
            status, out = 200, {}
            try:
                office_actions.check_request(self.headers, self.server.server_address[1])
                length = int(self.headers.get("Content-Length") or 0)
                out = {"text": voice.transcribe(self.rfile.read(length) if length else b"")}
            except office_actions.Refused as exc:
                status, out = 403, {"error": str(exc)}
            except (voice.VoiceError, subprocess.TimeoutExpired, ValueError) as exc:
                status, out = 400, {"error": str(exc)}
            body = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _meeting(self):
            status, out = 200, {}
            try:
                office_actions.check_request(self.headers, self.server.server_address[1])
                length = int(self.headers.get("Content-Length") or 0)
                if length > 16384:
                    raise office_actions.Refused("request too large")
                req = json.loads(self.rfile.read(length) or b"{}")
                op, text = req.get("op"), (req.get("text") or "").strip()
                if op == "say" and not 0 < len(text) <= office_actions.MAX_REPLY:
                    raise office_actions.Refused(f"text must be 1-{office_actions.MAX_REPLY} characters")
                if op == "start" and len(text) > office_actions.MAX_REPLY:   # empty = the type's own opening
                    raise office_actions.Refused(f"text must be at most {office_actions.MAX_REPLY} characters")
                repos = {p["project"]: p["repo"] for p in manifest.projects()}
                if op == "start":
                    kind = str(req.get("type") or "status")
                    needs_finance = FINANCE_ON and "finance" in meeting.load_config().get("types", {}).get(kind, {}).get("data", [])
                    fin = FINANCE.get() if needs_finance else None
                    fin_text = None
                    if fin and "error" not in fin:
                        from orgrove import finance  # imports office; loaded lazily to avoid a cycle
                        fin_text = finance.report(fin)
                    out = MEETING.start(text, cache.get(), kind, fin_text)
                elif op == "say":
                    out = MEETING.say(text)
                elif op == "wrap":
                    out = MEETING.wrap(repos)
                    cache.at = 0
                elif op == "cancel":
                    out = MEETING.cancel()
                else:
                    raise office_actions.Refused("unknown op")
            except office_actions.Refused as exc:
                status, out = 403, {"error": str(exc)}
            except meeting.MeetingError as exc:
                status, out = 409, {"error": str(exc)}
            except (ValueError, subprocess.CalledProcessError) as exc:
                status, out = 400, {"error": (getattr(exc, "stderr", "") or str(exc))[:300]}
            body = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(prog="orgrove-office")
    p.add_argument("--project", help="one project only (default: all projects)")
    p.add_argument("--port", type=int, default=8730)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    handler = make_handler(Cache(a.project))
    for port in range(a.port, a.port + 10):  # take the next free port if this one is in use
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"no free port in {a.port}-{a.port + 9}")
    url = f"http://127.0.0.1:{port}/"
    print(f"ORGrove Office on {url}  (Ctrl+C to stop)", flush=True)
    if not a.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
