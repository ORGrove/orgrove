"""Config v2 loader and validator (#37): the enforcement point for the locked rules.

A v2 tree is `orgrove.toml`, `pipelines/*.toml`, `departments/*.toml` and `projects/*.toml`, every file with
`schema_version = 2` (docs/product-plan.md, Config schema). `check(root)` returns every violation, each naming the
file and the rule; `load(root)` returns the typed `Org` or raises `ConfigError` carrying all of them.

Fail closed: an unknown key is an error (a typo must not drop a rule), a missing safety setting counts as unsafe
(a gate without `locked = true` is unlocked, a role without `contents = "read"` is not read-only), and the guardrail
paths and the human gate are injected by code, so no file can switch them off.
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from orgrove.gate import PROTECTED

SCHEMA_VERSION = 2
KINDS = ("manager", "lead", "worker", "reviewer", "checker")
READ_ONLY_KINDS = ("reviewer", "checker")
GATE_KINDS = ("merge", "publish")
SCOPE_DEFAULTS = ("exclusive", "paths")

# Guardrail paths every project gets, whatever its file says: the agent harness (gate.PROTECTED), CI workflows,
# the org config, the budget caps and the ledger.
GUARDRAILS = tuple(PROTECTED) + (".github/workflows/*", "orgrove.toml", "pipelines/*", "departments/*",
                                 "projects/*", "config/budget.toml", "ledger/*")

KEYS = {
    "org": {"schema_version", "name", "humans", "budget", "gates"},
    "human": {"login"},
    "budget": {"daily_usd", "monthly_usd", "max_run_ceiling_usd"},
    "gate": {"kind", "locked", "approvers"},
    "pipeline": {"schema_version", "name", "stages", "gates", "review_roles", "risk_default"},
    "department": {"schema_version", "name", "pipeline", "roles"},
    "role": {"kind", "reports_to", "headcount", "duties", "outbox", "enabled", "contents", "budget_share",
             "ceiling_usd", "scope_default", "skill", "model"},
    "project": {"schema_version", "project", "repo", "departments", "owners", "guardrails", "rules", "commands"},
    "guardrails": {"paths", "extra", "remove"},
}


class ConfigError(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass
class Human:
    id: str
    login: str = ""


@dataclass
class Gate:
    id: str
    kind: str
    locked: bool
    approvers: list = field(default_factory=list)


@dataclass
class Role:
    id: str
    department: str
    kind: str
    reports_to: str
    headcount: int = 1
    duties: list = field(default_factory=list)
    outbox: list = field(default_factory=list)
    enabled: bool = True
    contents: str = "read"
    budget_share: float = 0.0
    ceiling_usd: float = 0.0
    scope_default: str = ""


@dataclass
class Pipeline:
    id: str
    stages: list
    gates: list
    review_roles: list
    file: str = ""


@dataclass
class Department:
    id: str
    pipeline: str
    roles: dict


@dataclass
class Project:
    id: str
    repo: str
    departments: list
    guardrails: list  # the injected set plus the project's own additions
    owners: list = field(default_factory=list)


@dataclass
class Org:
    name: str
    humans: dict
    gates: dict
    budget: dict
    pipelines: dict
    departments: dict
    projects: dict

    def roles(self):
        return {r.id: r for d in self.departments.values() for r in d.roles.values()}

    def resolved_reports_to(self, role_id):
        """Who a role reports to today: a disabled lead is skipped, so its reports go to its own manager."""
        roles = self.roles()
        seen = set()
        target = roles[role_id].reports_to
        while target in roles and not roles[target].enabled and roles[target].kind == "lead":
            if target in seen:
                break
            seen.add(target)
            target = roles[target].reports_to
        return target


def _read(path, root, errors):
    name = path.relative_to(root).as_posix()
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            errors.append(f"{name}: path-outside-tree: file resolves outside the config root")
            return name, None
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        errors.append(f"{name}: unreadable: {e.strerror or e}")
        return name, None
    except tomllib.TOMLDecodeError as e:
        errors.append(f"{name}: invalid-toml: {e}")
        return name, None
    if data.get("schema_version") != SCHEMA_VERSION or isinstance(data.get("schema_version"), bool):
        errors.append(f"{name}: schema-version: schema_version = {SCHEMA_VERSION} is required "
                      f"(found {data.get('schema_version')!r})")
        return name, None
    return name, data


def _unknown(name, where, table, kind, errors):
    for k in sorted(set(table) - KEYS[kind]):
        errors.append(f"{name}: unknown-key: {where}{k!r} is not a v2 key")


def _strs(name, where, value, errors):
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    errors.append(f"{name}: bad-type: {where} must be a list of strings")
    return []


def _num(name, where, value, errors):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0 \
            or value == float("inf"):
        errors.append(f"{name}: bad-type: {where} must be a finite number, not negative")
        return 0.0
    return float(value)


def _inside_tree(path):
    p = Path(str(path))
    return not p.is_absolute() and ".." not in p.parts and "\\" not in str(path)


def _parse_org(root, errors):
    name, d = _read(root / "orgrove.toml", root, errors)
    if d is None:
        return None
    _unknown(name, "", d, "org", errors)
    humans = {}
    for hid, h in (d.get("humans") or {}).items():
        if isinstance(h, dict):
            _unknown(name, f"humans.{hid}.", h, "human", errors)
        humans[hid] = Human(hid, str((h or {}).get("login", "")) if isinstance(h, dict) else "")
    gates = {}
    for gid, g in (d.get("gates") or {}).items():
        if not isinstance(g, dict):
            errors.append(f"{name}: bad-type: gates.{gid} must be a table")
            continue
        _unknown(name, f"gates.{gid}.", g, "gate", errors)
        gates[gid] = Gate(gid, str(g.get("kind", "")), g.get("locked") is True,
                          _strs(name, f"gates.{gid}.approvers", g.get("approvers", []), errors))
    budget = d.get("budget") or {}
    _unknown(name, "budget.", budget, "budget", errors)
    nums = {k: _num(name, f"budget.{k}", budget[k], errors) for k in KEYS["budget"] if k in budget}
    return name, d, humans, gates, nums


def _parse_role(name, dept, rid, r, errors):
    where = f"roles.{rid}."
    if not isinstance(r, dict):
        errors.append(f"{name}: bad-type: roles.{rid} must be a table")
        return None
    _unknown(name, where, r, "role", errors)
    kind = r.get("kind")
    if kind not in KINDS:
        errors.append(f"{name}: bad-kind: {where}kind must be one of {', '.join(KINDS)} (found {kind!r})")
    hc = r.get("headcount", 1)
    if isinstance(hc, bool) or not isinstance(hc, int) or hc < 1:
        errors.append(f"{name}: bad-type: {where}headcount must be a whole number, 1 or more")
        hc = 1
    return Role(
        rid, dept, str(kind), str(r.get("reports_to", "")), hc,
        _strs(name, where + "duties", r.get("duties", []), errors),
        _strs(name, where + "outbox", r.get("outbox", []), errors),
        r.get("enabled", True) is True, str(r.get("contents", "")),
        _num(name, where + "budget_share", r.get("budget_share", 0), errors),
        _num(name, where + "ceiling_usd", r.get("ceiling_usd", 0), errors),
        str(r.get("scope_default", "")))


def _load(root):
    """(Org or None, errors): every file is read and every rule checked, so one run reports everything."""
    root = Path(root)
    errors = []
    parsed = _parse_org(root, errors)
    org = None
    if parsed:
        oname, od, humans, gates, budget = parsed
        org = Org(str(od.get("name", "")), humans, gates, budget, {}, {}, {})
        _rule_gates(oname, org, errors)
    else:
        oname = "orgrove.toml"

    for path in sorted((root / "pipelines").glob("*.toml")):
        name, d = _read(path, root, errors)
        if d is None:
            continue
        _unknown(name, "", d, "pipeline", errors)
        org = org or Org("", {}, {}, {}, {}, {}, {})
        org.pipelines[path.stem] = Pipeline(
            path.stem, _strs(name, "stages", d.get("stages", []), errors),
            _strs(name, "gates", d.get("gates", []), errors),
            _strs(name, "review_roles", d.get("review_roles", []), errors), name)

    dept_files = {}
    for path in sorted((root / "departments").glob("*.toml")):
        name, d = _read(path, root, errors)
        if d is None:
            continue
        _unknown(name, "", d, "department", errors)
        roles = {}
        for rid, r in (d.get("roles") or {}).items():
            role = _parse_role(name, path.stem, rid, r, errors)
            if role:
                roles[rid] = role
        org = org or Org("", {}, {}, {}, {}, {}, {})
        org.departments[path.stem] = Department(path.stem, str(d.get("pipeline", "")), roles)
        dept_files[path.stem] = name

    proj_files = {}
    for path in sorted((root / "projects").glob("*.toml")):
        name, d = _read(path, root, errors)
        if d is None:
            continue
        _unknown(name, "", d, "project", errors)
        g = d.get("guardrails") or {}
        _unknown(name, "guardrails.", g, "guardrails", errors)
        declared = _strs(name, "guardrails.paths", g["paths"], errors) if "paths" in g else None
        if g.get("remove"):
            errors.append(f"{name}: guardrail-removed: guardrails.remove is refused: guardrail paths are injected "
                          f"by code and cannot be removed ({', '.join(map(str, g['remove']))})")
        if declared is not None:
            for gp in GUARDRAILS:
                if gp not in declared:
                    errors.append(f"{name}: guardrail-omitted: guardrails.paths omits the guardrail path {gp!r}")
        for rule in _strs(name, "rules", d.get("rules", []), errors):
            if not _inside_tree(rule):
                errors.append(f"{name}: path-outside-tree: rules entry {rule!r} must stay inside the repository")
        org = org or Org("", {}, {}, {}, {}, {}, {})
        extra = [p for p in (declared or []) + _strs(name, "guardrails.extra", g.get("extra", []), errors)
                 if p not in GUARDRAILS]
        org.projects[path.stem] = Project(
            str(d.get("project", path.stem)), str(d.get("repo", "")),
            _strs(name, "departments", d.get("departments", []), errors),
            list(GUARDRAILS) + extra, _strs(name, "owners", d.get("owners", []), errors))
        proj_files[path.stem] = name

    if org:
        _rules_hierarchy(org, dept_files, errors)
        _rules_roles(org, dept_files, oname, errors)
        _rules_refs(org, dept_files, proj_files, errors)
    return org, errors


def _rule_gates(name, org, errors):
    for g in org.gates.values():
        if g.kind not in GATE_KINDS:
            errors.append(f"{name}: bad-kind: gates.{g.id}.kind must be one of {', '.join(GATE_KINDS)} "
                          f"(found {g.kind!r})")
        if not g.locked:
            errors.append(f"{name}: gate-unlocked: gates.{g.id} must have locked = true: the human gate "
                          f"cannot be switched off")
        if not g.approvers:
            errors.append(f"{name}: gate-no-approver: gates.{g.id}.approvers must name at least one human "
                          f"in [humans]: a gate nobody can approve is not a human gate")
        for a in g.approvers:
            if a not in org.humans:
                errors.append(f"{name}: unknown-human: gates.{g.id}.approvers names {a!r}, not in [humans]")
    if not any(g.kind in GATE_KINDS and g.locked for g in org.gates.values()):
        errors.append(f"{name}: no-human-gate: a locked human merge (or publish) gate is required")


def _rules_hierarchy(org, files, errors):
    roles = org.roles()
    seen_in = {}
    for dept in org.departments.values():
        for rid in dept.roles:
            if rid in seen_in:
                errors.append(f"{files[dept.id]}: duplicate-role: role {rid!r} is also defined in "
                              f"{files[seen_in[rid]]}")
            seen_in[rid] = dept.id
    for rid, r in roles.items():
        f = files[r.department]
        chain, target = [rid], r.reports_to
        while True:
            if target in org.humans:
                break
            if target in chain:
                errors.append(f"{f}: hierarchy-cycle: roles.{rid} reports in a loop: "
                              f"{' -> '.join(chain + [target])}")
                break
            if target not in roles:
                errors.append(f"{f}: hierarchy-no-human: roles.{rid} reports_to chain ends at {target!r}, "
                              f"which is not a role or a human in orgrove.toml [humans]")
                break
            chain.append(target)
            target = roles[target].reports_to


def _rules_roles(org, files, oname, errors):
    total = 0.0
    cap = org.budget.get("max_run_ceiling_usd")
    daily = org.budget.get("daily_usd")
    for key in sorted(KEYS["budget"]):
        if key not in org.budget:
            errors.append(f"{oname}: budget-missing: [budget] must set {key}: a missing cap is unsafe, "
                          f"so no ceiling is ever checked against it")
    for dept in org.departments.values():
        f = files[dept.id]
        for r in dept.roles.values():
            if r.kind in READ_ONLY_KINDS and r.contents != "read":
                errors.append(f"{f}: read-only-role: roles.{r.id} is a {r.kind} and must have "
                              f"contents = \"read\" (found {r.contents!r})")
            if r.kind == "lead" and r.headcount != 1:
                errors.append(f"{f}: lead-headcount: roles.{r.id} is a lead and must have headcount = 1 "
                              f"(found {r.headcount})")
            if r.headcount > 1 and r.scope_default not in SCOPE_DEFAULTS:
                errors.append(f"{f}: scope-default: roles.{r.id} has headcount {r.headcount} and needs "
                              f"scope_default = \"exclusive\" or \"paths\"")
            total += r.budget_share
            for label, limit in (("max_run_ceiling_usd", cap), ("daily_usd", daily)):
                if limit is not None and r.ceiling_usd > limit:
                    errors.append(f"{f}: ceiling-over-cap: roles.{r.id}.ceiling_usd ${r.ceiling_usd:g} is above "
                                  f"{label} ${limit:g} in orgrove.toml")
    if total > 1 + 1e-9:
        errors.append(f"{oname}: budget-shares: role budget_share values add up to {total:g}, more than 1")


def _rules_refs(org, dfiles, pfiles, errors):
    roles = org.roles()
    for dept in org.departments.values():
        if dept.pipeline not in org.pipelines:
            errors.append(f"{dfiles[dept.id]}: unknown-ref: pipeline {dept.pipeline!r} has no pipelines/ file")
    for pl in org.pipelines.values():
        for g in pl.gates:
            if g not in org.gates:
                errors.append(f"{pl.file}: unknown-ref: gate {g!r} is not in orgrove.toml [gates]")
        if not any(g in org.gates and org.gates[g].kind in GATE_KINDS and org.gates[g].locked for g in pl.gates):
            errors.append(f"{pl.file}: no-human-gate: the pipeline must use at least one locked merge (or "
                          f"publish) gate")
        for r in pl.review_roles:
            if r not in roles:
                errors.append(f"{pl.file}: unknown-ref: review role {r!r} is not defined in departments/")
            elif roles[r].kind not in READ_ONLY_KINDS or roles[r].contents != "read":
                errors.append(f"{pl.file}: read-only-role: review role {r!r} must be a reviewer or checker "
                              f"with contents = \"read\" (found {roles[r].kind!r}, {roles[r].contents!r})")
    for p in org.projects.values():
        for d in p.departments:
            if d not in org.departments:
                errors.append(f"{pfiles.get(p.id, p.id)}: unknown-ref: department {d!r} has no departments/ file")


def check(root):
    """Every violation in the tree, as `file: rule: message` lines. Empty means valid."""
    return _load(root)[1]


def load(root):
    org, errors = _load(root)
    if errors or org is None:
        raise ConfigError(errors or ["orgrove.toml: unreadable: no config found"])
    return org
