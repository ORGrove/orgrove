# Using ORGrove day to day

You have installed ORGrove (INSTALL.md). This guide is about using it: giving the team work, watching it happen,
and the few moments where a person decides. It takes about ten minutes to read.

The short version: **open a GitHub issue, watch it move, merge the pull request.**

## 1. Give the team work: open an issue

Work starts as an issue in your project repo, the repo you named during the install. Open it on GitHub as one of
the people you named in question 5 of the install: the agents take instructions only from them. An issue from
anyone else is read as information, never as an order.

No issue, no work. The agents never start anything on their own.

### What to write

The PM agent turns your issue into a plan. The clearer the issue, the fewer questions it asks you. Say:

- **What you want**, in a sentence or two, from the user's side.
- **Why**, if it isn't obvious. It helps the PM choose between two ways of doing it.
- **How you will know it works**: what you would click, run or see.
- **What it must not touch**, if anything: a file, a page, an API other teams use.

One change per issue. "Add a CSV export to the orders page" is a good issue. "Improve the orders page" is a
conversation, and the PM will ask you to split it.

An example:

> **Title:** Orders page: download the current list as CSV
>
> On the orders page, a "Download CSV" button saves the orders currently shown (after filters) as a CSV file:
> order number, date, customer, total, status. It is for the accounts team, who copy the table by hand today.
>
> Done when: with a filter applied, the file has exactly the rows on screen, and opens in a spreadsheet with the
> columns above. Don't change the orders API: the mobile app uses it.

To give every issue that shape, copy `templates/new-work.yml` from this folder into your project repo as
`.github/ISSUE_TEMPLATE/new-work.yml`. GitHub then offers a "New work" form when someone opens an issue.

## 2. When it starts

- **At once, usually.** Opening an issue, commenting or merging a pull request wakes ORGrove immediately. A
  schedule every 30 minutes is the backstop if a wake-up is missed.
- **The PM plans first.** The plan appears as a comment on the issue: acceptance criteria, scope, a security note,
  size and priority.
- **Dev takes up to two tickets at a time.** Others wait in order, planned and ready. That is normal, not stuck.

If nothing happens, the Office says why for every idle agent (section 4). The usual reasons are in section 6.

## 3. What happens to a ticket

The ticket's progress is a `stage/*` label on the issue. There is always at most one. An open issue with no stage
label is in the backlog; a closed issue is done.

| Label | What it means | Who moves it on |
|---|---|---|
| (none) | In the backlog, waiting to be planned | The PM |
| `stage/planned` | The plan is written on the issue | Dev, when it has room |
| `stage/in-dev` | Dev is building it on a branch `agent/<issue>-<name>`, with tests | Dev |
| `stage/agent-review` | Pushed. CI runs first; then QA and Security review it. They can only read | QA and Security |
| `stage/awaiting-review` | Both approved and CI is green: **a pull request is waiting for you** | **You**, by merging |
| `stage/deployed-dev` | Merged, and your dev environment deployed it (only if the project has one) | Automatic |
| `stage/sanity` | The Sanity agent checks the deployed change | Sanity |

Then the issue is closed: done. A project without a dev environment closes the issue when you merge.

Two things send a ticket back, and both are normal:

- **CI fails, or a reviewer finds a problem.** The ticket goes back to `stage/in-dev` with the label `needs/fix`.
  A reviewer's finding becomes a small sub-issue linked to the ticket. Dev fixes it and it goes round again.
- **After three review rounds** without a pass, it stops and comes to you (`needs/human`) instead of looping and
  spending.

Other labels you may see on a ticket:

| Label | What it means |
|---|---|
| `agent/pm`, `agent/dev`, `agent/qa`, ... | That agent is working on the ticket right now |
| `needs/plan` | Queued for the PM to plan |
| `needs/fix` | Back with Dev to fix what CI or a reviewer found |
| `needs/human` | **A question for you.** Answer it in a comment |
| `needs/delivery` | A question the Delivery Lead is checking first; it reaches you only if the agents can't settle it |
| `blocked/budget` | A spending cap is reached; it waits (section 6) |
| `blocked/deps` | It waits for other tickets to land first; ORGrove releases it on its own |

## 4. Where to watch

| Where | What you see |
|---|---|
| **The issue** | The plan, each agent's report, questions for you, and the ticket's cost so far |
| **The pull request** | The change itself, with the QA and Security reviews. This is what you merge |
| **Actions** in your `orgrove` repo | Every agent run and dispatcher tick, with its log |
| **The Office**, on your computer | The whole team at once |

Start the Office from the folder you installed from:

```sh
bin/orgrove-office            # opens http://127.0.0.1:8730 (the next free port if that one is taken)
```

It reads GitHub live and shows:

- **Pipeline**: every open ticket in its stage.
- **Who's doing what**: each agent, the ticket it is on, and, when it is idle, why.
- **ORGrove Review · waiting on you**: everything that needs a person. Check this first.
- **Spend log** and **Agent runs**: what each run cost and how it ended.
- **Roadmap**: target dates the PM has set.

Every state in the Office links to the issue, pull request or run behind it.

## 5. Your part: three moments

The agents do the work. A person makes three kinds of decision, and ORGrove waits for them:

1. **Answer questions.** When the PM or another agent needs a decision only you can make, the issue gets
   `needs/human` and a plain question in a comment. Reply in a comment. The ticket carries on at the next wake-up,
   which your comment triggers.
2. **Review the pull request.** It arrives when CI is green and QA and Security have both approved. Read it as you
   would a colleague's. To ask for changes, comment on the issue or the pull request.
3. **Merge.** Only a person merges. The agents never merge, deploy to production or publish anything.

Anything on the never-do list in `projects/<project>.toml` (`[limits]`), such as a production deploy or a change
to `.github/workflows`, comes to you as a question, whatever the ticket says.

## 6. Holding, stopping and money

| You want to | Do this |
|---|---|
| Keep the agents off one ticket for now | Add the label `blocked/parked`. Remove it to bring the ticket back |
| Have a ticket wait for your go-ahead | Add `needs/approval`. The agents ignore it until you remove the label |
| Stop every agent | The kill switch: `gh variable set ORGROVE_ENABLED -R YOUR-ORG/orgrove --body false` (`true` to start again) |
| Spend less | Lower the caps in `config/budget.toml`, in a pull request you merge |

**What it costs.** A small ticket, from plan to merged pull request, costs about $1 of Claude usage on your key.
Each issue shows a comment, "ORGrove dispatcher: cost so far", updated after every run. The Office's spend log has
the detail.

**Caps.** Before any run starts, ORGrove reserves its full ceiling against your daily and monthly caps. When a cap
is reached nothing starts, and the ticket gets `blocked/budget`. It continues on its own the next day or month, or
when you raise the cap. Your Anthropic workspace limit is a second stop behind ORGrove's own.

**When nothing is happening**, the Office's "Who's doing what" says why. The usual reasons: a cap is reached; the
ticket is waiting for your answer (`needs/human`); Dev already has two tickets; the kill switch is off; or the
issue was opened by someone not named in question 5.

INSTALL.md section 5 lists problems and fixes, and `python3 -m orgrove.doctor` checks the whole setup.
