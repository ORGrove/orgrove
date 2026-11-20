"""The contract test: does a REAL model, given a role's real instructions, produce a result the real validator accepts?

    bin/contract-test                      every role once
    bin/contract-test --roles qa dev       just these
    bin/contract-test --force-refusal dev  also corrupt dev's result on purpose and prove the real correction fixes it

Why it exists (L-021): on 2026-11-10 the first two acceptance runs on real GitHub, with real credits, each found a defect
that no test had: Dev sent a stage label where the outbox wanted an id, and QA was told to name the number of a
sub-issue that did not exist yet. The tests used a scripted fake model and invented numbers, so they passed by construction.
A fake model cannot tell you what a real model does with the instructions you wrote. This can, in a sandbox, before a
person spends a cent on a real install.

What it does, per role: builds the prompt exactly as the workflow does (orgrove.prompt, orgrove.runcfg, the role's skill
and VERDICT.md), runs the real `claude -p` in a small fixture project (QA and Security review a branch with a real bug in
it), then checks `orgrove-out/result.json` with selfcheck.check, the function the apply job uses. A role passes when its
result is accepted, on the first try or after the in-run correction. Nothing touches GitHub: a stand-in `gh` refuses, the
tracker is a fake, and the project's origin is a bare repository in the sandbox.

It needs the `claude` command and an Anthropic login or API key, so it is not part of the unit tests. It costs model usage:
about a dollar's worth for every role once with the cheaper models.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from orgrove import manifest, outbox, selfcheck

ROLES = ("pm", "dev", "qa", "security", "delivery")
PROJECT = "contract"
ISSUE = 1
RUN_URL = "https://example.invalid/contract-test"
SKIP = {"site", "node_modules", ".git", "__pycache__", "out", ".venv", ".pytest_cache", "services", ".claude"}

TICKET = """Add a small calculator module

Goal: a `Calculator` class in `hello/calculator.py` with `add(a, b)`, `subtract(a, b)` and `divide(a, b)`.

## Acceptance criteria
- `add(2, 3)` is 5 and `subtract(5, 3)` is 2
- `divide(6, 3)` is 2; dividing by zero raises `CalculatorError` with the message "Cannot divide by zero"
- tests in `tests/test_calculator.py` prove each of the above
"""

GOOD_CODE = '''class CalculatorError(Exception):
    pass


class Calculator:
    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        return a - b

    def divide(self, a, b):
        if b == 0:
            raise CalculatorError("Cannot divide by zero")
        return a / b
'''
# The branch under review has a real defect: subtract adds, and dividing by zero is not handled. QA must say so.
BUGGY_CODE = GOOD_CODE.replace("return a - b", "return a + b").replace('''        if b == 0:
            raise CalculatorError("Cannot divide by zero")
''', "")
TESTS = '''import unittest

from hello.calculator import Calculator


class CalculatorTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(Calculator().add(2, 3), 5)

    def test_divide(self):
        self.assertEqual(Calculator().divide(6, 3), 2)


if __name__ == "__main__":
    unittest.main()
'''
DELIVERY_QUESTION = """Question from PM on #1: should a ticket that is on hold count against Dev's limit of 2 planned tickets?

