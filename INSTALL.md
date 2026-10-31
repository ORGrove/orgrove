# Installing ORGrove

ORGrove runs in **your own GitHub organisation**, with **your own Anthropic API key**. Nothing runs on our servers:
the agents work in your GitHub Actions, and the Office runs on your computer. One command installs it; this guide
explains what it does, what it costs, and how to fix what can go wrong. The last section lists every step by hand,
for IT teams who want to see or repeat them.

**Try it in a test organisation first:** a free GitHub organisation with no other code in it, and a $10 limit on
the Anthropic key. About 15 minutes.

## 1. Before you start

On your computer (macOS or Linux):

- **Python 3.11 or newer.** Check with `python3 --version`. On a Mac `python3` is often 3.9: install a newer one
  (`brew install python@3.12`). `./install` finds it by itself.
- **git**: `git --version`.
- **The GitHub CLI 2.40 or newer**, logged in: `gh auth login`. On Linux, check `gh --version`: the packages in
  Ubuntu 22.04 and Debian 12 are too old, so install it from https://cli.github.com (Ubuntu 24.04's is fine). The installer asks GitHub for any extra access it needs, and GitHub
  shows you what it is before you approve it.

On the web:

- **A GitHub organisation you own.** The free plan is enough. To make one: github.com/account/organizations/new?plan=free.
- **An Anthropic API key with a spend limit.** At platform.claude.com: **Settings > Workspaces > Create workspace**
  (for example `orgrove`), set its **spend limit** (for a test, $10 a month), then **Settings > API keys > Create key**
  in that workspace. Copy the key; it is shown once. A Claude subscription login (Pro, Max, Team) can't be used:
  Anthropic doesn't allow it in other products.

## 2. Install

```sh
git clone https://github.com/ORGrove/orgrove
cd orgrove
./install
```

Or download the ZIP from GitHub, unpack it and run `./install` in that folder. Your install goes to a new `orgrove`
repo in your organisation; the copy you cloned stays linked as `upstream`, for updates.

It asks five things:

1. **Your GitHub organisation**: the name in `github.com/<name>`.
2. **Your Anthropic API key.** Paste it; it stays hidden. It goes straight into a GitHub secret and is never saved
   on your computer or shown again. Anthropic is asked once whether the key works.
3. **The repo the agents work on.** Press Enter for a small sample project (recommended for a first try), or name
   an existing repo of yours. An existing repo is never changed directly: the installer opens a pull request with
   what it needs (a CI workflow and a small notify workflow), for you to merge.
4. **Spend caps**: a daily and a monthly limit in US dollars (defaults $10 and $50).
5. **Who may give the agents work**: GitHub logins, comma-separated. Issues and comments from anyone else are read
   as data, never as instructions.

Then your browser opens **twice**, once for each GitHub App ORGrove uses. Each time, click **Create** and then
**Install** on GitHub's own page. You never download a key or copy an ID.

**No browser on that computer** (a Linux server over SSH, a container)? Fix the port of those pages and forward it
to the computer you browse on: `ssh -L 8790:127.0.0.1:8790 you@server`, then on the server `./install --port 8790`,
and open the address it prints. GitHub sends you back to that same address, so it must reach the installer.

The installer then sets everything else up, checks it with a dry run of the dispatcher (about a minute; it calls no model, so no Claude cost),
and asks whether to switch the agents on. If it stops for any reason, run `./install` again: it continues where it
stopped.

**What it creates:**

| | Where |
|---|---|
| The **control repo**, `orgrove`: ORGrove's workflows, your settings and the spend ledger | your organisation, private |
| The **project repo** the agents work on (the sample is `hello`) | your organisation, private |
| The **ORGrove App**: the agents act as it; its key is only in a secret of the control repo | your organisation |
| The **notify App**: starts ORGrove the moment a person acts; it can do nothing else | your organisation |
| Settings: `config/` and `projects/<project>.toml` in the control repo | the control repo |

Progress (names and IDs only, never a key) is kept in `~/.orgrove/installs/`.

## 3. Your first ticket

Open an issue in the project repo, as one of the people you named in question 5, describing what you want. Then:

1. The **PM** agent plans it: acceptance criteria, scope, a security note. If it needs a decision from you, it asks
   in a comment and labels the issue `needs/human`; answer in a comment and it carries on.
2. The **Developer** agent builds it on a branch, with tests.
3. **QA** and **Security** review it. They can only read.
4. A pull request comes to you. **A person merges every change**: the agents never do.

Watch them in the **Office** on your computer: `bin/orgrove-office` in the downloaded folder. It opens in your
browser at http://127.0.0.1:8730 and shows who is working on what, live from GitHub.

## 4. Costs and safety

| | Paid to | How much |
|---|---|---|
| **Claude** (the agents) | Anthropic, on your key | A small ticket, from plan to merged PR, costs about **$1**. Your caps stop the spending |
| **GitHub Actions minutes** | GitHub | Every agent run and dispatcher tick is a workflow run. Private repos use your plan's free minutes first |

