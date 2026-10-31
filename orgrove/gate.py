"""Open the PR only when QA and Security both approved the current head (#6) and CI on it is green (#151).

    python -m orgrove.gate --repo owner/repo --issue 123
        exit 0 = PR opened (or already open), exit 3 = not ready (reasons printed)
"""
import argparse
import json
import re
import subprocess
import sys

from orgrove import ci, manifest, office, org, verdict

NOT_READY = 3


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def latest_verdicts(comments, head):
    """role -> latest valid verdict for this head, from comments by the App only."""
    found = {}
    for c in comments:
        if c["user"]["login"] != org.BOT_LOGIN:
            continue
        v = verdict.parse(c["body"], expected_head=head)
        if v:
            found[v["role"]] = v
    return found


def readiness(verdicts, open_sub_issues, ci_state="green"):
    """Why the head may not become a PR yet; empty = ready. ci_state is orgrove.ci.ci_state of the head."""
    reasons = []
    if ci_state != "green":
        reasons.append(f"CI on the head is {ci_state}")
    for role in ("qa", "security"):
        v = verdicts.get(role)
        if not v:
            reasons.append(f"no valid {role} verdict for the current head")
        elif v["decision"] != "approve":
            reasons.append(f"{role} requested changes")
    if open_sub_issues:
        reasons.append("open sub-issues: " + ", ".join(f"#{n}" for n in open_sub_issues))
    return reasons


def infra_files(files, patterns):
    """Changed files that are infrastructure/IAM code, so the owner knows a merge reaches AWS."""
    from fnmatch import fnmatch
    return [f for f in files if any(fnmatch(f, p) for p in patterns)]


# Agent harness, hooks, rules and CI (#116): an agent diff touching these never becomes a PR.
PROTECTED = [".claude/*", "*/.claude/*", ".wolf/hooks/*", ".mcp.json", "CLAUDE.md", "*/CLAUDE.md", ".github/*"]


def protected_files(files):
    from fnmatch import fnmatch
    return [f for f in files if any(fnmatch(f, p) for p in PROTECTED)]


# An owner's comment approving one exact head lets that head's protected files through (2026-10-28, #691).
# Agents comment as the App, never as an owner, so they can't approve their own protected change; the owner's
# code-owner review of the PR is still required after this.
APPROVE_PROTECTED = re.compile(r"orgrove:approve-protected\s+([0-9a-f]{7,40})\b")


def owner_protected_approval(comments, owners, head):
    """The owner comment that approves protected files on exactly this head, or None."""
    owners = {o.lower() for o in owners}
    for c in reversed(comments):
        if c["user"]["login"].lower() not in owners:
            continue
        for sha in APPROVE_PROTECTED.findall(c.get("body") or ""):
            if head.startswith(sha):
                return c
    return None


def pr_body(issue, head, verdicts, closed_sub_issues, infra=(), protected=(), approval=None):
    # "Refs", not "Closes": the ticket stays open through the Dev deploy until Sanity verifies it.
    lines = []
    if protected:
        lines += ["> **🔒 Protected files (agent harness, rules or CI), approved by "
                  f"@{approval['user']['login']} for this exact head:** {approval.get('html_url', '')}",
                  "> " + ", ".join(f"`{f}`" for f in protected[:12]) + (" …" if len(protected) > 12 else ""), ""]
    if infra:
        lines += ["> **⚠️ Infrastructure / IAM change: merging applies it to AWS through CI.**",
                  "> " + ", ".join(f"`{f}`" for f in infra[:12]) + (" …" if len(infra) > 12 else ""), ""]
    lines += [f"Refs #{issue}.", "", f"Reviewed head: `{head}`", ""]
    for role in ("qa", "security"):
        v = verdicts[role]
        notes = [f["title"] for f in v.get("findings", []) if f.get("severity") != "blocking"]
        lines.append(f"- **{role.upper()}:** approved (round {v['round']})" + (f"; notes: {'; '.join(notes)}" if notes else ""))
    if closed_sub_issues:
        lines += ["", "Sub-issues raised and fixed: " + ", ".join(f"#{n}" for n in closed_sub_issues)]
    lines += ["", "Opened by ORGrove after both agent reviews. **A human merges.**"]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", type=int, required=True)
    a = p.parse_args(argv)

    heads = gh("api", f"repos/{a.repo}/git/matching-refs/heads/agent/{a.issue}-", "-q", ".[].ref").split()
    if len(heads) != 1:
        print(f"expected one agent/{a.issue}-* branch, found {heads}")
        return NOT_READY
    branch = heads[0].removeprefix("refs/heads/")
    head = gh("api", f"repos/{a.repo}/commits/{branch}", "-q", ".sha").strip()
    comments = json.loads(gh("api", "--paginate", "--slurp", f"repos/{a.repo}/issues/{a.issue}/comments"))
    comments = [c for page in comments for c in page]
    subs = json.loads(gh("api", f"repos/{a.repo}/issues/{a.issue}/sub_issues"))
    open_subs = [s["number"] for s in subs if s["state"] == "open"]
    closed_subs = [s["number"] for s in subs if s["state"] == "closed"]

    verdicts = latest_verdicts(comments, head)
    reasons = readiness(verdicts, open_subs, ci.ci_state(ci.head_checks(a.repo, head, office.gh_json)))
    if reasons:
        print("not ready: " + "; ".join(reasons))
        return NOT_READY
    title = gh("api", f"repos/{a.repo}/issues/{a.issue}", "-q", ".title").strip()
    existing = gh("pr", "list", "-R", a.repo, "--head", branch, "--json", "url", "-q", ".[].url").strip()
    # Requesting the owners' review is what notifies them (GitHub Mobile / email), so they can merge from anywhere.
    reviewers = [arg for o in manifest.owners_for_repo(a.repo) for arg in ("--reviewer", o)]
    base = gh("api", f"repos/{a.repo}", "-q", ".default_branch").strip()
    changed = gh("api", f"repos/{a.repo}/compare/{base}...{branch}", "-q", ".files[].filename").split()
    blocked = protected_files(changed)
    approval = owner_protected_approval(comments, manifest.owners_for_repo(a.repo), head) if blocked else None
    if blocked and approval:
        print(f"protected files approved by {approval['user']['login']} for {head}: " + ", ".join(blocked))
    elif blocked:
        owners = " ".join(f"@{o}" for o in manifest.owners_for_repo(a.repo))
        gh("issue", "edit", str(a.issue), "-R", a.repo, "--add-label", "needs/human")
        gh("issue", "comment", str(a.issue), "-R", a.repo, "--body",
           f"{owners} ORGrove gate refused to open a PR: the branch changes protected files "
           f"(agent harness, hooks, rules or CI): {', '.join(f'`{f}`' for f in blocked[:12])}. A human decides. "
           f"To let this exact head through, an owner comments `orgrove:approve-protected {head[:12]}`.")
        print("not ready: protected files changed: " + ", ".join(blocked))
        return NOT_READY
    infra = infra_files(changed, manifest.infra_paths_for_repo(a.repo))
    url = existing or gh("pr", "create", "-R", a.repo, "--head", branch,
                         "--title", ("[infra] " if infra else "") + title,
                         "--body", pr_body(a.issue, head, verdicts, closed_subs, infra,
                                                  blocked if approval else (), approval), *reviewers).strip()
    gh("issue", "edit", str(a.issue), "-R", a.repo, "--remove-label", "stage/agent-review",
       "--add-label", "stage/awaiting-review")
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
