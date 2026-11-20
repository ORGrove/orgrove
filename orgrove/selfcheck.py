"""Check a role's result before its run finishes, and let the model correct it in the same session (L-021).

Before this, a refused result was only ever seen in the apply job's log. The model never learned why; reviewers are told
never to read the ticket's comments, so even a note on the ticket could not reach them; and the dispatcher could only start
the same role again, paying again, until something stopped it (2026-11-10, runs 1 and 2: 12 runs and 2 runs, both for
one mistake the model was never told about). This is the standard remedy: validate at the boundary and give the producer
the error while it still has the context to fix it.

    python -m orgrove.selfcheck --project P --role R --issue N --workdir work --result result.json ...

It runs in the agent job after the model has finished. It reads the model's `orgrove-out/result.json` with the same
function the apply job uses (cli.process_result, a dry run that only reads), so a result it passes is a result apply
accepts. If the result is refused, the model is resumed in its own session with the exact reasons, at most twice. What it
sees is the validator's message about the format of its own output: never the ticket's comments, another reviewer's
verdict or anything about the history, so a reviewer's independence is untouched. The corrections' cost is added to the
run's cost, so the ledger stays true. Any failure here falls back to what happened before: the apply job decides.
"""
import argparse
import json
import os
import subprocess
import sys

MAX_CORRECTIONS = 2
CORRECTION_SHARE = 0.25     # of the run's ceiling, per correction: a format fix is a few cents, not a second run
MIN_BUDGET = 0.03           # below this a correction cannot even start


def check(project, role, issue, outbox_dir, tracker=None):
    """The reasons the apply job would refuse this result (an empty list when it would accept it)."""
    from orgrove import cli
    path = os.path.join(outbox_dir, "result.json")
    _, _, report = cli.process_result(project, role, issue, path, tracker=tracker, dry_run=True, out=lambda *_: None)
    return [line[len("REJECTED: "):] for line in report if line.startswith("REJECTED")]


def correction(reasons):
    """What the model is told: the validator's reasons about the format of its own result, and nothing else."""
    return ("Your result file `orgrove-out/result.json` was checked and refused, so none of it would be applied:\n\n"
            + "\n".join(f"- {r}" for r in reasons)
            + "\n\nFix the file so that every point above is met. Keep everything else in it as it was, do not redo the "
              "work, and do not change any other file. Reply `done` when it is saved.")


def run(first, validate, resume, ceiling, out=print, max_corrections=MAX_CORRECTIONS):
    """`first`: the JSON claude printed for the run (session_id, total_cost_usd, num_turns). `validate()` returns the
    reasons the result is refused (empty if fine). `resume(session_id, prompt, budget)` runs one correction and returns
    claude's JSON for it, or None. Returns `first` with the corrections' cost and turns added and a `selfcheck` record."""
    record = {"corrections": 0, "refused": []}
    session = first.get("session_id")
    spent = float(first.get("total_cost_usd") or 0)
    if first.get("is_error") or not session:
        out("selfcheck: the run itself failed, so there is no result to correct")
        return first | {"selfcheck": record | {"skipped": "run failed"}}
    reasons = validate()
    while reasons and record["corrections"] < max_corrections:
        budget = min(ceiling * CORRECTION_SHARE, max(ceiling - spent, 0))
        if budget < MIN_BUDGET:
            out(f"selfcheck: the result is refused but only ${budget:.2f} of the run's budget is left: not asking for a correction")
            break
        out(f"selfcheck: the result is refused ({len(reasons)} reason{'s' * (len(reasons) != 1)}); asking the model to correct it "
            f"({record['corrections'] + 1} of {max_corrections}): " + "; ".join(reasons)[:400])
        reply = resume(session, correction(reasons), budget)
        record["corrections"] += 1
        if not reply or reply.get("is_error"):
            out("selfcheck: the correction run failed; leaving the result as it is for the apply job to decide")
            break
        spent += float(reply.get("total_cost_usd") or 0)
        first = first | {"total_cost_usd": round(float(first.get("total_cost_usd") or 0) + float(reply.get("total_cost_usd") or 0), 6),
                         "num_turns": int(first.get("num_turns") or 0) + int(reply.get("num_turns") or 0)}
        session = reply.get("session_id") or session
        reasons = validate()
    if reasons:
        record["refused"] = reasons
        out("selfcheck: still refused after " + (f"{record['corrections']} correction(s)" if record["corrections"] else "no correction")
            + ": the apply job will say so on the ticket")
    elif record["corrections"]:
        out(f"selfcheck: fixed after {record['corrections']} correction(s)")
    else:
        out("selfcheck: the result passes")
    return first | {"selfcheck": record}


def claude_resume(session_id, prompt, budget, model, tools, cwd, role_file=None, timeout=600, claude="claude"):
    """One correction: the same session, the same model and tools as the run, a small budget of its own."""
    cmd = [claude, "-p", "--resume", session_id, "--setting-sources", "user", "--model", model,
           "--max-budget-usd", f"{budget:.2f}", "--output-format", "json", "--allowedTools", *tools]
    if role_file:
        cmd[2:2] = ["--append-system-prompt-file", role_file]
    try:
        done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, timeout=timeout, check=False)
        return json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def main(argv=None):
    p = argparse.ArgumentParser(prog="orgrove.selfcheck")
    for flag in ("project", "role", "issue", "workdir", "result", "ceiling", "model", "tools-file"):
        p.add_argument(f"--{flag}", required=True)
    p.add_argument("--role-file")
    a = p.parse_args(argv)
    try:
        with open(a.result) as f:
            first = json.load(f)
        tools = [t for t in open(a.tools_file).read().splitlines() if t.strip()]
        outbox_dir = os.path.join(a.workdir, "orgrove-out")
        out = run(first, lambda: check(a.project, a.role, int(a.issue), outbox_dir),
                  lambda sid, prompt, budget: claude_resume(sid, prompt, budget, a.model, tools, a.workdir, a.role_file),
                  float(a.ceiling))
        with open(a.result, "w") as f:
            json.dump(out, f)
    except Exception as e:  # noqa: BLE001 (this step must never stop a run: the apply job still decides, as before)
        print(f"selfcheck: skipped ({type(e).__name__}: {str(e)[:200]})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
