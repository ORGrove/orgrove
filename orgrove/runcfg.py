"""Resolve one role run's settings for the workflow (#6).

    python -m orgrove.runcfg --project <id> --role qa --tools-file tools.txt >> "$GITHUB_OUTPUT"
"""
import argparse

from orgrove import budget, manifest

# Free time (#122): the runner gathers inputs and posts results; the model only reads.
FREE_TIME_TOOLS = ["Read", "Grep", "Glob"]

# GitHub App token permissions per role. Reviewers get read-only code access, so
# "can't edit the branch" is enforced by GitHub, not only by the skill text.
TOKEN = {
    "write": {"contents": "write", "issues": "write", "pull-requests": "read"},
    "read": {"contents": "read", "issues": "write", "pull-requests": "read"},
}


def resolve(project, role, mode="ticket"):
    prof = manifest.project(project)
    dept = manifest.department(prof["departments"][0])
    # `role` is what the dispatcher dispatched: a skill name. The role that serves this project runs it (#112).
    name = manifest.role_for(dept, role, project)
    if name is None:
        raise SystemExit(f"no role serves {role} for project {project}")
    r = dept["roles"][name]
    if mode == "pr" and r["skill"] not in ("qa", "security"):   # only reviewers read a PR from outside (#82)
        raise SystemExit(f"pr mode is for qa and security, not {r['skill']}")
    # A PR from outside is untrusted: no project programs and no setup, so nothing from its tree runs (#82, #354).
    if r.get("runs_commands") and mode != "pr":  # the project's own test/contract programs (validated in manifest.validate_project)
        r = r | {"tools": r["tools"] + prof.get("tools", [])}
    if mode == "free_time":
        ft = budget.load()["free_time"]
        if name not in ft["roles"] or not prof.get("free_time"):
            raise SystemExit(f"free time is not set up for {name} on {project}")
        r = r | {"model": ft["model"], "ceiling_usd": ft["ceiling_usd"], "tools": FREE_TIME_TOOLS, "contents": "read",
                 "free_time": True}
    owner, repo = prof["repo"].split("/")
    return prof, r, {
        "repo": prof["repo"],
        "repo_owner": owner,
        "repo_name": repo,
        "model": r["model"],
        "ceiling": str(r["ceiling_usd"]),
        "skill": r["skill"],
        "setup": "true" if mode == "pr" else prof["commands"].get("setup", "true"),
        **{f"perm_{k.replace('-', '_')}": v for k, v in TOKEN[r["contents"]].items()},
        # Commenting on a PR needs Pull requests write (probe in orgrove#122: 403 with read, 422 with write).
        # Only the runner posts; the free-time model has Read/Grep/Glob and no shell.
        **({"perm_pull_requests": "write"} if r.get("free_time") else {}),
        "perm_projects": "write" if r["skill"] == "pm" else "read",
        "owner_mention": " @".join(prof.get("owners", [])),
        "kind": mode,
        "max_prs": str(budget.load().get("free_time", {}).get("max_prs", 0)),
    }


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--tools-file", required=True)
    p.add_argument("--mode", choices=["ticket", "free_time", "pr"], default="ticket")
    a = p.parse_args(argv)
    _, r, out = resolve(a.project, a.role, a.mode)
    with open(a.tools_file, "w") as f:
        f.write("\n".join(r["tools"]) + "\n")
    for k, v in out.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    main()
