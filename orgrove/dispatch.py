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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import os

from orgrove import budget, ci, executor, freetime, gate, manifest, office, org, outbox, prreview, stages, standup, sync_labels, update_branch, verdict
from orgrove.actions import BoardStatus, CloseItem, Dispatch, OpenPR, Skip, as_dict, label_edit

PRIORITY = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
HOLD = {"needs/human", "blocked/budget", "needs/approval", "blocked/deps", "needs/delivery", "blocked/parked"}  # meeting drafts wait for the owner (#45)
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
DELIVERY_TRIES = 2   # Delivery Lead runs on one issue in a day before a hold goes straight to the owner
LOOP_RUNS = 6        # paid runs of one role on one ticket in a local day; then that role stops on it until tomorrow (L-018)
OUTBOX_REFUSED_MAX = 2   # paid runs of one role on one ticket whose results were refused, one after another; the next is not paid
REVIEW_LOOP_RUNS = 4   # reviewers: max_review_rounds (3) plus the one more try an owner can grant
# Paid reviews by one reviewer (QA, Security) on one ticket in the month's ledger; a further review goes to the
# Delivery Lead (needs/delivery), and after an owner's or the Delivery Lead's answer one more is allowed (L-019,
# owner decision 2026-11-05: $18 of $67 in five days went on third and later reviews of the same ticket).
REVIEW_PAID_MAX = 2
REVIEWERS = ("qa", "security")


def loop_cap(role):
    return REVIEW_LOOP_RUNS if role in ("qa", "security", "sanity") else LOOP_RUNS
