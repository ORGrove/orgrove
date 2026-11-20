"""Set up this copy of ORGrove for your organisation (#159 U3).

    python -m orgrove.init [--answers answers.toml] [--apply] [--force]
    python -m orgrove.init --add-project [--answers answers.toml] [--apply] [--force]

Asks a few questions (or reads them from --answers) and writes config/org.toml, projects/<id>.toml and the caps in
config/budget.toml. Project files of another owner (a copy may carry the publisher's) are moved to
projects/other/, where nothing reads them. With --apply it also creates the pipeline labels in the project repo
and an empty `ledger` branch in this repo; without it, GitHub is not touched. Then it prints the GitHub App
steps, the variables and secrets to set, and `python -m orgrove.doctor` to check everything.

--add-project onboards one more project on an install that already exists (#11): it asks only the project
questions and writes only projects/<id>.toml (config/org.toml and the caps are never touched), after refusing a repo
under another owner, a project or repo already installed, and a profile the validator rejects. The new project
picks after the installed ones (`order`). With --apply it creates the pipeline labels in the new repo only.
"""
import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from orgrove import budget, doctor, editions, manifest, org

QUESTIONS = [
    # key, question, default (str or callable on the answers so far), validator regex
    ("owner", "GitHub organisation (or user) that owns your repos", None, r"[A-Za-z0-9-]{1,39}"),
    ("repo_name", "This ORGrove repo's name on GitHub", "orgrove", r"[A-Za-z0-9._-]{1,100}"),
    ("app_slug", "Your GitHub App's URL name (github.com/apps/<name>)", None, r"[a-z0-9-]{1,34}"),
    ("app_user_id", "The App bot's user id (blank: look it up)", "", r"\d*"),
    ("client_name", "Name shown as the client in the Office", lambda a: a["owner"], r".{1,60}"),
    ("project", "Short id for your first project (lowercase)", None, r"[a-z][a-z0-9-]{1,30}"),
    ("project_name", "Its display name", lambda a: a["project"].capitalize(), r".{1,60}"),
    ("project_repo", "Its repo (owner/name)", lambda a: f"{a['owner']}/{a['project']}", r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+"),
    ("board", "Its GitHub Project board number (blank: none)", "", r"\d*"),
    ("owners", "GitHub logins who may instruct the agents (comma-separated)", None, r"[A-Za-z0-9-]+(\s*,\s*[A-Za-z0-9-]+)*"),
    ("setup", "Command run before each agent starts", "true", r".+"),
    ("test", "Scoped test command; {scope} is replaced by the test paths", "pytest -q {scope}", r".*\{scope\}.*"),
    ("contract", "Repo-wide check command, run before every hand-off", "pytest -q", r"[^{}]+"),
    ("daily_usd", "Daily spend cap in US dollars", "10", r"\d+(\.\d+)?"),
    ("monthly_usd", "Monthly spend cap in US dollars", "50", r"\d+(\.\d+)?"),
    ("timezone", "Your timezone, where a day and a month start (IANA name, e.g. Europe/London)", "UTC",
     r"[A-Za-z_]+(/[A-Za-z0-9_+-]+)*"),
]

# The project questions of --add-project: the project's own keys above, then the optional ones (blank = left out).
# A fifth element names the answer that must be non-blank for the question to be asked.
PROJECT_KEYS = ["project", "project_name", "project_repo", "board", "owners", "setup", "test", "contract"]
ADD_PROJECT_QUESTIONS = [q for k in PROJECT_KEYS for q in QUESTIONS if q[0] == k] + [
    ("build", "Build command (blank: none)", "", r"[^{}]*"),
    ("deploy_url", "URL of its deployed dev environment (blank: a merged PR closes its ticket)", "", r"(https://\S+)?"),
    ("deployed_by", "File name of the workflow that deploys it (e.g. deploy.yml)", "deploy.yml", r"[\w.-]+\.ya?ml", "deploy_url"),
    ("smoke_paths", "Paths to check after a deploy (comma-separated)", "/", r"/\S*(\s*,\s*/\S*)*", "deploy_url"),
]

DEFAULT_FORBIDDEN = [
    "deploy to prod",
    "calling cloud APIs or applying infrastructure (terraform apply, cdk deploy, cloud CLI writes, console changes)",
    "edits to .github/workflows",
    "edits to the agent harness or rules: .claude/, .mcp.json, CLAUDE.md (the gate refuses them)",
    "disabling tests or checks",
    "public posts, emails, or customer-visible publishing",
]


def toml_str(value):
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def ask_all(answers, prompt=input, out=print, questions=QUESTIONS):
    """Fill every missing answer by asking, with the default in brackets; repeat until it is valid."""
    for key, question, default, shape, *needs in questions:
        if needs and not answers.get(needs[0]):   # e.g. no deploy URL: nothing to ask about its workflow
            answers[key] = ""
            continue
        if key in answers:
            value = str(answers[key]).strip()
        else:
            d = default(answers) if callable(default) else default
            while True:
                raw = prompt(f"{question}{f' [{d}]' if d not in (None, '') else ''}: ").strip()
                value = raw or (d or "")
                if re.fullmatch(shape, value):
                    break
                out(f"  that doesn't look right; expected {shape}")
        if not re.fullmatch(shape, value):
            raise SystemExit(f"--answers: {key} = {value!r} does not match {shape}")
        answers[key] = value
    return answers


def lookup_bot_id(slug, run=doctor.gh):
    ok, out = run("api", f"users/{slug}[bot]", "--jq", ".id")
    return int(out) if ok and str(out).isdigit() else None


def org_toml(a):
    return f"""# Organisation settings, written by python -m orgrove.init. Edit by hand or run init again with --force.

# Where this install lives on GitHub. Every project repo must be under `owner` (one App token covers one owner).
[github]
owner = {toml_str(a['owner'])}
repo = {toml_str(a['owner'] + '/' + a['repo_name'])}   # this repo: it runs the workflows and holds the ledger branch
app_slug = {toml_str(a['app_slug'])}
app_user_id = {int(a['app_user_id'])}

# How the Office shows the client. Optional: logo_light / logo_dark (PNG in config/brand/) and accent (#rrggbb).
[brand]
client_name = {toml_str(a['client_name'])}
"""


def project_tools(a):
    """Bash rules for the programs the project's commands use that the agents aren't already allowed to run."""
    dept = manifest.department("engineering")
    need = []
    for r in dept["roles"].values():
        if r.get("runs_commands"):
            for cmd in (a["test"], a["contract"], a.get("build", "")):
                need += [manifest.rule_for(part) for part in manifest.blocked(cmd, r["tools"] + need)]
    return list(dict.fromkeys(need))


def project_toml(a):
    owners = [o.strip() for o in a["owners"].split(",") if o.strip()]
    board_owner, _ = a["project_repo"].split("/")
    board = f"board = {{ owner = {toml_str(board_owner)}, number = {int(a['board'])} }}\n" if a["board"] else ""
    forbidden = "".join(f"  {toml_str(f)},\n" for f in DEFAULT_FORBIDDEN)
    tools = project_tools(a)
    tools = (f"tools = {toml_str(tools)}   # extra commands the Dev, QA and DevOps agents may run "
             f"(the programs in [commands])\n") if tools else ""
    order = f"order = {int(a['order'])}   # the dispatcher serves projects in this order: installed ones pick first\n" if a.get("order") else ""
    build = f"build = {toml_str(a['build'])}\n" if a.get("build") else ""
    env = ""
    if a.get("deploy_url"):
        paths = [x.strip() for x in a.get("smoke_paths", "/").split(",") if x.strip()] or ["/"]
        env = (f"\n[environments.dev]\nurl = {toml_str(a['deploy_url'])}\ndeployed_by = {toml_str(a['deployed_by'])}\n"
               f"smoke_paths = {toml_str(paths)}\n")
    return f"""# Project profile, written by python -m orgrove.init. Everything project-specific lives here.
project = {toml_str(a['project'])}
name = {toml_str(a['project_name'])}
{order}repo = {toml_str(a['project_repo'])}
{board}departments = ["engineering"]
human_gates = ["merge"]                 # locked: a person merges every change
owners = {toml_str(owners)}             # only these humans (and the App) can instruct agents
rules = ["README.md"]                   # what every agent reads first, in order: add your CLAUDE.md or guidelines
{tools}
[review]
external_prs = false

[commands]
setup = {toml_str(a['setup'])}
test = {toml_str(a['test'])}
contract = {toml_str(a['contract'])}
{build}{env}
[limits]
# Never automated for this project, whatever a ticket says. Agents raise needs/human instead.
forbidden = [
{forbidden}]
"""


def set_caps(budget_text, daily, monthly, timezone="UTC"):
    """The caps and timezone lines replaced whole (their comments were the previous owner's notes); every other
    line of config/budget.toml is kept."""
    lines = {
        "daily_usd": f"daily_usd = {float(daily)}   # hard cap per local day: the budget gate refuses a run that would pass it",
        "monthly_usd": f"monthly_usd = {float(monthly)}   # hard cap per local month; keep the Anthropic Console limit close to it",
        "timezone": f"timezone = {toml_str(timezone)}   # \"day\" and \"month\" are measured in this zone",
    }
    for key, line in lines.items():
        budget_text, n = re.subn(rf"^{key} = .*$", lambda _, line=line: line, budget_text, count=1, flags=re.MULTILINE)
        if n != 1:
            raise SystemExit(f"config/budget.toml has no top-level {key} line")
    budget_text = re.sub(r"^# Budget gate.*$", "# Budget gate (#3). Changing a cap is an owner decision: edit here, in a PR.",
                         budget_text, count=1, flags=re.MULTILINE)
    # A new install starts without the publisher's override or per-repo shares (their repos aren't the new owner's)
    return re.sub(r"^\[(daily_override|project_daily)\][^\[]*", "", budget_text, flags=re.MULTILINE)


def write(a, root=manifest.ROOT, force=False, out=print):
    """Write the config for answers `a` under `root`. Returns the files written (moved ones are announced via out)."""
    org_path, prof_path = root / "config" / "org.toml", root / "projects" / f"{a['project']}.toml"
    budget_path = root / "config" / "budget.toml"
    # Everything is checked before any file is moved or written: a refused answer leaves the copy as it was.
    new_budget = set_caps(budget_path.read_text(), a["daily_usd"], a["monthly_usd"], a.get("timezone", "UTC"))
    errors = budget.validate(tomllib.loads(new_budget)) + manifest.validate_project(tomllib.loads(project_toml(a)))
    if errors:
        raise SystemExit("not written: " + "; ".join(errors))
    moved = []
    if org_path.exists():
        with open(org_path, "rb") as f:
            previous_owner = tomllib.load(f).get("github", {}).get("owner")
        if previous_owner == org.PLACEHOLDER:   # a fresh copy's template: replaced, nothing to set aside
            org_path.unlink()
        elif previous_owner and previous_owner != a["owner"]:  # a fresh copy carries its publisher's config: set it aside
            dest = root / "config" / "other" / "org.toml"
            dest.parent.mkdir(exist_ok=True)
            org_path.rename(dest)
            moved.append(dest)
            out(f"moved config/org.toml -> {dest.relative_to(root)} (it was {previous_owner}'s)")
    for path in (org_path, prof_path):
        if path.exists() and not force:
            raise SystemExit(f"{path.relative_to(root)} exists; run again with --force to replace it")
    others = []
    for path in sorted((root / "projects").glob("*.toml")):
        if path == prof_path:
            continue
        with open(path, "rb") as f:
            repo = tomllib.load(f).get("repo", "")
        if repo.split("/")[0] != a["owner"]:
            others.append(path)
    org_path.write_text(org_toml(a))
    prof_path.parent.mkdir(exist_ok=True)   # a clone has no projects/ yet: git keeps no empty folder
    prof_path.write_text(project_toml(a))
    budget_path.write_text(new_budget)
    for path in others:  # another owner's projects: kept, but where nothing reads them
        dest = root / "projects" / "other" / path.name
        dest.parent.mkdir(exist_ok=True)
        path.rename(dest)
        moved.append(dest)
        out(f"moved {path.relative_to(root)} -> {dest.relative_to(root)} (not under {a['owner']})")
    return [org_path, prof_path, budget_path]


def installed_owner(root):
    """The owner in config/org.toml, or None when there is no real one yet (missing file or a fresh copy's template)."""
    path = root / "config" / "org.toml"
    if not path.exists():
        return None
    with open(path, "rb") as f:
        owner = tomllib.load(f).get("github", {}).get("owner")
    return None if owner in (None, org.PLACEHOLDER) else owner


def write_project(a, root=manifest.ROOT, force=False):
    """--add-project: write projects/<id>.toml only. Everything is checked first; a refusal writes nothing.
    Returns the file written."""
    owner = installed_owner(root)
    if not owner:
        raise SystemExit("not written: no config/org.toml for this install yet; run `python -m orgrove.init` first")
    if a["project_repo"].split("/")[0] != owner:
        raise SystemExit(f"not written: {a['project_repo']} is not under {owner}, the owner in config/org.toml "
                         "(one App token covers one owner)")
    prof_path = root / "projects" / f"{a['project']}.toml"
    profiles = {}
    for path in sorted((root / "projects").glob("*.toml")) if (root / "projects").is_dir() else []:
        with open(path, "rb") as f:
            profiles[path] = tomllib.load(f)
    if not force:
        if prof_path in profiles or any(p.get("project") == a["project"] for p in profiles.values()):
            raise SystemExit(f"not written: project {a['project']} is already in projects/; use --force to replace it")
        if any(p.get("repo") == a["project_repo"] for p in profiles.values()):
            raise SystemExit(f"not written: {a['project_repo']} is already a project in projects/; use --force to replace it")
    # A replaced profile keeps its place in the order; a new one picks after every installed project.
    others = [p for path, p in profiles.items() if path != prof_path]
    own = profiles.get(prof_path, {}).get("order")
    a = dict(a, order=own or max([p.get("order", 1) for p in others], default=0) + 1)
    errors = manifest.validate_project(tomllib.loads(project_toml(a)))
    if errors:
        raise SystemExit("not written: " + "; ".join(errors))
    prof_path.parent.mkdir(exist_ok=True)
    prof_path.write_text(project_toml(a))
    return prof_path


def gh_post(path, body):
    """POST a JSON body with gh api. Returns (ok, parsed answer or error text)."""
    out = subprocess.run(["gh", "api", path, "--method", "POST", "--input", "-"], input=json.dumps(body),
                         capture_output=True, text=True, check=False)
    if out.returncode:
        return False, (out.stderr or out.stdout).strip()
    return True, json.loads(out.stdout)


def create_ledger(repo, run=doctor.gh, post=gh_post):
    """An empty `ledger` branch (only a README), created with the Git data API: no clone, no push. Idempotent."""
    if run("api", f"repos/{repo}/branches/ledger")[0]:
        return "ledger branch already exists"
    readme = "# Ledger\n\nORGrove's append-only activity log: one JSONL file per local day under events/. Only the workflows write here.\n"
    steps = [
        ("blob", f"repos/{repo}/git/blobs", lambda prev: {"content": readme, "encoding": "utf-8"}),
        ("tree", f"repos/{repo}/git/trees", lambda prev: {"tree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": prev["sha"]}]}),
        ("commit", f"repos/{repo}/git/commits", lambda prev: {"message": "Start the ledger (orgrove init)", "tree": prev["sha"], "parents": []}),
        ("branch", f"repos/{repo}/git/refs", lambda prev: {"ref": "refs/heads/ledger", "sha": prev["sha"]}),
    ]
    prev = {}
    for name, path, body in steps:
        ok, prev = post(path, body(prev))
        if not ok:
            raise SystemExit(f"could not create the ledger branch ({name}): {str(prev)[:200]}")
    return f"created the ledger branch in {repo}"


