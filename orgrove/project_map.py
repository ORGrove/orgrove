"""A project map made by code, no model call (plan 2026-11-05-agent-context-budget, step 2).

The roles that build or plan (Dev, DevOps, PM) got the repo and had to explore it on every run: list the folders,
find the test command, open the Makefile. This gives them the answer up front, from the checked-out default branch:
the folders with their size, the commands the repo itself declares (Makefile targets, package.json scripts,
docker-compose services, CI workflows) and the key files. It is capped (MAX_CHARS) so it stays a map, not the code,
and it changes only when the repo's layout does, so the prompt cache keeps it between runs.

    python -m orgrove.project_map work/        # prints the map
"""
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

MAX_CHARS = 5000          # about 1,300 tokens: Aider's default repo map is ~1,000
ROLES = ("dev", "devops", "pm")   # pm also covers pm-orgrove; QA, Security and Sanity judge a diff and don't get it
KEY_FILES = ("README.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", "Makefile", "package.json", "pyproject.toml",
             "docker-compose.yml", "compose.yml", "Dockerfile", "requirements.txt", "go.mod", "Cargo.toml")


def for_role(role):
    return role in ROLES or role.startswith("pm")


def tracked(root):
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"], check=True, capture_output=True, text=True).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [p for p in out.splitlines() if p]


def _folders(files):
    tops, subs = {}, {}
    for p in files:
        parts = p.split("/")
        if len(parts) == 1:
            continue
        tops[parts[0]] = tops.get(parts[0], 0) + 1
        if len(parts) > 2:
            key = (parts[0], parts[1])
            subs[key] = subs.get(key, 0) + 1
    lines = []
    for top, n in sorted(tops.items(), key=lambda kv: (-kv[1], kv[0]))[:14]:
        kids = sorted(((s, k) for (t, s), k in subs.items() if t == top), key=lambda kv: (-kv[1], kv[0]))[:6]
        inner = ", ".join(f"{s}/ ({k})" for s, k in kids)
        lines.append(f"- `{top}/` ({n} files)" + (f": {inner}" if inner else ""))
    # One level deeper for the few big subfolders, where the code is (a client repo's api/ folder had 696 files).
    for (top, sub), n in sorted(subs.items(), key=lambda kv: -kv[1])[:3]:
        if n < 50:
            break
        deep = {}
        for p in files:
            parts = p.split("/")
            if len(parts) > 3 and parts[0] == top and parts[1] == sub:
                deep[parts[2]] = deep.get(parts[2], 0) + 1
        inner = ", ".join(f"{d}/ ({k})" for d, k in sorted(deep.items(), key=lambda kv: (-kv[1], kv[0]))[:8])
        if inner:
            lines.append(f"- `{top}/{sub}/`: {inner}")
    return lines


def _commands(root, files):
    out = []
    for mk in sorted(p for p in files if p.split("/")[-1] == "Makefile" and p.count("/") <= 1)[:3]:
        targets = re.findall(r"^([A-Za-z0-9][\w.-]*):(?!=)", (root / mk).read_text(errors="replace"), re.M)
        if targets:
            out.append(f"- `{mk}` targets: " + ", ".join(dict.fromkeys(targets[:20])))
    for pj in sorted(p for p in files if p.split("/")[-1] == "package.json" and p.count("/") <= 2
                     and "node_modules" not in p)[:4]:
        try:
            scripts = json.loads((root / pj).read_text()).get("scripts") or {}
        except (ValueError, OSError):
            continue
        if scripts:
            out.append(f"- `{pj}` scripts: " + ", ".join(f"`{k}`" for k in list(scripts)[:15]))
    for py in sorted(p for p in files if p.split("/")[-1] == "pyproject.toml" and p.count("/") <= 3)[:3]:
        try:
            tools = sorted((tomllib.loads((root / py).read_text()).get("tool") or {}))
        except (tomllib.TOMLDecodeError, OSError):
            continue
        if tools:
            out.append(f"- `{py}` configures: " + ", ".join(tools))
    for dc in sorted(p for p in files if p.split("/")[-1] in ("docker-compose.yml", "compose.yml") and p.count("/") <= 1)[:2]:
        block = re.search(r"^services:\s*\n(.*?)(?=^\S|\Z)", (root / dc).read_text(errors="replace"), re.M | re.S)
        services = re.findall(r"^  ([A-Za-z0-9][\w.-]*):\s*$", block.group(1), re.M) if block else []
        if services:
            out.append(f"- `{dc}` services: " + ", ".join(services[:15]))
    flows = sorted(p.split("/")[-1] for p in files if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml")))
    if flows:
        out.append("- CI workflows (`.github/workflows/`): " + ", ".join(flows[:20]))
    return out


def build(root):
    """The map of the repo checked out at `root`, or "" when it isn't a git checkout."""
    root = Path(root)
    files = tracked(root)
    if not files:
        return ""
    keys = [p for p in files if p.split("/")[-1] in KEY_FILES and p.count("/") <= 1]
    lines = ["## Project map", "",
             f"Made by ORGrove from the checked-out default branch ({len(files)} tracked files), by code, not by a "
             "model. Use it instead of exploring; open a file when you need its contents.", "",
             "**Folders** (files in each, and the biggest subfolders):", *_folders(files)]
    cmds = _commands(root, files)
    if cmds:
        lines += ["", "**Commands the repo declares:**", *cmds]
    if keys:
        lines += ["", "**Key files:** " + ", ".join(f"`{k}`" for k in sorted(keys)[:20])]
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n- (map cut at its size limit)"


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "."))
