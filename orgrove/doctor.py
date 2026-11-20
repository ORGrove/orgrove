"""Is this install ready? Read-only checks, each with the fix (#159 U4).

    python -m orgrove.doctor [--tick]

Checks this machine (Python, gh, git), the config (config/org.toml, projects/*.toml, departments, budget), the
control repo on GitHub (workflows, the ledger branch, the kill switch, the App ID, the App key and the Anthropic
API key), the GitHub App (installed on every repo it needs, with the permissions the workflows ask for) and each
project (repo, pipeline labels, board). --tick also runs one dispatcher decision in dry-run mode (reads only).

It never writes anything. Anything it cannot verify is "not checked", which fails like an error: unverified is
not a pass. Exit 0 only when every check is ok.
"""
import argparse
import base64
import json
import re
import shutil
import subprocess
import sys

from orgrove import brand, budget, editions, licence, manifest, org, stages

# What the workflows ask the App for (create-github-app-token permission-* in dispatch, role-run, retro).
# Optional ones only feed reports: their absence is said, not failed.
APP_PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write", "actions": "read",
                   "checks": "read", "organization_projects": "write"}
# Optional, each for one report: GitHub billing (finance); Dependabot alerts (ops-health, the publisher's only:
# not in a client copy, which then doesn't ask for it).
APP_OPTIONAL = ({"organization_administration": "read"}   # billing: the wiki and finance, ORGrove Pro's
                if editions.has("wiki") or editions.has("finance") else {}) | (
    {"vulnerability_alerts": "read"} if (manifest.ROOT / "orgrove" / "ops_health.py").exists() else {})
# The read-only App the agent job mints its token from (#41, #262): contents write only so Dev can push its branch;
# it may never write the tracker (issues, pull requests), checks or actions.
READ_APP_PERMISSIONS = {"contents": "write", "issues": "read", "pull_requests": "read", "actions": "read",
                        "checks": "read"}
READ_APP_NO_WRITE = ("issues", "pull_requests", "actions", "checks")
LEVEL = {"read": 1, "write": 2, "admin": 3}
WORKFLOWS = ("dispatch.yml", "role-run.yml")
SECRETS = ("ORGROVE_APP_PRIVATE_KEY", "ANTHROPIC_API_KEY")
# A Claude subscription login: not allowed for a third-party product (Anthropic's Agent SDK terms, #159).
SUBSCRIPTION_SECRETS = ("CLAUDE_CODE_OAUTH_TOKEN",)


def gh(*args):
    """The default runner: gh, JSON out when there is any. Returns (ok, parsed output or stderr text)."""
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if out.returncode:
        return False, (out.stderr or out.stdout).strip()
    text = out.stdout.strip()
    try:
        return True, json.loads(text) if text else {}
    except json.JSONDecodeError:
        return True, text


def runs_on_agent_branches(workflow):
    """True when a workflow's push trigger covers the agents' branches (agent/<n>-...). QA and Security start only
    once CI on the agent's head is green (#151), so a project without such CI waits for ever (#159 sandbox)."""
    on = re.search(r"^on:[ \t]*(.*?)(?=^\S|\Z)", workflow, re.S | re.M)
    if not on:
        return False
    head, *rest = on.group(1).split("\n")
    if "push" in head:                                   # on: push / on: [push, pull_request]
        return True
    lines = [line for line in rest if line.strip() and not line.strip().startswith("#")]
    for i, line in enumerate(lines):
        if re.match(r"\s+push:", line):
            indent = len(line) - len(line.lstrip())
            block = []
            for nxt in lines[i + 1:]:
                if len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                block.append(nxt)
            text = "\n".join(block)
            if "branches-ignore" in text:
                return "agent/" not in text
            if "branches:" not in text:
                return True                              # every branch
            return "agent/" in text or bool(re.search(r"""['"]\*\*['"]""", text))
    return False


TRACKER_PERMISSIONS = ("permission-issues", "permission-pull-requests")
# The only secrets the agent job may reference (#262): the model key, and the key of the read-only App. Anything a
# prompt-injected run can read in that job (Runner.Worker memory) must be unable to mint tracker write.
AGENT_JOB_SECRETS = ("ANTHROPIC_API_KEY", "ORGROVE_READ_APP_PRIVATE_KEY")


def real_installations(listing):
    """The records in GitHub's `orgs/<owner>/installations` answer. It can list an installation as null (2026-11-10: three
    nulls for Apps the owner had deleted), which is not an installation of anything and must not crash a lookup."""
    found = listing.get("installations", []) if isinstance(listing, dict) else []
    return [i for i in found if isinstance(i, dict)]


