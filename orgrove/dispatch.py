"""Background dispatcher (#7): coffee-break assignment.

Every tick (a finished run, a wake-up, a project's notify, the Office, or the 30-min schedule) it reads the same
state the Office shows and decides:
  1. deterministic moves: merged PR -> deployed-dev; CI red on the head under review -> in-dev + needs/fix
     before any reviewer is paid for it (#151); review asked for changes -> in-dev + needs/fix;
     too many review rounds -> needs/human; both reviews approved a green head -> gate opens the PR
  2. which roles get work: a free role takes the next ticket waiting (after break_min, 0 by default: no break while
     work waits)
     for it (priority, then oldest). One new run per role per tick; nothing waiting = stays on break.
The budget gate still runs inside every role run; here we only skip runs that would obviously be
refused, so a full day doesn't spam blocked/budget labels.

    python -m orgrove.dispatch [--project <id>] [--dry-run]
"""
import argparse
import copy
import importlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import os

from orgrove import budget, ci, freetime, gate, manifest, office, org, stages, sync_labels, verdict

PRIORITY = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
HOLD = {"needs/human", "blocked/budget", "needs/approval", "blocked/deps"}  # meeting drafts wait for the owner (#45)
# A ticket that builds on other open tickets waits under blocked/deps, with this marker in its body naming them.
# The dispatcher releases it once they have all landed. It is not the owner's question (2026-10-30: #34-#36).
AFTER = re.compile(r"<!--\s*orgrove:after\s+([#\d ,]+?)\s*-->")
CI_RED = "orgrove:ci-red"   # marker on the dispatcher's bounce comment; counted as a review round (#151)
CI_NONE = "orgrove:ci-none"  # marker on the comment that sent a head with no CI back to Dev to bring main in
NO_CI_MIN = 45              # a head with no CI result after this long goes back to Dev once, then to the owner
NO_DEPLOY_MIN = 10          # no run of environments.dev.deployed_by this long after a merge: the deploy's path filters
                            # left this change out (nothing to deploy), so the ticket closes as merged
DEPLOY_POLL_MIN = 1         # a merge's deploy: the next tick is booked for when it should end (its recent successful
                            # runs' median length), then every minute past that; none at all with an install's own timer
DEPLOY_FAILED = "orgrove:deploy-failed"   # marker in the dispatcher's comment: one DevOps round per ticket
# The dispatcher's own "→ labels +[...needs/human...]" comment: an owner reply after it answers a hold.
HELD = re.compile(r"ORGrove dispatcher: .*→ labels \+\[[^\]]*needs/human", re.I)   # either spelling of the name (before 2026-10-29 too)
# After the AI provider refuses a run (usage limit, bad key), nobody starts for this long; then one tick tries again.
# On 2026-10-28 a workspace limit refused every run for hours while the status said "on break".
REFUSAL_PAUSE_MIN = 30
# GitHub's 10-minute schedule ran only every few hours on 2026-10-28, and never in a new organisation's first hours
# (#159 sandbox), so the dispatcher books its own next tick (wake.yml) whenever something will need one and no
# running agent will start it. A wake-up sleeps on a runner, and sleeping is billed: every reason is bounded.
WAKE_MAX_MIN = 20          # a coffee break with work waiting behind it, ending within this
FOLLOW_UP_MIN = 1          # this tick changed labels but started nobody: the next tick acts on them
CI_POLL_MIN = 3            # a review waits for CI on the agent's head (bounded: NO_CI_MIN sends it back to Dev)
OWNER_WAKES_MIN = (10, 20)  # waiting on a person (merge, question, draft): then stop; the Office's buttons start a
                            # tick at once. Not booked when a timer ticks anyway: GitHub's schedule ran in the last
                            # SCHEDULE_ALIVE_MIN, or the install has its own ([wake] owner_checks = false in org.toml).
SCHEDULE_ALIVE_MIN = 35     # the schedule runs every 30 min (dispatch.yml), plus GitHub's usual delay


def order(cards):
    return sorted(cards, key=lambda c: (PRIORITY.get(c.get("priority"), 9), c["number"]))


def plan_order(cards, focus=None):
    """The roadmap order: focus milestone first, then target date (undated last), priority, oldest."""
    return sorted(cards, key=lambda c: (0 if focus and c.get("milestone") == focus else 1,
                                        c.get("target") or "9999-12-31",
                                        PRIORITY.get(c.get("priority"), 9), c["number"]))


def board_sync(cfg, cards, board, open_numbers, status_map=None):
    """Keep the board's Status true: agent stages mirror config; anything closed is Done.

    status_map translates the pipeline's board statuses to the names a project's board uses.
    """
    to_board = lambda s: (status_map or {}).get(s, s)  # noqa: E731
    mirror = {s["id"]: s["board_status"] for s in cfg["stage"]}
    actions = []
    for c in cards:
        want = "Done" if c["stage"] == "done" else mirror.get(c["stage"])
        if want and c.get("on_board") and c.get("board_status") != to_board(want):
            actions.append({"kind": "board", "issue": c["number"], "item_id": board[c["number"]]["item_id"],
                            "status": to_board(want), "why": f"stage {c['stage']}"})
    for n, b in board.items():
        if n not in open_numbers and b["status"] != to_board("Done") and not any(a["issue"] == n for a in actions):
            actions.append({"kind": "board", "issue": n, "item_id": b["item_id"], "status": to_board("Done"),
                            "why": "issue is closed"})
    return actions


def on_break_long_enough(row, now):
    if row["status"] != "break":
        return False
    if row["break_since"] is None:
        return True
    since = datetime.fromisoformat(row["break_since"].replace("Z", "+00:00"))
    return (now - since).total_seconds() >= row["break_min"] * 60


