"""Keep the bot's own PRs up to date with main (orgrove#93).

Both product repos require a PR to be up to date before merge. Each tick, for every open PR the bot authored on an
`agent/*` branch of the same repo: BEHIND gets main merged in (the update-branch API: a merge, never a rebase or a
force-push); DIRTY goes back to Dev with `needs/fix`. One attempt per (PR, head SHA), written to the ledger.

The call uses ORGROVE_UPDATE_TOKEN only (contents write + pull requests write on the project repos, no `workflows`
permission). It is passed to the one `gh api` call through its environment, never on a command line or in output.
PR titles and bodies are never read.
"""
import os
import re
import subprocess

from orgrove import org
from orgrove.actions import label_edit

EVENT = "update_branch"
UPDATE_ENV_VAR = "ORGROVE_UPDATE_TOKEN"
BRANCH = re.compile(r"^agent/(\d+)-")


def mine(pr):
    """Bot-authored, `agent/*`, same repository. Anything else is somebody's branch."""
    author = (pr.get("author") or {}).get("login", "")
    return (author in (org.BOT_AUTHOR, org.BOT_AUTHOR.removeprefix("app/"), org.BOT_LOGIN)
            and bool(BRANCH.match(pr.get("headRefName", ""))) and not pr.get("isCrossRepository", False))


def last_results(events, repo):
    """(pr, head) -> the newest recorded result for that attempt."""
    return {(e["pr"], e["head"]): e.get("result") for e in events if e.get("type") == EVENT and e.get("repo") == repo}


def conflicts_before(events, repo, pr):
    """How many times this PR was already sent back for a conflict (any earlier head)."""
    return sum(1 for e in events if e.get("type") == EVENT and e.get("repo") == repo and e.get("pr") == pr
               and e.get("result") == "conflict")


def plan(prs, results):
    """[(pr, 'update' | 'conflict' | 'announce')]: what each of the bot's PRs needs that it hasn't had at this head."""
    out = []
    for pr in prs:
        if not mine(pr):
            continue
        seen = results.get((pr["number"], pr["headRefOid"]))
        if seen == "refused_workflow":   # refused and the owner not yet told: only the label move is left
            out.append((pr, "announce"))
        elif seen is None and pr.get("mergeStateStatus") == "BEHIND":
            out.append((pr, "update"))
        elif seen is None and pr.get("mergeStateStatus") == "DIRTY":
            out.append((pr, "conflict"))
    return out


def put_update(repo, pr, token):
    """PUT .../update-branch with expected_head_sha. Returns (ok, error text)."""
    env = {**os.environ, "GH_TOKEN": token}
    r = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{pr['number']}/update-branch",
                        "-f", f"expected_head_sha={pr['headRefOid']}"],
                       capture_output=True, text=True, env=env)
    return r.returncode == 0, ((r.stderr or "") + (r.stdout or "")).replace(token, "***").strip()[:300]


def run(repo, prs, cards, events, token, dry_run, record, now, day, execute, call=None):
    """Returns summary lines. `execute(moves)` applies label moves. Never raises on a refused update.

    The ledger is the once-per-head guard, so a PUT is made only after its attempt is recorded; a label move is
    recorded only after it was applied, so a failed move is retried on the next tick."""
    call = call or put_update
    if not token:
        return [f"update-branch: skipped, {UPDATE_ENV_VAR} is not set"]
    at_review = {c["number"] for c in cards if c["stage"] == "awaiting-review"}
    lines = []

    def log(pr, result):
        """Append the event; True when it reached the ledger. The issue lets the retro charge a conflict the run
        that fixed it (the time an agent spent only on merging main)."""
        try:
            ok = record({"type": EVENT, "day": day, "ts": now.isoformat(timespec="seconds"), "repo": repo,
                         "pr": pr["number"], "issue": int(BRANCH.match(pr["headRefName"]).group(1)),
                         "head": pr["headRefOid"], "result": result})
        except (subprocess.CalledProcessError, RuntimeError) as e:
            lines.append(f"warning: update-branch attempt not recorded: {e}")
            return False
        if not ok:
            lines.append(f"warning: update-branch attempt on PR #{pr['number']} not recorded (no ledger): not acting")
        return bool(ok)

    def move(pr, action, result):
        try:
            execute([action])
        except (subprocess.CalledProcessError, RuntimeError) as e:
            lines.append(f"warning: update-branch label move for PR #{pr['number']} failed, retrying next tick: {e}")
            return
        log(pr, result)

    for pr, what in plan(prs, last_results(events, repo)):
        n, head = pr["number"], pr["headRefOid"]
        issue = int(BRANCH.match(pr["headRefName"]).group(1))
        lines.append(f"update-branch: PR #{n} {what} at {head[:7]}")
        if dry_run:
            continue
        if what == "update":
            if not log(pr, "attempt"):
                continue
            ok, err = call(repo, pr, token)
            if ok:
                log(pr, "updated")
                continue
            lines.append(f"update-branch: PR #{n} refused: {err}")
            if "workflow" in err.lower() and issue in at_review:
                log(pr, "refused_workflow")
                what = "announce"
            else:
                log(pr, "refused")
        if what == "announce" and issue in at_review:
            move(pr, label_edit(issue=issue, remove=[], add=["needs/delivery"],
                      why=f"update-branch on PR #{n} was refused because merging main would change "
                             "`.github/workflows` files, and the update token has no permission for those",
                      note="Please merge main into the branch yourself (the PR's *Update branch* button "
                              "or a local merge), then CI reruns on the combined code."), "announced")
        elif what == "conflict" and issue in at_review and conflicts_before(events, repo, n):
            # A second conflict on the same PR: two tickets keep editing the same code, which is a planning
            # problem, not a merge. The Delivery Lead sequences them with the PM (owner, 2026-11-03).
            move(pr, label_edit(issue=issue, remove=[], add=["needs/delivery"],
                      why=f"PR #{n} conflicts with main again",
                      note=f"{pr.get('url', '')} is DIRTY at `{head}`, for the second time.\n\n"
                              "Delivery Lead: two tickets keep changing the same code. Follow **Branch conflicts** "
                              "in your skill: find the ticket on main that collides, decide which change wins, "
                              "write that on this ticket, and route Dev to merge `origin/main` (and the PM to "
                              "sequence the two if more is planned on the same files)."), "conflict")
        elif what == "conflict" and issue in at_review:
            move(pr, label_edit(issue=issue, remove=["stage/awaiting-review"],
                      add=["stage/in-dev", "needs/fix"], why=f"PR #{n} conflicts with main",
                      note=f"{pr.get('url', '')} is DIRTY at `{head}`.\n\nDev: merge `origin/main` into the "
                              "branch, resolve the conflicts, run `commands.contract`, push. QA and Security "
                              "review the new head; the PR updates itself."), "conflict")
    return lines
