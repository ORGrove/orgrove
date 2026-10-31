"""What an agent run said, in the run log as well as the summary page.

On 2026-10-28 every agent run failed for hours with "You have reached your specified workspace API usage limits",
and nobody could tell why: the reason was only on the run's summary page, which the API can't read. Now:
- one status line always goes to the log, plus the reason when the run failed (or the CLI didn't answer in JSON);
- the agent's full answer goes to the log too when the repo variable ORGROVE_DEBUG is "true". Flip it with
  `gh variable set ORGROVE_DEBUG -R <orgrove repo> --body true` (and back to false); no code change, no deploy.
The summary page always gets the full answer, as before.

    python -m orgrove.agent_result RESULT_JSON --label "pm on #542" [--debug true] [--summary FILE]
    python -m orgrove.agent_result RESULT_JSON --refusal     # prints the provider's refusal, or nothing
"""
import argparse
import json
import sys

LIMIT = 3000


def render(raw, label, debug=False):
    """(log text, summary text) for one run's --output-format json result."""
    try:
        r = json.loads(raw)
    except ValueError:
        r = None
    if not isinstance(r, dict):          # the CLI died before answering: what it printed is the reason
        text = f"**{label}**: no JSON result from the agent\n\n{raw.strip()[:LIMIT] or '(empty)'}\n"
        return text, text
    failed = bool(r.get("is_error"))
    head = (f"**{label}**: cost ${r.get('total_cost_usd')} · turns {r.get('num_turns')} · "
            f"{round((r.get('duration_ms') or 0) / 1000)}s · error={r.get('is_error')} · subtype={r.get('subtype')}\n")
    body = (r.get("result") or "")[:LIMIT]
    summary = head + "\n" + body + "\n"
    log = summary if failed or debug else head
    return log, summary


def refusal(raw):
    """The AI provider's error when it refused the run ("API Error: 400 ... usage limits"), else ''.
    Only provider errors: an agent that ran and failed at its task is not a refusal."""
    try:
        r = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(r, dict) or not r.get("is_error"):
        return ""
    text = (r.get("result") or "").strip()
    return text[:300] if text.startswith("API Error") else ""


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("result")
    p.add_argument("--label", default="agent")
    p.add_argument("--refusal", action="store_true", help="print only the provider's refusal message, if any")
    p.add_argument("--debug", default="false", help='"true" (the ORGROVE_DEBUG repo variable) puts the full answer in the log')
    p.add_argument("--summary", help="append the summary text here ($GITHUB_STEP_SUMMARY)")
    a = p.parse_args(argv)
    try:
        with open(a.result, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        raw = f"(no result file: {e})"
    if a.refusal:
        sys.stdout.write(refusal(raw))
        return
    log, summary = render(raw, a.label, a.debug.strip().lower() == "true")
    sys.stdout.write(log)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(summary)


if __name__ == "__main__":
    main()