def decide(dept, cfg, cards, roster_rows, review, merged, backlog, now, spent_today, daily_cap,
           focus=None, roadmap_request=None, has_env=True, answered=(), explain=None, budget_kind="daily", deploys=None,
           sanity_passed=(), refused=None, awaiting_ci=None, deps=None, project=None):
    """Pure decision step. Returns a list of actions; see module docstring.

    review:  issue -> {"head", "verdicts": {role: verdict}, "rounds": {role: n}, "open_subs": [..],
                       "ci": green|red|pending|none, "ci_failing": [(name, url)], "ci_bounces": n, "ci_kicks": n,
                       "head_age_min": m, "granted": bool}
             Counts restart when an owner answers a hold, and "granted" then allows one more try.
             A missing "ci" means green (callers that predate #151).
    merged:  set of issue numbers whose agent PR has been merged
    backlog: open issues with no stage label (PM candidates), each a card dict
    focus: milestone planned first; then by target date (undated last), priority, oldest
    roadmap_request: issue number of the roadmap issue when it carries needs/roadmap
    has_env: False for projects with no deploy environment; a merged PR then closes the ticket
    answered: held (needs/human) issues whose newest comment is an owner's reply
    explain: optional list; gets one status per roster role saying what it does or why it is idle (#98)
    budget_kind: which cap `daily_cap` really is today ("daily" or "monthly"), for the explanation
    sanity_passed: deployed-dev issues Sanity has passed since they were deployed, but that are still open
    refused: the AI provider's refusal of the latest run, while it is recent; nobody is started
    awaiting_ci: awaiting-review issues with an open agent PR -> {"head", "ci", "ci_failing", "ci_bounces", "url"}.
             main moves after the reviews, so a PR can turn red while it waits for the owner; it goes back to Dev.
    deps:    blocked/deps issues -> the issue numbers they wait for that haven't landed yet (empty = release)
    project: the project these cards belong to. A role acts only in its `projects` (#112), and work is picked by
             the role's skill, so two roles with one skill (the two PMs) each serve their own project.
    """
    actions = []
    # GitHub closes a ticket when its PR says "Closes #n", and people close tickets too: whatever closed it, a
    # closed ticket's stage label becomes stage/done, quietly (no comment on a closed ticket).
    live = {s.get("label", f"stage/{s['id']}") for s in cfg["stage"]}   # replay fixtures keep only the id
    for c in cards:
        if c["stage"] == "done" and (stale := sorted(live & set(c["labels"]))):
            actions.append({"kind": "label", "issue": c["number"], "remove": stale, "add": [stages.DONE_LABEL],
                            "why": "closed", "quiet": True})
    for c in cards:
        if "blocked/deps" in c["flags"] and (deps or {}).get(c["number"]) == []:
            actions.append({"kind": "label", "issue": c["number"], "remove": ["blocked/deps"], "add": [],
                            "why": "every ticket it waits for has landed",
                            "note": f"Released: the tickets this one builds on are merged. It continues from stage {c['stage']}."})
            c["flags"] = [f for f in c["flags"] if f != "blocked/deps"]
    for c in cards:
        if c["number"] in answered and "needs/human" in c["flags"]:
            back_to_pm = c["stage"] == "backlog"
            actions.append({"kind": "label", "issue": c["number"], "remove": ["needs/human"],
                            "add": ["needs/plan"] if back_to_pm and "needs/plan" not in c["flags"] else [],
                            "why": "the owner replied; " + ("back to the PM to re-plan with the answer" if back_to_pm
                                                            else f"back to stage {c['stage']}")})
            c["flags"] = [f for f in c["flags"] if f != "needs/human"] + (["needs/plan"] if back_to_pm else [])
    active = [c for c in cards if c["stage"] != "done" and not HOLD & set(c["flags"])]
    max_rounds = cfg["max_review_rounds"]
    infra = lambda c: "area/infra" in c["labels"]  # noqa: E731

    for c in active:
        n = c["number"]
        if c["stage"] == "awaiting-review" and n in merged:
            if has_env:
                # Sanity checks the build that has the merge, so the ticket waits for the project's deploy of the
                # merge commit; a failed deploy goes to DevOps once, then to the owner (#159).
                d = (deploys or {}).get(n) or {"state": "none", "age_min": 0}
                if d["state"] == "success":
                    actions.append({"kind": "label", "issue": n, "remove": ["stage/awaiting-review"],
                                    "add": ["stage/deployed-dev"], "why": f"PR merged by a human and deployed ({d['url']})"})
                    c["stage"] = "deployed-dev"
                elif d["state"] == "failure" and d.get("failures", 0) >= 1:
                    actions.append({"kind": "label", "issue": n, "remove": [], "add": ["needs/human"],
                                    "why": f"the deploy failed again after DevOps's fix ({d['url']})"})
                    c["stage"] = "handled"
                elif d["state"] == "failure":
                    actions.append({"kind": "label", "issue": n, "remove": ["stage/awaiting-review"],
                                    "add": ["stage/in-dev", "needs/fix", "area/infra"],
                                    "why": "the deploy of the merge failed",
                                    "note": f"Deploy run: {d['url']}\n\nDevOps: follow **On `deploy:failed`** in your skill: "
                                            "read the failed job's log, classify the cause, and fix it in code on a new "
                                            f"agent branch if it is a code cause; otherwise `needs/human`.\n<!-- {DEPLOY_FAILED} -->"})
                    c["stage"] = "handled"
                elif d["state"] == "none" and d["age_min"] >= NO_DEPLOY_MIN:
                    actions.append({"kind": "close", "issue": n,
                                    "why": f"PR merged by a human; the deploy workflow didn't run for this change in "
                                           f"{NO_DEPLOY_MIN} min (its path filters leave it out), so there is nothing "
                                           "for Sanity to check"})
                    c["stage"] = "handled"
                else:
                    c["stage"] = "handled"   # the deploy is running or about to: the next tick looks again
            else:
                actions.append({"kind": "close", "issue": n,
                                "why": "PR merged by a human; this project has no deploy environment to check"})
                c["stage"] = "handled"
        elif c["stage"] == "awaiting-review" and (awaiting_ci or {}).get(n, {}).get("ci") == "red":
            # The PR was green when the gate opened it, but main has moved since: the owner can't merge a red PR,
            # so it isn't theirs to look at (2026-10-30: #31's PR sat red in "waiting on you").
            a = awaiting_ci[n]
            if a.get("ci_bounces", 0) >= max_rounds:
                actions.append({"kind": "label", "issue": n, "remove": [], "add": ["needs/human"],
                                "why": f"its PR is red again after {a['ci_bounces']} CI round(s)"})
            else:
                checks = "\n".join(f"- {name}: {url}" for name, url in a.get("ci_failing", [])) or "- (no names)"
                actions.append({"kind": "label", "issue": n, "remove": ["stage/awaiting-review"],
                                "add": ["stage/in-dev", "needs/fix"], "why": "its open PR went red after the reviews",
                                "note": f"PR {a.get('url', '')} is red on `{a['head']}` (CI runs on the PR merged with "
                                        f"current main):\n{checks}\n\nDev: merge `origin/main` into the branch, fix the "
                                        "cause (not the test), run `commands.contract`, push. The PR updates itself."
                                        f"\n<!-- {CI_RED} head={a['head']} -->"})
            c["stage"] = "handled"
        elif c["stage"] == "deployed-dev" and n in sanity_passed:
            # Sanity passed but left the ticket open, as it should for a long-running ticket that ships in chunks.
            # Left at deployed-dev, Sanity re-checked the same build every break (#43: 6 rounds on 2026-10-28).
            actions.append({"kind": "label", "issue": n, "remove": ["stage/deployed-dev"], "add": ["needs/plan"],
                            "why": "Sanity passed the deployed build and the ticket is still open",
                            "note": "PM: plan the next chunk, or close the ticket if nothing is left."})
            c["stage"] = "handled"
        elif c["stage"] == "agent-review" and n in review:
            r = review[n]
            state = r.get("ci", "green")
            limit = 1 if r.get("granted") else max_rounds   # the owner answered a hold: one more try
            if state == "red":
                # Nobody reviews a head CI has already failed: it goes straight back to Dev (#151).
                if r.get("ci_bounces", 0) >= limit:
                    actions.append({"kind": "label", "issue": n, "remove": [], "add": ["needs/human"],
                                    "why": f"CI still red after {r.get('ci_bounces', 0)} round(s)"})
                else:
                    checks = "\n".join(f"- {name}: {url}" for name, url in r.get("ci_failing", [])) or "- (no names)"
                    actions.append({"kind": "label", "issue": n, "remove": ["stage/agent-review"],
                                    "add": ["stage/in-dev", "needs/fix"], "why": "CI is red on the head; no review until it is green",
                                    "note": f"Failing checks on `{r['head']}`:\n{checks}\n\nDev: read the failing job log, fix the cause "
                                            f"(not the test), run `commands.contract`, push.\n<!-- {CI_RED} head={r['head']} -->"})
                c["stage"] = "handled"
                continue
            if state == "none" and r.get("head_age_min", 0) >= NO_CI_MIN:
                # Usually a branch cut before Agent CI existed: a push runs the workflows IN the pushed commit,
                # so it has none to run (#599, 2026-10-28). Dev brings main in once; only then is it the owner's.
                if r.get("ci_kicks", 0) >= 1:
                    actions.append({"kind": "label", "issue": n, "remove": [], "add": ["needs/human"],
                                    "why": f"still no CI result {NO_CI_MIN} min after Dev brought main into the branch"})
                else:
                    actions.append({"kind": "label", "issue": n, "remove": ["stage/agent-review"],
                                    "add": ["stage/in-dev", "needs/fix"],
                                    "why": f"no CI result on the head after {NO_CI_MIN} min; reviews wait for CI",
                                    "note": f"`{r['head']}` has no CI run. A push runs the workflows in the pushed commit, so "
                                            "a branch cut before Agent CI existed has none. Dev: merge `origin/main` into "
                                            "the branch (a merge commit, no other change), run `commands.contract`, push."
                                            f"\n<!-- {CI_NONE} head={r['head']} -->"})
                c["stage"] = "handled"
                continue
            changes = [role for role, v in r["verdicts"].items() if v["decision"] == "changes"]
            if changes:
                if any(r["rounds"].get(role, 0) >= limit for role in changes):
                    actions.append({"kind": "label", "issue": n, "remove": [], "add": ["needs/human"],
                                    "why": f"still failing review after {max(r['rounds'].get(x, 0) for x in changes)} rounds"})
                else:
                    actions.append({"kind": "label", "issue": n, "remove": ["stage/agent-review"],
                                    "add": ["stage/in-dev", "needs/fix"], "why": f"{', '.join(changes)} asked for changes"})
                c["stage"] = "handled"
            elif not gate.readiness(r["verdicts"], r["open_subs"], state):
                actions.append({"kind": "gate", "issue": n, "why": "QA and Security approved the head"})
                c["stage"] = "handled"

    def waiting(role):
        if role in ("qa", "security"):
            return [c for c in active if c["stage"] == "agent-review" and c["number"] in review
                    and role not in review[c["number"]]["verdicts"] and review[c["number"]].get("ci", "green") == "green"]
        if role in ("dev", "devops"):
            mine = [c for c in active if infra(c) == (role == "devops")]
            fixes = [c for c in mine if c["stage"] == "in-dev" and "needs/fix" in c["flags"]]
            in_flight = [c for c in mine if c["stage"] in ("in-dev", "agent-review")]
            wip = dept["roles"][role].get("wip", 1)
            fresh = [c for c in mine if c["stage"] == "planned"] if len(in_flight) < wip else []
            return order(fixes) + order(fresh)
        if role == "sanity":
            return [c for c in active if c["stage"] == "deployed-dev"]
        if role == "pm":
            asked = [c for c in active if "needs/plan" in c["flags"]]
            roadmap = [{"number": roadmap_request, "flags": ["needs/roadmap"]}] if roadmap_request else []
            queued = [c for c in active if c["stage"] in ("planned", "in-dev")]
            room = len(queued) < dept["roles"]["dev"].get("wip", 1)
            ready = [b for b in backlog if not HOLD & set(b["flags"])]
            # Real work first: while Dev has room, plan the next roadmap ticket before dating more of the roadmap.
            return order(asked) + (plan_order(ready, focus) if room else []) + roadmap
        return []

    def why_empty(role):
        """The reason a role with nothing to pick up is idle: held by the owner, blocked by another
        agent, or simply nothing there. Uses the same stage rules as waiting()."""
        stages_of = {"pm": ("backlog",), "dev": ("planned", "in-dev"), "devops": ("planned", "in-dev"),
                     "qa": ("agent-review",), "security": ("agent-review",), "sanity": ("deployed-dev",)}
        mine = [c for c in cards if c["stage"] in stages_of.get(role, ())
                and (role not in ("dev", "devops") or infra(c) == (role == "devops"))]
        held = [c for c in mine if HOLD & set(c["flags"])]
        if role in ("qa", "security"):
            stuck = [c for c in mine if c not in held and review.get(c["number"], {}).get("ci") == "blocked"]
            if stuck:
                return "waiting_owner", [c["number"] for c in stuck], \
                    "GitHub did not start CI on " + ", ".join(f"#{c['number']}" for c in stuck) + \
                    " (billing or spending limit); re-run it once Actions is unblocked"
            on_ci = [c for c in mine if c not in held and review.get(c["number"], {}).get("ci", "green") in ("pending", "none")]
            if on_ci:
                return "waiting_agent", [c["number"] for c in on_ci], \
                    "waiting for CI on " + ", ".join(f"#{c['number']}" for c in on_ci) + " before reviewing"
        if role in ("dev", "devops"):
            in_flight = [c for c in active if infra(c) == (role == "devops") and c["stage"] in ("in-dev", "agent-review")]
            planned = [c for c in active if infra(c) == (role == "devops") and c["stage"] == "planned"]
            if planned and len(in_flight) >= dept["roles"][role].get("wip", 1):
                return "waiting_agent", [c["number"] for c in in_flight], \
                    "work-in-progress limit reached: " + ", ".join(f"#{c['number']}" for c in in_flight) + " still in dev/review"
        if role == "pm" and [b for b in backlog if not HOLD & set(b["flags"])]:
            return "waiting_agent", [], "Dev's planned queue is full, so nothing new is planned yet"
        if held:
            flags = sorted({f for c in held for f in HOLD & set(c["flags"])})
            kind = {("blocked/budget",): "budget_daily", ("blocked/deps",): "waiting_agent"}.get(tuple(flags), "waiting_owner")
            return kind, [c["number"] for c in held], f"{len(held)} ticket(s) held: {', '.join(flags)}"
        return "nothing_to_do", [], "nothing in this role's stages"

    queue = {}

    def tell(role, reason, issues=(), detail="", until=None):
        if explain is not None:
            explain.append({"role": role, "reason": reason, "issues": list(issues), "detail": detail, "until": until,
                            "next": [c["number"] for c in queue.get(role, [])[:3]]})

    budget_left = daily_cap - spent_today
    for row in roster_rows:
        role = row["role"]
        if not manifest.serves(dept["roles"][role], project):
            continue
        skill = dept["roles"][role].get("skill", role)   # fixtures recorded before #112 carry no skill
        queue[role] = order(waiting(skill)) if skill not in ("dev", "devops", "pm") else waiting(skill)
        if row["status"] == "working":
            tell(role, "working", [o["number"] for o in row.get("on", [])], "run in progress")
            continue
        if refused:
            tell(role, "provider_refused", detail=refused)
            continue
        if not on_break_long_enough(row, now):
            since = datetime.fromisoformat(row["break_since"].replace("Z", "+00:00"))
            end = since + timedelta(minutes=row["break_min"])
            tell(role, "on_break", detail=f"{max(int((end - now).total_seconds() // 60), 0) + 1} min left",
                 until=end.isoformat(timespec="seconds"))
            continue
        todo = queue[role]
        if not todo:
            tell(role, *why_empty(skill))
            continue
        ceiling = dept["roles"][role]["ceiling_usd"]
        if ceiling > budget_left:
            actions.append({"kind": "skip", "role": role, "issue": todo[0]["number"], "why": "not enough budget left today"})
            tell(role, f"budget_{budget_kind}", [todo[0]["number"]],
                 f"needs ${ceiling:.2f}, ${max(budget_left, 0):.2f} left under the {budget_kind} cap")
            continue
        budget_left -= ceiling
        pick = todo[0]
        tell(role, "starting", [pick["number"]], "picked up this tick")
        if skill == "pm" and not {"needs/plan", "needs/roadmap"} & set(pick["flags"]):
            actions.append({"kind": "label", "issue": pick["number"], "remove": [], "add": ["needs/plan"],
                            "why": "next in the plan; the planned queue has room"})
        actions.append({"kind": "run", "role": role, "issue": pick["number"],
                        "why": f"{role} on coffee break >= {row['break_min']} min" if row["break_min"] else f"{role} is free"})
    return actions


