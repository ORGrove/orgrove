"""Stage modes (#275, epic #46): how a tracker carries the pipeline's stages.

- `labels` (default, today's behaviour): one `stage/*` label per stage; there is nothing to map.
- `workflow`: each stage maps to its own native workflow state (`stage_map`), for trackers with real states.
- `hybrid`: a coarse native state plus a fine `stage/*` label, for workflows with fewer states than the
  pipeline. Several stages may share a native state; each stage still maps to exactly one.

- `scoped_labels` (#284, GitLab `stage::x`): each stage is the label `<scope>::<stage>`; flags stay plain labels.
  We keep the one-stage rule ourselves (`set_stage_labels`): exclusivity of scoped labels is a paid GitLab tier.

Pure functions over config: no tracker calls, no credentials.
"""
import re

MODES = ("labels", "workflow", "hybrid", "scoped_labels")
DEFAULT_SCOPE = "stage"
NAME = re.compile(r"[a-z0-9-]+")
OPTIONAL_STAGES = ("backlog", "done")     # not in [[stage]], but real stages a tracker may have a state for


def load(prof):
    """(mode, stage_map) from a project profile's [tracker] table. No table means `labels`."""
    tracker = prof.get("tracker", {})
    return tracker.get("stage_mode", "labels"), tracker.get("stage_map", {})


def scope(prof):
    """The label scope of `scoped_labels` mode, from the profile's [tracker] table."""
    return prof.get("tracker", {}).get("scope", DEFAULT_SCOPE)


def _validate_scoped(stages_cfg, stage_map, label_scope):
    errors = []
    if stage_map:
        errors.append("tracker.stage_map needs stage_mode = workflow or hybrid; in scoped_labels mode it would be ignored")
    if not isinstance(label_scope, str) or not NAME.fullmatch(label_scope):
        errors.append(f"tracker.scope must match [a-z0-9-]+, got {label_scope!r}")
    errors += [f"stage name {s['id']!r} must match [a-z0-9-]+" for s in stages_cfg["stage"]
               if not NAME.fullmatch(s["id"])]
    errors += [f"flag {f!r} must not contain '::' (that is a scoped label)" for f in stages_cfg["flags"] if "::" in f]
    return errors


def stage_label(label_scope, stage):
    return f"{label_scope}::{stage}"


def set_stage_labels(labels, label_scope, stage):
    """(remove, add) that leave exactly one `<scope>::*` label, or none for backlog.

    Every other scoped label is removed explicitly: on GitLab Free the platform does not drop the old one.
    """
    prefix = f"{label_scope}::"
    target = None if stage == "backlog" else stage_label(label_scope, stage)
    remove = sorted(l for l in labels if l.startswith(prefix) and l != target)
    add = [target] if target and target not in labels else []
    return remove, add


def read_stage(labels, label_scope):
    """(stage, anomalies) from an item's labels. Two stage labels set by hand are reported with both names,
    and the stage is then None: never silently resolved to one."""
    prefix = f"{label_scope}::"
    found = sorted(l[len(prefix):] for l in labels if l.startswith(prefix))
    if len(found) > 1:
        return None, [f"item carries more than one {label_scope}:: label: {found}"]
    return (found[0] if found else "backlog"), []


def validate(stages_cfg, mode, stage_map, label_scope=DEFAULT_SCOPE):
    """Every reason the mode and map are unusable; an empty list means they are fine."""
    if mode not in MODES:
        return [f"tracker.stage_mode must be one of {', '.join(MODES)}, got {mode!r}"]
    if mode == "scoped_labels":
        return _validate_scoped(stages_cfg, stage_map, label_scope) if isinstance(stage_map, dict) \
            else ["tracker.stage_map must be a table of stage = native state"]
    if not isinstance(stage_map, dict):
        return ["tracker.stage_map must be a table of stage = native state"]
    if mode == "labels":
        return ["tracker.stage_map needs stage_mode = workflow or hybrid; in labels mode it would be ignored"] \
            if stage_map else []
    required = [s["id"] for s in stages_cfg["stage"]]
    errors = [f"stage_map names unknown stage {k!r}" for k in stage_map if k not in required + list(OPTIONAL_STAGES)]
    for stage, state in stage_map.items():
        if not isinstance(state, str) or not state.strip():
            errors.append(f"stage_map: stage {stage!r} needs a native state name")
    errors += [f"stage_map leaves stage {s!r} unmapped" for s in required if s not in stage_map]
    if mode == "workflow":
        by_state = {}
        for stage, state in stage_map.items():
            if isinstance(state, str) and stage in required + list(OPTIONAL_STAGES):
                by_state.setdefault(state, []).append(stage)
        errors += [f"stage_map maps stages {sorted(stages)} to one state {state!r}; workflow mode needs one state each "
                   "(use hybrid for a workflow with fewer states)"
                   for state, stages in by_state.items() if len(stages) > 1]
    return errors


def validate_profile(stages_cfg, prof):
    return validate(stages_cfg, *load(prof), scope(prof))


def unreachable(stages_cfg, stage_map, native_transitions):
    """Pipeline [[transition]]s the native workflow can't make, each with the native states involved.

    `native_transitions` is `{from_state: {to_state, ...}}`. A move is one native transition; two stages in the
    same native state (hybrid) only need the label change. A transition with an end the map doesn't cover
    (`needs-human`, or an unmapped backlog/done) is not a state change and is skipped.
    """
    missing = []
    for t in stages_cfg["transition"]:
        src, dst = stage_map.get(t["from"]), stage_map.get(t["to"])
        if src is None or dst is None or src == dst or dst in native_transitions.get(src, ()):
            continue
        missing.append({**t, "from_state": src, "to_state": dst})
    return missing
