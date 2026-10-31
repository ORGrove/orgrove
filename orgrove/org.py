"""Who this install is (#159): the GitHub owner, this control repo and the App's bot, from config/org.toml [github].

The control repo is the copy of ORGrove that runs the workflows and holds the ledger (and, in Pro, the wiki).
GitHub App names are unique across GitHub, so every customer's App has its own slug and bot login.

    python -m orgrove.org >> "$GITHUB_OUTPUT"   # owner, repo, repo_name, repositories, bot_login, bot_email
"""
import tomllib

from orgrove import manifest

ORG_FILE = manifest.ROOT / "config" / "org.toml"
KEYS = {"owner", "repo", "app_slug", "app_user_id"}
PLACEHOLDER = "YOUR-ORG"   # the owner in a fresh copy's config/org.toml, before `orgrove init` (#159)


def load(path=ORG_FILE, projects=None):
    """The [github] table, validated against every project profile."""
    with open(path, "rb") as f:
        g = tomllib.load(f).get("github", {})
    if missing := KEYS - set(g):
        raise ValueError(f"config/org.toml [github]: missing {sorted(missing)}")
    if unknown := set(g) - KEYS:
        raise ValueError(f"config/org.toml [github]: unknown keys {sorted(unknown)}")
    if g["repo"].count("/") != 1 or g["repo"].split("/")[0] != g["owner"]:
        raise ValueError(f"[github] repo must be {g['owner']}/<name>, got {g['repo']!r}")
    if not isinstance(g["app_user_id"], int):
        raise ValueError("[github] app_user_id must be a number (gh api '/users/<app_slug>[bot]' --jq .id)")
    # One App token covers one owner, so every project repo must live under it.
    for prof in manifest.projects() if projects is None else projects:
        if prof["repo"].split("/")[0] != g["owner"]:
            raise ValueError(f"project {prof['project']}: {prof['repo']} is not under {g['owner']}")
    return g


def people(path=None):
    """Role -> person name (config/people.toml, #137), shared by the Office and meetings."""
    with open(path or manifest.ROOT / "config" / "people.toml", "rb") as f:
        return tomllib.load(f)


def keychain_service(path=ORG_FILE):
    """The macOS Keychain item holding this install's Anthropic key for Office meetings: one per install, so a
    sandbox's meetings are never billed to another install's key (#159). org.toml [meeting] keychain_service
    overrides it (an install that started before #159 keeps the old shared name, "orgrove-anthropic")."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("meeting", {}).get("keychain_service") or f"orgrove-anthropic/{cfg['github']['repo']}"


def owner_checks(path=ORG_FILE):
    """Whether the dispatcher books its own spaced wake-ups while waiting on a person (dispatch.wake_plan). On by
    default; an install with its own timer (an AWS heartbeat, infra/heartbeat.yaml) sets [wake] owner_checks = false (#159)."""
    with open(path, "rb") as f:
        value = tomllib.load(f).get("wake", {}).get("owner_checks", True)
    if not isinstance(value, bool):
        raise ValueError("[wake] owner_checks must be true or false")
    return value


def settings(g):
    """Everything derived from [github], as the workflows and the code use it."""
    login = f"{g['app_slug']}[bot]"
    names = [g["repo"].split("/")[1]] + [p["repo"].split("/")[1] for p in manifest.projects()]
    return {
        "owner": g["owner"],
        "repo": g["repo"],
        "repo_name": g["repo"].split("/")[1],
        "repositories": ",".join(sorted(set(names))),   # every repo the App tokens must reach
        "bot_login": login,
        "bot_author": f"app/{g['app_slug']}",             # how `gh pr list --author` names the App
        "bot_email": f"{g['app_user_id']}+{login}@users.noreply.github.com",
    }


S = settings(load())
OWNER, REPO, BOT_LOGIN, BOT_AUTHOR, BOT_EMAIL = S["owner"], S["repo"], S["bot_login"], S["bot_author"], S["bot_email"]


def main():
    for k, v in S.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    main()