# ---------- gathering state (GitHub, via gh) ----------

def review_counts(comments, owners):
    """Review rounds per role, CI bounces and no-CI kicks from an issue's comments. An owner's reply to one of the
    dispatcher's holds restarts the counts and grants one more try: otherwise "one more round" bounced straight
    back to the owner on the next tick, because the old counts still read as over the limit (#23, 2026-10-28)."""
    owners = {o.lower() for o in owners}
    rounds, bounces, kicks, held, granted = {}, 0, 0, False, False
    for cm in comments:
        body = cm.get("body") or ""
        login = cm["user"]["login"]
        if login.lower() in owners:
            if held:
                rounds, bounces, kicks, held, granted = {}, 0, 0, False, True
            continue
        if login != org.BOT_LOGIN:
            continue
        held = held or bool(HELD.search(body))
        bounces += CI_RED in body
        kicks += CI_NONE in body
        for block in verdict.BLOCK.findall(body):
            try:
                role = json.loads(block).get("role")
            except json.JSONDecodeError:
                continue
            if role in ("qa", "security"):
                rounds[role] = rounds.get(role, 0) + 1
    return {"rounds": rounds, "ci_bounces": bounces, "ci_kicks": kicks, "granted": granted}


def review_state(repo, cards, owners=()):
    out = {}
    for c in cards:
        if c["stage"] != "agent-review":
            continue
        n = c["number"]
        refs = office.gh_json("api", f"repos/{repo}/git/matching-refs/heads/agent/{n}-")
        if len(refs) != 1:
            continue
        commit = office.gh_json("api", f"repos/{repo}/commits/{refs[0]['ref'].removeprefix('refs/heads/')}")
        head = commit["sha"]
        checks = ci.head_checks(repo, head, office.gh_json)
        pushed = datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00"))
        comments = office.gh_json("api", f"repos/{repo}/issues/{n}/comments?per_page=100")
        subs = office.gh_json("api", f"repos/{repo}/issues/{n}/sub_issues")
        out[n] = {"head": head, "verdicts": gate.latest_verdicts(comments, head), **review_counts(comments, owners),
                  "open_subs": [s["number"] for s in subs if s["state"] == "open"],
                  "ci": ci.ci_state(checks), "ci_failing": ci.failing(checks),
                  "head_age_min": (datetime.now(timezone.utc) - pushed).total_seconds() / 60}
    return out