The dispatcher counts only tickets being worked on (it skips held ones); the PM skill counts every planned and in-dev ticket.
They disagree, and the PM does not know which rule wins. Nothing else is blocked."""


class Sandbox:
    """A copy of this ORGrove configured like a customer's install, a fixture project, and a stand-in `gh`."""

    def __init__(self, tmp, source=None):
        self.source = Path(source) if source else manifest.ROOT
        self.tmp = Path(tmp)
        self.root = self.tmp / "orgrove"
        self.work = self.tmp / "project"
        self.origin = self.tmp / "origin.git"
        self.bin = self.tmp / "bin"
        self.make_root()
        self.make_project()
        self.make_gh()

    def sh(self, *args, cwd=None, env=None):
        done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False, env=env)
        if done.returncode:
            raise RuntimeError(f"{' '.join(args)[:80]} failed: {(done.stderr or done.stdout)[:300]}")
        return done.stdout.strip()

    def make_root(self):
        def ignore(_, names):
            return [n for n in names if n in SKIP or n.endswith((".pyc", ".png", ".glb"))]
        shutil.copytree(self.source, self.root, ignore=ignore)      # a customer's tree: the Community package, ideally
        from orgrove import init
        answers = {"owner": "contractorg", "repo_name": "orgrove", "app_slug": "contractorg-orgrove", "app_user_id": "1",
                   "client_name": "contractorg", "project": PROJECT, "project_repo": "contractorg/project", "board": "",
                   "owners": "contractor", "setup": "true", "test": "python -m unittest {scope}",
                   "contract": "python -m unittest discover -s tests -t .", "daily_usd": "10", "monthly_usd": "50",
                   "timezone": "UTC", "project_name": "project"}
        (self.root / "projects").mkdir(exist_ok=True)
        init.write(init.ask_all(answers), root=self.root, force=True, out=lambda m: None)

    def make_project(self):
        self.sh("git", "init", "-q", "-b", "main", str(self.work))
        for k, v in (("user.name", "contract"), ("user.email", "contract@example.invalid")):
            self.sh("git", "config", k, v, cwd=self.work)
        (self.work / "hello").mkdir()
        (self.work / "tests").mkdir()
        (self.work / "README.md").write_text("# project\n\nStandard library only.\n")
        (self.work / "hello" / "__init__.py").write_text("")
        (self.work / "tests" / "__init__.py").write_text("")
        self.sh("git", "add", "-A", ".", cwd=self.work)
        self.sh("git", "commit", "-q", "-m", "start", cwd=self.work)
        self.sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.sh("git", "remote", "add", "origin", str(self.origin), cwd=self.work)
        self.sh("git", "push", "-q", "-u", "origin", "main", cwd=self.work)
        self.sh("git", "remote", "set-head", "origin", "main", cwd=self.work)

    def make_gh(self):
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text("#!/bin/sh\necho 'gh is not available in the contract test: work from the files you were given' >&2\nexit 1\n")
        gh.chmod(0o755)

    def env(self):
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "PYTHONPATH": str(self.root)}

    def review_branch(self, code):
        """The branch QA and Security review: the implementation (buggy or not) and its tests. Returns (head, diff)."""
        self.reset_outbox()
        self.sh("git", "checkout", "-q", "-f", "-B", f"agent/{ISSUE}-calculator", "main", cwd=self.work)
        (self.work / "hello" / "calculator.py").write_text(code)
        (self.work / "tests" / "test_calculator.py").write_text(TESTS)
        self.sh("git", "add", "hello", "tests", cwd=self.work)            # never the previous role's orgrove-out
        self.sh("git", "commit", "-q", "-m", "Add the calculator (#1)", cwd=self.work)
        self.sh("git", "push", "-q", "-f", "origin", f"agent/{ISSUE}-calculator", cwd=self.work)
        head = self.sh("git", "rev-parse", "HEAD", cwd=self.work)
        diff = self.sh("git", "diff", "origin/main...HEAD", cwd=self.work)
        return head, diff

    def start_branch(self):
        """Back to a clean main: Dev builds the branch itself."""
        self.reset_outbox()
        self.sh("git", "checkout", "-q", "-f", "main", cwd=self.work)
        if self.sh("git", "branch", "--list", f"agent/{ISSUE}-calculator", cwd=self.work):
            self.sh("git", "branch", "-q", "-D", f"agent/{ISSUE}-calculator", cwd=self.work)

    def reset_outbox(self):
        shutil.rmtree(self.work / "orgrove-out", ignore_errors=True)


# what the ticket looks like to the tracker when the role runs (its labels decide which moves are allowed)
STATE = {"pm": ("needs/plan",), "dev": ("stage/planned",), "qa": ("stage/agent-review",), "security": ("stage/agent-review",),
         "delivery": ("stage/in-dev", "needs/delivery")}


def tracker_for(role):
    return outbox.FakeTracker(STATE[role], open_issues={ISSUE, 2, 3, 7})