def _jobs(workflow):
    """job id -> its text, from the top-level `jobs:` block (2-space job keys)."""
    m = re.search(r"^jobs:[ \t]*\n(.*)", workflow, re.S | re.M)
    parts = re.split(r"^  (?=[\w-]+:[ \t]*$)", m.group(1), flags=re.M) if m else []
    return {p.split(":", 1)[0]: p for p in parts if re.match(r"[\w-]+:", p)}


def agent_job_tracker_writes(workflow):
    """What lets the job that runs the model write to the tracker (#41). [] means the agent job is least-privilege.
    It fails closed: what it can't prove read-only counts as write. The agent job is the one that runs `claude`.
    - It references no secret but AGENT_JOB_SECRETS, and every `private-key:` is the read-only App's (#262).
    - It declares its own job-level `permissions:` (else it inherits the workflow's), with no write-all and no
      issues/pull-requests write.
    - Every App token it mints sets permission-issues and permission-pull-requests to read or none (a token with
      no such input gets every permission of the installation).
    - Every GH_TOKEN/GITHUB_TOKEN (step or job level) is one of those read-only tokens: never a secret,
      `github.token` or any other expression."""
    agents = {k: t for k, t in _jobs(workflow).items() if re.search(r"^\s*(?:timeout \S+ )?claude -p\b", t, re.M)}
    if not agents:
        return ["no job runs the agent (`claude -p`)"]
    problems = []
    for job, text in agents.items():
        head = re.split(r"^    steps:", text, flags=re.M)[0]
        perms = re.search(r"^    permissions:[ \t]*(.*)\n((?:      .*\n|[ \t]*\n)*)", head + "\n", re.M)
        if not perms:
            problems.append(f"job {job}: no job-level permissions, so it inherits the workflow's")
        else:
            inline, block = perms.group(1).split("#")[0].strip(), perms.group(2)
            if inline and inline not in ("{}", "read-all"):
                problems.append(f"job {job}: job-level permissions are `{inline}`")
            if re.search(r"^\s+(?:issues|pull-requests):\s*(?!read\b|none\b)\S", block, re.M):
                problems.append(f"job {job}: job-level permissions grant issues or pull-requests write")
        # A closed allowlist: every mention of `secrets` must be one of the allowed names, so a renamed tracker key,
        # `secrets['X']`, `toJSON(secrets)` and `secrets: inherit` all fail. Comments are not exempt (fail closed).
        left = re.sub(r"\bsecrets\.(\w+)", lambda m: "" if m.group(1).upper() in AGENT_JOB_SECRETS else m.group(0), text,
                      flags=re.I)
        for name in sorted({m.upper() for m in re.findall(r"\bsecrets\.(\w+)", left, re.I)}):
            problems.append(f"job {job}: it references secrets.{name}, which is not one of {', '.join(AGENT_JOB_SECRETS)}")
        if re.search(r"\bsecrets\b(?!\.\w)", left, re.I):
            problems.append(f"job {job}: it references `secrets` in a form the check can't tie to an allowed name")
        for key in re.findall(r"^\s*private-key:\s*(.*?)\s*$", text, re.M):
            if not re.fullmatch(r"\$\{\{\s*secrets\.ORGROVE_READ_APP_PRIVATE_KEY\s*\}\}", key, re.I):
                problems.append(f"job {job}: a token is minted with private-key {key}, not the read-only App's key")
        steps = re.split(r"^      - ", text, flags=re.M)[1:]
        readers = set()
        for st in steps:
            if "create-github-app-token" not in st:
                continue
            ident = re.search(r"^\s+id:\s*(\S+)", st, re.M)
            name = ident.group(1) if ident else "?"
            ok = True
            for key in TRACKER_PERMISSIONS:
                v = re.search(rf"^\s+{key}:\s*(.+?)\s*$", st, re.M)
                if not v:
                    problems.append(f"job {job}: the token step {name} sets no {key}, so it gets every permission")
                    ok = False
                elif v.group(1).strip("'\"") not in ("read", "none"):
                    problems.append(f"job {job}: the token step {name} sets {key}: {v.group(1)}")
                    ok = False
            if ok and ident:
                readers.add(name)
        for env in re.findall(r"^\s+(?:GH_TOKEN|GITHUB_TOKEN):\s*(.+?)\s*$", text, re.M):
            used = re.fullmatch(r"\$\{\{\s*steps\.([\w-]+)\.outputs\.token\s*\}\}", env)
            if not (used and used.group(1) in readers):
                problems.append(f"job {job}: a token is passed as {env}, not a read-only App token of this job")
    return problems


def has_keychain_item(service):
    """Whether a macOS Keychain item exists. `security` without -w prints its attributes, never the secret; no
    `security` at all (Linux) means no item (CI on Linux, #159)."""
    try:
        return subprocess.run(["security", "find-generic-password", "-s", service],
                              capture_output=True, check=False).returncode == 0
    except FileNotFoundError:
        return False


