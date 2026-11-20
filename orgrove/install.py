"""Install ORGrove in your GitHub organisation with one command.

    ./install                 (the same as python3 -m orgrove.install)

Asks about five things, then does the rest: the control repo, the three GitHub Apps (two clicks each, on GitHub's own
pages, through the App Manifest flow: nobody downloads a key or copies an ID), every secret and variable, the
project repo's CI and notify workflow, the config, the checks, and switching the agents on. Each step checks
first and acts only if needed, so running it again continues where it stopped. Progress (names and IDs, never a
key) is kept in ~/.orgrove/installs/<owner>__<repo>/install.json.

Plan: #159, docs/plans/2026-10-29-one-command-install.md (in ORGrove's own repository).
"""
import argparse
import base64
import getpass
import html
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from orgrove import doctor, editions, init, manifest

CONTROL = "orgrove"     # the control repo's name (plan decision 2)
SAMPLE = "hello"        # the sample project's repo name
# Where releases are cloned from (Community, Pro). A clone of one becomes `upstream`: the install pushes to the
# owner's own repo, and an update can later be pulled from upstream.
RELEASE_REPOS = ("ORGrove/orgrove", "ORGrove/release")
STARTER = manifest.ROOT / "templates" / "starter"
NOTIFY = manifest.ROOT / "templates" / "orgrove-notify.yml"
INSTALLS = Path.home() / ".orgrove" / "installs"
SCOPES = ("repo", "workflow", "read:org", "project")
# `gh variable` arrived in 2.31 and `gh auth status` moved to stdout in 2.40. Ubuntu 22.04 ships 2.4 and Debian 12
# 2.23, so an older gh is refused up front instead of failing midway (2026-10-31; tested on Ubuntu 24.04's 2.45).
GH_MIN = (2, 40)
ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models"
ANTHROPIC_MESSAGES = "https://api.anthropic.com/v1/messages"
PROBE_MODEL = "claude-haiku-5-5"     # the cheapest model ORGrove uses: the check below asks it one word, max_tokens 1
GITHUB_API = "https://api.github.com"


# --- small helpers ---------------------------------------------------------------------------------------------

def gh(*args, input=None):
    """gh, with optional stdin (secrets go in here, never in an argument). Returns (ok, parsed JSON or text)."""
    out = subprocess.run(["gh", *args], input=input, capture_output=True, text=True, check=False)
    if out.returncode:
        return False, (out.stderr or out.stdout).strip()
    text = out.stdout.strip()
    try:
        return True, json.loads(text) if text else {}
    except json.JSONDecodeError:
        return True, text


def git(*args, cwd):
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    if out.returncode:
        raise SystemExit(f"git {' '.join(args[:2])} failed: {(out.stderr or out.stdout).strip()[:300]}")
    return out.stdout.strip()


def local_timezone(localtime="/etc/localtime"):
    """The computer's IANA timezone (where a day and a month start for the caps), or UTC."""
    target = os.path.realpath(localtime)
    if "zoneinfo/" in target:
        name = target.split("zoneinfo/", 1)[1]
        try:
            ZoneInfo(name)
            return name
        except (ValueError, KeyError, OSError):
            pass
    return "UTC"


def clean_key(pasted):
    """The key without what terminals and copying add: bracketed-paste markers, quotes, spaces (rehearsal 2026-10-30)."""
    key = re.sub(r"\x1b?\[20[01]~", "", pasted)
    return re.sub(r"\s", "", key).strip("'\"")


def key_problem(key):
    """Why a pasted key can't be used, or None. Its format is not guessed: Anthropic, asked next, decides (rehearsal
    2026-10-30, where a guessed format refused real keys)."""
    if not key:
        return "nothing arrived: paste with Cmd+V (or right-click > Paste), then press Enter"
    if key.startswith("sk-ant-oat"):
        return "that is a Claude subscription login; ORGrove needs an API key from platform.claude.com > API keys"
    return None


