"""The Executor: the one place the dispatcher's typed actions become writes (#34).

`Executor(...).apply(actions, dry_run)` prints each action's dict view (the step-summary line), applies it through the
ports and returns one `Event` per action, in order, including a `Skip` and every action of a dry run (marked not
applied). It reaches the platform only through the objects it was given: `GitHub` in production, the in-memory fakes in
tests (`tests/test_executor.py`); nothing here constructs a fake.

Two operations the Executor needs are not on the #33 `Tracker` protocol yet, because one dispatcher action is one
`gh` call: `edit_labels(issue, add, remove)` (a combined add+remove edit, never split) and `close(issue, comment)`.
`BoardMirror.mirror(item_id, stage)` is called with the board's own item id and status name, as GitHub needs both.
"""
import json
import os
import subprocess
from dataclasses import dataclass

from orgrove import actions as A
from orgrove import gate, manifest, office, stages, sync_labels

NEW_LABELS = {stages.DONE_LABEL, "needs/delivery"}


@dataclass(frozen=True)
class Event:
    action: dict            # the action's dict view
    applied: bool           # False for a Skip and for a dry run


class GitHub:
    """The GitHub side: exactly the `gh` calls the dispatcher made before the Executor existed."""

    def __init__(self, repo, skill_of, board_ids):
        self.repo = repo
        self.skill_of = skill_of        # role -> the skill role-run.yml takes
        self.board_ids = board_ids      # () -> (project id, status field id, {status name: option id})
        self._ids = None

    def ensure_schema(self, stages_cfg=None):
        # The sync only creates the labels that are missing (an install from before stage/done lacks it).
        sync_labels.main(self.repo)

    def edit_labels(self, issue, add, remove):
        args = ["issue", "edit", str(issue), "-R", self.repo]
        if remove:
            args += ["--remove-label", ",".join(remove)]
        if add:
            args += ["--add-label", ",".join(add)]
        subprocess.run(["gh", *args], check=True, capture_output=True)

    def comment(self, issue, body):
        subprocess.run(["gh", "issue", "comment", str(issue), "-R", self.repo, "--body", body],
                       check=True, capture_output=True)

    def close(self, issue, comment):
        subprocess.run(["gh", "issue", "close", str(issue), "-R", self.repo, "--comment", comment],
                       check=True, capture_output=True)

    def mirror(self, item_id, status):
        self._ids = self._ids or self.board_ids()
        pid, field, options = self._ids
        subprocess.run(["gh", "project", "item-edit", "--project-id", pid, "--id", item_id,
                        "--field-id", field, "--single-select-option-id", options[status]],
                       check=True, capture_output=True)

    def open_pr(self, issue):
        gate.main(["--repo", self.repo, "--issue", str(issue)])

    def submit(self, role, issue, inputs):
        # Starting a workflow needs actions:write on orgrove, which the App token deliberately
        # lacks; the dispatcher's own GITHUB_TOKEN is passed in as ORGROVE_DISPATCH_TOKEN.
        env = {**os.environ, "GH_TOKEN": os.environ.get("ORGROVE_DISPATCH_TOKEN", os.environ.get("GH_TOKEN", ""))}
        subprocess.run(["gh", "workflow", "run", "role-run.yml", "-R", office.ORGROVE_REPO,
                        "-f", f"role={self.skill_of(role)}", "-f", f"issue={issue}", "-f", f"project={inputs['project']}",
                        "-f", f"mode={inputs['mode']}"],
                       check=True, capture_output=True, env=env)


class Executor:
    def __init__(self, tracker, board, runner, open_pr, project, owners, stages_cfg=None):
        self.tracker, self.board, self.runner = tracker, board, runner
        self.open_pr = open_pr          # issue -> None: hands the ticket to the gate
        self.project = project
        self.owners = owners            # () -> logins to @mention on needs/human
        self.stages_cfg = stages_cfg
        self._synced = False

    def apply(self, actions, dry_run):
        events = []
        for a in actions:
            view = A.as_dict(a)
            print(json.dumps(view))
            applied = not dry_run and not isinstance(a, A.Skip)
            if applied:
                self._apply_one(a)
            events.append(Event(view, applied))
        return events

    def _apply_one(self, a):
        if isinstance(a, (A.SetStage, A.AddFlag, A.RemoveFlag)):
            if NEW_LABELS & set(a.add) and not self._synced:
                # stage/done (2026-10-30) and needs/delivery (2026-11-02) are new: GitHub refuses to add a missing
                # label. One label list per tick that needs it.
                self.tracker.ensure_schema(self.stages_cfg)
                self._synced = True
            self.tracker.edit_labels(a.issue, list(a.add), list(a.remove))
            if a.quiet:
                return
            # needs/human is a question for the owners: mention them so it reaches their phone.
            mention = " ".join(f"@{o}" for o in self.owners()) + " " if "needs/human" in a.add else ""
            note = f"\n\n{a.note}" if a.note else ""
            self.tracker.comment(a.issue, f"{mention}ORGrove dispatcher: {a.why} → labels +{list(a.add)} −{list(a.remove)}{note}")
        elif isinstance(a, A.BoardStatus):
            self.board.mirror(a.item_id, a.status)
        elif isinstance(a, A.CloseItem):
            self.tracker.close(a.issue, f"ORGrove dispatcher: {a.why}.")
        elif isinstance(a, A.OpenPR):
            self.open_pr(a.issue)
        elif isinstance(a, A.Dispatch):
            self.runner.submit(a.role, a.issue, {"project": self.project, "mode": a.mode or "ticket"})
        else:
            raise TypeError(f"not an action: {a!r}")


def github(repo, project, skill_of, board_ids):
    """The production Executor: one GitHub object behind every port."""
    gh = GitHub(repo, skill_of, board_ids)
    return Executor(gh, gh, gh, gh.open_pr, project, lambda: manifest.owners_for_repo(repo))
