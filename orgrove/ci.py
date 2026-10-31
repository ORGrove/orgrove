"""CI state of a commit (orgrove#151): one rule for the Office, the merge button, the dispatcher and the gate.

Before #151 only humans saw red/green (the Office board and the merge button), and only once a PR was open.
The agent pipeline never read CI, so QA and Security reviewed heads that CI had already failed.

Checks are in `statusCheckRollup` shape: check runs carry `status` + `conclusion`, commit statuses carry `state`.
"""

GREEN = {"SUCCESS", "SKIPPED", "NEUTRAL"}
WAITING = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
# GitHub's annotation on a job it refused to start (billing, spending limit). The code was never run, so this is
# not a red: on 2026-10-28 reading it as one bounced #483 back to Dev over a billing block (#151).
NOT_STARTED = "The job was not started"
BLOCKED = "NOT_STARTED"


def ci_state(checks):
    """'green', 'red', 'blocked' (a job GitHub never started), 'pending', or 'none' (nothing reported yet).
    Red wins: one failure is enough to stop. Blocked beats pending: nothing moves until the owner unblocks it."""
    if not checks:
        return "none"
    pending = blocked = False
    for c in checks:
        result = (c.get("conclusion") or c.get("state") or "").upper()
        if (c.get("status") or "COMPLETED").upper() != "COMPLETED" or result in WAITING:
            pending = True
        elif result == BLOCKED:
            blocked = True
        elif result not in GREEN:
            return "red"
    return "blocked" if blocked else "pending" if pending else "green"


def ci_green(checks):
    """True only when there is at least one check and every check completed successfully."""
    return ci_state(checks) == "green"


def failing(checks):
    """The red checks, as (name, url) pairs, for a comment a Developer can act on."""
    out = []
    for c in checks:
        result = (c.get("conclusion") or c.get("state") or "").upper()
        if (c.get("status") or "COMPLETED").upper() == "COMPLETED" and result not in GREEN | WAITING | {BLOCKED}:
            out.append((c.get("name") or c.get("context") or "?", c.get("url") or c.get("detailsUrl") or ""))
    return out


def head_checks(repo, sha, gh):
    """Every check run on one commit, in rollup shape. Works before any PR exists.

    Check runs only: that is what GitHub Actions reports. Legacy commit statuses need the App's "Commit statuses"
    permission, which it doesn't have; asking for them returned HTTP 403 and stopped every dispatcher tick
    (2026-10-28, #151)."""
    runs = gh("api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
    out = []
    for r in runs:
        conclusion = (r.get("conclusion") or "").upper()
        if conclusion == "FAILURE" and never_started(repo, r, gh):
            conclusion = BLOCKED
        out.append({"name": r["name"], "status": (r.get("status") or "").upper(),
                    "conclusion": conclusion, "url": r.get("html_url", "")})
    return out


def never_started(repo, run, gh):
    """True when GitHub refused to start this job. Any error reading the annotations keeps it a failure:
    a wrong red costs one Dev round, a wrong green would let unrun code through."""
    if not (run.get("output") or {}).get("annotations_count"):
        return False
    try:
        notes = gh("api", f"repos/{repo}/check-runs/{run['id']}/annotations")
    except Exception:  # noqa: BLE001 — fall back to "failure", never to "green"
        return False
    return any((a.get("message") or "").startswith(NOT_STARTED) for a in notes)