PLAN_TRIES = 2       # PM runs on one needs/plan issue in a day before it waits in the backlog for Dev's room
HELD = re.compile(r"ORGrove dispatcher: .*→ labels \+\[[^\]]*needs/(?:human|delivery)", re.I)   # either spelling of the name (before 2026-10-29 too)
# After the AI provider refuses a run (usage limit, bad key), nobody starts for this long; then one tick tries again.
# On 2026-10-28 a workspace limit refused every run for hours while the status said "on break".
REFUSAL_PAUSE_MIN = 30
# A refusal that names when access returns ("You will regain access on 2026-12-01 at 00:00 UTC", a workspace's monthly
# limit) pauses until then, but tries once every REFUSAL_PROBE_H in case the owner raised the limit. 2026-11-05/06:
# the 30-minute pause started four refused runs every hour (55 in 16 h) for a limit that ends on the 1st.
REFUSAL_PROBE_H = 12
REGAIN = re.compile(r"regain access on (\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2}) UTC")
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
            actions.append(BoardStatus(issue=c["number"], item_id=board[c["number"]]["item_id"],
                            status=to_board(want), why=f"stage {c['stage']}"))
    for n, b in board.items():
        if n not in open_numbers and b["status"] != to_board("Done") and not any(a.issue == n for a in actions):
            actions.append(BoardStatus(issue=n, item_id=b["item_id"], status=to_board("Done"),
                            why="issue is closed"))
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
           sanity_passed=(), sanity_failed=(), outbox_stuck=None, refused=None, awaiting_ci=None, deps=None, project=None, unvetted=(), delivery_tries=None, plan_tries=None,
           use="", pm_seen=None, pm_touched=(), paid_runs=None, review_paid=None, external_prs=False, pr_reviews=()):
    """Pure decision step. Returns a list of actions; see module docstring.

    review:  issue -> {"head", "verdicts": {role: verdict}, "rounds": {role: n}, "open_subs": [..],
                       "ci": green|red|pending|none, "ci_failing": [(name, url)], "ci_bounces": n, "ci_kicks": n,
                       "head_age_min": m, "granted": bool, "since": iso time of that answer or None}
             Counts restart when an owner answers a hold, and "granted" then allows one more try.
             A missing "ci" means green (callers that predate #151).
    merged:  set of issue numbers whose agent PR has been merged
    backlog: open issues with no stage label (PM candidates), each a card dict
    focus: milestone planned first; then by target date (undated last), priority, oldest
    roadmap_request: issue number of the roadmap issue when it carries needs/roadmap
    has_env: False for projects with no deploy environment; a merged PR then closes the ticket
    answered: held (needs/human) issues whose newest comment is an owner's reply
    plan_tries: issue -> PM runs on it today. At PLAN_TRIES a needs/plan ticket in the backlog loses the label and
             waits for Dev's room (2026-11-02: the PM re-checked #316 four times in six minutes with Dev's queue full).
    pm_seen: issue -> when its last settled PM run ended (pm_last_runs). A needs/plan ticket the PM already ran on and
             that is not in pm_touched is not given to the PM again; a first plan (never run), an owner reply and the
             roadmap batch always start one, and a backlog ticket is still planned when Dev has room (#77).
    pm_touched: issues that changed for the PM since its last run (pm_changes: an owner or agent comment; the
             dispatcher's own label notes and other people's comments don't count)
    external_prs: the project's [review] external_prs switch. Off: pr_reviews is not read at all (#82)
    pr_reviews: open PRs with {"number", "author", "head", "labels", "verdicts": {role: verdict}} (prreview.reviews_needed).
             With the switch on, QA and Security are queued (mode "pr") for each PR that no [bot] opened, that has no
             skip-agent-review label and whose current head has no verdict from that role; tickets come first
    paid_runs: (issue, role) -> runs that reached the model today. At loop_cap(role) that role is not started on that
             ticket again until tomorrow, whatever the cause: a loop is stopped by its cost, not by knowing its bug
             (L-018; 2026-11-03: QA and Security approved #667's head 61 times, $7.52, because of L-017).
    review_paid: (issue, reviewer) -> settle times of its paid runs this month (review_paid()). A reviewer with
             REVIEW_PAID_MAX of them since the last answered hold (1 after an answer) is not started again: the
             ticket goes to the Delivery Lead, whatever the verdicts say (L-019).
    use: the project's spend line (standup.use_line) for the note on a stalled ticket (orgrove.standup)
    delivery_tries: issue -> Delivery Lead runs on it today. At DELIVERY_TRIES a hold goes straight to the owner, so
             the Delivery Lead and a rule that keeps re-raising the hold can't loop.
    unvetted: needs/human issues with no Delivery Lead question since the owner last spoke: they go to the Delivery
             Lead first (needs/delivery), whoever added the label (2026-11-02, owner decision)
    explain: optional list; gets one status per roster role saying what it does or why it is idle (#98)
    budget_kind: which cap `daily_cap` really is today ("daily" or "monthly"), for the explanation
    sanity_passed: deployed-dev issues Sanity has passed since they were deployed, but that are still open
    sanity_failed: deployed-dev issues whose newest Sanity verdict since the deploy is a fail: back to Dev
    outbox_stuck: (issue, role) -> the reasons its last OUTBOX_REFUSED_MAX results were refused (outbox_refusals). Not started
             again; the ticket goes once to the Delivery Lead, in plain words (L-021)
    refused: the AI provider's refusal of the latest run, while it is recent; nobody is started
    awaiting_ci: awaiting-review issues with an open agent PR -> {"head", "ci", "ci_failing", "ci_bounces", "url"}.
             main moves after the reviews, so a PR can turn red while it waits for the owner; it goes back to Dev.
    deps:    blocked/deps issues -> the issue numbers they wait for that haven't landed yet (empty = release)
    project: the project these cards belong to. A role acts only in its `projects` (#112), and work is picked by
             the role's skill, so two roles with one skill (the two PMs) each serve their own project.
    """
    actions = []
    tries = delivery_tries or {}

    def hold(n):
        return "needs/human" if tries.get(n, 0) >= DELIVERY_TRIES else "needs/delivery"

    for c in cards:
        if "needs/delivery" in c["flags"] and tries.get(c["number"], 0) >= DELIVERY_TRIES and c["stage"] != "done":
            actions.append(label_edit(issue=c["number"], remove=["needs/delivery"], add=["needs/human"],
                            why=f"the Delivery Lead has looked at it {tries[c['number']]} times today without settling it",
                            note="Its notes are in the comments above."))
            c["flags"] = [f for f in c["flags"] if f != "needs/delivery"] + ["needs/human"]
    for c in cards:
        if ("needs/plan" in c["flags"] and c["stage"] == "backlog" and c["number"] not in answered
                and (plan_tries or {}).get(c["number"], 0) >= PLAN_TRIES):
            actions.append(label_edit(issue=c["number"], remove=["needs/plan"], add=[],
                            why=f"the PM has looked at it {plan_tries[c['number']]} times today",
                            note="It stays in the backlog; the dispatcher asks the PM again when Dev has room."))
            c["flags"] = [f for f in c["flags"] if f != "needs/plan"]
    # GitHub closes a ticket when its PR says "Closes #n", and people close tickets too: whatever closed it, a
    # closed ticket's stage label becomes stage/done, quietly (no comment on a closed ticket).
    live = {s.get("label", f"stage/{s['id']}") for s in cfg["stage"]}   # replay fixtures keep only the id
    for c in cards:
        if c["stage"] == "done" and (stale := sorted(live & set(c["labels"]))):
            actions.append(label_edit(issue=c["number"], remove=stale, add=[stages.DONE_LABEL],
                            why="closed", quiet=True))
    for c in cards:
        if "blocked/deps" in c["flags"] and (deps or {}).get(c["number"]) == []:
            actions.append(label_edit(issue=c["number"], remove=["blocked/deps"], add=[],
                            why="every ticket it waits for has landed",
                            note=f"Released: the tickets this one builds on are merged. It continues from stage {c['stage']}."))
            c["flags"] = [f for f in c["flags"] if f != "blocked/deps"]
    for c in cards:
        if c["number"] in answered and "needs/human" in c["flags"]:
            back_to_pm = c["stage"] == "backlog"
            actions.append(label_edit(issue=c["number"], remove=["needs/human"],
                            add=["needs/plan"] if back_to_pm and "needs/plan" not in c["flags"] else [],
                            why="the owner replied; " + ("back to the PM to re-plan with the answer" if back_to_pm
                                                            else f"back to stage {c['stage']}")))
            c["flags"] = [f for f in c["flags"] if f != "needs/human"] + (["needs/plan"] if back_to_pm else [])
    for c in cards:
        if (c["number"] in unvetted and c["number"] not in answered and "needs/human" in c["flags"] and c["stage"] != "done"
                and hold(c["number"]) == "needs/delivery"):
            actions.append(label_edit(issue=c["number"], remove=["needs/human"], add=["needs/delivery"],
                            why="a question for the owner goes to the Delivery Lead first, who settles it with the "
                                   "agents or asks the owner plainly"))
            c["flags"] = [f for f in c["flags"] if f != "needs/human"] + ["needs/delivery"]
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
                    actions.append(label_edit(issue=n, remove=["stage/awaiting-review"],
                                    add=["stage/deployed-dev"], why=f"PR merged by a human and deployed ({d['url']})"))
                    c["stage"] = "deployed-dev"
                elif d["state"] == "failure" and d.get("failures", 0) >= 1:
                    actions.append(label_edit(issue=n, remove=[], add=[hold(n)],
                                    why=f"the deploy failed again after DevOps's fix ({d['url']})"))
                    c["stage"] = "handled"
                elif d["state"] == "failure":
                    actions.append(label_edit(issue=n, remove=["stage/awaiting-review"],
                                    add=["stage/in-dev", "needs/fix", "area/infra"],
                                    why="the deploy of the merge failed",
                                    note=f"Deploy run: {d['url']}\n\nDevOps: follow **On `deploy:failed`** in your skill: "
                                            "read the failed job's log, classify the cause, and fix it in code on a new "
                                            f"agent branch if it is a code cause; otherwise `needs/delivery`.\n<!-- {DEPLOY_FAILED} -->"))
                    c["stage"] = "handled"
                elif d["state"] == "none" and d["age_min"] >= NO_DEPLOY_MIN:
                    actions.append(CloseItem(issue=n,
                                    why=f"PR merged by a human; the deploy workflow didn't run for this change in "
                                           f"{NO_DEPLOY_MIN} min (its path filters leave it out), so there is nothing "
                                           "for Sanity to check"))
                    c["stage"] = "handled"
                else:
                    c["stage"] = "handled"   # the deploy is running or about to: the next tick looks again
            else:
                actions.append(CloseItem(issue=n,
                                why="PR merged by a human; this project has no deploy environment to check"))
                c["stage"] = "handled"
        elif c["stage"] == "awaiting-review" and (awaiting_ci or {}).get(n, {}).get("ci") == "red":
            # The PR was green when the gate opened it, but main has moved since: the owner can't merge a red PR,
            # so it isn't theirs to look at (2026-10-30: #31's PR sat red in "waiting on you").
            a = awaiting_ci[n]
            if a.get("ci_bounces", 0) >= max_rounds:
                actions.append(label_edit(issue=n, remove=[], add=[hold(n)],
                                why=f"its PR is red again after {a['ci_bounces']} CI round(s)"))
            else:
                checks = "\n".join(f"- {name}: {url}" for name, url in a.get("ci_failing", [])) or "- (no names)"
                actions.append(label_edit(issue=n, remove=["stage/awaiting-review"],
                                add=["stage/in-dev", "needs/fix"], why="its open PR went red after the reviews",
                                note=f"PR {a.get('url', '')} is red on `{a['head']}` (CI runs on the PR merged with "
                                        f"current main):\n{checks}\n\nDev: merge `origin/main` into the branch, fix the "
                                        "cause (not the test), run `commands.contract`, push. The PR updates itself."
                                        f"\n<!-- {CI_RED} head={a['head']} -->"))
            c["stage"] = "handled"
        elif c["stage"] == "deployed-dev" and n in sanity_passed:
            # Sanity passed but left the ticket open, as it should for a long-running ticket that ships in chunks.
            # Left at deployed-dev, Sanity re-checked the same build every break (#43: 6 rounds on 2026-10-28).
            actions.append(label_edit(issue=n, remove=["stage/deployed-dev"], add=["needs/plan"],
                            why="Sanity passed the deployed build and the ticket is still open",
                            note="PM: plan the next chunk, or close the ticket if nothing is left."))
            c["stage"] = "handled"
        elif c["stage"] == "deployed-dev" and n in sanity_failed:
            # Sanity is a reviewer: it judges and raises the linked bug, the dispatcher moves the ticket (L-021).
            actions.append(label_edit(issue=n, remove=["stage/deployed-dev"], add=["stage/in-dev", "needs/fix"],
                            why="Sanity failed the deployed build",
                            note="Dev: Sanity's verdict and its linked bug say what failed on the deployed build. "
                                 "Fix it on the ticket's branch, close the linked sub-issue, and send it back to review."))
            c["stage"] = "handled"
        elif c["stage"] == "agent-review" and n in review:
            r = review[n]
            state = r.get("ci", "green")
            limit = 1 if r.get("granted") else max_rounds   # the owner answered a hold: one more try
            if state == "red":
                # Nobody reviews a head CI has already failed: it goes straight back to Dev (#151).
                if r.get("ci_bounces", 0) >= limit:
                    actions.append(label_edit(issue=n, remove=[], add=[hold(n)],
                                    why=f"CI still red after {r.get('ci_bounces', 0)} round(s)"))
                else:
                    checks = "\n".join(f"- {name}: {url}" for name, url in r.get("ci_failing", [])) or "- (no names)"
                    actions.append(label_edit(issue=n, remove=["stage/agent-review"],
                                    add=["stage/in-dev", "needs/fix"], why="CI is red on the head; no review until it is green",
                                    note=f"Failing checks on `{r['head']}`:\n{checks}\n\nDev: read the failing job log, fix the cause "
                                            f"(not the test), run `commands.contract`, push.\n<!-- {CI_RED} head={r['head']} -->"))
                c["stage"] = "handled"
                continue
            if state == "none" and r.get("head_age_min", 0) >= NO_CI_MIN:
                # Usually a branch cut before Agent CI existed: a push runs the workflows IN the pushed commit,
                # so it has none to run (#599, 2026-10-28). Dev brings main in once; only then is it the owner's.
                if r.get("ci_kicks", 0) >= 1:
                    actions.append(label_edit(issue=n, remove=[], add=[hold(n)],
                                    why=f"still no CI result {NO_CI_MIN} min after Dev brought main into the branch"))
                else:
                    actions.append(label_edit(issue=n, remove=["stage/agent-review"],
                                    add=["stage/in-dev", "needs/fix"],
                                    why=f"no CI result on the head after {NO_CI_MIN} min; reviews wait for CI",
                                    note=f"`{r['head']}` has no CI run. A push runs the workflows in the pushed commit, so "
                                            "a branch cut before Agent CI existed has none. Dev: merge `origin/main` into "
                                            "the branch (a merge commit, no other change), run `commands.contract`, push."
                                            f"\n<!-- {CI_NONE} head={r['head']} -->"))
                c["stage"] = "handled"
                continue
            changes = [role for role, v in r["verdicts"].items() if v["decision"] == "changes"]
            if changes:
                if any(r["rounds"].get(role, 0) >= limit for role in changes):
                    actions.append(label_edit(issue=n, remove=[], add=[hold(n)],
                                    why=f"still failing review after {max(r['rounds'].get(x, 0) for x in changes)} rounds"))
                else:
                    actions.append(label_edit(issue=n, remove=["stage/agent-review"],
                                    add=["stage/in-dev", "needs/fix"], why=f"{', '.join(changes)} asked for changes"))
                c["stage"] = "handled"
            elif over := reviews_over(n, r, review_paid):
                actions.append(label_edit(issue=n, remove=[], add=[hold(n)],
                                why="; ".join(f"{x} has reviewed it {k} times (paid)" for x, k in over)
                                    + " and would be asked again",
                                note=f"A review costs money each time, so after {REVIEW_PAID_MAX} paid reviews by one "
                                     "reviewer the next is a decision, not a reflex. Delivery Lead: read the verdicts "
                                     "above and either settle it (one more review is then allowed) or ask the owner."))
                c["stage"] = "handled"
            elif not gate.readiness(r["verdicts"], r["open_subs"], state):
                actions.append(OpenPR(issue=n, why="QA and Security approved the head"))
                c["stage"] = "handled"

    # Daily standup (2026-11-03): a ticket that holds a slot and hasn't moved goes to the Delivery Lead, so a
    # deadlock is found the same day, not by the owner. Not while the day's money is gone: nobody could act on it.
    working_on = {o["number"] for row in roster_rows if row["status"] == "working" for o in row.get("on", [])}
    delivery_ceiling = dept["roles"].get("delivery", {}).get("ceiling_usd", 0)
    if daily_cap - spent_today >= delivery_ceiling:
        for c, why, age in standup.stalled([c for c in active if c["stage"] != "handled"], review, working_on, now):
            actions.append(label_edit(issue=c["number"], remove=[], add=[hold(c["number"])],
                            why=f"no change for {age:.0f} h while it holds a slot",
                            note=standup.note(why, age, use)))
            c["flags"] = c["flags"] + [hold(c["number"])]
            c["stage"] = "handled"

    def pm_unchanged(c):
        """True when the PM already ran on this issue and nothing meant for it has happened since."""
        n = c["number"]
        return bool((pm_seen or {}).get(n)) and n not in answered and n not in pm_touched

    def external(role):
        """Open PRs this reviewer has not seen at their current head (#82)."""
        if not external_prs:
            return []
        return [{"number": p["number"], "flags": [], "mode": "pr"} for p in pr_reviews
                if not prreview.is_bot(p["author"]) and prreview.SKIP_LABEL not in p["labels"]
                and role not in p["verdicts"]]

    def waiting(role):
        if role in ("qa", "security"):
            return [c for c in active if c["stage"] == "agent-review" and c["number"] in review
                    and role not in review[c["number"]]["verdicts"] and review[c["number"]].get("ci", "green") == "green"] \
                + external(role)
        if role in ("dev", "devops"):
            mine = [c for c in active if infra(c) == (role == "devops")]
            fixes = [c for c in mine if c["stage"] == "in-dev" and "needs/fix" in c["flags"]]
            in_flight = [c for c in mine if c["stage"] in ("in-dev", "agent-review")]
            wip = dept["roles"][role].get("wip", 1)
            fresh = [c for c in mine if c["stage"] == "planned"] if len(in_flight) < wip else []
            return order(fixes) + order(fresh)
        if role == "sanity":
            return [c for c in active if c["stage"] == "deployed-dev"]
        if role == "delivery":
            return order([c for c in cards if c["stage"] != "done" and "needs/delivery" in c["flags"]])
        if role == "pm":
            asked = [c for c in active if "needs/plan" in c["flags"] and not pm_unchanged(c)]
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
        held = [c for c in mine if HOLD & set(c["flags"]) and "blocked/parked" not in c["flags"]]   # parked: nobody's wait
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
            kind = "budget_daily" if flags == ["blocked/budget"] else \
                "waiting_agent" if set(flags) <= {"blocked/deps", "needs/delivery"} else "waiting_owner"
            return kind, [c["number"] for c in held], f"{len(held)} ticket(s) held: {', '.join(flags)}"
        return "nothing_to_do", [], "nothing in this role's stages"

    queue = {}

    def tell(role, reason, issues=(), detail="", until=None):
        if explain is not None:
            explain.append({"role": role, "reason": reason, "issues": list(issues), "detail": detail, "until": until,
                            "next": [c["number"] for c in queue.get(role, [])[:3]]})

    stuck_cards = {c["number"]: c for c in cards}
    for (n, skill), reasons in sorted((outbox_stuck or {}).items()):
        c = stuck_cards.get(n)
        if c and not {"needs/delivery", "needs/human"} & set(c["flags"]) and c["stage"] not in ("handled", "done"):
            actions.append(label_edit(issue=n, remove=[], add=["needs/delivery"],
                            why=f"{skill} sent {len(reasons)} results in a row that were refused, so nothing it did reached the ticket",
                            note=f"Stopped: the last {len(reasons)} runs of {skill} on this ticket were paid for, but what they asked "
                                 f"for was refused, so nothing changed here. The latest reason: {reasons[-1]} A third run would "
                                 "repeat it, so it stops. Delivery Lead: if an instruction or a rule is wrong, say what must "
                                 "change; if the ticket is the problem, route it back."))
            c["flags"] = list(c["flags"]) + ["needs/delivery"]
    budget_left = daily_cap - spent_today
    for row in roster_rows:
        role = row["role"]
        if not manifest.serves(dept["roles"][role], project):
            continue
        skill = dept["roles"][role].get("skill", role)   # fixtures recorded before #112 carry no skill
        queue[role] = order(waiting(skill)) if skill not in ("dev", "devops", "pm") else waiting(skill)
        looped = [c for c in queue[role] if (paid_runs or {}).get((c["number"], skill), 0) >= loop_cap(skill)]
        stuck = [c for c in queue[role] if c not in looped and (c["number"], skill) in (outbox_stuck or {})]
        queue[role] = [c for c in queue[role] if c not in looped and c not in stuck]
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
        if not todo and stuck:
            tell(role, "outbox_refused", [c["number"] for c in stuck],
                 "; ".join(f"#{c['number']}: {outbox_stuck[(c['number'], skill)][-1]}" for c in stuck)
                 + f". Not started again: its last {OUTBOX_REFUSED_MAX} results were refused, so nothing changed")
            continue
        if not todo and looped:
            tell(role, "loop_stopped", [c["number"] for c in looped],
                 "; ".join(f"#{c['number']}: {paid_runs[(c['number'], skill)]} paid runs today" for c in looped)
                 + f". Stopped on it until tomorrow: {loop_cap(skill)} runs in a day means it is going round in a circle")
            continue
        if not todo:
            tell(role, *why_empty(skill))
            continue
        ceiling = dept["roles"][role]["ceiling_usd"]
        if ceiling > budget_left:
            actions.append(Skip(role=role, issue=todo[0]["number"], why="not enough budget left today"))
            tell(role, f"budget_{budget_kind}", [todo[0]["number"]],
                 f"needs ${ceiling:.2f}, ${max(budget_left, 0):.2f} left under the {budget_kind} cap")
            continue
        budget_left -= ceiling
        pick = todo[0]
        tell(role, "starting", [pick["number"]], "picked up this tick")
        if skill == "pm" and not {"needs/plan", "needs/roadmap"} & set(pick["flags"]):
            actions.append(label_edit(issue=pick["number"], remove=[], add=["needs/plan"],
                            why="next in the plan; the planned queue has room"))
        actions.append(Dispatch(role=role, issue=pick["number"], mode=pick.get("mode"),
                        why=f"{role} on coffee break >= {row['break_min']} min" if row["break_min"] else f"{role} is free"))
    return actions