def prepare(sb, role, tmp):
    """The files the workflow prepares for a role, and the command to run it: (prompt, role_md, model, ceiling, tools)."""
    d = Path(tmp) / f"inputs-{role}-{len(list(Path(tmp).glob('inputs-*')))}"     # a role can run twice (the forced refusal)
    d.mkdir()
    body = DELIVERY_QUESTION if role == "delivery" else TICKET
    (d / "body.md").write_text(body)
    # PM, Dev and DevOps get the ticket through the ticket brief, built by code from the thread (the workflow runs
    # orgrove.ticket_brief): here from the fixture ticket and one owner comment. Reviewers get the body and the diff only.
    from orgrove import ticket_brief
    issue = {"number": ISSUE, "title": body.splitlines()[0], "body": body, "labels": [{"name": n} for n in STATE[role]]}
    thread = [{"user": {"login": "contractor"}, "created_at": "2026-11-11T09:00:00Z", "body": "Please build this today."}]
    (d / "brief.md").write_text(ticket_brief.build(issue, thread, ["contractor"], "contract-app[bot]")
                                if ticket_brief.for_role(role) else "")
    extra = []
    if role in ("qa", "security"):
        head, diff = sb.review_branch(BUGGY_CODE)
        (d / "review.diff").write_text(diff)
        extra = ["--head", head, "--diff-file", str(d / "review.diff"), "--round", "1"]
    elif role == "dev":
        sb.start_branch()
    cfg = sb.sh(sys.executable, "-m", "orgrove.runcfg", "--project", PROJECT, "--role", role, "--tools-file", str(d / "tools.txt"),
                cwd=sb.root, env=sb.env())
    kv = dict(line.split("=", 1) for line in cfg.splitlines() if "=" in line)
    prompt = sb.sh(sys.executable, "-m", "orgrove.prompt", "--project", PROJECT, "--role", role, "--issue", str(ISSUE),
                   "--body-file", str(d / "body.md"), "--workdir", str(sb.work), "--stable-out", str(d / "stable.md"),
                   "--brief-file", str(d / "brief.md"), "--run-url", RUN_URL, "--outbox", *extra, cwd=sb.root, env=sb.env())
    (d / "prompt.md").write_text(prompt)
    skill = kv["skill"]
    # the workflow's `cat skills/<skill>/SKILL.md skills/VERDICT.md stable.md`
    (d / "role.md").write_text((sb.root / "skills" / skill / "SKILL.md").read_text() + (sb.root / "skills" / "VERDICT.md").read_text()
                               + (d / "stable.md").read_text())
    tools = [t for t in (d / "tools.txt").read_text().splitlines() if t.strip()]
    return d, kv["model"], float(kv["ceiling"]), tools


def pinned_version(source):
    """The Claude Code version the workflow installs."""
    text = (Path(source) / ".github" / "workflows" / "role-run.yml").read_text()
    return re.search(r'CLAUDE_CODE_VERSION:\s*"([^"]+)"', text).group(1)


def pinned_claude(source):
    """The `claude` the workflow runs (its CLAUDE_CODE_VERSION), installed once into ~/.orgrove/contract-claude. A newer
    model needs a newer Claude Code than the one on a person's computer (the PM's model refused 2.1.153), and the test
    is only worth having if it runs what the workflow runs. ORGROVE_CLAUDE overrides it with any command."""
    if os.environ.get("ORGROVE_CLAUDE"):
        return os.environ["ORGROVE_CLAUDE"]
    version = pinned_version(source)
    home = Path.home() / ".orgrove" / "contract-claude"
    binary = home / "node_modules" / ".bin" / "claude"
    have = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=False).stdout if binary.exists() else ""
    if version not in have:
        home.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--prefix", str(home), f"@anthropic-ai/claude-code@{version}"],
                              capture_output=True, text=True, check=False)
        if done.returncode or not binary.exists():
            raise RuntimeError(f"couldn't install Claude Code {version}: {(done.stderr or done.stdout)[-300:]}")
    return str(binary)


def run_claude(sb, d, model, ceiling, tools, claude="claude", timeout=900):
    """The run itself, as the workflow does it. Returns claude's JSON (or a stand-in saying it failed)."""
    cmd = [claude, "-p", "--setting-sources", "user", "--model", model, "--max-budget-usd", f"{ceiling}",
           "--append-system-prompt-file", str(d / "role.md"), "--output-format", "json", "--allowedTools", *tools]
    sb.reset_outbox()
    try:
        done = subprocess.run(cmd, input=(d / "prompt.md").read_text(), capture_output=True, text=True, cwd=sb.work, env=sb.env(),
                              timeout=timeout, check=False)
        return json.loads(done.stdout)
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return {"is_error": True, "result": f"{type(e).__name__}: {e}", "total_cost_usd": 0}


def sabotage(sb, role):
    """Corrupt the model's result the way real mistakes look, so the real correction has something to fix."""
    path = sb.work / "orgrove-out" / "result.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    if role == "dev":
        data["stage_request"] = "no-such-stage"
    else:
        data["merge"] = True
    path.write_text(json.dumps(data))
    return True


def summarise(sb):
    """A line about what the role asked for, for the report: the keys it used and, for a verdict, its decision."""
    try:
        data = json.loads((sb.work / "orgrove-out" / "result.json").read_text())
    except (OSError, ValueError):
        return "no result file"
    parts = sorted(k for k in data if k != "comment")
    v = data.get("verdict")
    if isinstance(v, dict):
        parts.append(f"verdict {v.get('decision')} with {len(v.get('findings', []))} finding(s), {len(data.get('sub_items', []))} sub-item(s)")
    return ", ".join(parts)


