"""Pipeline stages and allowed transitions, loaded from config/stages.toml."""
import tomllib
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config" / "stages.toml"
TERMINAL = {"backlog", "done", "needs-human"}


def load(path=CONFIG):
    with open(path, "rb") as f:
        return tomllib.load(f)


def _as_list(v):
    return v if isinstance(v, list) else [v]


def stage_ids(cfg):
    return [s["id"] for s in cfg["stage"]]


def labels(cfg, where="project"):
    """Every label ORGrove writes in a project repo (stage labels, flags and [labels.project]) or in the control
    repo ([labels.control])."""
    if where == "control":
        return list(cfg["labels"]["control"])
    return [s["label"] for s in cfg["stage"]] + list(cfg["flags"]) + list(cfg["labels"]["project"])


def label_descriptions(cfg):
    return ({s["label"]: f"ORGrove stage: {s['id']}" for s in cfg["stage"]} | cfg["flags"]
            | cfg["labels"]["project"] | cfg["labels"]["control"])


# A closed ticket's label: GitHub shows where it ended, not the stage it was in when it closed (rehearsal,
# 2026-10-30: #1 sat closed at stage/awaiting-review). Not a [[stage]]: closed is done whatever the labels say.
DONE_LABEL = "stage/done"


def stage_of(cfg, issue_labels, closed=False):
    """Current stage of an issue from its labels. Raises if it carries two stage labels."""
    if closed:
        return "done"
    by_label = {s["label"]: s["id"] for s in cfg["stage"]}
    found = [by_label[label] for label in issue_labels if label in by_label]
    if len(found) > 1:
        raise ValueError(f"issue has more than one stage label: {found}")
    return found[0] if found else "backlog"


def allowed(cfg, frm, to, actor):
    return any(
        t["from"] == frm and t["to"] == to and actor in _as_list(t["by"])
        for t in cfg["transition"]
    )


def validate(cfg):
    """Every transition refers to a known stage, and every stage can be reached and left."""
    known = set(stage_ids(cfg)) | TERMINAL
    errors = []
    for t in cfg["transition"]:
        for end in ("from", "to"):
            if t[end] not in known:
                errors.append(f"unknown stage {t[end]!r} in transition {t['from']}->{t['to']}")
    for sid in stage_ids(cfg):
        if not any(t["to"] == sid for t in cfg["transition"]):
            errors.append(f"stage {sid!r} cannot be entered")
        if not any(t["from"] == sid for t in cfg["transition"]):
            errors.append(f"stage {sid!r} cannot be left")
    return errors
