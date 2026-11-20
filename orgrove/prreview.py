"""Review of pull requests ORGrove did not open (#82).

    python -m orgrove.prreview prepare --repo R --pr N --out DIR [--role qa|security] [--work work]

`prepare` is the PR twin of the workflow's "Prepare inputs" step for QA and Security: it checks the PR head out in
`work/`, writes the PR body (the reviewer's ticket) to DIR/body.md and the diff against the PR's base to
DIR/review.diff, and prints `head=<sha>` and `round=<n>` for the workflow to read.

The PR is untrusted input. Nothing from its tree runs here: no setup, no hooks, no install, only fetch, checkout and
diff (with external diff drivers and textconv off).
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

from orgrove import gate, office, org

SKIP_LABEL = "skip-agent-review"   # the owner's opt-out for one PR
REVIEWERS = ("qa", "security")


class PrError(RuntimeError):
    """A PR that can't be reviewed; the message is for the person reading the run."""


def is_bot(login):
    return (login or "").lower().endswith("[bot]")


def open_prs(repo, gh=None):
    """Open PRs reduced to what the dispatcher uses: number, author, head SHA and label names. One REST list
    (1 request a page, nothing nested is asked for beyond what GitHub returns), read through every page (L-017).
    Raises on any failed read: the caller reports it as unchecked, never as "no PRs" (L-012)."""
    pages = (gh or office.gh_json)("api", f"repos/{repo}/pulls?state=open&per_page=100", "--paginate", "--slurp")
    if pages is None:
        raise PrError(f"no answer from the open pull requests of {repo}")
    return [{"number": p["number"], "author": p["user"]["login"], "head": p["head"]["sha"],
             "labels": [x["name"] for x in p.get("labels", [])]} for page in pages for p in page]


def reviews_needed(repo, gh=None, comments=None):
    """One entry per open PR with the verdicts its current head already has, from the PR's own comments. A failed
    comments read raises: an unread PR is never taken as "no verdict yet", or the same head would be queued twice."""
    read = comments or (lambda n: office.issue_comments(repo, n))
    out = []
    for p in open_prs(repo, gh):
        if is_bot(p["author"]) or SKIP_LABEL in p["labels"]:
            out.append(p | {"verdicts": {}, "skipped": True})
            continue
        out.append(p | {"verdicts": {r: {"decision": v["decision"]} for r, v in gate.latest_verdicts(read(p["number"]), p["head"]).items()}})
    return out


def round_for(comments, role=None):
    """The review round this run is: the verdicts the App already posted on the PR (any head) for the role, plus one."""
    counts = {r: 0 for r in REVIEWERS}
    for c in comments:
        if c["user"]["login"] != org.BOT_LOGIN:
            continue
        for found in re.findall(r'"role":\s*"(qa|security)"', c["body"]) if "orgrove:verdict" in c["body"] else []:
            counts[found] += 1
    return (counts[role] if role else max(counts.values())) + 1


def git(work, *args):
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", work, *args], capture_output=True, text=True)
    if r.returncode:
        raise PrError(f"git {args[0]} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def prepare(repo, pr, out, role=None, work="work", gh=None, run=git, comments=None):
    gh = gh or office.gh_json
    try:
        info = gh("api", f"repos/{repo}/pulls/{pr}")
    except (subprocess.CalledProcessError, RuntimeError) as e:
        raise PrError(f"pull request #{pr} of {repo} can't be read: {e}") from e
    if not info or info.get("state") != "open":
        raise PrError(f"pull request #{pr} of {repo} is not open")
    head, base = info["head"]["sha"], info["base"]["ref"]
    run(work, "fetch", "-q", "origin", f"pull/{pr}/head", base)
    run(work, "checkout", "-q", "--detach", head)
    if run(work, "rev-parse", "HEAD").strip() != head:
        raise PrError(f"the head of #{pr} moved or could not be fetched: expected {head}")
    diff = run(work, "diff", "--no-ext-diff", "--no-textconv", f"origin/{base}...{head}")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "body.md").write_text(info.get("body") or "")
    (out / "review.diff").write_text(diff)
    read = comments or (lambda n: office.issue_comments(repo, n))
    try:
        return head, round_for(read(pr), role)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        raise PrError(f"the comments of #{pr} can't be read, so its review round is unknown: {e}") from e


def main(argv=None):
    p = argparse.ArgumentParser(prog="orgrove.prreview")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prepare")
    s.add_argument("--repo", required=True)
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--role", choices=REVIEWERS)
    s.add_argument("--work", default="work")
    a = p.parse_args(argv)
    try:
        head, rnd = prepare(a.repo, a.pr, a.out, a.role, a.work)
    except PrError as e:
        print(f"prreview: {e}", file=sys.stderr)
        return 1
    print(f"head={head}")
    print(f"round={rnd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