def awaiting_pr_ci(repo, cards, owners=()):
    """CI on the open agent PR of each awaiting-review issue (see decide's awaiting_ci)."""
    waiting = {c["number"] for c in cards if c["stage"] == "awaiting-review"}
    if not waiting:
        return {}
    prs = office.gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", "100",
                         "--json", "number,headRefName,headRefOid,url")
    out = {}
    for n in waiting:
        mine = [pr for pr in prs if pr["headRefName"].startswith(f"agent/{n}-")]
        if len(mine) != 1:
            continue
        head = mine[0]["headRefOid"]
        checks = ci.head_checks(repo, head, office.gh_json)
        state = ci.ci_state(checks)
        entry = {"head": head, "ci": state, "url": mine[0]["url"]}
        if state == "red":
            comments = office.gh_json("api", f"repos/{repo}/issues/{n}/comments?per_page=100")
            entry |= {"ci_failing": ci.failing(checks), "ci_bounces": review_counts(comments, owners)["ci_bounces"]}
        out[n] = entry
    return out


def waits_for(body):
    """Issue numbers named in a body's <!-- orgrove:after #31 #33 --> marker."""
    return sorted({int(x) for m in AFTER.findall(body or "") for x in re.findall(r"\d+", m)})


def dependency_state(repo, cards):
    """blocked/deps issues -> the numbers they still wait for. A dependency has landed when it is closed or already
    on Dev (deployed-dev). An issue with no marker is left held, so a typo never releases work early."""
    held = [c for c in cards if "blocked/deps" in c["flags"] and c["stage"] != "done"]
    if not held:
        return {}
    stage = {c["number"]: c["stage"] for c in cards}
    out = {}
    for c in held:
        body = office.gh_json("issue", "view", str(c["number"]), "-R", repo, "--json", "body")["body"]
        wanted = waits_for(body)
        if not wanted:
            continue
        left = []
        for d in wanted:
            st = stage.get(d)
            if st is None:
                st = "done" if office.gh_json("issue", "view", str(d), "-R", repo, "--json", "state")["state"] == "CLOSED" else "open"
            if st not in ("done", "deployed-dev"):
                left.append(d)
        out[c["number"]] = left
    return out


