"""Department manifests and project profiles (#5): load and validate."""
import re
import shlex
import tomllib
from pathlib import Path

from orgrove import budget, stage_modes, stages

ROOT = Path(__file__).resolve().parent.parent
MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
METHODS = {"skills"}  # "aidlc" removed 2026-10-27 (never wired; AI-DLC dropped)
DEPLOY_EVENTS = {"failed"}   # the dispatcher: a failed run of environments.dev.deployed_by on a merge (#159)
REVIEWERS = {"qa", "security", "sanity"}
OUTBOX_KEYS = ("verdict", "comment", "questions", "sub_items", "stage_request", "owner_question", "route",
               "flags", "waits_for", "close", "comment_on", "plan", "board", "milestone")  # = orgrove.outbox.KEYS
CLOSE_MODES = ("sub_issues", "any_open")   # = orgrove.outbox.CLOSE_MODES
# What a reviewer (QA, Security, Sanity) may never have: they read and judge; the dispatcher makes the moves.
READ_ONLY_KEYS = ("stage_request", "flags", "waits_for", "close", "comment_on", "plan", "board", "milestone")


def _load(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def department(name):
    return _load(ROOT / "departments" / f"{name}.toml")


def project(name):
    return _load(ROOT / "projects" / f"{name}.toml")


def serves(role, project):
    """A department role acts in its `projects` (default: every project)."""
    return project is None or "projects" not in role or project in role["projects"]


def role_for(dept, skill, project=None):
    """The role of `dept` that runs `skill` for `project`, or None when no role serves that pair.
    The dispatcher dispatches the skill name; this is the one place that turns it into a role (#112).
    Without a project (old runs that name none) the first role with that skill answers."""
    return next((name for name, r in dept["roles"].items() if r["skill"] == skill and serves(r, project)), None)


def project_of_repo(repo):
    return next((p["project"] for p in projects() if p["repo"] == repo), None)


def resolve_role(skill, project_id=None, repo=None):
    """The role name behind a skill seen in a run-name or a ledger event, for display and spend.
    Unknown projects and skills come back unchanged: history must stay readable."""
    project_id = project_id or (project_of_repo(repo) if repo else None)
    prof = next((p for p in projects() if p["project"] == project_id), None) or projects()[0]
    return role_for(department(prof["departments"][0]), skill, project_id) or skill


def projects():
    """Every project profile, product first (`order`, default 1, then name)."""
    profs = [_load(path) for path in (ROOT / "projects").glob("*.toml")]
    return sorted(profs, key=lambda p: (p.get("order", 1), p["project"]))


def infra_paths_for_repo(repo):
    """Glob patterns for infrastructure/IAM code in the project that targets this repo."""
    for path in sorted((ROOT / "projects").glob("*.toml")):
        prof = _load(path)
        if prof["repo"] == repo:
            return prof.get("limits", {}).get("infra_paths", [])
    return []


def owners_for_repo(repo):
    """The human owners of whichever project profile targets this repo (for review requests and mentions)."""
    for path in sorted((ROOT / "projects").glob("*.toml")):
        prof = _load(path)
        if prof["repo"] == repo:
            return prof.get("owners", [])
    return []


def skill_name(skill):
    """The `name:` in a skill's frontmatter, or None."""
    path = ROOT / "skills" / skill / "SKILL.md"
    if not path.exists():
        return None
    m = re.search(r"^---\n(.*?)\n---", path.read_text(), re.S)
    found = re.search(r"^name:\s*(\S+)", m.group(1), re.M) if m else None
    return found.group(1) if found else None


def segments(cmd):
    """A shell command as its simple commands (split on && || ; | &, quotes kept), each a token list:
    what a Bash(...) rule is matched against."""
    lex = shlex.shlex(cmd.replace("{scope}", "SCOPE"), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    out, cur = [], []
    for tok in lex:
        if tok in ("&&", "||", ";", "|", "&"):
            out, cur = out + [cur] if cur else out, []
        else:
            cur.append(tok)
    return out + [cur] if cur else out


def bash_rule(tool):
    """Bash(gh issue:*) -> (["gh", "issue"], True); Bash(ls) -> (["ls"], False); anything else -> None."""
    m = re.fullmatch(r"Bash\((.+?)(:\*)?\)", tool)
    return (m.group(1).split(), bool(m.group(2))) if m else None


def blocked(cmd, tools):
    """The parts of cmd that no Bash(...) rule in tools allows: the agent would be refused them."""
    rules = [r for r in map(bash_rule, tools) if r]
    return [" ".join(seg) for seg in segments(cmd)
            if not any(seg[:len(words)] == words if prefix else seg == words for words, prefix in rules)]


def rule_for(part):
    """The narrowest useful rule that allows one blocked part: Bash(python -m pytest:*), Bash(pytest:*)."""
    words = part.split()
    return f"Bash({' '.join(words[:3])}:*)" if len(words) > 2 and words[1] == "-m" else f"Bash({words[0]}:*)"


def validate_department(dept):
    errors = []
    cfg = stages.load(ROOT / dept["stages"])
    stage_ids = set(stages.stage_ids(cfg))
    flags = set(cfg["flags"])
    run_max = budget.load()["max_run_ceiling_usd"]
    for scope, value in [("department", dept.get("break_min", 15))] + [
        (role, r["break_min"]) for role, r in dept["roles"].items() if "break_min" in r
    ]:
        if not isinstance(value, int) or not 0 <= value <= 240:
            errors.append(f"{scope}: break_min must be whole minutes between 0 and 240")
    if "merge" not in dept["human_gates"]:
        errors.append("engineering departments must gate merge on a human")
    share = 0.0
    known = {p["project"] for p in projects()}
    seen = {}
    for role, r in dept["roles"].items():
        share += r["budget_share"]
        if "projects" in r:
            if not isinstance(r["projects"], list) or not r["projects"]:
                errors.append(f"{role}: projects must be a non-empty list of project ids")
            for pr in r["projects"] if isinstance(r["projects"], list) else []:
                if pr not in known:
                    errors.append(f"{role}: projects names unknown project {pr!r}")
        for pr in (r["projects"] if isinstance(r.get("projects"), list) else sorted(known)):
            if (r["skill"], pr) in seen:
                errors.append(f"{role}: {seen[(r['skill'], pr)]} already serves skill {r['skill']} for {pr}")
            seen[(r["skill"], pr)] = role
        if skill_name(r["skill"]) != f"orgrove-{r['skill']}":
            errors.append(f"{role}: skills/{r['skill']}/SKILL.md missing or misnamed")
        if r["model"] not in MODELS:
            errors.append(f"{role}: unknown model {r['model']}")
        if r["method"] not in METHODS:
            errors.append(f"{role}: unknown method {r['method']}")
        if role in REVIEWERS:
            writes = {"Edit", "Write", "Bash(git:*)", "Bash(git push:*)"} & set(r["tools"])
            if r["contents"] != "read" or writes:
                errors.append(f"{role}: reviewers must be read-only (contents={r['contents']}, tools {sorted(writes)})")
        outbox = r.get("outbox")
        if not isinstance(outbox, list) or set(outbox) - set(OUTBOX_KEYS):
            errors.append(f"{role}: outbox must list keys from {list(OUTBOX_KEYS)}")
        else:
            barred = READ_ONLY_KEYS if role in REVIEWERS else ()
            for key in barred:
                if key in outbox:
                    errors.append(f"{role}: reviewers may not have {key} in their outbox")
            policy = r.get("flags", {})
            if "flags" in outbox:
                if not isinstance(policy, dict) or not policy or set(policy) - {"add", "remove"} or not all(
                        isinstance(v, list) and all(x in flags for x in v) for v in policy.values()):
                    errors.append(f"{role}: flags must be a table of add/remove lists of labels from [flags] in stages.toml")
            elif "flags" in r:
                errors.append(f"{role}: has a flags policy but no flags in its outbox")
            if "close" in outbox:
                if r.get("close") not in CLOSE_MODES:
                    errors.append(f"{role}: close must be one of {list(CLOSE_MODES)}")
            elif "close" in r:
                errors.append(f"{role}: has a close policy but no close in its outbox")
        if not 0 < r["ceiling_usd"] <= run_max:
            errors.append(f"{role}: ceiling ${r['ceiling_usd']} outside (0, ${run_max}]")
        for trig in r["on"]:
            kind, _, value = trig.partition(":")
            ok = (
                (kind == "stage" and value in stage_ids)
                or (kind == "label" and (value in flags or value.startswith("area/")))
                or (kind == "deploy" and value in DEPLOY_EVENTS)
            )
            if not ok:
                errors.append(f"{role}: unknown trigger {trig!r}")
    if share > 1.0 + 1e-9:
        errors.append(f"budget shares add up to {share:.2f} > 1")
    return errors


def validate_project(prof):
    errors = []
    for d in prof["departments"]:
        if not (ROOT / "departments" / f"{d}.toml").exists():
            errors.append(f"unknown department {d}")
            continue
        dept = department(d)
        missing = set(dept["human_gates"]) - set(prof["human_gates"])
        if missing:
            errors.append(f"project drops human gates required by {d}: {sorted(missing)}")
        # The agents that run the project's commands must be allowed to (#159: init's pytest default was refused).
        refused = {}
        for role, r in dept["roles"].items():
            if r.get("runs_commands"):
                for name, cmd in prof.get("commands", {}).items():
                    if name != "setup" and isinstance(cmd, str):  # the runner runs setup, not an agent
                        for part in blocked(cmd, r["tools"] + prof.get("tools", [])):
                            refused.setdefault((name, part), []).append(role)
        for (name, part), roles in refused.items():
            errors.append(f"commands.{name}: the {'/'.join(roles)} agents may not run {part!r}; "
                          f"add {rule_for(part)} to the project's tools")
    if not prof.get("owners"):
        errors.append("owners must name at least one human")
    if not prof.get("rules"):
        errors.append("rules must list the project's instruction files")
    if "{scope}" not in prof["commands"]["test"]:
        errors.append("commands.test must take {scope} (scoped runs, not full sweeps)")
    contract = prof["commands"].get("contract")
    if not isinstance(contract, str) or not contract.strip() or "{scope}" in contract:
        errors.append("commands.contract must be a fixed command for the repo-wide invariant tests (#151)")
    for env, e in prof.get("environments", {}).items():
        if not e["url"].startswith("https://"):
            errors.append(f"environment {env} url must be https")
        if not re.fullmatch(r"[\w.-]+\.ya?ml", str(e.get("deployed_by", ""))):
            errors.append(f"environment {env} needs deployed_by: the project's deploy workflow file (e.g. deploy.yml), "
                          "so tickets wait for the deploy of their merge")
    for tool in prof.get("tools", []):
        rule = bash_rule(tool)
        if not rule or rule[0][0] == "git":
            errors.append(f"tools: {tool!r} is not allowed: only Bash(...) rules for the project's own commands, never git")
    if not prof.get("limits", {}).get("forbidden"):
        errors.append("limits.forbidden must not be empty")
    ft = prof.get("free_time")
    if ft is not None and (not isinstance(ft.get("issue"), int) or ft["issue"] not in prof.get("skip_issues", [])):
        errors.append("free_time.issue must be a ticket number listed in skip_issues (never planned as work)")
    errors += stage_modes.validate_profile(stages.load(), prof)
    review = prof.get("review", {})
    if not isinstance(review.get("external_prs"), bool):
        errors.append("review.external_prs must be set to true or false")
    elif review["external_prs"]:
        # Defined so it can be switched on later; refused until it really reviews (no placeholder toggles).
        errors.append("review.external_prs = true is not built yet (orgrove#82); set it to false")
    return errors