def apply(a, out=print):
    """GitHub writes, only with --apply: ORGrove's labels in both repos and an empty ledger branch."""
    from orgrove import sync_labels
    sync_labels.main(a["project_repo"])
    sync_labels.main(f"{a['owner']}/{a['repo_name']}", "control")
    out(create_ledger(f"{a['owner']}/{a['repo_name']}"))


def apply_project(a):
    """--add-project --apply: the pipeline labels in the new repo only (no ledger branch, no control-repo labels)."""
    from orgrove import sync_labels
    sync_labels.main(a["project_repo"])


def project_next_steps(a, control_repo):
    repo = a["project_repo"]
    return f"""
Next steps for {repo}
 1. Install the GitHub App on {repo}: Settings > GitHub Apps > your App > Repository access.
 2. Notify workflow (a person's ticket, comment or merge starts a tick at once), in {repo}:
     copy templates/orgrove-notify.yml to .github/workflows/orgrove-notify.yml
     gh variable set ORGROVE_NOTIFY_APP_ID -R {repo} --body <the notify App's ID>
     gh variable set ORGROVE_CONTROL_REPO -R {repo} --body {control_repo.split('/')[1]}
     gh secret set ORGROVE_NOTIFY_PRIVATE_KEY -R {repo} < <the notify App's .pem>
    The notify App must be installed on {repo} too.
 3. Commit projects/{a['project']}.toml and push to {control_repo}.
 4. Check everything: python -m orgrove.doctor
"""


