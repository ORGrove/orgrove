# Role skills (#5)

One skill per role: `pm`, `dev`, `qa`, `security`, `devops`, `sanity`, `delivery`, plus `retro` (daily, #151). Reviewing roles post verdicts in the format in [VERDICT.md](VERDICT.md).

- **Questions for the owner (2026-11-02):** no agent asks the owner directly. An agent that needs a decision adds
  `needs/delivery` with a comment. The Delivery Lead (`skills/delivery`) settles it with the agents, or asks the
  owner one question in a fixed shape (decision, why now, options with cost, recommendation, what happens without an
  answer, the reply), with the `orgrove:owner-question` marker, and moves the hold to `needs/human`. The dispatcher
  sends any `needs/human` without such a question (since the owner last spoke) back to the Delivery Lead, and after
  two Delivery Lead runs on one ticket in a day it hands the hold to the owner as it is.

- **How they load:** the role runner (#7) checks out the project repo and runs Claude Code inside it, so the project's own `CLAUDE.md` and rules apply. It copies `skills/<role>/` into the runner's user-level `~/.claude/skills/`. Skills are never committed into a project repo.
- **What a run sees:** reviewers (`qa`, `security`) get only the ticket body and the diff. The runner doesn't pass them the Developer's comments.
- **Precedence:** project rules beat skills. Every skill starts by reading the profile's `rules`.
- **Lessons (#151):** every role prompt also carries `lessons/<project>.md`: numbered mistakes agents already paid
  for, each with a rule and a check. The daily `retro` (`skills/retro`, `orgrove/retro.py`, `.github/workflows/retro.yml`)
  proposes new lessons and records repeats by PR, and opens a "make it a gate" ticket when a lesson keeps recurring.
  The retro is not a department role: it runs once a day on its own schedule and budget line (`config/retro.toml`).
- **Changing a skill:** by PR to this repo. `tests/test_manifest.py` checks each skill exists, is named `orgrove-<role>`, and has `## Never` and `## Example` sections.
