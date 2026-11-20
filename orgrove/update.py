"""Take a newer ORGrove release into an installed copy, as a pull request a person merges (plan phase C4).

    bin/orgrove-update                    uses the `upstream` remote (a copy you cloned) to find the newer release
    bin/orgrove-update --from ../orgrove-release   a newer release you downloaded and unpacked (a ZIP install)
    bin/orgrove-update --dry-run          says what would change and changes nothing

How it decides, file by file. `bin/package` records a fingerprint of every file it ships (.orgrove-manifest.json).
A file you have not touched since is replaced by the new release's. A file you changed is KEPT and listed, so your
edits are never overwritten, and a file the release removed goes only if you never touched it. Your own settings are
never touched or reported: config/org.toml, the spend caps, projects/, lessons/ and what the installer wrote.
The update runs on its own branch, the config doctor checks the result before anything leaves this computer, and a
pull request is opened: a person merges every change.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from orgrove import licence

MANIFEST = ".orgrove-manifest.json"
MARKER = ".orgrove-install"
SKIP_PARTS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
# Yours, not the release's: the installer wrote them or you edit them. Never replaced, never reported.
OWNED = ("config/org.toml", "config/budget.toml", "projects/", "lessons/", "config/other/", "config/licence.json")


def owned(path):
    return any(path == o or (o.endswith("/") and path.startswith(o)) for o in OWNED)


def fingerprint(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def tree(root):
    """{relative path: fingerprint} of the files in a copy, without git's own files, caches, the manifest or the marker."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_PARTS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if rel in (MANIFEST, MARKER) or p.is_symlink():
                continue
            out[rel] = fingerprint(p)
    return out


def write_manifest(root):
    root = Path(root)
    (root / MANIFEST).write_text(json.dumps(tree(root), indent=1, sort_keys=True) + "\n")