def reviews_over(n, r, review_paid):
    """[(reviewer, paid reviews)] for each reviewer still to give a verdict on this head that has used its paid
    reviews of ticket n: REVIEW_PAID_MAX, or 1 since an answered hold (r["since"])."""
    cap = 1 if r.get("granted") else REVIEW_PAID_MAX
    since = _at(r["since"]) if r.get("since") else None
    out = []
    for x in REVIEWERS:
        if x in r["verdicts"]:
            continue
        k = sum(1 for t in (review_paid or {}).get((n, x), ()) if since is None or _at(t) > since)
        if k >= cap:
            out.append((x, k))
    return out


# ---------- gathering state (GitHub, via gh) ----------

def review_counts(comments, owners):
    """Review rounds per role, CI bounces and no-CI kicks from an issue's comments. An owner's reply to one of the
    dispatcher's holds restarts the counts and grants one more try: otherwise "one more round" bounced straight
    back to the owner on the next tick, because the old counts still read as over the limit (#23, 2026-10-28)."""
    owners = {o.lower() for o in owners}
    rounds, bounces, kicks, held, granted, since = {}, 0, 0, False, False, None
    for cm in comments:
        body = cm.get("body") or ""
        login = cm["user"]["login"]
        if login.lower() in owners or (login == org.BOT_LOGIN and outbox.DELIVERY_ROUTE in body):
            # The owner's reply, or the Delivery Lead settling it without the owner, answers the hold.
            if held:
                rounds, bounces, kicks, held, granted = {}, 0, 0, False, True
                since = cm.get("created_at")
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
    return {"rounds": rounds, "ci_bounces": bounces, "ci_kicks": kicks, "granted": granted, "since": since}