def one_role(sb, role, tmp, force_refusal=False, out=print, claude="claude"):
    started = time.time()
    d, model, ceiling, tools = prepare(sb, role, tmp)
    first = run_claude(sb, d, model, ceiling, tools, claude)
    result = {"role": role, "model": model, "forced": force_refusal, "seconds": 0, "cost": float(first.get("total_cost_usd") or 0),
              "outcome": "", "reasons": [], "corrections": 0, "note": "", "asked": ""}
    if first.get("is_error") or not first.get("session_id"):
        result.update(outcome="run failed", note=str(first.get("result"))[:200])
    else:
        result["asked"] = summarise(sb)
        if force_refusal:
            sabotage(sb, role)
        tracker = tracker_for(role)
        saved = manifest.ROOT
        manifest.ROOT = sb.root         # the sandbox's own project profile, as a customer's install would have it
        try:
            outdir = str(sb.work / "orgrove-out")
            checked = selfcheck.run(
                first, lambda: selfcheck.check(PROJECT, role, ISSUE, outdir, tracker=tracker),
                lambda sid, prompt, budget: selfcheck.claude_resume(sid, prompt, budget, model, tools, str(sb.work), str(d / "role.md"),
                                                                    claude=claude),
                ceiling, out=lambda m: out(f"    {m}"))
            result["corrections"] = checked["selfcheck"]["corrections"]
            result["reasons"] = checked["selfcheck"]["refused"] or []
            result["cost"] = float(checked.get("total_cost_usd") or 0)
            result["outcome"] = ("refused" if result["reasons"] else "accepted first time" if not result["corrections"]
                                 else f"accepted after {result['corrections']} correction(s)")
        finally:
            manifest.ROOT = saved
    result["seconds"] = round(time.time() - started)
    return result


def report(results):
    lines = [f"{'role':<10}{'outcome':<34}{'corrections':<13}{'cost':<9}{'time':<7}note", "-" * 90]
    for r in results:
        label = r["role"] + (" (forced)" if r["forced"] else "")
        lines.append(f"{label:<10}{r['outcome']:<34}{r['corrections']:<13}${r['cost']:<8.2f}{r['seconds']:<7}"
                     + ("; ".join(r["reasons"])[:60] if r["reasons"] else r["note"] or r.get("asked", "")))
    return "\n".join(lines)


def passed(results):
    """Every role's result ended up accepted, and a forced refusal was fixed by a real correction (not just accepted)."""
    return all(r["outcome"].startswith("accepted") and (not r["forced"] or r["corrections"] >= 1) for r in results)


def main(argv=None):
    p = argparse.ArgumentParser(prog="bin/contract-test", description=__doc__.split("\n")[0])
    p.add_argument("--roles", nargs="+", choices=ROLES, default=list(ROLES))
    p.add_argument("--force-refusal", nargs="*", choices=ROLES, default=[], metavar="ROLE",
                   help="also corrupt this role's result on purpose and prove the real correction fixes it")
    p.add_argument("--json", metavar="FILE", help="write the results here")
    p.add_argument("--source", metavar="DIR", help="the ORGrove tree to copy into the sandbox (default: this one). Use a built "
                   "Community package for the fidelity of a customer's install: the publisher's tree has its own two-PM setup")
    a = p.parse_args(argv)
    if not shutil.which("npm") and not os.environ.get("ORGROVE_CLAUDE"):
        print("npm is needed to install the Claude Code the workflow uses (or set ORGROVE_CLAUDE to a claude command)", file=sys.stderr)
        return 2
    source = a.source or str(manifest.ROOT)
    claude = pinned_claude(source)
    print(f"using {claude}", flush=True)
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp, a.source)
        runs = [(r, False) for r in a.roles] + [(r, True) for r in a.force_refusal]
        for role, forced in runs:
            print(f"== {role}{' (forced refusal)' if forced else ''}", flush=True)
            results.append(one_role(sb, role, tmp, forced, out=lambda m: print(m, flush=True), claude=claude))
            print(f"   -> {results[-1]['outcome']} (${results[-1]['cost']:.2f}, {results[-1]['seconds']}s)", flush=True)
    print("\n" + report(results))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1))
    ok = passed(results)
    print("\n" + ("CONTRACT PASSES: every role's real result was accepted." if ok else "CONTRACT FAILS: see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