def read_manifest(root):
    try:
        data = json.loads((Path(root) / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def marker(root):
    """(release id, edition) from .orgrove-install, or (None, None)."""
    try:
        text = (Path(root) / MARKER).read_text()
    except OSError:
        return None, None
    sha = re.search(r"^packaged from (\S+)", text, re.M)
    ed = re.search(r"^edition = (community|pro)$", text, re.M)
    return sha and sha.group(1), ed and ed.group(1)


class Plan:
    def __init__(self):
        self.add, self.replace, self.remove = [], [], []
        self.kept = []         # you changed it and the release did too: yours stays, and it is listed
        self.kept_gone = []    # you deleted it and the release still ships it: it stays deleted

    @property
    def changes(self):
        return len(self.add) + len(self.replace) + len(self.remove)


def make_plan(installed, release):
    """What the update does, from the installed copy, the release's files, and the manifest of what was shipped."""
    shipped = read_manifest(installed)
    if shipped is None:
        raise UpdateError("this copy has no record of what it shipped with (.orgrove-manifest.json), so an update could "
                          "overwrite your changes. Install a newer release once; updates work from then on.")
    mine, new, plan = tree(installed), tree(release), Plan()
    for path in sorted(new):
        if owned(path):
            continue
        if path not in mine:
            if path not in shipped:
                plan.add.append(path)
            elif shipped[path] != new[path]:     # you deleted it, and the release changed it since: said once
                plan.kept_gone.append(path)
        elif mine[path] == new[path]:
            continue
        elif path in shipped and mine[path] == shipped[path]:
            plan.replace.append(path)            # untouched since it shipped: the release's version
        elif shipped.get(path) != new[path]:
            plan.kept.append(path)               # you changed it and so did the release
    for path in sorted(shipped):
        if path in new or owned(path) or path not in mine:
            continue
        if mine[path] == shipped[path]:
            plan.remove.append(path)             # the release dropped it and you never touched it
        else:
            plan.kept.append(path)
    return plan


def apply_plan(installed, release, plan):
    installed, release = Path(installed), Path(release)
    shipped = read_manifest(installed) or {}
    new = tree(release)
    for path in plan.add + plan.replace:
        dest = installed / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(release / path, dest)       # keeps the executable bit of bin/ scripts
        shipped[path] = new[path]
    for path in plan.remove:
        (installed / path).unlink()
        shipped.pop(path, None)
        parent = (installed / path).parent
        while parent != installed and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    for path in plan.kept_gone + [p for p in plan.kept if p in new]:
        shipped[path] = new[path]                 # told once: not reported again until the release changes it again
    (installed / MANIFEST).write_text(json.dumps(dict(sorted(shipped.items())), indent=1) + "\n")
    if (release / MARKER).exists():
        shutil.copy2(release / MARKER, installed / MARKER)


class UpdateError(SystemExit):
    pass


def summary(plan, old_id, new_id):
    def lines(title, paths, cap=40):
        if not paths:
            return []
        shown = [f"- `{p}`" for p in paths[:cap]] + ([f"- … and {len(paths) - cap} more"] if len(paths) > cap else [])
        return [f"**{title}** ({len(paths)})", *shown, ""]
    out = [f"Release `{str(old_id)[:7]}` to `{str(new_id)[:7]}`.", ""]
    out += lines("Updated", plan.replace) + lines("Added", plan.add) + lines("Removed (the release dropped them)", plan.remove)
    out += lines("Left as you have it: you changed these and the release did too", plan.kept)
    out += lines("Left deleted: you removed these and the release still ships them", plan.kept_gone)
    out += ["Your settings (`config/org.toml`, the spend caps, `projects/`, `lessons/`) are never touched.",
            "A person merges every change: review the diff, then merge."]
    return "\n".join(out)


def run_git(*args, cwd):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if out.returncode:
        raise UpdateError(f"git {' '.join(args[:2])} failed: {(out.stderr or out.stdout).strip()[:200]}")
    return out.stdout.strip()


def run_gh(*args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return out.returncode == 0, (out.stdout or out.stderr).strip()


def run_doctor(root):
    out = subprocess.run([sys.executable, "-m", "orgrove.doctor", "--config"], cwd=root, capture_output=True, text=True,
                         check=False)
    return out.returncode == 0, (out.stdout or out.stderr).strip()


def fetch_release(root, source, git, tmp):
    """The newer release as a folder: the one given, or `upstream`'s default branch checked out into a temp folder."""
    if source:
        path = Path(source).expanduser().resolve()
        if not (path / MARKER).exists():
            raise UpdateError(f"{path} isn't an ORGrove release (no {MARKER}): unpack a release and point --from at its folder")
        return path
    if "upstream" not in git("remote", cwd=root).split():
        raise UpdateError("this copy has no `upstream` remote (a ZIP install has none). Download the newer release, unpack it, "
                          "and run: bin/orgrove-update --from <that folder>")
    git("fetch", "-q", "upstream", cwd=root)
    try:
        branch = git("symbolic-ref", "--short", "refs/remotes/upstream/HEAD", cwd=root).split("/", 1)[-1]
    except SystemExit:           # a remote that never said which branch is its default
        branch = "main"
    dest = Path(tmp) / "release"
    dest.mkdir()
    archive = subprocess.run(["git", "archive", f"upstream/{branch}"], cwd=root, capture_output=True, check=False)
    if archive.returncode:
        raise UpdateError(f"couldn't read upstream/{branch}: {archive.stderr.decode()[:200]}")
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    return dest


def run_ok(fn, *args, **kw):
    try:
        fn(*args, **kw)
        return True
    except SystemExit:
        return False


def update(root, source=None, dry_run=False, out=print, git=run_git, gh=run_gh, doctor=run_doctor):
    """Returns the pull request's address, or None when there is nothing to do or it was a dry run."""
    root = Path(root).resolve()
    if not (root / ".git").exists():
        raise UpdateError("this folder is not a git copy of your ORGrove install: run bin/orgrove-update in the folder "
                          "`./install` ran from (its git remote `origin` is your orgrove repo)")
    old_id, edition = marker(root)
    if not edition:
        raise UpdateError("this isn't an ORGrove install (no .orgrove-install)")
    if edition == "pro" and not (lic := licence.status(root)).runs:   # #426: a new Pro release needs a licence
        raise UpdateError(f"ORGrove Pro updates need a licence that is running, and {lic.why}. Put the "
                          f"{licence.FILE} you were sent in this folder (or write to {licence.CONTACT}), then update")
    if git("status", "--porcelain", cwd=root):
        raise UpdateError("this folder has changes that aren't committed: commit or discard them, then update")
    with tempfile.TemporaryDirectory() as tmp:
        release = fetch_release(root, source, git, tmp)
        new_id, new_edition = marker(release)
        if new_edition != edition:
            raise UpdateError(f"that release is the {new_edition} edition and this install is {edition}: an update never "
                              "changes the edition")
        plan = make_plan(root, release)
        if plan.changes == 0 and new_id == old_id:
            out(f"Already up to date ({str(old_id)[:7]}).")
            return None
        out(summary(plan, old_id, new_id))
        if dry_run:
            out("\n(dry run: nothing was changed)")
            return None
        if plan.changes == 0:
            out("\nNothing to change in your files; the release only records a newer version. Not opening a pull request.")
            return None
        original = git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
        branch = f"orgrove/update-{str(new_id)[:7]}"
        if git("branch", "--list", branch, cwd=root):
            raise UpdateError(f"the branch {branch} from an earlier update is still here: merge or delete it "
                              f"(git branch -D {branch}), then update again")
        git("switch", "-q", "-c", branch, cwd=root)
        try:
            apply_plan(root, release, plan)
            ok, said = doctor(root)
            if not ok:
                raise UpdateError("the updated copy fails its own config check, so nothing was pushed:\n" + said[-600:])
            git("add", "-A", ".", cwd=root)
            git("-c", "user.name=ORGrove", "-c", "user.email=noreply@orgrove.local", "commit", "-q", "-m",
                f"ORGrove: update to {str(new_id)[:7]}", cwd=root)
            git("push", "-q", "-u", "origin", branch, cwd=root)
            ok, url = gh("pr", "create", "--head", branch, "--title", f"ORGrove: update to {str(new_id)[:7]}",
                         "--body", summary(plan, old_id, new_id))
            if not ok:
                raise UpdateError(f"the update is pushed as the branch {branch}, but the pull request couldn't be opened: {url[:200]}")
        except BaseException:
            git("reset", "-q", "--hard", cwd=root)       # the tree was clean at the start: only our own changes go
            git("clean", "-qfd", cwd=root)
            git("switch", "-q", original, cwd=root)
            run_ok(git, "branch", "-q", "-D", branch, cwd=root)
            raise
        git("switch", "-q", original, cwd=root)
        out(f"\nPull request: {url}\nMerge it when you have read the diff. Nothing changes in your install until you do.")
        return url


def main(argv=None):
    p = argparse.ArgumentParser(prog="bin/orgrove-update", description=__doc__.split("\n")[0])
    p.add_argument("--from", dest="source", help="a newer release you downloaded and unpacked (default: the `upstream` remote)")
    p.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")
    p.add_argument("--write-manifest", metavar="DIR", help="(used by bin/package) record what a release ships")
    p.add_argument("--root", default=".", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if a.write_manifest:
        write_manifest(a.write_manifest)
        return 0
    try:
        update(a.root, a.source, a.dry_run)
    except UpdateError as e:
        print(f"update stopped: {e.code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
