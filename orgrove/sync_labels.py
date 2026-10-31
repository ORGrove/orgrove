"""Create any label ORGrove writes that a repo is missing. Additive only: never edits or deletes.

    python -m orgrove.sync_labels <owner>/<repo> [--control]   # a project repo, or the control repo
"""
import json
import subprocess
import sys

from orgrove import stages

COLOURS = {"stage": "1d76db", "needs": "d93f0b", "blocked": "b60205", "agent": "5319e7", "priority": "fbca04",
           "type": "0075ca", "area": "c5def5", "finance": "0e8a16"}


def existing(repo):
    out = subprocess.run(
        ["gh", "label", "list", "-R", repo, "--limit", "500", "--json", "name"],
        check=True, capture_output=True, text=True,
    ).stdout
    return {row["name"] for row in json.loads(out)}


def main(repo, where="project"):
    cfg = stages.load()
    have = existing(repo)
    descriptions = stages.label_descriptions(cfg)
    for name in stages.labels(cfg, where):
        if name in have:
            continue
        colour = COLOURS[name.split("/")[0]]
        subprocess.run(
            ["gh", "label", "create", name, "-R", repo, "--color", colour,
             "--description", descriptions[name][:100]],
            check=True,
        )
        print(f"created {name}")


if __name__ == "__main__":
    main(sys.argv[1], "control" if "--control" in sys.argv[2:] else "project")
