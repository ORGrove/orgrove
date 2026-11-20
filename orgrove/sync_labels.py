"""Create any label ORGrove writes that a repo is missing. Additive only: never edits or deletes.

    python -m orgrove.sync_labels <owner>/<repo> [--control]   # a project repo, or the control repo
"""
import json
import subprocess
import sys
import time

from orgrove import stages

COLOURS = {"stage": "1d76db", "needs": "d93f0b", "blocked": "b60205", "agent": "5319e7", "priority": "fbca04",
           "type": "0075ca", "area": "c5def5", "finance": "0e8a16",
           "skip-agent-review": "ededed"}


def existing(repo):
    out = subprocess.run(
        ["gh", "label", "list", "-R", repo, "--limit", "500", "--json", "name"],
        check=True, capture_output=True, text=True,
    ).stdout
    return {row["name"] for row in json.loads(out)}


TRANSIENT = ("rate limit", "abuse", "secondary", "try again", "timed out", "timeout", "connection", "http 5", "502", "503", "504")


def create(repo, name, colour, description, run=None, sleep=time.sleep):
    """One label. GitHub throttles bursts of writes and sometimes blips: a transient answer is retried after a pause, and
    anything else stops with GitHub's own words, not a bare 'CalledProcessError' (acceptance run 3, 2026-11-11)."""
    run = run or subprocess.run
    for attempt in range(4):
        got = run(["gh", "label", "create", name, "-R", repo, "--color", colour, "--description", description],
                  capture_output=True, text=True)
        if got.returncode == 0:
            return
        said = (got.stderr or got.stdout or "").strip()
        if "already exists" in said.lower():       # made meanwhile (an earlier try that stopped half way): that is the goal
            return
        if attempt < 3 and any(t in said.lower() for t in TRANSIENT):
            sleep(5 * (attempt + 1))
            continue
        raise SystemExit(f"GitHub would not create the label {name} on {repo}: {said or 'no reason given'}. "
                         f"Press Try again: labels already made are kept.")


def main(repo, where="project"):
    cfg = stages.load()
    have = existing(repo)
    descriptions = stages.label_descriptions(cfg)
    for name in stages.labels(cfg, where):
        if name in have:
            continue
        create(repo, name, COLOURS[name.split("/")[0]], descriptions[name][:100])
        print(f"created {name}")


if __name__ == "__main__":
    main(sys.argv[1], "control" if "--control" in sys.argv[2:] else "project")
