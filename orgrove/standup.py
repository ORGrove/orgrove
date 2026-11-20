"""Daily standup (owner rule, 2026-11-03): work that can't move is found by the dispatcher, not by the owner.

On 2026-11-03 Dev's and DevOps's slots had been full for days with tickets nobody could move: #599 approved and green
but held by a sub-issue whose fix was on #599's own branch, #41 the same with #263, and #31 left at review after its
PR merged with "Refs #31". The product project spent $0.79 and $0.51 of its $15 share on 1 and 2 Nov, and nobody noticed.

A ticket that holds a Dev or DevOps slot (in-dev or agent-review, not on hold) and that nobody has touched for
STALL_HOURS is stalled. The dispatcher sends it to the Delivery Lead (needs/delivery) with what it found and the
project's spend, every tick, at $0 when nothing is stuck. She settles it with the PM and the agents, or, after
DELIVERY_TRIES in a day, it goes to the owner as it is (dispatch.decide's hold()).
"""
from datetime import datetime

from orgrove import gate

STALL = "orgrove:stall"   # marker on the dispatcher's note, so the Delivery Lead (and a reader) knows what this is
STALL_HOURS = 12          # agents run around the clock: an in-flight ticket untouched this long is not waiting its turn
IN_FLIGHT = ("in-dev", "agent-review")


def age_hours(card, now):
    """Hours since the ticket last changed (a comment, a label); None when the card has no time (old fixtures)."""
    ts = card.get("updated")
    if not ts:
        return None
    return (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 3600


def diagnose(card, review):
    """One plain sentence on why this ticket can't move, from the same facts the gate reads."""
    n = card["number"]
    if card["stage"] == "agent-review" and n not in review:
        return (f"#{n} is at review, but it has no agent branch of its own (`agent/{n}-…`), so nobody can review it "
                "or open its PR. Usually its fix rides on another ticket's branch, or its PR already merged with "
                "\"Refs\" instead of \"Closes\".")
    r = review.get(n)
    if r and card["stage"] == "agent-review":
        reasons = gate.readiness(r["verdicts"], r["open_subs"], r.get("ci", "green"))
        if reasons and all(x.startswith("open sub-issues") for x in reasons):
            subs = ", ".join(f"#{s}" for s in r["open_subs"])
            return (f"#{n} is approved by QA and Security and CI is green, but the gate won't open its PR while its "
                    f"sub-issue(s) {subs} are open. If a sub-issue's fix is on #{n}'s branch, it can't close before "
                    f"#{n} merges, so the two wait for each other.")
        if reasons:
            return f"#{n} is at review and can't become a PR: " + "; ".join(reasons) + "."
    return f"#{n} is at {card['stage']} and no agent has worked on it or commented."


def stalled(active, review, working_on, now, hours=STALL_HOURS):
    """(card, why) for each in-flight ticket nobody has touched for `hours`; skips one an agent is on right now."""
    out = []
    for c in active:
        if c["stage"] not in IN_FLIGHT or c["number"] in working_on:
            continue
        age = age_hours(c, now)
        if age is not None and age >= hours:
            out.append((c, diagnose(c, review), age))
    return out


def use_line(yesterday, today, share):
    """The project's agent spend against its daily share: an empty day with work open means something is stuck."""
    if not share:
        return f"Agent spend: ${yesterday:.2f} yesterday, ${today:.2f} today."
    return (f"Agent spend: ${yesterday:.2f} yesterday ({yesterday / share:.0%} of the ${share:.0f} daily share), "
            f"${today:.2f} today.")


def note(why, age, use):
    return (f"**Daily standup: this ticket hasn't moved for {age:.0f} hours** and holds a Dev or DevOps slot, so "
            f"nothing new starts behind it.\n\n{why}\n\n{use}\n\n"
            "Delivery Lead: find the cause and settle it with the PM and the agents (your skill's **Stalled work**). "
            "If only the owner can unblock it, ask them one question.\n"
            f"<!-- {STALL} -->")