- **Caps, enforced twice.** ORGrove's own daily and monthly caps (`config/budget.toml`): before any run starts it
  reserves that run's full ceiling, and when a cap is reached nothing starts and the ticket is labelled
  `blocked/budget`. Anthropic's workspace limit is the second stop behind it.
- **When idle, ORGrove sleeps.** A person's ticket, comment or merge wakes it at once; a 30-minute schedule is the
  backstop. An idle check costs no Claude money, only a GitHub Actions minute.
- **Reviewers can only read**: GitHub, not a prompt, stops them from editing.
- **A list of things agents never do**, in `projects/<project>.toml` under `[limits]`: deploying to production,
  changing cloud accounts, editing `.github/workflows`, disabling tests, public posts or customer-visible publishing.
  A ticket that needs one of them comes to you as a question.
- **The kill switch** stops every agent at the next tick:

  ```sh
  gh variable set ORGROVE_ENABLED -R YOUR-ORG/orgrove --body false
  ```

- **Your data** stays in your GitHub; Claude sees what a run sends it, under your Anthropic account.
  OpsVision AI Systems Pvt Ltd, who make ORGrove, receive nothing.

## 5. Troubleshooting

Start with `python3 -m orgrove.doctor --tick` in the downloaded folder: most problems are a ✗ line there, with its
fix.

| What you see | Why | Fix |
|---|---|---|
| `./install` says **this copy isn't an ORGrove Community or Pro release** | The folder wasn't downloaded as a release | Download a release and run `./install` from it |
| **that doesn't look like ... / Anthropic refused that key** | Empty paste, a subscription login, or a revoked key | Paste an API key from platform.claude.com > API keys |
| GitHub won't create an App because **its name is taken** | An earlier install's App is still there | Delete it at your organisation's **Settings > Developer settings > GitHub Apps**, then run `./install` again |
| **Nothing happens** after you open an issue | The agents are switched off, or you aren't one of the people named in question 5 | Check `ORGROVE_ENABLED` is `true` (`gh variable list -R YOUR-ORG/orgrove`) and who may give work (`projects/<project>.toml`, `owners`) |
| A ticket is labelled **`needs/human`** | An agent has a question only you can answer | Answer it in a comment; it carries on at the next tick |
| A ticket is labelled **`blocked/budget`** | Today's or this month's cap is reached | Wait for the next day, or raise the cap in `config/budget.toml` |
| A ticket stays at review **"waiting for CI"** | QA and Security start only when CI on the agent's branch is green | Add CI that runs on push to `agent/**` (by hand, step A) |
| `doctor`: **N labels missing** | ORGrove's labels aren't in the repo yet | `python3 -m orgrove.sync_labels YOUR-ORG/YOUR-PROJECT` (add `--control` for the control repo) |
| Agent runs **fail within seconds** | Anthropic's workspace limit is reached (separate from ORGrove's caps) | Raise it on platform.claude.com, or wait for its reset |
| The **Office doesn't open** at 127.0.0.1:8730 | Another program has that port; the Office then takes the next free one | Use the address it prints, or choose one: `bin/orgrove-office --port 8740` |

Still stuck? Write to **info@orgrove.com** with the output of `python3 -m orgrove.doctor`.

## 6. By hand (for IT teams)

`./install` does all of this; these are the same steps, for teams that want to see or repeat them.

**A. CI on the agents' branches.** The developer agent pushes to `agent/<ticket>-…` branches, and QA and Security
start only when CI there is green. The project repo needs a workflow whose `push:` covers `agent/**`. The installer
adds one (the sample has it already; an existing repo gets it in a pull request). For a Python project:

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main, 'agent/**']
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m unittest discover -s tests -t . -v
```

**B1. The ORGrove App.** Organisation **Settings > Developer settings > GitHub Apps > New GitHub App**. Webhook off.
Repository permissions: Actions read, Checks read, Contents read and write, Issues read and write, Pull requests
read and write. Organization permission: Projects read and write. Install it on the control repo and each project
repo. Its private key goes into the control repo's secret `ORGROVE_APP_PRIVATE_KEY`, its ID into the variable
`ORGROVE_APP_ID`.

**B2. The notify App.** A second App with one permission, Repository > Actions: read and write, installed on the
control repo only. In each project repo: the variables `ORGROVE_NOTIFY_APP_ID` and `ORGROVE_CONTROL_REPO` (the
control repo's name), the secret `ORGROVE_NOTIFY_PRIVATE_KEY`, and `templates/orgrove-notify.yml` copied to
`.github/workflows/orgrove-notify.yml`. A person's ticket, comment or merge then starts ORGrove at once.

**C. The Anthropic key**: the control repo's secret `ANTHROPIC_API_KEY`.

**D. Settings.** `python3 -m orgrove.init` asks the setup questions and writes `config/` and `projects/`; push them
to the control repo with ORGrove's code. `python3 -m orgrove.doctor --tick` checks everything. Then switch on:
`gh variable set ORGROVE_ENABLED -R YOUR-ORG/orgrove --body true`.
