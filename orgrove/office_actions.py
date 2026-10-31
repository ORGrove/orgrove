"""Owner actions from the local Office (#62): reply to an agent's question, merge a bot PR.

Everything runs with the owner's own `gh` login on the owner's machine. The HTTP layer is
deliberately narrow:
  - the server binds 127.0.0.1 only;
  - every POST needs the per-process CSRF token that only the served page knows;
  - Host and Origin must be this server (blocks other websites and DNS rebinding);
  - repos must be a configured project; only reply, merge, and approve/reject of meeting drafts exist.
After an action the dispatcher is started, so agents react at once instead of at the next heartbeat.
"""
import json
import secrets
import subprocess

from orgrove import manifest, org
from orgrove.ci import ci_green  # noqa: F401  (one CI rule for the Office and the agents, orgrove#151)

TOKEN = secrets.token_urlsafe(32)
MAX_REPLY = 4000


class Refused(Exception):
    pass


def check_request(headers, port, token=None):
    """Raise Refused unless the request comes from this Office page on this machine."""
    token = token or TOKEN
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if headers.get("Host") not in allowed_hosts:
        raise Refused("bad Host")
    origin = headers.get("Origin")
    if origin not in {f"http://{h}" for h in allowed_hosts}:
        raise Refused("bad Origin")
    if not secrets.compare_digest(headers.get("X-ORGrove-CSRF") or "", token):
        raise Refused("bad CSRF token")


def validate(body, repos):
    """Return a cleaned action dict or raise Refused."""
    action = body.get("action")
    repo = body.get("repo")
    number = body.get("number")
    if repo not in repos:
        raise Refused("unknown repo")
    if not isinstance(number, int) or number <= 0:
        raise Refused("bad number")
    if action == "reply":
        text = (body.get("text") or "").strip()
        if not text or len(text) > MAX_REPLY:
            raise Refused(f"reply must be 1-{MAX_REPLY} characters")
        return {"action": "reply", "repo": repo, "number": number, "text": text}
    if action in ("merge", "approve", "reject"):
        return {"action": action, "repo": repo, "number": number}
    raise Refused("unknown action")


def _gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def perform(act):
    repo, n = act["repo"], act["number"]
    if act["action"] == "reply":
        _gh("issue", "comment", str(n), "-R", repo, "--body", act["text"])
        msg = "Reply posted. The dispatcher has been started, so the agent picks it up in a minute or two."
    elif act["action"] in ("approve", "reject"):
        labels = json.loads(_gh("issue", "view", str(n), "-R", repo, "--json", "labels"))["labels"]
        if not any(label["name"] == "needs/approval" for label in labels):
            raise Refused("that issue isn't a draft awaiting approval")
        if act["action"] == "approve":
            _gh("issue", "edit", str(n), "-R", repo, "--remove-label", "needs/approval")
            msg = f"Approved #{n}. The PM will plan it."
        else:
            _gh("issue", "close", str(n), "-R", repo, "--reason", "not planned", "--comment", "Draft rejected by the owner.")
            msg = f"Rejected and closed #{n}."
    else:
        pr = json.loads(_gh("pr", "view", str(n), "-R", repo, "--json", "author,state,statusCheckRollup,title"))
        if pr["state"] != "OPEN":
            raise Refused(f"PR is {pr['state'].lower()}")
        if pr["author"]["login"] not in (org.BOT_AUTHOR, org.BOT_AUTHOR.removeprefix("app/")):
            raise Refused("only PRs opened by ORGrove can be merged from the Office")
        if not ci_green(pr.get("statusCheckRollup") or []):
            raise Refused("CI is not green yet")
        _gh("pr", "merge", str(n), "-R", repo, "--squash")
        msg = f"Merged #{n}. The deploy and Sanity follow automatically."
    from orgrove.office import ORGROVE_REPO  # lazy: office imports this module
    try:  # wake the agents now rather than at the next hourly heartbeat
        _gh("workflow", "run", "dispatch.yml", "-R", ORGROVE_REPO, "-f", "dry_run=false")
    except subprocess.CalledProcessError:
        msg += " (Couldn't start the dispatcher; the next heartbeat will.)"
    return msg


def project_repos():
    return {p["repo"] for p in manifest.projects()}