def next_steps(a):
    repo = f"{a['owner']}/{a['repo_name']}"
    perms = "\n".join(f"     - {name.replace('_', ' ')}: {level}" for name, level in doctor.APP_PERMISSIONS.items())
    optional = "\n".join(f"     - {name.replace('_', ' ')}: {level}" for name, level in doctor.APP_OPTIONAL.items())
    return f"""
Next steps
 1. GitHub App ({a['app_slug']}): Settings > Developer settings > GitHub Apps, with these permissions:
{perms}
    optional (reports only):
{optional}
    Install it on {repo} and {a['project_repo']}. Generate a private key.
 2. Variables and secrets on {repo}:
     gh variable set ORGROVE_APP_ID -R {repo} --body <the App's ID>
     gh variable set ORGROVE_ENABLED -R {repo} --body false        # the kill switch: true when ready
     gh secret set ORGROVE_APP_PRIVATE_KEY -R {repo} < <the .pem file>
     gh secret set ANTHROPIC_API_KEY -R {repo}                     # an Anthropic API key, never a subscription login
    and a monthly spend limit of about ${float(a['monthly_usd']):g} on platform.claude.com > Settings > Limits.
 3. Notify App (a second, small App: a person's ticket, comment or merge starts a tick at once):
    permission Actions: read and write, nothing else; install it on {repo} only. Then in {a['project_repo']}:
     copy templates/orgrove-notify.yml to .github/workflows/orgrove-notify.yml
     gh variable set ORGROVE_NOTIFY_APP_ID -R {a['project_repo']} --body <the notify App's ID>
     gh variable set ORGROVE_CONTROL_REPO -R {a['project_repo']} --body {a['repo_name']}
     gh secret set ORGROVE_NOTIFY_PRIVATE_KEY -R {a['project_repo']} < <the notify App's .pem>
 4. Commit config/ and projects/, push to {repo}.
 5. Check everything: python -m orgrove.doctor --tick
""" + (f""" 6. Optional, for meetings in the Office (macOS): store an Anthropic API key for this install:
     security add-generic-password -s orgrove-anthropic/{repo} -a orgrove -w
""" if editions.has("meetings") else "")   # meetings are ORGrove Pro's (orgrove/editions.py)