def key_accepted(key, opener=urllib.request.urlopen):
    """True/False when Anthropic answers for the key (a free model list), None when it can't be asked."""
    req = urllib.request.Request(ANTHROPIC_MODELS, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with opener(req, timeout=20):  # noqa: S310  a fixed https constant
            return True
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except (urllib.error.URLError, OSError):
        return None


def key_usable(key, opener=urllib.request.urlopen):
    """(True, "") when the key can really make a request, (False, plain words) when Anthropic says it can't, and
    (None, "") when that can't be told (offline, a model the workspace lacks, a busy moment). The free model list
    only proves the key exists: a workspace at its spending limit passed it, and the first agent run then failed
    (rehearsal, 2026-11-10). This asks for one token from the cheapest model: a fraction of a cent."""
    body = json.dumps({"model": PROBE_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(ANTHROPIC_MESSAGES, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with opener(req, timeout=30):  # noqa: S310  a fixed https constant
            return True, ""
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read().decode("utf-8", "replace")).get("error", {}).get("message", "")
        except (ValueError, OSError, AttributeError):
            said = ""
        if e.code in (401, 403):
            return False, "Anthropic refused that key (revoked, or a typo)"
        if e.code == 400 and "usage limit" in said.lower():
            back = re.search(r"regain access on (\d{4}-\d{2}-\d{2})", said)
            until = f" until {back.group(1)}" if back else ""
            return False, (f"that key's workspace has reached its spending limit and will not work{until}. Raise the "
                           "limit at platform.claude.com > Settings > Limits, or paste a key from another workspace")
        return None, ""
    except (urllib.error.URLError, OSError):
        return None, ""


def detect_commands(files, read):
    """The project's setup, scoped test and repo-wide check commands, from the files at its root; None if unknown.
    `files`: names at the repo root; `read(name)`: that file's text ('' if unreadable)."""
    if "package.json" in files:
        try:
            scripts = json.loads(read("package.json") or "{}").get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        if scripts.get("test"):
            return {"setup": "npm ci" if "package-lock.json" in files else "npm install",
                    "test": "npm test -- {scope}", "contract": "npm test", "language": "node"}
    setup = "pip install -r requirements.txt" if "requirements.txt" in files else "true"
    config = ("pyproject.toml", "setup.cfg", "tox.ini", "requirements.txt", "requirements-dev.txt")
    if "pytest.ini" in files or "conftest.py" in files or any("pytest" in read(f) for f in config if f in files):
        return {"setup": setup, "test": "python -m pytest -q {scope}", "contract": "python -m pytest -q",
                "language": "python"}
    if "tests" in files:
        return {"setup": setup, "test": "python -m unittest {scope}",
                "contract": "python -m unittest discover -s tests -t .", "language": "python"}
    return None


def ci_workflow(contract, setup, language):
    """A CI workflow for an existing project that has none on the agents' branches: it runs the repo-wide check
    on every push to agent/** (QA and Security start only when it is green, #151)."""
    steps = {"python": "      - uses: actions/setup-python@v5\n        with:\n          python-version: \"3.12\"\n",
             "node": "      - uses: actions/setup-node@v4\n        with:\n          node-version: \"22\"\n"}.get(language, "")
    setup_step = f"      - run: {setup}\n" if setup and setup != "true" else ""
    return f"""# Added by ORGrove's installer: runs the project's checks on the agents' branches, so QA and Security
# review only green code. Your own CI is untouched.
name: orgrove-ci
on:
  push:
    branches: ['agent/**']
permissions:
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
{steps}{setup_step}      - run: {contract}
"""


def jwt(app_id, pem, now=None):
    """A GitHub App JWT signed with openssl (stdlib has no RSA), or None without openssl."""
    if not shutil.which("openssl"):
        return None
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    now = int(now or time.time())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"iat": now - 60, "exp": now + 300, "iss": str(app_id)}).encode())
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, pem.encode())
        os.close(fd)
        signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", path], input=f"{head}.{body}".encode(),
                                capture_output=True, check=False)
    finally:
        os.remove(path)
    return f"{head}.{body}.{b64(signed.stdout)}" if signed.returncode == 0 else None


def github(method, path, token=None, scheme="Bearer", opener=urllib.request.urlopen):
    req = urllib.request.Request(GITHUB_API + path, method=method,  # noqa: S310  a fixed https constant
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "orgrove-install"})
    if token:
        req.add_header("Authorization", f"{scheme} {token}")
    with opener(req, timeout=30) as r:  # noqa: S310  a fixed https constant
        return json.load(r)


# --- the two-click GitHub App flow ------------------------------------------------------------------------------

class AppFlow:
    BACK = "go back to the terminal"     # the web installer says "the installer"
    """GitHub's App Manifest flow on 127.0.0.1: GitHub's pre-filled "Create GitHub App" page, then its install page.
    `on_created(conversion)` runs as soon as GitHub hands over the App (id, slug and private key): it stores the key
    in a GitHub secret at once, so no key is ever written to disk or shown. Proven live on 2026-10-29 (plan)."""

    def __init__(self, owner, app_manifest, repos_hint, on_created, exchange=None, port=0, step=None):
        self.owner, self.manifest, self.repos_hint, self.on_created = owner, app_manifest, repos_hint, on_created
        self.exchange = exchange or (lambda code: github("POST", f"/app-manifests/{code}/conversions"))
        self.state = secrets.token_urlsafe(16)
        self.result = {}
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                flow.handle(self, u.path, dict(urllib.parse.parse_qsl(u.query)))

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.manifest = {**app_manifest, "redirect_url": f"{self.base}/created", "setup_url": f"{self.base}/installed/{self.state}"}

    def page(self, req, body, status=200):
        req.send_response(status)
        req.send_header("Content-Type", "text/html; charset=utf-8")
        req.end_headers()
        req.wfile.write(f"<!doctype html><meta charset=utf-8><title>ORGrove install</title>"
                        f"<body style='font:16px system-ui;max-width:40em;margin:3em auto'>{body}".encode())

    def handle(self, req, path, q):
        if path == "/":
            action = f"https://github.com/organizations/{self.owner}/settings/apps/new?state={self.state}"
            self.page(req, f"""<form id=f method=post action="{html.escape(action)}">
<input type=hidden name=manifest value="{html.escape(json.dumps(self.manifest))}">
<p>Taking you to GitHub to create <b>{html.escape(self.manifest['name'])}</b>…</p>
<button>Continue to GitHub</button></form><script>document.getElementById('f').submit()</script>""")
        elif path == "/created":
            if q.get("state") != self.state or not q.get("code"):
                return self.page(req, f"<p>This link isn't from this install. {self.BACK.capitalize()}.</p>", 400)
            try:
                conv = self.exchange(q["code"])
                self.on_created(conv)   # stores the key in a GitHub secret before anything else
            except Exception as e:  # noqa: BLE001  shown on the page and in the terminal, never swallowed
                self.result.update(error=str(e)[:300], installation="")
                return self.page(req, f"<p>Something went wrong: {html.escape(str(e)[:300])}</p>"
                                      f"<p>{self.BACK.capitalize()}.</p>", 500)
            self.result.update(id=conv["id"], slug=conv["slug"], pem=conv["pem"])
            repos = ", ".join(f"<b>{html.escape(r)}</b>" for r in self.repos_hint)
            self.page(req, f"""<p>✓ Created <b>{html.escape(conv['slug'])}</b>.</p>
<p>Next, install it. On GitHub's page choose <b>Only select repositories</b>, pick {repos}, and click
<b>Install</b>.</p><p><a href="https://github.com/apps/{html.escape(conv['slug'])}/installations/new">Install it
on GitHub</a></p>""")
        elif path == f"/installed/{self.state}":    # GitHub's setup redirect; the state in the path guards it
            self.result["installation"] = q.get("installation_id", "")
            self.page(req, f"<p>✓ Installed. You can close this tab and {self.BACK}.</p>")
        else:
            self.page(req, "<p>Not found.</p>", 404)

    def run(self, open_url=webbrowser.open, out=print):
        out(f"  opening your browser at {self.base}/ (if it doesn't open, copy that address into it)")
        open_url(f"{self.base}/")
        while "installation" not in self.result:
            self.server.handle_request()
        self.server.server_close()
        if self.result.get("error"):
            raise SystemExit(f"GitHub didn't finish creating the App: {self.result['error']}. Run ./install again.")
        return self.result


