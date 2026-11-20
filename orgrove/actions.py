"""The typed actions `dispatch.decide()` and `dispatch.board_sync()` return, and the Executor applies (#34).

Each action is a frozen dataclass. `to_dict()` is its dict view: exactly the dict the dispatcher emitted before the
types existed (same `kind`, same keys, no extras), which is what the golden fixtures, the step-summary lines and the
shadow diff compare. One action is one edit: a combined add+remove label edit stays a single action (one `gh issue
edit` and one comment), it is never split.

`RequestReview` and `Comment` from the ticket are not defined: nothing emits them yet. The ticket that first does adds
the dataclass, its dict view and its Executor branch together.
"""
from dataclasses import dataclass


def _listed(self, *names):
    for name in names:
        object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class _LabelEdit:
    issue: int
    remove: tuple = ()
    add: tuple = ()
    why: str = ""
    note: str | None = None
    quiet: bool = False
    kind = "label"

    def __post_init__(self):
        _listed(self, "remove", "add")

    def to_dict(self):
        d = {"kind": self.kind, "issue": self.issue, "remove": list(self.remove), "add": list(self.add), "why": self.why}
        if self.note is not None:
            d["note"] = self.note
        if self.quiet:
            d["quiet"] = True
        return d


@dataclass(frozen=True)
class SetStage(_LabelEdit):
    """A label edit that adds a `stage/*` label."""


@dataclass(frozen=True)
class AddFlag(_LabelEdit):
    """A label edit that adds flags (and may remove some)."""


@dataclass(frozen=True)
class RemoveFlag(_LabelEdit):
    """A label edit that only removes labels."""


def label_edit(issue, remove, add, why, note=None, quiet=False):
    """The one label edit `gh issue edit` makes, as the action type that names what it does."""
    cls = SetStage if any(x.startswith("stage/") for x in add) else AddFlag if add else RemoveFlag
    return cls(issue, remove, add, why, note, quiet)


@dataclass(frozen=True)
class BoardStatus:
    issue: int
    item_id: str
    status: str
    why: str
    kind = "board"

    def to_dict(self):
        return {"kind": self.kind, "issue": self.issue, "item_id": self.item_id, "status": self.status, "why": self.why}


@dataclass(frozen=True)
class CloseItem:
    issue: int
    why: str
    kind = "close"

    def to_dict(self):
        return {"kind": self.kind, "issue": self.issue, "why": self.why}


@dataclass(frozen=True)
class OpenPR:
    """Hands the ticket to the gate, which opens the PR once QA and Security approved the head."""
    issue: int
    why: str
    kind = "gate"

    def to_dict(self):
        return {"kind": self.kind, "issue": self.issue, "why": self.why}


@dataclass(frozen=True)
class Dispatch:
    role: str
    issue: int
    why: str
    mode: str | None = None     # None: the default "ticket" mode, and no `mode` key in the dict view
    kind = "run"

    def to_dict(self):
        d = {"kind": self.kind, "role": self.role, "issue": self.issue}
        if self.mode is not None:
            d["mode"] = self.mode
        return d | {"why": self.why}


@dataclass(frozen=True)
class Skip:
    role: str
    issue: int
    why: str
    kind = "skip"

    def to_dict(self):
        return {"kind": self.kind, "role": self.role, "issue": self.issue, "why": self.why}


def as_dict(action):
    """The dict view of an action; a plain dict (a shadow candidate's) passes through."""
    return action if isinstance(action, dict) else action.to_dict()