def main(argv=None, prompt=input, out=print):
    p = argparse.ArgumentParser(prog="orgrove.init", description=__doc__.split("\n")[0])
    p.add_argument("--answers", type=Path, help="a TOML file with the answers (no questions asked)")
    p.add_argument("--apply", action="store_true", help="also create the labels and the ledger branch on GitHub")
    p.add_argument("--force", action="store_true", help="replace config/org.toml and the project file if they exist (--add-project: the project file)")
    p.add_argument("--add-project", action="store_true",
                   help="onboard one more project on an existing install: writes only projects/<id>.toml")
    a = p.parse_args(argv)
    answers = tomllib.loads(a.answers.read_text()) if a.answers else {}
    if a.add_project:
        if not installed_owner(manifest.ROOT):
            raise SystemExit("not written: no config/org.toml for this install yet; run `python -m orgrove.init` first")
        answers["owner"] = installed_owner(manifest.ROOT)   # not asked: the install's owner
        answers = ask_all(answers, prompt, out, ADD_PROJECT_QUESTIONS)
        path = write_project(answers, force=a.force)
        out(f"wrote {path.relative_to(manifest.ROOT)}")
        if a.apply:
            apply_project(answers)
        out(project_next_steps(answers, org.load()["repo"]))
        return 0
    answers = ask_all(answers, prompt, out)
    if not answers["app_user_id"]:
        found = lookup_bot_id(answers["app_slug"])
        if not found:
            raise SystemExit(f"no bot user {answers['app_slug']}[bot] on GitHub yet: create the App, then run init again")
        answers["app_user_id"] = str(found)
    for path in write(answers, force=a.force, out=out):
        out(f"wrote {path.relative_to(manifest.ROOT)}")
    if a.apply:
        apply(answers, out=out)
    out(next_steps(answers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