def edition_of(root):
    """community or pro, from the copy's .orgrove-install (bin/package writes it); None for any other folder."""
    try:
        found = re.search(r"^edition = (community|pro)$", (root / ".orgrove-install").read_text(), re.M)
    except OSError:
        return None
    return found and found.group(1)


def has(part):
    """Whether this copy ships a part only ORGrove Pro has (orgrove/editions.py): wiki, meetings."""
    return editions.has(part)


def app_manifest(name, owner, permissions, description):
    return {"name": name[:34], "url": f"https://github.com/{owner}/{CONTROL}", "description": description,
            "hook_attributes": {"url": "https://example.invalid/orgrove-uses-no-webhooks", "active": False},
            "public": False, "default_permissions": dict(permissions)}


# --- the installer ----------------------------------------------------------------------------------------------

class Installer:
    def __init__(self, root=manifest.ROOT, run=gh, ask=input, ask_secret=getpass.getpass, out=print,
                 open_url=webbrowser.open, installs=INSTALLS, app_flow=AppFlow, sleep=time.sleep, git_run=git, port=0,
                 interactive=None):
        # a command only a person can run (gh's own device login): the terminal runs it, the web installer asks them to
        self.interactive = interactive or (lambda cmd: subprocess.run(cmd, check=False))
        self.port = port       # the App pages' local port: 0 = any free one; fixed for SSH or Docker forwarding
        self.root, self.gh, self.ask_raw, self.ask_secret, self.out = root, run, ask, ask_secret, out
        self.open_url, self.installs, self.app_flow, self.sleep, self.git = open_url, installs, app_flow, sleep, git_run
        self.s = {}            # progress: names and IDs only, never a key
        self.key = None        # the Anthropic key, in memory for this run only

    # progress file
    def state_path(self):
        return self.installs / f"{self.s['owner']}__{CONTROL}" / "install.json"

    def save(self):
        p = self.state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.s, indent=2, sort_keys=True) + "\n")

    def resume(self):
        """A stopped install of this folder, if there is one."""
        for p in sorted(self.installs.glob(f"*__{CONTROL}/install.json")):
            s = json.loads(p.read_text())
            if s.get("root") == str(self.root) and not s.get("done"):
                return s
        return None

    # questions
    def ask(self, question, default="", shape=r".+"):
        while True:
            raw = self.ask_raw(f"{question}{f' [{default}]' if default else ''}: ").strip()
            value = raw or default
            if re.fullmatch(shape, value):
                return value
            self.out("  that doesn't look right, try again")

    def yes(self, question, default=True):
        answer = self.ask(f"{question} ({'Y/n' if default else 'y/N'})", "", r"(?i)(y|yes|n|no)?")
        return default if not answer else answer.lower().startswith("y")

    def step(self, text):
        self.out(f"\n== {text}")

    # 1. this computer
    def preflight(self):
        self.step("Checking this computer")
        if not (self.root / ".orgrove-install").exists():
            raise SystemExit("run ./install from a downloaded copy of ORGrove (this folder isn't one)")
        if not edition_of(self.root):   # a copy built without --edition could carry Pro's parts (rehearsal, 2026-10-30)
            raise SystemExit("this copy isn't an ORGrove Community or Pro release: download one, then run ./install again")
        for tool, fix in (("git", "install git"), ("gh", "install the GitHub CLI: https://cli.github.com")):
            if not shutil.which(tool):
                raise SystemExit(f"{tool} not found: {fix}, then run ./install again")
        ok, version = self.gh("--version")
        found = re.search(r"(\d+)\.(\d+)", str(version))
        if found and (int(found[1]), int(found[2])) < GH_MIN:
            raise SystemExit(f"the GitHub CLI is too old ({found[0]}; ORGrove needs {GH_MIN[0]}.{GH_MIN[1]} or newer): "
                             "install the current one from https://cli.github.com, then ./install again")
        ok, status = self.gh("auth", "status")
        if not ok:
            raise SystemExit("the GitHub CLI isn't logged in: run `gh auth login`, then ./install again")
        have = set(re.findall(r"'([a-z:_]+)'", str(status)))
        missing = [s for s in SCOPES if s not in have]
        if missing:
            self.out(f"  the GitHub CLI needs more access ({', '.join(missing)}); GitHub will ask you to approve it")
            self.interactive(["gh", "auth", "refresh", "-h", "github.com", "-s", ",".join(missing)])
            ok, status = self.gh("auth", "status")
            still = [s for s in SCOPES if s not in set(re.findall(r"'([a-z:_]+)'", str(status)))]
            if still:
                raise SystemExit(f"the GitHub CLI still lacks access ({', '.join(still)}): run "
                                 f"`gh auth refresh -h github.com -s {','.join(still)}`, then try again")
        ok, login = self.gh("api", "user", "--jq", ".login")
        if not ok:
            raise SystemExit(f"can't read your GitHub login: {login}")
        self.s["login"] = login
        self.out(f"  ✓ Python {platform.python_version()}, git, GitHub CLI logged in as {login}")

    # 2. the questions
    def questions(self):
        self.step("Five questions")
        if "owner" not in self.s:
            while True:
                owner = self.ask("1. Your GitHub organisation (the name in github.com/<name>)", "", r"[A-Za-z0-9-]{1,39}")
                ok, m = self.gh("api", f"orgs/{owner}/memberships/{self.s['login']}")
                if ok and m.get("role") == "admin" and m.get("state") == "active":
                    break
                self.out(f"  you need to be an owner of {owner} on GitHub (and it must be an organisation)")
            self.s["owner"] = owner
            ok, plan = self.gh("api", f"orgs/{owner}", "--jq", ".plan.name")
            self.s["plan"] = plan if ok else "unknown"
        owner = self.s["owner"]
        self.s.setdefault("root", str(self.root))
        if not self.secret_exists(self.repo, "ANTHROPIC_API_KEY"):
            self.out("2. Your Anthropic API key (platform.claude.com > API keys). It is hidden as you paste it, goes")
            self.out("   straight into a GitHub secret, and is never saved or shown.")
            while True:
                key = clean_key(self.ask_secret("   API key: "))
                problem = key_problem(key)
                if not problem:
                    accepted = key_accepted(key)
                    if accepted is False:
                        problem = "Anthropic refused that key (revoked, or a typo)"
                    elif accepted is None:
                        self.out("   (couldn't reach Anthropic to test it; using it as it is)")
                    else:
                        usable, why = key_usable(key)     # one real one-word request: a fraction of a cent
                        if usable is False:
                            problem = why
                        elif usable is None:
                            self.out("   (the key exists, but a test request couldn't be made; using it as it is)")
                if not problem:
                    break
                self.out(f"   {problem}")
            self.key = key
        else:
            self.out("2. Anthropic API key: already set on GitHub")
        if "project" not in self.s:
            name = self.ask(f"3. The repo the agents work on: a repo name in {owner}, or Enter for a sample project",
                            "", r"([A-Za-z0-9._-]{1,100})?")
            sample = not name or not self.gh("api", f"repos/{owner}/{name}")[0]
            if name and sample:
                self.out(f"   there's no {owner}/{name}: ORGrove will create it as a sample project")
            name = name or SAMPLE
            while sample and self.gh("api", f"repos/{owner}/{name}")[0]:   # never take over a repo it didn't make
                name = self.ask(f"   {owner}/{name} already exists. A name for the new sample project", "",
                                r"[A-Za-z0-9._-]{1,100}")
            self.s["sample"], self.s["project"] = sample, name
        if "daily" not in self.s:
            self.s["daily"] = self.ask("4. Daily spend cap in US dollars", "10", r"\d+(\.\d+)?")
            self.s["monthly"] = self.ask("   Monthly spend cap in US dollars", "50", r"\d+(\.\d+)?")
        if "owners" not in self.s:
            self.s["owners"] = self.ask("5. GitHub logins who may give the agents work (comma-separated)",
                                        self.s["login"], r"[A-Za-z0-9-]+(\s*,\s*[A-Za-z0-9-]+)*")
        self.s.setdefault("timezone", local_timezone())
        self.save()

    @property
    def repo(self):
        return f"{self.s['owner']}/{CONTROL}"

    @property
    def project_repo(self):
        return f"{self.s['owner']}/{self.s['project']}"

    def secret_exists(self, repo, name):
        ok, names = self.gh("secret", "list", "-R", repo, "--json", "name", "--jq", ".[].name")
        return ok and name in str(names).split()

    def set_secret(self, repo, name, value):
        ok, out = self.gh("secret", "set", name, "-R", repo, input=value)
        if not ok:
            raise SystemExit(f"couldn't set the secret {name} on {repo}: {out[:200]}")

    def set_variable(self, repo, name, value):
        ok, out = self.gh("variable", "set", name, "-R", repo, "--body", str(value))
        if not ok:
            raise SystemExit(f"couldn't set the variable {name} on {repo}: {out[:200]}")

    def variables(self, repo):
        ok, rows = self.gh("variable", "list", "-R", repo, "--json", "name,value")
        return {r["name"]: r["value"] for r in rows} if ok and isinstance(rows, list) else {}

    # 3. the repos
    def repos(self):
        self.step("Repos")
        for repo, what in ((self.repo, "ORGrove's control repo"),) + (
                ((self.project_repo, "the sample project"),) if self.s["sample"] else ()):
            if self.gh("api", f"repos/{repo}")[0]:
                self.out(f"  ✓ {repo} exists")
                continue
            ok, out = self.gh("repo", "create", repo, "--private", "--description",
                              "ORGrove: an AI engineering team" if repo == self.repo else "A sample project for ORGrove")
            if not ok:
                raise SystemExit(f"couldn't create {repo}: {out[:200]}")
            self.out(f"  ✓ created {repo} ({what}, private)")
        if self.s["sample"] and not self.gh("api", f"repos/{self.project_repo}/commits", "--jq", ".[0].sha")[0]:
            self.push_tree(self.project_repo, self.sample_files(), "A sample project for trying ORGrove")
            self.out(f"  ✓ pushed the sample project, its CI and the notify workflow into {self.project_repo}")

    def sample_files(self):
        files = {str(p.relative_to(STARTER)): p.read_text() for p in sorted(STARTER.rglob("*")) if p.is_file()
                 and "__pycache__" not in p.parts}
        files[".github/workflows/orgrove-notify.yml"] = NOTIFY.read_text()
        return files

    def push_tree(self, repo, files, message, branch="main"):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name, text in files.items():
                (d / name).parent.mkdir(parents=True, exist_ok=True)
                (d / name).write_text(text)
            self.git("init", "-q", "-b", branch, cwd=d)
            self.git("add", "-A", ".", cwd=d)
            self.commit(message, cwd=d)
            self.git("push", "-q", f"https://github.com/{repo}.git", f"HEAD:{branch}", cwd=d)

    # 4. the Apps
    APP_ROLES = (("app", "The main App", "The agents act as it. It reads and writes code, tickets and pull requests in your two repos, and it can never merge."),
                 ("notify", "The notify App", "It only wakes ORGrove up when a person opens a ticket, comments or merges."),
                 ("read", "The read-only App", "The agents' model runs as it, so a tricked model can read your tickets but never change them."))

    def app_step(self, key):
        """Where this App sits among the three, for the page: its number, its job, and the ones already done."""
        keys = [k for k, _, _ in self.APP_ROLES]
        role = next(r for k, r, _ in self.APP_ROLES if k == key)
        why = next(w for k, _, w in self.APP_ROLES if k == key)
        return {"n": keys.index(key) + 1, "of": len(keys), "role": role, "why": why,
                "done": [self.s[k]["slug"] for k in keys[:keys.index(key)] if k in self.s]}

    def apps(self):
        self.step("GitHub Apps (three, one after another; each is two clicks in your browser)")
        owner = self.s["owner"]
        if "app" not in self.s:
            self.out("  The main App: the agents act as it. It may read and write code, issues and pull requests in")
            self.out(f"  {self.repo} and {self.project_repo} only, and it can never merge (a person merges every change).")

            def store_main(conv):
                self.set_secret(self.repo, "ORGROVE_APP_PRIVATE_KEY", conv["pem"])
                self.set_variable(self.repo, "ORGROVE_APP_ID", conv["id"])
            flow = self.app_flow(owner, app_manifest(f"{owner}-orgrove", owner, doctor.APP_PERMISSIONS,
                                                     "ORGrove's agents: plan, build and review; a person merges."),
                                 [CONTROL, self.s["project"]], store_main, port=self.port, step=self.app_step("app"))
            r = flow.run(self.open_url, self.out)
            self.s["app"] = {"id": r["id"], "slug": r["slug"], "installation": r["installation"]}
            self.save()
            self.check_reach(r, [self.repo, self.project_repo])
            self.out(f"  ✓ {r['slug']} created and installed")
        if "notify" not in self.s:
            self.out(f"  The notify App: it only starts ORGrove when a person acts in {self.project_repo}.")

            def store_notify(conv):
                self.set_secret(self.project_repo, "ORGROVE_NOTIFY_PRIVATE_KEY", conv["pem"])
                self.set_variable(self.project_repo, "ORGROVE_NOTIFY_APP_ID", conv["id"])
                self.set_variable(self.project_repo, "ORGROVE_CONTROL_REPO", CONTROL)
            flow = self.app_flow(owner, app_manifest(f"{owner}-orgrove-notify", owner, {"actions": "write"},
                                                     "Starts ORGrove when a person acts in a project repo."),
                                 [CONTROL], store_notify, port=self.port, step=self.app_step("notify"))
            r = flow.run(self.open_url, self.out)
            self.s["notify"] = {"id": r["id"], "slug": r["slug"], "installation": r["installation"]}
            self.save()
            self.check_reach(r, [self.repo])
            self.out(f"  ✓ {r['slug']} created and installed")
        if "read" not in self.s:
            self.out("  The read-only App: the agents' model runs as it, so it can read your tickets and pull requests but")
            self.out("  never change them. A model that is tricked into misbehaving can't write to your tracker.")

            def store_read(conv):
                self.set_secret(self.repo, "ORGROVE_READ_APP_PRIVATE_KEY", conv["pem"])
                self.set_variable(self.repo, "ORGROVE_READ_APP_ID", conv["id"])
            flow = self.app_flow(owner, app_manifest(f"{owner}-orgrove-read", owner, doctor.READ_APP_PERMISSIONS,
                                                     "The agents' model runs as this App: it reads, it can't write tickets."),
                                 [CONTROL, self.s["project"]], store_read, port=self.port, step=self.app_step("read"))
            r = flow.run(self.open_url, self.out)
            self.s["read"] = {"id": r["id"], "slug": r["slug"], "installation": r["installation"]}
            self.save()
            self.check_reach(r, [self.repo, self.project_repo])
            self.out(f"  ✓ {r['slug']} created and installed")

    def check_reach(self, app, wanted, attempts=20):
        """The repos the new installation can reach, read as the App itself (only it can see them). An early check
        only: when it can't be made (no openssl, GitHub unreachable), the dispatcher's dry run proves it later."""
        pem = app.pop("pem")    # held for this check only; never saved
        if not app.get("installation") or not jwt(app["id"], pem):
            return   # proven later by the dispatcher dry run
        missing = wanted
        for _ in range(attempts):
            try:
                tok = github("POST", f"/app/installations/{app['installation']}/access_tokens", jwt(app["id"], pem))["token"]
                listed = github("GET", "/installation/repositories?per_page=100", tok)["repositories"]
            except (urllib.error.URLError, OSError, KeyError, ValueError):
                return
            have = {r["full_name"].lower() for r in listed}
            missing = [w for w in wanted if w.lower() not in have]
            if not missing:
                return
            page = f"https://github.com/organizations/{self.s['owner']}/settings/installations/{app['installation']}"
            self.out(f"  {app['slug']} can't reach {', '.join(missing)} yet. Add it on {page}, then press Enter.")
            self.open_url(page)
            self.ask_raw("")
        raise SystemExit(f"{app['slug']} still can't reach {', '.join(missing)}")

    # 5. secrets and switches
    def secrets(self):
        self.step("Secrets")
        if self.key:
            self.set_secret(self.repo, "ANTHROPIC_API_KEY", self.key)
            self.out(f"  ✓ the Anthropic key is a secret on {self.repo}")
        if "ORGROVE_ENABLED" not in self.variables(self.repo):
            self.set_variable(self.repo, "ORGROVE_ENABLED", "false")
        self.out("  ✓ the kill switch is off until the checks pass")

    # 6. the project repo
    def project(self):
        self.step(f"The project repo ({self.project_repo})")
        repo = self.project_repo
        if self.s["sample"]:   # the starter's own commands (templates/starter/README.md)
            found = detect_commands({"tests"}, lambda name: "")
        else:
            ok, root = self.gh("api", f"repos/{repo}/contents/", "--jq", ".[].name")
            files = set(str(root).split()) if ok else set()
            found = detect_commands(files, lambda name: self.file(repo, name))
        if found:
            self.out(f"  tests: {found['test']} (checks: {found['contract']})")
        else:
            self.out("  ORGrove couldn't tell how this project runs its tests.")
            found = {"setup": self.ask("  Command to prepare it before each agent starts", "true"),
                     "test": self.ask("  Command to run some tests; {scope} is where the test paths go",
                                      "", r".*\{scope\}.*"),
                     "contract": self.ask("  Command to run all its checks", "", r"[^{}]+"), "language": ""}
        self.s["commands"] = found
        self.save()
        if self.s["sample"]:
            return
        add = {}
        if not self.ci_on_agent_branches(repo):
            add[".github/workflows/orgrove-ci.yml"] = ci_workflow(found["contract"], found["setup"], found["language"])
        if not self.gh("api", f"repos/{repo}/contents/.github/workflows/orgrove-notify.yml")[0]:
            add[".github/workflows/orgrove-notify.yml"] = NOTIFY.read_text()
        if add:
            self.setup_pr(repo, add)

    def file(self, repo, name):
        ok, content = self.gh("api", f"repos/{repo}/contents/{name}", "--jq", ".content")
        try:
            return base64.b64decode(str(content)).decode("utf-8", "replace") if ok else ""
        except ValueError:
            return ""

    def ci_on_agent_branches(self, repo):
        ok, paths = self.gh("api", f"repos/{repo}/contents/.github/workflows", "--jq", ".[].path")
        return ok and any(doctor.runs_on_agent_branches(self.file(repo, p)) for p in str(paths).split())

    def setup_pr(self, repo, add):
        """One PR adding ORGrove's files to an existing project: its default branch is the owner's (plan decision 1)."""
        ok, prs = self.gh("pr", "list", "-R", repo, "--head", "orgrove/setup", "--state", "all", "--json", "number,state,url")
        pr = prs[0] if ok and prs else None
        if not pr:
            with tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp) / "repo"
                ok, out = self.gh("repo", "clone", repo, str(d), "--", "--depth", "1", "-q")
                if not ok:
                    raise SystemExit(f"couldn't clone {repo}: {out[:200]}")
                self.git("switch", "-q", "-c", "orgrove/setup", cwd=d)
                for name, text in add.items():
                    (d / name).parent.mkdir(parents=True, exist_ok=True)
                    (d / name).write_text(text)
                self.git("add", *add, cwd=d)
                self.commit("ORGrove: CI on the agents' branches and the notify workflow", cwd=d)
                self.git("push", "-q", "origin", "orgrove/setup", cwd=d)
            body = ("Added by ORGrove's installer.\n\n" + "\n".join(f"- `{n}`" for n in add) +
                    "\n\n`orgrove-ci.yml` runs your checks on the agents' branches, so QA and Security review only "
                    "green code. `orgrove-notify.yml` starts ORGrove when a person opens a ticket, comments or merges. "
                    "Nothing else in your repo changes.")
            ok, url = self.gh("pr", "create", "-R", repo, "--head", "orgrove/setup", "--title",
                              "ORGrove: CI on the agents' branches and the notify workflow", "--body", body)
            if not ok:
                raise SystemExit(f"couldn't open the setup PR in {repo}: {url[:200]}")
            pr = {"url": url, "state": "OPEN"}
        while pr["state"] != "MERGED":
            self.out(f"  Merge this PR in your project, then press Enter: {pr['url']}")
            self.open_url(pr["url"])
            self.ask_raw("")
            ok, pr = self.gh("pr", "view", pr["url"], "--json", "state,url")
            if not ok:
                raise SystemExit(f"couldn't read {pr}")
        self.out("  ✓ the setup PR is merged")

    # 7. config and code
    def configure(self):
        self.step("Configuration")
        c = self.s["commands"]
        answers = {"owner": self.s["owner"], "repo_name": CONTROL, "app_slug": self.s["app"]["slug"],
                   "app_user_id": "", "client_name": self.s["owner"], "project": re.sub(r"[^a-z0-9-]", "-",
                   self.s["project"].lower()).strip("-")[:31] or SAMPLE, "project_repo": self.project_repo,
                   "board": "", "owners": self.s["owners"], "setup": c["setup"], "test": c["test"],
                   "contract": c["contract"], "daily_usd": self.s["daily"], "monthly_usd": self.s["monthly"],
                   "timezone": self.s["timezone"]}
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", answers["project"]):
            answers["project"] = "p-" + answers["project"][:29]
        answers["project_name"] = self.s["project"]
        for _ in range(10):   # the App's bot user appears a moment after the App is created
            bot = init.lookup_bot_id(answers["app_slug"], run=self.gh)
            if bot:
                break
            self.sleep(3)
        else:
            raise SystemExit(f"GitHub has no bot user for {answers['app_slug']} yet: run ./install again in a minute")
        answers["app_user_id"] = str(bot)
        org_toml = self.root / "config" / "org.toml"
        ours = org_toml.exists() and f'owner = "{self.s["owner"]}"' in org_toml.read_text()
        init.write(init.ask_all(answers), root=self.root, force=ours, out=lambda m: self.out(f"  {m}"))
        self.out("  ✓ config/org.toml, the project file and the caps are written")
        self.push_code()
        init.apply(answers, out=lambda m: self.out(f"  {m}"))
        self.out("  ✓ labels and the ledger branch")

    def push_code(self):
        root = self.root
        if not (root / ".git").exists():
            self.git("init", "-q", "-b", "main", cwd=root)
        remotes = self.git("remote", cwd=root).split()
        url = f"https://github.com/{self.repo}.git"
        if "origin" in remotes and "upstream" not in remotes and self.is_release(self.git("remote", "get-url", "origin", cwd=root)):
            # cloned from ORGrove's releases: keep that as upstream (for updates), push the install to the owner's repo
            self.git("remote", "rename", "origin", "upstream", cwd=root)
            remotes = self.git("remote", cwd=root).split()
            self.out(f"  the release you cloned stays as the remote `upstream`; your install goes to {self.repo}")
        if "origin" not in remotes:
            self.git("remote", "add", "origin", url, cwd=root)
        elif self.git("remote", "get-url", "origin", cwd=root).lower().removesuffix(".git") != url.lower()[:-4]:
            raise SystemExit(f"this folder's git remote isn't {url}: run ./install from a fresh copy of ORGrove")
        self.git("add", "-A", ".", cwd=root)
        if self.git("status", "--porcelain", cwd=root):
            self.commit("ORGrove, set up by ./install", cwd=root)
        self.git("push", "-q", "-u", "origin", "HEAD:main", cwd=root)
        self.out(f"  ✓ ORGrove and its config are pushed to {self.repo}")

    def commit(self, message, cwd):
        """git commit, also on a computer where git doesn't know who you are yet (a fresh Linux box or Mac): then as
        your GitHub login, with GitHub's no-reply address, so no personal email is used or asked for."""
        who = []
        try:
            self.git("config", "user.email", cwd=cwd)
        except SystemExit:   # git exits 1 when it isn't set
            login = self.s.get("login") or "orgrove"
            who = ["-c", f"user.name={login}", "-c", f"user.email={login}@users.noreply.github.com"]
        self.git(*who, "commit", "-q", "-m", message, cwd=cwd)

    @staticmethod
    def is_release(remote_url):
        name = re.sub(r"^(https://github\.com/|git@github\.com:)", "", remote_url.strip()).removesuffix(".git").lower()
        return name in {r.lower() for r in RELEASE_REPOS}

    # 8. the wiki
    def wiki(self):
        if not has("wiki"):   # ORGrove Community has no wiki
            return
        if self.s.get("plan") in ("free", "unknown"):
            self.out("\n  (No wiki: on GitHub's free plan wikis exist only on public repos. ORGrove works the same;")
            self.out("   the nightly pages are skipped.)")
            return
        if self.s.get("wiki") is False or not self.yes("\nSet up the wikis for the nightly pages and meeting notes?"):
            self.s["wiki"] = False
            return
        self.step("Wikis")
        for repo in (self.repo, self.project_repo):
            self.gh("api", "-X", "PATCH", f"repos/{repo}", "-F", "has_wiki=true")
            while not self.wiki_exists(repo):
                url = f"https://github.com/{repo}/wiki"
                self.out(f"  GitHub creates a wiki only with its first page. Open {url}, click Create the first page,")
                self.out("  then Save page, and press Enter here (or type skip: the nightly pages are then skipped).")
                self.open_url(url)
                if self.ask_raw("").strip().lower() == "skip":
                    self.s["wiki"] = False
                    self.save()
                    return
            self.out(f"  ✓ {repo} has its wiki")
        self.s["wiki"] = True
        self.save()

    def wiki_exists(self, repo):
        out = subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                              "ls-remote", f"https://github.com/{repo}.wiki.git"], capture_output=True, text=True,
                             check=False)
        return out.returncode == 0 and bool(out.stdout.strip())

    # 9. checks
    def check(self):
        self.step("Checking everything")
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
        for _ in range(10):   # a just-pushed workflow takes a moment to appear on GitHub
            ok, out = self.gh("workflow", "run", "dispatch.yml", "-R", self.repo, "-f", "dry_run=true")
            if ok:
                break
            self.sleep(5)
        else:
            raise SystemExit(f"couldn't start the dispatcher's dry run: {out[:200]}")
        self.out("  a dry run of the dispatcher proves the App's access (it reads only); about a minute")
        run = None
        for _ in range(60):
            self.sleep(5)
            ok, runs = self.gh("run", "list", "-R", self.repo, "--workflow", "dispatch.yml", "--limit", "5",
                               "--json", "databaseId,status,conclusion,createdAt")
            mine = [r for r in runs if r["createdAt"] >= started] if ok and isinstance(runs, list) else []
            if mine and mine[0]["status"] == "completed":
                run = mine[0]
                break
        if not run or run["conclusion"] != "success":
            raise SystemExit(f"the dry run didn't succeed ({run and run['conclusion']}): see "
                             f"https://github.com/{self.repo}/actions, then run ./install again")
        out = subprocess.run([sys.executable, "-m", "orgrove.doctor", "--tick"], cwd=self.root, capture_output=True,
                             text=True, check=False)
        self.out(out.stdout.rstrip())
        if out.returncode:
            raise SystemExit("Fix the lines marked ✗ or ? (each says how), then run ./install again.")

    # 10. on
    def switch_on(self):
        if self.variables(self.repo).get("ORGROVE_ENABLED") == "true":
            return
        if self.yes("\nEverything is ready. Switch the agents on now?"):
            self.set_variable(self.repo, "ORGROVE_ENABLED", "true")
            # Switching the agents on is no event the dispatcher wakes for: it would wait for its next scheduled run, which
            # GitHub can leave hours apart (2026-11-10: nine hours between two). Start it now; a failure is harmless.
            started, _ = self.gh("workflow", "run", "dispatch.yml", "-R", self.repo)
            self.out("  ✓ the agents are on" + (" and the dispatcher is started" if started else ""))
        else:
            self.out(f"  Later: gh variable set ORGROVE_ENABLED -R {self.repo} --body true")

    def meeting_key(self):
        if not has("meetings"):   # ORGrove Community has no meetings
            return
        service = f"orgrove-anthropic/{self.repo}"
        if platform.system() != "Darwin" or not shutil.which("security"):
            return
        if doctor.has_keychain_item(service):
            return
        if not self.key:
            self.out(f"\nFor meetings in the Office, store an API key in your Keychain:\n"
                     f"  security add-generic-password -U -s {service} -a orgrove -w")
            return
        if self.yes("\nUse the same key for meetings in the Office (kept in your macOS Keychain)?"):
            # security -i reads the command from stdin, so the key is never in a process argument
            subprocess.run(["security", "-i"], input=f"add-generic-password -U -s {service} -a orgrove -w {self.key}\n",
                           capture_output=True, text=True, check=False)
            self.out("  ✓ stored" if doctor.has_keychain_item(service) else "  couldn't store it; Office meetings won't start")

    def monitoring_floor(self):
        """Pro: the optional Monitoring floor (Floor 2). Asked last and defaulting to no, so the five questions stay five."""
        if not has("monitoring"):
            return
        if not self.yes("\nAdd the Monitoring floor (Floor 2: watches your AWS with read-only access)? Optional, free to set up", default=False):
            self.out("  Later: bin/orgrove-monitor-setup")
            return
        from orgrove import monitor_setup
        monitor_setup.setup(ask=self.ask_raw, out=self.out)

    def finish(self):
        self.s["done"] = True
        self.save()
        self.out(f"\nORGrove is installed. Give the agents their first ticket:\n  https://github.com/{self.project_repo}/issues/new")
        self.out("Watch them in the Office: bin/orgrove-office")

    def run(self):
        self.out("ORGrove install. It asks five things and does the rest; running it again continues where it stopped.")
        self.preflight()
        prior = self.resume()
        if prior and self.yes(f"Continue the install for {prior['owner']} you started earlier?"):
            self.s = {**prior, "login": self.s["login"]}
        self.questions()
        self.repos()
        self.apps()
        self.secrets()
        self.project()
        self.configure()
        self.wiki()
        self.check()
        self.switch_on()
        self.meeting_key()
        self.monitoring_floor()
        self.finish()


def main(argv=None):
    p = argparse.ArgumentParser(prog="./install", description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=0, help="local port of the installer page (default: any free port). "
                   "Fix it when the browser is on another machine, then forward it: ssh -L 8790:127.0.0.1:8790")
    p.add_argument("--cli", action="store_true", help="ask the questions in this terminal instead of in a page in your browser")
    a = p.parse_args(argv)
    try:
        if a.cli:
            Installer(port=a.port).run()
        else:
            from orgrove import webinstall   # lazy: the terminal installer needs none of it
            webinstall.run(port=a.port)
    except KeyboardInterrupt:
        print("\nStopped. Run ./install again to continue where it stopped.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