class _Done:
    """subprocess.CompletedProcess's three fields, from the (ok, out, err) the floor's setup code uses."""
    def __init__(self, ok, out, err):
        self.returncode, self.stdout, self.stderr = 0 if ok else 1, out, err


class Doctor:
    def __init__(self, run=gh, which=shutil.which, version=sys.version_info, keychain=None, role_run=None):
        self.run, self.which, self.version = run, which, version
        self.role_run = role_run   # role-run.yml's text; read from this checkout when None
        # Whether a macOS Keychain item exists: `security` without -w prints its attributes, never the secret.
        self.keychain = keychain or has_keychain_item
        self.results = []   # (status, area, message, fix); status: ok | fail | unchecked | warn

    def note(self, status, area, message, fix=""):
        self.results.append((status, area, message, fix))

    def failed(self):
        return any(s in ("fail", "unchecked") for s, *_ in self.results)

    # --- this machine --------------------------------------------------------------------------------------

    def python(self):
        v = self.version
        if (v[0], v[1]) >= (3, 11):
            self.note("ok", "machine", f"Python {v[0]}.{v[1]}")
        else:
            self.note("fail", "machine", f"Python {v[0]}.{v[1]} is too old", "install Python 3.11 or newer")

    def machine(self):
        self.python()
        for tool, fix in (("git", "install git"), ("gh", "install the GitHub CLI: https://cli.github.com")):
            if self.which(tool):
                self.note("ok", "machine", f"{tool} found")
            else:
                self.note("fail", "machine", f"{tool} not found", fix)
        ok, out = self.run("api", "user", "--jq", ".login")
        if ok:
            self.note("ok", "machine", f"gh is logged in as {out}")
        else:
            self.note("fail", "machine", "gh is not logged in", "gh auth login")
        # macOS: Office meetings (ORGrove Pro) read this install's own Anthropic key (#159)
        if self.which("security") and editions.has("meetings"):
            try:
                service = org.keychain_service()
            except (OSError, KeyError, ValueError):
                service = None
            if service and self.keychain(service):
                self.note("ok", "machine", f"meeting key in the Keychain ({service})")
            elif service:
                self.note("warn", "machine", f"no meeting key in the Keychain ({service}): Office meetings won't start",
                          f"security add-generic-password -s {service} -a orgrove -w   (paste an Anthropic API key)")

    # --- config --------------------------------------------------------------------------------------------

    def config(self):
        try:
            self.github = org.load()
            if self.github["owner"] == org.PLACEHOLDER:   # a fresh copy: nothing to check until init has run
                self.note("warn" if self.config_only else "fail", "config", "not set up yet: config/org.toml is the template",
                          "python3 -m orgrove.init --apply")
                self.github, self.profiles = None, []
                return
            self.note("ok", "config", f"config/org.toml [github]: {self.github['repo']}, App {self.github['app_slug']}")
        except (ValueError, FileNotFoundError, KeyError) as e:
            self.github = None
            self.note("fail", "config", f"config/org.toml [github]: {e}", "run python -m orgrove.init, or fix the file")
        try:
            b = brand.load()
            self.note("ok", "config", f"[brand]: {b.get('client_name', 'ORGrove only')}")
        except ValueError as e:
            self.note("fail", "config", f"config/org.toml [brand]: {e}", "fix the [brand] table")
        self.profiles = []
        for path in sorted((manifest.ROOT / "projects").glob("*.toml")):
            try:
                prof = manifest._load(path)
                errors = manifest.validate_project(prof)
            except (KeyError, TypeError, ValueError) as e:
                errors, prof = [f"unreadable: {e}"], None
            if errors:
                self.note("fail", "config", f"projects/{path.name}: " + "; ".join(errors), "fix the project file")
            else:
                self.profiles.append(prof)
                self.note("ok", "config", f"projects/{path.name}: {prof['repo']}")
        if not self.profiles and not any(r[1] == "config" and "projects/" in r[2] for r in self.results):
            self.note("fail", "config", "no project in projects/", "run python -m orgrove.init")
        for name in sorted({d for p in self.profiles for d in p["departments"]}):
            errors = manifest.validate_department(manifest.department(name))
            if errors:
                self.note("fail", "config", f"departments/{name}.toml: " + "; ".join(errors), "fix the department file")
            else:
                self.note("ok", "config", f"departments/{name}.toml")
        caps = budget.load()
        errors = budget.validate(caps)
        if not errors:
            self.note("ok", "config", f"budget: ${caps['daily_usd']:g}/day, ${caps['monthly_usd']:g}/month")
            self.note("warn", "config", f"set a monthly spend limit of about ${caps['monthly_usd']:g} on the Anthropic "
                                        "Console too (the second, provider-side cap)", "platform.claude.com > Settings > Limits")
        else:
            self.note("fail", "config", "config/budget.toml: " + "; ".join(errors), "edit config/budget.toml")

    def licence(self):
        """The Pro licence (orgrove/licence.py, #426). Silent in Community and in ORGrove's own source checkout."""
        if not any(editions.installed(p) for p in editions.PARTS):
            return
        st = licence.status()
        if st.state == "source":
            return
        fix = f"write to {licence.CONTACT} for a licence file, and put it at {licence.FILE}"
        if st.state == "valid":
            parts = "every Pro part" if st.parts == "all" else ", ".join(st.parts)
            self.note("ok" if st.days_left > 30 else "warn", "licence", f"{st.why}: {parts}",
                      "" if st.days_left > 30 else f"renew before {st.expires}: {fix}")
        elif st.state == "grace":
            self.note("warn", "licence", st.why, fix)
        elif st.state == "invalid":
            self.note("fail", "licence", st.why, f"use the {licence.FILE} you were sent, unchanged, or {fix}")
        else:                                   # missing, expired, early: the Pro parts are off, the agents run
            self.note("warn", "licence", f"{st.why}; the agents and the classic Office keep working", fix)

    # --- the control repo ----------------------------------------------------------------------------------

    def control_repo(self):
        if not self.github:
            self.note("unchecked", "github", "control repo: config/org.toml has no valid [github]")
            return
        repo = self.github["repo"]
        ok, out = self.run("api", f"repos/{repo}", "--jq", '"\\(.full_name) \\(.has_wiki)"')
        if not ok:
            self.note("fail", "github", f"{repo} is not reachable: {out[:120]}", "create it, or fix [github] repo")
            return
        self.note("ok", "github", f"{repo} is reachable")
        # a free organisation has no wikis on private repos (#159); the wiki is ORGrove Pro's (orgrove/editions.py)
        if str(out).split()[-1:] == ["false"] and editions.has("wiki"):
            self.note("warn", "github", f"{repo} has no wiki: the nightly daily page and owner to-do are not published",
                      "Settings > Features > Wikis, then create the first page (private repos need a paid GitHub plan)")
        for wf in WORKFLOWS:
            ok, out = self.run("api", f"repos/{repo}/contents/.github/workflows/{wf}", "--jq", ".name")
            self.note("ok" if ok else "fail", "github", f"workflow {wf}" + ("" if ok else " is missing"),
                      "" if ok else "copy .github/workflows from the ORGrove release into the control repo")
        need = set(stages.labels(stages.load(), "control"))
        ok, out = self.run("label", "list", "-R", repo, "--limit", "500", "--json", "name")
        missing = sorted(need - {x["name"] for x in out}) if ok else sorted(need)
        self.note("ok" if not missing else "fail", "github",
                  f"all {len(need)} control-repo labels" if not missing else f"{len(missing)} control-repo labels missing "
                  f"({', '.join(missing)})", "" if not missing else f"python -m orgrove.sync_labels {repo} --control")
        ok, out = self.run("api", f"repos/{repo}/branches/ledger", "--jq", ".name")
        self.note("ok" if ok else "fail", "github", "ledger branch" + ("" if ok else " is missing"),
                  "" if ok else "python -m orgrove.init --apply (creates an empty ledger branch)")
        ok, variables = self.run("variable", "list", "-R", repo, "--json", "name,value")
        if not ok:
            self.note("unchecked", "github", f"repo variables: {variables[:120]}", "needs admin on the repo")
        else:
            v = {x["name"]: x["value"] for x in variables}
            self.note("ok" if v.get("ORGROVE_APP_ID", "").isdigit() else "fail", "github",
                      "variable ORGROVE_APP_ID" + ("" if v.get("ORGROVE_APP_ID", "").isdigit() else " is missing"),
                      "" if v.get("ORGROVE_APP_ID", "").isdigit() else f"gh variable set ORGROVE_APP_ID -R {repo} --body <App ID>")
            switch = v.get("ORGROVE_ENABLED")
            if switch == "true":
                self.note("ok", "github", "kill switch ORGROVE_ENABLED=true (agents run)")
            elif switch == "false":
                self.note("warn", "github", "kill switch ORGROVE_ENABLED=false: agents are stopped",
                          f"gh variable set ORGROVE_ENABLED -R {repo} --body true")
            else:
                self.note("fail", "github", "kill switch ORGROVE_ENABLED is not set",
                          f"gh variable set ORGROVE_ENABLED -R {repo} --body false   (true when ready)")
        ok, secrets = self.run("secret", "list", "-R", repo, "--json", "name")
        if not ok:
            self.note("unchecked", "github", f"repo secrets: {secrets[:120]}", "needs admin on the repo")
            return
        names = {x["name"] for x in secrets}
        for s in SECRETS:
            self.note("ok" if s in names else "fail", "github", f"secret {s}" + ("" if s in names else " is missing"),
                      "" if s in names else f"gh secret set {s} -R {repo}")
        for s in SUBSCRIPTION_SECRETS:
            if s in names:
                self.note("fail", "github", f"secret {s} is a Claude subscription login: a third-party product must use "
                                            "an Anthropic API key", f"gh secret delete {s} -R {repo}; set ANTHROPIC_API_KEY")

    # --- the GitHub App ------------------------------------------------------------------------------------

    def app(self):
        if not self.github:
            return
        slug, owner = self.github["app_slug"], self.github["owner"]
        ok, out = self.run("api", f"orgs/{owner}/installations")
        if not ok:
            self.note("unchecked", "app", f"App installations on {owner}: {out[:120]}",
                      "gh auth refresh -s admin:org (or ask an org owner to run doctor)")
            return
        inst = next((i for i in real_installations(out) if i.get("app_slug") == slug), None)
        if not inst:
            self.note("fail", "app", f"App {slug} is not installed on {owner}", f"install it: https://github.com/apps/{slug}")
            return
        self.note("ok", "app", f"App {slug} is installed on {owner}")
        perms = inst.get("permissions", {})
        for name, need in APP_PERMISSIONS.items():
            have = perms.get(name)
            good = LEVEL.get(have, 0) >= LEVEL[need]
            self.note("ok" if good else "fail", "app", f"permission {name}: {have or 'none'} (needs {need})",
                      "" if good else f"App settings > Permissions: {name} = {need}, then accept on the installation")
        for name, need in APP_OPTIONAL.items():
            if LEVEL.get(perms.get(name), 0) < LEVEL[need]:
                self.note("warn", "app", f"optional permission {name} ({need}) is off: the report that uses it says "
                                         "\"not measured\"", f"App settings > Permissions: {name} = {need}")
        wanted = {self.github["repo"]} | {p["repo"] for p in self.profiles}
        if inst.get("repository_selection") == "all":
            self.note("ok", "app", "App can reach every repo of the owner")
            return
        # GitHub lists an installation's repos only to a token the App issued, never to a gh login. The evidence a
        # login can read: the dispatcher mints an App token for exactly these repos, and that step fails if one is
        # not in the installation. A successful recent run is proof; anything else is not checked.
        page = f"https://github.com/organizations/{owner}/settings/installations/{inst['id']}"
        ok, runs = self.run("run", "list", "-R", self.github["repo"], "--workflow", "dispatch.yml", "--status", "success",
                            "--limit", "1", "--json", "databaseId,createdAt,headSha")
        if ok and runs:
            r = runs[0]
            self.note("ok", "app", f"App reaches {', '.join(sorted(wanted))}: dispatcher run {r['databaseId']} "
                                   f"({r['createdAt'][:16]}) minted its token for them")
        else:
            self.note("unchecked", "app", f"repos the App can reach ({', '.join(sorted(wanted))}): no successful "
                                          "dispatcher run yet to prove it",
                      f"confirm on {page}, then: gh workflow run dispatch.yml -R {self.github['repo']} -f dry_run=true"
                      "   (reads only; runs with the kill switch off)")

    def read_app(self):
        """The read-only App the agent job runs as: its variable and secret are set, it is installed on the owner,
        and it holds no write on the tracker (#41). Without it the agent job has no token to mint."""
        if not self.github:
            return
        repo, owner = self.github["repo"], self.github["owner"]
        ok, variables = self.run("variable", "list", "-R", repo, "--json", "name,value")
        ok2, secrets = self.run("secret", "list", "-R", repo, "--json", "name")
        if not (ok and ok2):
            self.note("unchecked", "read-app", "the read-only App's variable and secret", "needs admin on the repo")
            return
        app_id = {x["name"]: x["value"] for x in variables}.get("ORGROVE_READ_APP_ID", "")
        if not app_id.isdigit():
            self.note("fail", "read-app", "variable ORGROVE_READ_APP_ID is missing: the agent job has no token to use",
                      "run ./install again (it creates the read-only App), or see INSTALL.md B3")
            return
        if "ORGROVE_READ_APP_PRIVATE_KEY" not in {x["name"] for x in secrets}:
            self.note("fail", "read-app", "secret ORGROVE_READ_APP_PRIVATE_KEY is missing",
                      f"gh secret set ORGROVE_READ_APP_PRIVATE_KEY -R {repo} < <the read-only App's .pem>")
            return
        ok, out = self.run("api", f"orgs/{owner}/installations")
        if not ok:
            self.note("unchecked", "read-app", f"App installations on {owner}: {out[:120]}",
                      "gh auth refresh -s admin:org (or ask an org owner to run doctor)")
            return
        inst = next((i for i in real_installations(out) if str(i.get("app_id")) == app_id), None)
        if not inst:
            self.note("fail", "read-app", f"the read-only App (ID {app_id}) is not installed on {owner}",
                      "install it on the control repo and each project repo")
            return
        perms = inst.get("permissions", {})
        bad = [f"{n}: {perms[n]}" for n in READ_APP_NO_WRITE if LEVEL.get(perms.get(n), 0) > LEVEL["read"]]
        low = [n for n, need in READ_APP_PERMISSIONS.items() if LEVEL.get(perms.get(n), 0) < LEVEL[need]]
        if bad:
            self.note("fail", "read-app", f"read-only App {inst.get('app_slug')} can write the tracker ({', '.join(bad)})",
                      "App settings > Permissions: set these to read, then accept on the installation")
        elif low:
            self.note("fail", "read-app", f"read-only App {inst.get('app_slug')} lacks {', '.join(low)}",
                      "App settings > Permissions, then accept on the installation")
        else:
            self.note("ok", "read-app", f"read-only App {inst.get('app_slug')} is installed and cannot write the tracker")

    # --- each project --------------------------------------------------------------------------------------

    def projects(self):
        need = set(stages.labels(stages.load()))
        for prof in self.profiles:
            repo, pid = prof["repo"], prof["project"]
            ok, out = self.run("label", "list", "-R", repo, "--limit", "500", "--json", "name")
            if not ok:
                self.note("fail", pid, f"{repo} labels not readable: {out[:120]}", "check the repo name and your access")
                continue
            missing = sorted(need - {x["name"] for x in out})
            self.note("ok" if not missing else "fail", pid,
                      f"{repo}: all {len(need)} labels" if not missing else f"{repo}: {len(missing)} labels missing ({', '.join(missing[:4])}…)",
                      "" if not missing else f"python -m orgrove.sync_labels {repo}")
            self.project_ci(pid, repo)
            self.project_notify(pid, repo)
            for env, e in (prof.get("environments") or {}).items():
                wf = e.get("deployed_by")
                ok, _ = self.run("api", f"repos/{repo}/contents/.github/workflows/{wf}", "--jq", ".name")
                self.note("ok" if ok else "fail", pid,
                          f"{repo}: {env} is deployed by {wf}" if ok else f"{repo}: deploy workflow {wf} ({env}) not found",
                          "" if ok else f"set environments.{env}.deployed_by to the workflow that deploys {env}")
            board = prof.get("board")
            if not board:
                self.note("warn", pid, "no board in the project file: the Office roadmap stays empty")
                continue
            ok, out = self.run("project", "field-list", str(board["number"]), "--owner", board["owner"], "--format", "json")
            if not ok:
                self.note("fail", pid, f"board {board['owner']}#{board['number']} not readable: {out[:120]}",
                          "gh auth refresh -s project (or fix board in the project file)")
                continue
            have = {f["name"] for f in out.get("fields", [])}
            want = set(prof.get("board_fields", {"status": "Status"}).values())
            missing = sorted(want - have)
            self.note("ok" if not missing else "fail", pid,
                      f"board {board['owner']}#{board['number']}: fields {', '.join(sorted(want))}" if not missing
                      else f"board fields missing: {', '.join(missing)}", "" if not missing else "add the fields on the board")

    def project_ci(self, pid, repo):
        fix = (f"add a workflow to {repo} that runs your tests on push to 'agent/**' branches "
               "(INSTALL.md, By hand, step A)")
        ok, paths = self.run("api", f"repos/{repo}/contents/.github/workflows", "--jq", ".[].path")
        if not ok:
            self.note("fail", pid, f"{repo} has no CI workflow: QA and Security would wait for ever", fix)
            return
        for path in str(paths).split():
            ok, content = self.run("api", f"repos/{repo}/contents/{path}", "--jq", ".content")
            try:
                text = base64.b64decode(str(content)).decode("utf-8", "replace") if ok else ""
            except ValueError:  # binascii.Error: not a file's content
                text = ""
            if runs_on_agent_branches(text):
                self.note("ok", pid, f"{repo}: CI runs on agent branches ({path})")
                return
        self.note("fail", pid, f"{repo}: no workflow runs on push to agent branches: QA and Security would wait for ever",
                  fix)

    def project_notify(self, pid, repo):
        """templates/orgrove-notify.yml in the project repo, with the notify App's id, key and the control repo's
        name: a person's ticket, comment or merge starts a tick at once (#159)."""
        guide = "copy templates/orgrove-notify.yml into it as .github/workflows/orgrove-notify.yml (INSTALL.md, By hand, step B2)"
        if self.github and repo == self.github["repo"]:  # a project in the control repo: dispatch.yml starts on its events
            self.note("ok", pid, f"{repo}: a person's ticket, comment or merge starts the dispatcher (control repo)")
            return
        ok, _ = self.run("api", f"repos/{repo}/contents/.github/workflows/orgrove-notify.yml", "--jq", ".name")
        if not ok:
            if not org.owner_checks():  # the install has its own timer ([wake] owner_checks = false; an AWS heartbeat)
                self.note("warn", pid, f"{repo} has no orgrove-notify workflow: your own timer starts its ticks", guide)
            else:
                self.note("fail", pid, f"{repo} has no orgrove-notify workflow: new tickets wait for GitHub's schedule", guide)
            return
        control = self.github["repo"].split("/")[1] if self.github else None
        ok, variables = self.run("variable", "list", "-R", repo, "--json", "name,value")
        if not ok:
            self.note("unchecked", pid, f"{repo} variables: {str(variables)[:120]}", "needs admin on the repo")
        else:
            v = {x["name"]: x["value"] for x in variables}
            if not v.get("ORGROVE_NOTIFY_APP_ID", "").isdigit() or v.get("ORGROVE_CONTROL_REPO") != control:
                self.note("fail", pid, f"{repo}: notify variables missing or wrong",
                          f"gh variable set ORGROVE_NOTIFY_APP_ID -R {repo} --body <notify App ID>; "
                          f"gh variable set ORGROVE_CONTROL_REPO -R {repo} --body {control}")
                return
        ok, secrets = self.run("secret", "list", "-R", repo, "--json", "name")
        if ok and "ORGROVE_NOTIFY_PRIVATE_KEY" not in {x["name"] for x in secrets}:
            self.note("fail", pid, f"{repo}: secret ORGROVE_NOTIFY_PRIVATE_KEY is missing",
                      f"gh secret set ORGROVE_NOTIFY_PRIVATE_KEY -R {repo} < <the notify App's .pem>")
            return
        ok, runs = self.run("run", "list", "-R", repo, "--workflow", "orgrove-notify.yml", "--status", "success",
                            "--limit", "1", "--json", "databaseId,createdAt")
        if ok and runs:
            self.note("ok", pid, f"{repo}: notify works (run {runs[0]['databaseId']}, {runs[0]['createdAt'][:16]})")
        else:
            self.note("warn", pid, f"{repo}: notify is set up but has not run yet", "open a ticket or comment on one: "
                      "that run proves it (the Actions tab of the project repo shows it)")

    def agent_isolation(self):
        """The agent job holds no tracker write: only `orgrove apply`, in a later job, writes (#41)."""
        text = self.role_run
        if text is None:
            path = manifest.ROOT / ".github" / "workflows" / "role-run.yml"
            if not path.exists():
                self.note("warn", "isolation", "no role-run.yml in this checkout: agent token scope not checked")
                return
            text = path.read_text()
        problems = agent_job_tracker_writes(text)
        if problems:
            self.note("fail", "isolation", "the agent job can write to the tracker: " + "; ".join(problems),
                      "split role-run.yml into an agent job (read-only token) and an apply job (docs/design.md, Outbox)")
        else:
            self.note("ok", "isolation", "the agent job holds no issue or pull-request write token")

    def tick(self):
        out = subprocess.run([sys.executable, "-m", "orgrove.dispatch", "--dry-run"], capture_output=True, text=True,
                             cwd=manifest.ROOT, check=False)
        decided = [line for line in out.stdout.splitlines() if line.startswith("== ") and "action(s)" in line]
        if out.returncode == 0 and decided:
            self.note("ok", "tick", "dispatcher dry run: " + "; ".join(d.strip("= ") for d in decided))
        else:
            self.note("fail", "tick", f"dispatcher dry run failed: {(out.stderr or out.stdout).strip()[-200:]}",
                      "read the error above; doctor's earlier failures usually explain it")

    # --- the Monitoring floor (Pro) -------------------------------------------------------------------------

    def monitoring(self, aws_run=None):
        """When the floor is on and has an AWS profile: the login works, the profile is not an administrator, the reads work.
        Only reads. Silent when the floor is off or this copy has no Monitoring floor."""
        if not editions.has("monitoring"):
            return
        from orgrove import awsread, monitoring, monitor_setup
        try:
            c = monitoring.load()
        except (OSError, ValueError) as e:
            self.note("fail", "floor 2", f"config/monitoring.toml: {e}", "fix the line it names")
            return
        accounts = awsread.accounts_of(c["aws"])
        if not c["enabled"] or not accounts:
            return
        run = aws_run or monitor_setup.sh
        as_run = lambda cmd, **kw: _Done(*run(cmd))
        for ac in accounts:
            profile = ac["profile"]
            area = "floor 2" if len(accounts) == 1 else f"floor 2 {ac['account']}"
            ok, arn, err = run(["aws", "sts", "get-caller-identity", "--profile", profile, "--query", "Arn", "--output", "text"])
            if not ok:
                self.note("fail", area, f"AWS profile {profile!r}: {monitor_setup.login_hint(err, profile)}",
                          f"aws sso login --profile {profile}" if "sso" in err.lower() or "expired" in err.lower() else "bin/orgrove-monitor-setup")
                continue
            self.note("ok", area, f"AWS profile {profile!r} works ({arn.strip().split('/')[-2] if '/' in arn else arn.strip()})")
            if re.search(r"Administrator|PowerUser|:root|FullAccess", arn):
                self.note("warn", area, f"{profile!r} is an administrator: the floor would read as one",
                          "bin/orgrove-monitor-setup creates a read-only role and points the floor at it")
            checks = [(name, lambda args=args: awsread.run(args, ac, as_run)) for name, args in (
                ("CloudWatch", ["cloudwatch", "list-metrics", "--max-items", "1"]),
                ("CloudTrail", ["cloudtrail", "lookup-events", "--max-results", "1"]),
                ("GuardDuty", ["guardduty", "list-detectors"]))]
            checks.append(("CloudTrail in us-east-1 (IAM and sign-in events)", lambda: awsread.run(["cloudtrail", "lookup-events", "--max-results", "1"], ac, as_run, "us-east-1")))
            checks.append(("Billing (budgets)", lambda: awsread.billing_reading(ac, as_run)))
            checks += [(f"log group {g}", lambda g=g: awsread.run(["logs", "filter-log-events", "--log-group-name", g, "--max-items", "1"], ac, as_run))
                       for g in ac["log_groups"]]
            for name, fn in checks:
                try:
                    fn()
                    self.note("ok", area, f"can read {name}")
                except Exception as e:      # Unreadable carries the reason; anything else is a bug worth showing
                    self.note("fail", area, f"cannot read {name}: {e}", "check the role's permissions (infra/monitor-readonly.yaml) and the names in config/monitoring.toml")

    def check(self, with_tick=False, config_only=False):
        self.config_only = config_only
        if config_only:  # the files alone: no network, no gh login (CI on every push, #159)
            self.python()
            self.config()
            self.licence()          # before init too: a fresh Pro copy says what its licence is (#426)
            self.agent_isolation()   # reads the workflow file only. The packaging gate runs this, so a copy whose
            return self              # workflow the doctor would fail never reaches an install (#41, 2026-11-10)
        self.machine()
        self.config()
        self.licence()              # before init too: a fresh Pro copy says what its licence is (#426)
        self.control_repo()
        self.agent_isolation()
        self.app()
        self.read_app()
        self.projects()
        self.monitoring()
        # Only a real failure skips the dry run: "not checked" lines (the App's reach, proven only by a dispatcher
        # run) must not hide what the dry run finds; the first sandbox tick crashed where it would have shown (#159).
        if with_tick and any(r[0] == "fail" for r in self.results):
            self.note("unchecked", "tick", "dispatcher dry run skipped until the failures above are fixed")
        elif with_tick:
            self.tick()
        return self


def report(results):
    mark = {"ok": "✓", "fail": "✗", "unchecked": "?", "warn": "!"}
    lines = []
    for s, area, msg, fix in results:
        lines.append(f"{mark[s]} {area:10} {msg}")
        if fix and s != "ok":
            lines.append(f"  {'':10} fix: {fix}")
    bad = [r for r in results if r[0] in ("fail", "unchecked")]
    lines.append(f"\nDoctor: {'NOT ready' if bad else 'ready'}: {sum(r[0] == 'ok' for r in results)} ok, "
                 f"{sum(r[0] == 'fail' for r in results)} failed, {sum(r[0] == 'unchecked' for r in results)} not checked, "
                 f"{sum(r[0] == 'warn' for r in results)} to note")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="orgrove.doctor", description=__doc__.split("\n")[0])
    p.add_argument("--tick", action="store_true", help="also run one dispatcher decision in dry-run mode (reads only)")
    p.add_argument("--config", action="store_true", help="only the config files: no network, no gh login (CI)")
    a = p.parse_args(argv)
    d = Doctor().check(with_tick=a.tick, config_only=a.config)
    text = report(d.results)
    print(text.replace("\nDoctor: ", "\nDoctor (config files only): ", 1) if a.config else text)
    return 1 if d.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