def sanity_passes(repo, cards):
    """deployed-dev issues with a Sanity pass verdict from the bot posted after the issue last went to deployed-dev."""
    out = set()
    for c in cards:
        if c["stage"] != "deployed-dev":
            continue
        n = c["number"]
        # Every page: a long-running ticket has hundreds of events (#43), and the newest are on the last page.
        pages = office.gh_json("api", "--paginate", "--slurp", f"repos/{repo}/issues/{n}/events?per_page=100")
        since = max((e["created_at"] for page in pages for e in page if e.get("event") == "labeled"
                     and (e.get("label") or {}).get("name") == "stage/deployed-dev"), default="")
        comments = [cm for page in office.gh_json("api", "--paginate", "--slurp",
                                                  f"repos/{repo}/issues/{n}/comments?per_page=100") for cm in page]
        if sanity_passed(comments, since):
            out.add(n)
    return out


def sanity_passed(comments, since=""):
    """True when the bot's newest Sanity verdict after `since` (ISO time) is a pass."""
    last = None
    for cm in comments:
        if cm["user"]["login"] != org.BOT_LOGIN or cm["created_at"] < since:
            continue
        for block in verdict.BLOCK.findall(cm.get("body") or ""):
            try:
                v = json.loads(block)
            except json.JSONDecodeError:
                continue
            if v.get("role") == "sanity":
                last = v.get("decision")
    return last == "pass"


def provider_refusal(events, now, pause_min=REFUSAL_PAUSE_MIN):
    """The AI provider's message if the latest settled run was refused less than `pause_min` ago, else None."""
    settled = [e for e in events if e["type"] == "cost"]
    if not settled:
        return None
    last = max(settled, key=lambda e: e["ts"])
    if not last.get("error"):
        return None
    age = (now - datetime.fromisoformat(last["ts"])).total_seconds() / 60
    if age >= pause_min:
        return None
    return (f"the AI provider refused the last run ({last['role']} on {last['issue']}, {int(age)} min ago): "
            f"{last['error']} Next try in {int(pause_min - age) + 1} min.")


def merged_issues(repo, cards, gh=None):
    """Issues at awaiting-review whose agent PR is merged -> {"sha", "at"} of their latest merged PR (a deploy fix
    is a second PR on the same ticket). Matched on the PR's head ref name, not the branch, because repos may delete
    head branches on merge (#57)."""
    waiting = {c["number"] for c in cards if c["stage"] == "awaiting-review"}
    if not waiting:
        return {}
    gh = gh or office.gh_json
    prs = gh("pr", "list", "-R", repo, "--state", "merged", "--limit", "100",
             "--json", "number,headRefName,mergeCommit,mergedAt")
    out = {}
    for n in waiting:
        mine = [pr for pr in prs if pr["headRefName"].startswith(f"agent/{n}-")]
        if mine:
            last = max(mine, key=lambda pr: pr.get("mergedAt") or "")
            out[n] = {"sha": (last.get("mergeCommit") or {}).get("oid"), "at": last.get("mergedAt")}
    return out