def own_tickets(cards):
    """The cards that are tickets. A sub-issue is a finding of its parent's fix round, not work of its own: handed to the
    PM and Dev as a ticket it was planned and "worked" separately, cost two runs, and was never closed, so the parent's
    pull request waited for it (2026-11-10)."""
    return [c for c in cards if not c.get("parent")]


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
        comments = office.issue_comments(repo, n)
        subs = office.gh_json("api", f"repos/{repo}/issues/{n}/sub_issues")
        open_subs = [s["number"] for s in subs if s["state"] == "open"]
        resolved = {s for s, _ in gate.resolved_sub_issues(comments, head, open_subs)}   # the gate closes these when it opens the PR
        out[n] = {"head": head, "verdicts": gate.latest_verdicts(comments, head), **review_counts(comments, owners),
                  "open_subs": [s for s in open_subs if s not in resolved],
                  "ci": ci.ci_state(checks), "ci_failing": ci.failing(checks),
                  "head_age_min": (datetime.now(timezone.utc) - pushed).total_seconds() / 60}
    return out


def external_pr_reviews(prof):
    """Open PRs for QA and Security to review, or [] with the switch off: then the tick makes no pull-request read at
    all. A read that fails is reported as unchecked and queues nothing, never "no PRs" or "no verdict yet" (#82, L-012)."""
    if not (prof.get("review") or {}).get("external_prs"):
        return []
    try:
        return prreview.reviews_needed(prof["repo"])
    except (subprocess.CalledProcessError, RuntimeError, KeyError, TypeError, ValueError) as e:
        print(f"warning: pull-request review UNCHECKED for {prof['repo']}, none queued: {e}")
        return []


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
            comments = office.issue_comments(repo, n)
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
    return _sanity_with(repo, cards, "pass")