def _at(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def deploy_at(deploys, now):
    """When to look again at deploys still running or not yet started: the earliest expected end, at least now; a
    deploy past its expected end, or not started yet, is looked at again in DEPLOY_POLL_MIN."""
    times = [d["eta"] if d.get("eta") and d["eta"] > now else now + timedelta(minutes=DEPLOY_POLL_MIN)
             for d in deploys.values() if d["state"] in ("pending", "none")]
    return min(times) if times else None


def deploy_states(repo, workflow, merged, now, gh=None, known_failing=()):
    """For each merged ticket: the run of the project's deploy workflow on its merge commit (#159).
    -> {issue: {"state": success|failure|pending|none, "url", "age_min", "failures", "eta"}}; eta (pending only) is when
    the run should end: its start plus the median length of the workflow's last successful runs. A run whose only failed jobs
    are in the project's known_failing counts as a success (a job the project knows is failing, for a reason outside it).
    failures counts earlier DevOps rounds (the dispatcher's DEPLOY_FAILED comments) on the ticket."""
    gh, out, typical = gh or office.gh_json, {}, None
    for n, m in merged.items():
        runs = gh("run", "list", "-R", repo, "--workflow", workflow, "--commit", m["sha"], "--limit", "5",
                  "--json", "databaseId,status,conclusion,url,createdAt") if m.get("sha") else []
        run = max(runs, key=lambda r: r.get("createdAt") or "") if runs else None
        if run is None:
            state = "none"
        elif run.get("status") != "completed":
            state = "pending"
            if typical is None:   # once per call: the same workflow for every ticket
                done = gh("run", "list", "-R", repo, "--workflow", workflow, "--status", "success", "--limit", "5",
                          "--json", "createdAt,updatedAt")
                lengths = sorted((_at(r["updatedAt"]) - _at(r["createdAt"])).total_seconds() for r in done)
                typical = timedelta(seconds=lengths[len(lengths) // 2]) if lengths else timedelta(0)
        else:
            state = "success" if run.get("conclusion") in ("success", "skipped") else "failure"
        if state == "failure" and known_failing:
            jobs = gh("run", "view", str(run["databaseId"]), "-R", repo, "--json", "jobs").get("jobs", [])
            bad = {j["name"] for j in jobs if j.get("conclusion") not in ("success", "skipped", "neutral")}
            if bad and bad <= set(known_failing):
                state = "success"
        failures = 0
        if state == "failure":
            comments = gh("issue", "view", str(n), "-R", repo, "--json", "comments").get("comments", [])
            failures = sum(DEPLOY_FAILED in (c.get("body") or "") for c in comments)
        age = (now - _at(m["at"])).total_seconds() / 60 if m.get("at") else 0
        eta = _at(run["createdAt"]) + typical if state == "pending" and typical and run.get("createdAt") else None
        out[n] = {"state": state, "url": (run or {}).get("url"), "age_min": age, "failures": failures, "eta": eta}
    return out


def answered_issues(repo, cards, owners):
    """Held issues whose newest comment is from an owner, i.e. the owner has replied."""
    out = set()
    for c in cards:
        if "needs/human" not in c["flags"] or c["stage"] == "done":
            continue
        last = office.gh_json("api", f"repos/{repo}/issues/{c['number']}/comments?per_page=100")
        if last and last[-1]["user"]["login"].lower() in {o.lower() for o in owners}:
            out.add(c["number"])
    return out


def board_ids(prof):
    b = prof["board"]
    pid = office.gh_json("project", "view", str(b["number"]), "--owner", b["owner"], "--format", "json")["id"]
    status = prof.get("board_fields", {}).get("status", "Status")
    fields = office.gh_json("project", "field-list", str(b["number"]), "--owner", b["owner"], "--format", "json")["fields"]
    f = next(x for x in fields if x["name"] == status)
    return pid, f["id"], {o["name"]: o["id"] for o in f["options"]}


def skill_of(role, prof):
    """role-run.yml takes a skill (role: [pm, dev, ...]); runcfg resolves it to the role serving the project (#112)."""
    roles = manifest.department(prof["departments"][0])["roles"] if prof else {}
    return roles.get(role, {}).get("skill", role)


def execute(repo, project, actions, dry_run, prof=None):
    ids, labels_synced = None, False
    for a in actions:
        print(json.dumps(a))
        if dry_run or a["kind"] == "skip":
            continue
        if a["kind"] == "label":
            if stages.DONE_LABEL in a["add"] and not labels_synced:
                # stage/done is new (2026-10-30): an install from before it lacks the label, and GitHub refuses to
                # add a missing one. The sync only creates what is missing; one label list per tick that needs it.
                sync_labels.main(repo)
                labels_synced = True
            args = ["issue", "edit", str(a["issue"]), "-R", repo]
            if a["remove"]:
                args += ["--remove-label", ",".join(a["remove"])]
            if a["add"]:
                args += ["--add-label", ",".join(a["add"])]
            subprocess.run(["gh", *args], check=True, capture_output=True)
            if a.get("quiet"):
                continue
            # needs/human is a question for the owners: mention them so it reaches their phone.
            mention = " ".join(f"@{o}" for o in manifest.owners_for_repo(repo)) + " " if "needs/human" in a["add"] else ""
            note = f"\n\n{a['note']}" if a.get("note") else ""
            subprocess.run(["gh", "issue", "comment", str(a["issue"]), "-R", repo, "--body",
                            f"{mention}ORGrove dispatcher: {a['why']} → labels +{a['add']} −{a['remove']}{note}"],
                           check=True, capture_output=True)
        elif a["kind"] == "board":
            ids = ids or board_ids(prof)
            pid, field, options = ids
            subprocess.run(["gh", "project", "item-edit", "--project-id", pid, "--id", a["item_id"],
                            "--field-id", field, "--single-select-option-id", options[a["status"]]],
                           check=True, capture_output=True)
        elif a["kind"] == "close":
            subprocess.run(["gh", "issue", "close", str(a["issue"]), "-R", repo, "--comment",
                            f"ORGrove dispatcher: {a['why']}."], check=True, capture_output=True)
        elif a["kind"] == "gate":
            gate.main(["--repo", repo, "--issue", str(a["issue"])])
        elif a["kind"] == "run":
            # Starting a workflow needs actions:write on orgrove, which the App token deliberately
            # lacks; the dispatcher's own GITHUB_TOKEN is passed in as ORGROVE_DISPATCH_TOKEN.
            env = {**os.environ, "GH_TOKEN": os.environ.get("ORGROVE_DISPATCH_TOKEN", os.environ.get("GH_TOKEN", ""))}
            subprocess.run(["gh", "workflow", "run", "role-run.yml", "-R", office.ORGROVE_REPO,
                            "-f", f"role={skill_of(a['role'], prof)}", "-f", f"issue={a['issue']}", "-f", f"project={project}",
                            "-f", f"mode={a.get('mode', 'ticket')}"],
                           check=True, capture_output=True, env=env)


# Most informative reason first when a role is idle in several projects (#98).
REASON_RANK = ["working", "starting", "provider_refused", "on_break", "budget_monthly", "budget_daily", "waiting_owner",
               "waiting_agent", "nothing_to_do"]


def combine(per_project):
    """[(project, [status, ...]), ...] -> role -> one status. Busy anywhere is busy; otherwise the
    highest-ranked idle reason, with every project's detail kept."""
    out = {}
    for project, statuses in per_project:
        for st in statuses:
            st = st | {"project": project}
            cur = out.get(st["role"])
            if cur is None or REASON_RANK.index(st["reason"]) < REASON_RANK.index(cur["reason"]):
                bare = {k: v for k, v in (cur or {}).items() if k != "also"}
                out[st["role"]] = st | {"also": (cur or {}).get("also", []) + ([bare] if cur else [])}
            else:
                cur["also"] = cur.get("also", []) + [st]
    return out


def wake_plan(roles, now, max_min=WAKE_MAX_MIN, acted=False, waiting=(), owner_wakes=0, deploy_wait=None):
    """(when, why) for the next tick, or None. Nothing while an agent is working or starting: a finishing run
    already starts a tick. Otherwise the earliest of: a coffee break with work waiting behind it, ending within
    max_min ("break"); a minute after a tick that changed labels but started nobody ("follow-up"); every few
    minutes while a review waits for CI ("ci"); and, while something waits on a person, a few spaced wake-ups
    ("owner", owner_wakes of them already booked for this same wait)."""
    if any(st["reason"] in ("working", "starting") for st in roles.values()):
        return None
    plans = [(datetime.fromisoformat(st["until"]), "break") for st in roles.values()
             if st["reason"] == "on_break" and st.get("until") and st.get("next")]
    plans = [(t, why) for t, why in plans if t - now <= timedelta(minutes=max_min)]
    if acted:
        plans.append((now + timedelta(minutes=FOLLOW_UP_MIN), "follow-up"))
    if any(st["reason"] == "waiting_agent" and st.get("detail", "").startswith("waiting for CI") for st in roles.values()):
        plans.append((now + timedelta(minutes=CI_POLL_MIN), "ci"))
    if deploy_wait:   # when a merge's deploy should have ended (deploy_at); bounded by the deploy's own end
        plans.append((deploy_wait, "deploy"))
    if waiting and owner_wakes < len(OWNER_WAKES_MIN):
        plans.append((now + timedelta(minutes=OWNER_WAKES_MIN[owner_wakes]), "owner"))
    return min(plans) if plans else None


def next_wake(roles, now, max_min=WAKE_MAX_MIN, **kw):
    plan = wake_plan(roles, now, max_min, **kw)
    return plan[0] if plan else None


WAKE_WHY = {"break": "agents on break with work waiting", "follow-up": "this tick changed labels, the next acts on them",
            "ci": "a review waits for CI", "deploy": "a merge's deploy is running", "owner": "waiting on a person; the Office's buttons start a tick at once",
            "api": "the App's GitHub API allowance is below its reserve until it resets"}


def schedule_alive(events, now, minutes=SCHEDULE_ALIVE_MIN):
    """True when GitHub's schedule started a tick in the last `minutes` (each status records what started it)."""
    since = (now - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return any(e.get("type") == "status" and e.get("trigger") == "schedule" and e.get("ts", "") >= since for e in events)


def owner_wakes(events, waiting):
    """How many owner wake-ups the latest ticks booked for this same wait (the newest status events first)."""
    n = 0
    for ev in sorted((e for e in events if e.get("type") == "status"), key=lambda e: e.get("ts", ""), reverse=True):
        if ev.get("wake_why") != "owner" or ev.get("owner_waiting") != waiting:
            break
        n += 1
    return n


def owner_waiting(project, cards):
    """What is waiting on a human right now: questions, draft approvals, PRs to merge."""
    out = []
    for c in cards:
        if c["stage"] == "done":
            continue
        if "needs/human" in c["flags"]:
            out.append({"project": project, "issue": c["number"], "what": "question", "title": c["title"]})
        elif "needs/approval" in c["flags"]:
            out.append({"project": project, "issue": c["number"], "what": "approve draft", "title": c["title"]})
        elif c["stage"] == "awaiting-review":
            out.append({"project": project, "issue": c["number"], "what": "merge PR", "title": c["title"]})
    return out


def record_status(event):
    """Append one status event to the ledger (dispatcher only; skipped when no ledger is checked out)."""
    path = os.environ.get("ORGROVE_LEDGER_DIR")
    if not path:
        return False
    from orgrove.ledger import GitLedger
    GitLedger(path).transact(lambda _ledger: (event["day"], event, True))
    return True


def load_candidate(spec):
    """Import `module:function` (a candidate decide()). Returns (function, None) or (None, error text)."""
    try:
        module, _, name = spec.partition(":")
        if not module or not name:
            raise ValueError("expected module:function")
        fn = getattr(importlib.import_module(module), name)
        if not callable(fn):
            raise TypeError(f"{spec} is not callable")
        return fn, None
    except Exception as e:  # any import-time failure of arbitrary code: the live tick must go on
        return None, f"{type(e).__name__}: {e}"


def _key(action):
    return json.dumps(action, sort_keys=True, default=str)


def diff_actions(live, candidate):
    """Multiset difference of two action lists: (live only, candidate only), each in original order."""
    def minus(a, b):
        left = Counter(_key(x) for x in b)
        out = []
        for x in a:
            k = _key(x)
            if left[k] > 0:
                left[k] -= 1
            else:
                out.append(x)
        return out
    return minus(live, candidate), minus(candidate, live)


def shadow_compare(candidate, args, kwargs, live):
    """Run the candidate on deep copies of the live decide() arguments. It never reaches execute().
    Returns {"live_only", "candidate_only", "error"}."""
    try:
        got = candidate(*copy.deepcopy(args), **copy.deepcopy(kwargs))
        only_live, only_cand = diff_actions(live, list(got))
        return {"live_only": only_live, "candidate_only": only_cand, "error": None}
    except Exception as e:
        return {"live_only": [], "candidate_only": [], "error": f"{type(e).__name__}: {e}"}


def format_shadow(project, spec, result):
    """One Markdown section per project, for the Actions step summary."""
    lines = [f"### Shadow: {project} (candidate `{spec}`)"]
    if result["error"]:
        lines.append(f"candidate error: {result['error']}")
    elif not result["live_only"] and not result["candidate_only"]:
        lines.append("no differences")
    else:
        lines += [f"- live only: `{_key(x)}`" for x in result["live_only"]]
        lines += [f"+ candidate only: `{_key(x)}`" for x in result["candidate_only"]]
    return "\n".join(lines)


def record_shadow(event):
    """Append one shadow diff line to shadow/<day>.jsonl on the ledger (skipped when no ledger is checked out)."""
    path = os.environ.get("ORGROVE_LEDGER_DIR")
    if not path:
        return False
    from orgrove.ledger import GitLedger
    GitLedger(path).transact(lambda _ledger: (event["day"], event, True), subdir="shadow")
    return True


def claim(roster_rows, actions):
    """After one project's decisions, mark roles that were just given a run as working, so the
    next project in the same tick can't hand the same agent a second job."""
    taken = {a["role"] for a in actions if a["kind"] == "run"}
    return [r | {"status": "working", "break_since": None} if r["role"] in taken else r for r in roster_rows]


def free_time(bcfg, roles, roster_rows, now, events, budget_left, profiles, dry_run):
    """After every project had its chance at ticket work: a free role may take one free-time run (#122).

    Only on a project whose profile names a free_time ticket and that has something to look at.
    The role's status becomes "starting" so the office and the ledger show why it walks to its desk."""
    ft = bcfg.get("free_time", {})
    for role, blocked in freetime.decide(ft, bcfg["timezone"], roles, roster_rows, now, events, budget_left).items():
        if blocked:
            print(f"free time: {role} not started ({blocked})")
            continue
        for prof in [p for p in profiles if p.get("free_time")]:
            found = freetime.targets(prof["repo"], ft["max_prs"])
            if not found:
                print(f"free time: nothing to look at in {prof['repo']}")
                continue
            issue = prof["free_time"]["issue"]
            action = {"kind": "run", "role": role, "issue": issue, "mode": "free_time",
                      "why": f"free time: {len(found)} failing Dependabot PR(s) to look at"}
            execute(prof["repo"], prof["project"], [action], dry_run, prof)
            roles[role] = {"role": role, "reason": "starting", "issues": [issue], "project": prof["project"],
                           "detail": "free time: " + ", ".join(f"#{t['number']}" for t in found), "until": None, "next": []}
            budget_left -= ft["ceiling_usd"]
            break
    return budget_left


def api_pause(allowance, reserve, now, dry_run=False):
    """Below the reserve, the tick reads and starts nothing and books the next tick for just after the allowance
    resets: agents already running, the retro and the Office's buttons keep the rest (2026-10-30, the App's GraphQL
    allowance ran out twice and ticks failed). An unreadable allowance never pauses. True when paused."""
    if not allowance or allowance[0] >= reserve:
        return False
    left, reset = allowance
    wake = max(reset, now) + timedelta(minutes=1)
    print(f"paused: {left} GraphQL points left, below the reserve of {reserve}; nothing read or started. "
          f"The allowance resets at {reset.isoformat(timespec='seconds')}; "
          f"next tick booked for {wake.isoformat(timespec='seconds')} ({WAKE_WHY['api']})")
    if not dry_run and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:   # dispatch.yml starts wake.yml with it
            f.write(f"wake_at={wake.isoformat(timespec='seconds')}\n")
    return True


def main(argv=None):
    p = argparse.ArgumentParser(prog="orgrove.dispatch")
    p.add_argument("--project", help="only this project (default: every profile, in `order`)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--shadow", metavar="module:function",
                   help="also run this candidate decide() on the same state and print the diff; it never executes "
                        "anything. The candidate is arbitrary Python run in-process with the dispatcher's token: "
                        "in-process isolation is not a sandbox, so only pass a path you trust (owner-triggered only)")
    a = p.parse_args(argv)

    bcfg = budget.load()
    paused = api_pause(office.graphql_allowance(), bcfg.get("github_api", {}).get("graphql_reserve", 0),
                       datetime.now(timezone.utc), a.dry_run)
    if paused:
        return 0
    day = datetime.now(ZoneInfo(bcfg["timezone"])).date().isoformat()
    events = office.ledger_events(day[:7])
    spent_today, spent_month = budget.spent(events, day)
    budget_left = min(bcfg["daily_usd"], bcfg["monthly_usd"] - spent_month + spent_today) - spent_today
    budget_kind = "monthly" if bcfg["monthly_usd"] - spent_month < bcfg["daily_usd"] - spent_today else "daily"
    per_project, waiting_on_owner, acted, deploy_wait = [], [], False, None
    run_rows = office.runs(events)
    now = datetime.now(timezone.utc)
    refused = provider_refusal(events, now)
    roster_rows = None
    profiles = [manifest.project(a.project)] if a.project else manifest.projects()
    candidate, candidate_error = load_candidate(a.shadow) if a.shadow else (None, None)
    shadow_lines = []

    for prof in profiles:  # the product first: profiles are ordered, and each one claims the agents it uses
        dept = manifest.department(prof["departments"][0])
        cfg = stages.load(manifest.ROOT / dept["stages"])
        repo = prof["repo"]
        board = office.board_items(prof)
        skip = set(prof.get("skip_issues", []))
        cards = office.pipeline(repo, cfg, board, skip)
        open_numbers = {c["number"] for c in cards if c["stage"] != "done"} | skip
        fresh = office.roster(dept, cards, run_rows, prof["project"])
        if roster_rows is None:
            roster_rows = fresh
        else:  # a role busy in either project is busy
            busy = {r["role"] for r in fresh if r["status"] == "working"}
            roster_rows = [r | {"status": "working", "break_since": None} if r["role"] in busy else r for r in roster_rows]
        roadmap_request = None
        if prof.get("roadmap_issue"):
            labels = office.gh_json("issue", "view", str(prof["roadmap_issue"]), "-R", repo, "--json", "labels")["labels"]
            if any(label["name"] == "needs/roadmap" for label in labels):
                roadmap_request = prof["roadmap_issue"]
        merged = merged_issues(repo, cards)
        env = (prof.get("environments") or {}).get("dev")
        deploys = deploy_states(repo, env["deployed_by"], merged, now, known_failing=env.get("known_failing", ())) \
            if env and merged else {}
        due = deploy_at(deploys, now)
        deploy_wait = min(x for x in (deploy_wait, due) if x) if (deploy_wait or due) else None
        decide_args = (
            dept, cfg, cards, roster_rows, review_state(repo, cards, prof.get("owners", [])), merged,
            [c for c in cards if c["stage"] == "backlog"], now, 0.0, budget_left,
        )
        decide_kw = dict(
            focus=prof.get("milestone_focus"), roadmap_request=roadmap_request,
            has_env=bool(prof.get("environments")),
            answered=answered_issues(repo, cards, prof.get("owners", [])),
            budget_kind=budget_kind,
            sanity_passed=sanity_passes(repo, cards), refused=refused, deploys=deploys,
            awaiting_ci=awaiting_pr_ci(repo, cards, prof.get("owners", [])), deps=dependency_state(repo, cards), project=prof["project"],
        )
        explained = []
        live = decide(*decide_args, explain=explained, **decide_kw)
        actions = board_sync(cfg, cards, board, open_numbers, prof.get("status_map")) + live
        if a.shadow:
            result = {"live_only": [], "candidate_only": [], "error": candidate_error} if candidate_error else \
                shadow_compare(candidate, decide_args, decide_kw | {"explain": []}, live)
            shadow_lines.append((prof["project"], result))
        per_project.append((prof["project"], explained))
        waiting_on_owner += owner_waiting(prof["project"], cards)
        budget_left -= sum(dept["roles"][x["role"]]["ceiling_usd"] for x in actions if x["kind"] == "run")
        roster_rows = claim(roster_rows, actions)
        acted = acted or any(x["kind"] in ("label", "board", "close") for x in actions)
        print(f"== {prof['project']} ({repo}): {len(actions)} action(s)")
        execute(repo, prof["project"], actions, a.dry_run, prof)

    for project, result in shadow_lines:
        print(format_shadow(project, a.shadow, result))
        if not a.dry_run:
            try:  # like record_status: a failed shadow write never blocks or undoes live work
                record_shadow({"type": "shadow", "day": day, "ts": now.isoformat(timespec="seconds"),
                               "project": project, "candidate": a.shadow, **result})
            except (subprocess.CalledProcessError, RuntimeError) as e:
                print(f"warning: shadow diff not recorded: {e}")

    roles = combine(per_project)
    budget_left = free_time(bcfg, roles, roster_rows, now, events, budget_left, profiles, a.dry_run)
    print("== why each agent is (not) working")
    for role, st in roles.items():
        print(f"{role:9} {st['reason']:15} {' '.join(f'#{n}' for n in st['issues'])} {st['detail']}")
    timer = schedule_alive(events, now) or not org.owner_checks()
    plan = wake_plan(roles, now, acted=acted, waiting=[] if timer else waiting_on_owner,
                     owner_wakes=owner_wakes(events, waiting_on_owner),
                     deploy_wait=None if not org.owner_checks() else deploy_wait)   # an install's own timer covers it
    wake, why = plan if plan else (None, None)
    if wake and not a.dry_run and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:   # dispatch.yml starts wake.yml with it
            f.write(f"wake_at={wake.isoformat(timespec='seconds')}\n")
    if wake:
        print(f"next tick booked for {wake.isoformat(timespec='seconds')} ({WAKE_WHY[why]})")
    event = {"type": "status", "day": day, "ts": now.isoformat(timespec="seconds"), "roles": roles,
             "owner_waiting": waiting_on_owner, "spent_today": spent_today, "budget_left": round(budget_left, 4),
             "budget_kind": budget_kind, "wake_at": wake.isoformat(timespec="seconds") if wake else None,
             "wake_why": why, "trigger": os.environ.get("GITHUB_EVENT_NAME")}
    if not a.dry_run:
        try:  # after the actions: a failed status write must never undo or block real work
            if record_status(event):
                print("status recorded on the ledger")
        except (subprocess.CalledProcessError, RuntimeError) as e:
            print(f"warning: status not recorded: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