def sanity_fails(repo, cards):
    """deployed-dev issues whose newest Sanity verdict since the deploy is a fail."""
    return _sanity_with(repo, cards, "fail")


def _sanity_with(repo, cards, decision):
    out = set()
    for c in cards:
        if c["stage"] != "deployed-dev":
            continue
        n = c["number"]
        # Every page: a long-running ticket has hundreds of events (#43), and the newest are on the last page.
        pages = office.gh_json("api", "--paginate", "--slurp", f"repos/{repo}/issues/{n}/events?per_page=100")
        since = max((e["created_at"] for page in pages for e in page if e.get("event") == "labeled"
                     and (e.get("label") or {}).get("name") == "stage/deployed-dev"), default="")
        comments = office.issue_comments(repo, n)
        if sanity_decision(comments, since) == decision:
            out.add(n)
    return out


def sanity_passed(comments, since=""):
    """True when the bot's newest Sanity verdict after `since` (ISO time) is a pass."""
    return sanity_decision(comments, since) == "pass"


def sanity_decision(comments, since=""):
    """The decision of the bot's newest Sanity verdict after `since` (ISO time), or None."""
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
    return last


def provider_refusal(events, now, pause_min=REFUSAL_PAUSE_MIN):
    """The AI provider's message if the latest settled run was refused and its pause has not ended, else None.

    The pause is pause_min minutes; when the refusal says when access returns (REGAIN), it lasts until then, but
    never more than REFUSAL_PROBE_H hours at a time, so a limit the owner raises early is found within half a day."""
    settled = [e for e in events if e["type"] == "cost"]
    if not settled:
        return None
    last = max(settled, key=lambda e: e["ts"])
    if not last.get("error"):
        return None
    at = datetime.fromisoformat(last["ts"])
    m = REGAIN.search(last["error"])
    regain = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:00+00:00") if m else None
    until = min(regain, at + timedelta(hours=REFUSAL_PROBE_H)) if regain and regain > at else at + timedelta(minutes=pause_min)
    if now >= until:
        return None
    age = int((now - at).total_seconds() // 60)
    left = until - now
    nxt = f"{int(left.total_seconds() // 60) + 1} min" if left < timedelta(hours=1) else f"{left.total_seconds() / 3600:.0f} h"
    what = (f" Access returns {regain:%Y-%m-%d %H:%M} UTC; one try every {REFUSAL_PROBE_H} h until then. Raised the limit?"
            f" Try now: gh workflow run dispatch.yml -R {org.REPO} -f probe=true") if regain and regain > at else ""
    return (f"the AI provider refused the last run ({last['role']} on {last['issue']}, {age} min ago): "
            f"{last['error']}{what} Next try in {nxt}.")


def current_refusal(events, now):
    """provider_refusal, except on a tick started by hand with probe=true: that one tries the provider even inside a
    pause, for an owner who has just raised the limit. If the provider still refuses, the pause carries on."""
    if os.environ.get("ORGROVE_PROBE") == "true":
        return None
    return provider_refusal(events, now)


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


def owner_holds(repo, cards, owners):
    """(answered, unvetted, later) for the needs/human issues, from one comments read each.
    answered: the newest comment is an owner's, i.e. the owner has replied.
    unvetted: no Delivery Lead question (outbox.is_owner_question: the marker and every section, written by the bot)
              since an owner last commented.
    later: asked, and the question is marked no rush (outbox.is_later): not counted as waiting on the owner today."""
    answered, unvetted, later = set(), set(), set()
    owner_set = {o.lower() for o in owners}
    for c in cards:
        if "needs/human" not in c["flags"] or c["stage"] == "done":
            continue
        comments = office.issue_comments(repo, c["number"])
        if comments and comments[-1]["user"]["login"].lower() in owner_set:
            answered.add(c["number"])
            continue
        asked = owner_spoke = -1
        for i, x in enumerate(comments):
            if x["user"]["login"].lower() in owner_set:
                owner_spoke = i
            elif x["user"]["login"] == org.BOT_LOGIN and outbox.is_owner_question(x.get("body") or ""):
                asked = i
        if asked < owner_spoke or asked == -1:
            unvetted.add(c["number"])
        elif outbox.is_later(comments[asked]["body"]):
            later.add(c["number"])
    return answered, unvetted, later


def delivery_tries(events, day, repo):
    """issue -> Delivery Lead runs reserved on it this local day, from the ledger (issue numbers are per repo)."""
    return role_tries(events, day, repo, "delivery")


def role_tries(events, day, repo, role):
    """issue -> runs of `role` (as role-run names it: both PMs are `pm`) reserved on it this local day."""
    out = {}
    for e in events:
        if e["type"] == "reserve" and e.get("role") == role and e.get("day") == day:
            r, _, num = (e.get("issue") or "").partition("#")
            if r == repo and num.isdigit():
                out[int(num)] = out.get(int(num), 0) + 1
    return out


def pm_last_runs(events, repo):
    """issue -> when the last settled PM run on it ended. Runs still going (no cost yet) don't count."""
    reserved = {e["run_id"]: e["issue"] for e in events if e["type"] == "reserve" and e.get("role") == "pm"}
    out = {}
    for e in events:
        if e["type"] != "cost" or e["run_id"] not in reserved or not e.get("ts"):
            continue
        r, _, num = (reserved[e["run_id"]] or "").partition("#")
        if r == repo and num.isdigit() and (int(num) not in out or _at(e["ts"]) > _at(out[int(num)])):
            out[int(num)] = e["ts"]
    return out


def _pages(path):
    """Every page of a REST list (gh --paginate --slurp), flattened. Raises if gh fails."""
    return [x for page in office.gh_json("api", "--paginate", "--slurp", path) or [] for x in page]


def _mine(login, owner_set):
    """An owner or the ORGrove bot, whether GitHub writes its login with or without the `[bot]` suffix."""
    login = (login or "").lower()
    return login in owner_set or login.removesuffix("[bot]") == org.BOT_LOGIN.lower().removesuffix("[bot]")


def pm_changes(repo, cards, pm_seen, owners):
    """Issues with a needs/plan ask that changed for the PM since its last run ended (#77, #329, #336).
    A change is, after the run: an owner's or an agent's comment (the dispatcher's own notes don't count), a body
    edit by an owner or the bot, or a label change by an owner or the bot other than the dispatcher's own needs/plan.
    Other people's comments, edits and labels can neither start nor stop a PM run. The card's updatedAt filters
    first, so only a ticket something touched costs reads: all comment pages and all event pages (REST, not the
    GraphQL allowance) and one GraphQL field, `userContentEdits`, 1 point measured live (L-007).
    A card with no update time, or a thread that can't be read, counts as changed: the PM runs."""
    owner_set = {o.lower() for o in owners}
    owner, _, name = repo.partition("/")
    out = set()
    for c in cards:
        n, seen = c["number"], (pm_seen or {}).get(c["number"])
        if "needs/plan" not in c["flags"] or c["stage"] == "done" or not seen:
            continue
        if not c.get("updated"):
            out.add(n)
            continue
        if _at(c["updated"]) <= _at(seen):
            continue
        try:
            if _changed_since(repo, owner, name, n, _at(seen), owner_set):
                out.add(n)
        except Exception:
            out.add(n)
    return out


def _changed_since(repo, owner, name, n, seen, owner_set):
    for x in office.issue_comments(repo, n):   # every page (L-017)
        if (_mine((x.get("user") or {}).get("login"), owner_set) and _at(x["created_at"]) > seen
                and not (x.get("body") or "").startswith("ORGrove dispatcher:")):
            return True
    for x in _pages(f"repos/{repo}/issues/{n}/events?per_page=100"):
        if x.get("event") not in ("labeled", "unlabeled") or _at(x["created_at"]) <= seen:
            continue
        label = (x.get("label") or {}).get("name")
        dispatcher_note = label == "needs/plan" and not _mine_owner(x, owner_set)
        if _mine((x.get("actor") or {}).get("login"), owner_set) and not dispatcher_note:
            return True
    q = ("query($o:String!,$n:String!,$i:Int!){repository(owner:$o,name:$n){issue(number:$i)"
         "{userContentEdits(first:5){nodes{createdAt editor{login}}}}}}")
    data = office.gh_json("api", "graphql", "-f", f"query={q}", "-f", f"o={owner}", "-f", f"n={name}", "-F", f"i={n}")
    nodes = data["data"]["repository"]["issue"]["userContentEdits"]["nodes"] or []
    return any(_mine((e.get("editor") or {}).get("login"), owner_set) and _at(e["createdAt"]) > seen for e in nodes)


def _mine_owner(event, owner_set):
    """True when an owner (not the bot) made this event."""
    return ((event.get("actor") or {}).get("login") or "").lower() in owner_set


def paid_runs(events, day, repo):
    """(issue, role) -> runs that reached the model this local day (settled above $0, no error), for LOOP_RUNS.
    A refused run costs $0 and is L-014's pause, not a loop; counting it would stop a ticket for the provider's fault."""
    out = {}
    for e in events:
        if e["type"] == "cost" and e.get("day") == day and not e.get("error") and float(e.get("usd") or 0) > 0:
            r, _, num = (e.get("issue") or "").partition("#")
            if r == repo and num.isdigit():
                k = (int(num), e.get("role"))
                out[k] = out.get(k, 0) + 1
    return out


def outbox_refusals(events, day, repo, released=None):
    """(issue, role) -> the reasons, when its last OUTBOX_REFUSED_MAX paid runs today each had their result refused by
    the apply step, one after another. A run that applied clears the count; a provider refusal is not one (it has
    its own pause). A refused result changes nothing on the ticket, so without this the dispatcher starts the same
    role again, and pays again, until the loop cap (2026-11-10: 12 runs, $1.08, before anyone looked).

    `released` is issue -> when a person or the Delivery Lead decided about it (release_times). Refusals before that
    do not count: a stop that cannot be released is a loop of its own (2026-11-10: the owner answered, the fix was
    merged, QA was never started again, and the ticket went back to the Delivery Lead seven times)."""
    streak, released = {}, released or {}
    for e in sorted((e for e in events if e["type"] == "cost" and e.get("day") == day and not e.get("error")),
                    key=lambda e: e["ts"]):
        r, _, num = (e.get("issue") or "").partition("#")
        if r != repo or not num.isdigit():
            continue
        k = (int(num), e.get("role"))
        if int(num) in released and _at(e["ts"]) <= _at(released[int(num)]):
            continue                      # settled since: this refusal is history
        if e.get("outbox_refused"):
            streak[k] = streak.get(k, []) + [e["outbox_refused"]]
        elif float(e.get("usd") or 0) > 0:
            streak[k] = []
    return {k: v for k, v in streak.items() if len(v) >= OUTBOX_REFUSED_MAX}


def stuck_now(events, day, repo, owners, comments_of=None):
    """outbox_refusals, minus what a person or the Delivery Lead has since decided about (release_times)."""
    raw = outbox_refusals(events, day, repo)
    if not raw:
        return raw
    return outbox_refusals(events, day, repo, release_times(repo, raw, owners, comments_of))


def release_times(repo, stuck, owners, comments_of=None):
    """issue -> when the newest decision was made on it: a comment by an owner, or the Delivery Lead's route. Only for
    the tickets `stuck` names (two refusals in a row), so the extra reads are rare."""
    comments_of = comments_of or office.issue_comments
    owner_set = {o.lower() for o in owners}
    out = {}
    for n in sorted({n for n, _ in stuck}):
        for c in comments_of(repo, n):
            login = ((c.get("user") or {}).get("login") or "").lower()
            body = c.get("body") or ""
            by_owner = login in owner_set
            by_delivery = f"<!-- {outbox.DELIVERY_ROUTE}" in body and login.removesuffix("[bot]") == org.BOT_LOGIN.lower().removesuffix("[bot]")
            if (by_owner or by_delivery) and (n not in out or _at(c["created_at"]) > _at(out[n])):
                out[n] = c["created_at"]
    return out


def review_paid(events, repo):
    """(issue, reviewer) -> settle times of its paid runs (above $0, no error) in these events, for REVIEW_PAID_MAX.
    Like paid_runs, a refused $0 run is the provider's fault and doesn't count."""
    out = {}
    for e in events:
        if e["type"] == "cost" and e.get("role") in REVIEWERS and not e.get("error") and float(e.get("usd") or 0) > 0:
            r, _, num = (e.get("issue") or "").partition("#")
            if r == repo and num.isdigit():
                out.setdefault((int(num), e["role"]), []).append(e["ts"])
    return out


def answered_issues(repo, cards, owners):
    """Held issues whose newest comment is from an owner, i.e. the owner has replied."""
    return owner_holds(repo, cards, owners)[0]


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


def executor_for(prof):
    """The Executor that applies one project's actions: GitHub behind every port."""
    return executor.github(prof["repo"], prof["project"], lambda role: skill_of(role, prof), lambda: board_ids(prof))


# Most informative reason first when a role is idle in several projects (#98).
REASON_RANK = ["working", "starting", "provider_refused", "on_break", "loop_stopped", "outbox_refused", "budget_monthly", "budget_daily",
               "waiting_owner", "waiting_agent", "nothing_to_do", "parked"]   # every reason decide() and tick() tell


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


def owner_waiting(project, cards, later=()):
    """What is waiting on a human right now: questions, draft approvals, PRs to merge. A no-rush question is listed
    with later=True, so a count of what is waiting today leaves it out (2026-11-02: 13 "waiting", 1 urgent)."""
    out = []
    for c in cards:
        if c["stage"] == "done":
            continue
        if "needs/human" in c["flags"]:
            out.append({"project": project, "issue": c["number"], "what": "question", "title": c["title"]}
                       | ({"later": True} if c["number"] in later else {}))
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
    return json.dumps(as_dict(action), sort_keys=True, default=str)


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
    """Run the candidate on deep copies of the live decide() arguments. It never reaches the Executor.
    Returns {"live_only", "candidate_only", "error"}."""
    try:
        got = candidate(*copy.deepcopy(args), **copy.deepcopy(kwargs))
        only_live, only_cand = diff_actions(live, list(got))
        return {"live_only": [as_dict(x) for x in only_live], "candidate_only": [as_dict(x) for x in only_cand], "error": None}
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
    taken = {a.role for a in actions if isinstance(a, Dispatch)}
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
        for prof in [p for p in profiles if p.get("free_time") and not budget.parked(bcfg, p["repo"])]:
            found = freetime.targets(prof["repo"], ft["max_prs"])
            if not found:
                print(f"free time: nothing to look at in {prof['repo']}")
                continue
            issue = prof["free_time"]["issue"]
            action = Dispatch(role=role, issue=issue, mode="free_time",
                      why=f"free time: {len(found)} failing Dependabot PR(s) to look at")
            executor_for(prof).apply([action], dry_run)
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


def expired_lines(events, now):
    """Log lines for every unsettled reservation past its expiry, whatever day it was made (#41, #257)."""
    return budget.expired_log_lines(events, now.isoformat(timespec="seconds"))


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
    yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    events_2d = events if yesterday[:7] == day[:7] else office.ledger_events(yesterday[:7]) + events   # the 1st
    spent_today, _ = budget.spent(events, day)
    for line in expired_lines(events, datetime.now(timezone.utc)):
        print(line)   # a crashed run's reservation, still counted at its ceiling (#41)
    budget_left, budget_kind = budget.left(bcfg, events, day)
    per_project, waiting_on_owner, acted, deploy_wait = [], [], False, None
    run_rows = office.runs(events)
    now = datetime.now(timezone.utc)
    refused = current_refusal(events, now)
    roster_rows = None
    profiles = [manifest.project(a.project)] if a.project else manifest.projects()
    candidate, candidate_error = load_candidate(a.shadow) if a.shadow else (None, None)
    shadow_lines = []

    for prof in profiles:  # the product first: profiles are ordered, and each one claims the agents it uses
        dept = manifest.department(prof["departments"][0])
        cfg = stages.load(manifest.ROOT / dept["stages"])
        repo = prof["repo"]
        if budget.parked(bcfg, repo):   # the owner works on it in a session: no board reads, holds or runs here
            print(f"== {prof['project']} ({repo}): parked ([project_daily] share 0), nothing started")
            per_project.append((prof["project"], [
                {"role": r, "reason": "parked", "issues": [], "until": None, "next": [],
                 "detail": f"{prof['project']} is parked: its tickets are worked in the owner's session"}
                for r in dept["roles"] if manifest.serves(dept["roles"][r], prof["project"])]))
            continue
        board = office.board_items(prof)
        skip = set(prof.get("skip_issues", []))
        cards = own_tickets(office.pipeline(repo, cfg, board, skip))
        open_numbers = {c["number"] for c in cards if c["stage"] != "done"} | skip
        fresh = office.roster(dept, cards, run_rows, prof["project"], now)
        unheld = [label_edit(issue=n, remove=[f"agent/{skill}"], add=[], quiet=True,
                             why=f"agent/{skill} with no {skill} run on it: the run ended without removing it")
                  for n, skill in office.stale_labels(dept, cards, run_rows, prof["project"], now)]
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
        share_left, share_kind = budget.left(bcfg, events, day, repo)   # this project's [project_daily] share
        project_left, project_kind = (share_left, share_kind) if share_left < budget_left else (budget_left, budget_kind)
        decide_args = (
            dept, cfg, cards, roster_rows, review_state(repo, cards, prof.get("owners", [])), merged,
            [c for c in cards if c["stage"] == "backlog"], now, 0.0, project_left,
        )
        holds = owner_holds(repo, cards, prof.get("owners", []))
        pm_seen = pm_last_runs(events, repo)
        decide_kw = dict(
            focus=prof.get("milestone_focus"), roadmap_request=roadmap_request,
            has_env=bool(prof.get("environments")),
            answered=holds[0], unvetted=holds[1], delivery_tries=delivery_tries(events, day, repo),
            plan_tries=role_tries(events, day, repo, "pm"), paid_runs=paid_runs(events, day, repo),
            review_paid=review_paid(events, repo),
            pm_seen=pm_seen, pm_touched=pm_changes(repo, cards, pm_seen, prof.get("owners", [])),
            budget_kind=project_kind, external_prs=bool((prof.get("review") or {}).get("external_prs")),
            pr_reviews=external_pr_reviews(prof),
            sanity_passed=sanity_passes(repo, cards), sanity_failed=sanity_fails(repo, cards),
            outbox_stuck=stuck_now(events, day, repo, prof.get("owners", [])), refused=refused, deploys=deploys,
            awaiting_ci=awaiting_pr_ci(repo, cards, prof.get("owners", [])), deps=dependency_state(repo, cards), project=prof["project"],
            use=standup.use_line(budget.spent_project(events_2d, yesterday, repo), budget.spent_project(events, day, repo),
                                 bcfg.get("project_daily", {}).get(repo)),
        )
        explained = []
        live = decide(*decide_args, explain=explained, **decide_kw)
        actions = unheld + board_sync(cfg, cards, board, open_numbers, prof.get("status_map")) + live
        if a.shadow:
            result = {"live_only": [], "candidate_only": [], "error": candidate_error} if candidate_error else \
                shadow_compare(candidate, decide_args, decide_kw | {"explain": []}, live)
            shadow_lines.append((prof["project"], result))
        per_project.append((prof["project"], explained))
        waiting_on_owner += owner_waiting(prof["project"], cards, holds[2])
        budget_left -= sum(dept["roles"][x.role]["ceiling_usd"] for x in actions if isinstance(x, Dispatch))
        roster_rows = claim(roster_rows, actions)
        acted = acted or any(not isinstance(x, (Dispatch, OpenPR, Skip)) for x in actions)
        print(f"== {prof['project']} ({repo}): {len(actions)} action(s)")
        executor_for(prof).apply(actions, a.dry_run)
        update_token = os.environ.get(update_branch.UPDATE_ENV_VAR, "")
        if not update_token:
            print(f"update-branch: skipped, {update_branch.UPDATE_ENV_VAR} is not set")
        elif any(c["stage"] == "awaiting-review" for c in cards):   # bot PRs exist only there: no cards, no read (L-007)
            try:   # #93: a failed read or write here never stops the tick
                prs = office.gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", "100", "--json",
                                     "number,headRefName,headRefOid,author,mergeStateStatus,isCrossRepository,url")
                lines = update_branch.run(repo, prs, cards, events, update_token, a.dry_run, record_status, now, day,
                                          lambda moves: executor_for(prof).apply(moves, a.dry_run))
                print("\n".join(lines))
            except (subprocess.CalledProcessError, RuntimeError) as e:
                print(f"warning: update-branch step failed: {e}")

    for project, result in shadow_lines:
        print(format_shadow(project, a.shadow, result))
        if not a.dry_run:
            try:  # like record_status: a failed shadow write never blocks or undoes live work
                record_shadow({"type": "shadow", "day": day, "ts": now.isoformat(timespec="seconds"),
                               "project": project, "candidate": a.shadow, **result})
            except (subprocess.CalledProcessError, RuntimeError) as e:
                print(f"warning: shadow diff not recorded: {e}")

    roles = combine(per_project)
    roster_rows = roster_rows if roster_rows is not None else []   # every project parked: nobody was read
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
