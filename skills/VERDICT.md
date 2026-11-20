# Verdict format (QA, Security, Sanity)

A reviewing role ends its run with **one** issue comment. The comment has a short human summary, followed by exactly one machine block:

```
<!-- orgrove:verdict
{"role": "qa", "issue": 123, "branch": "agent/123-fix-x", "head": "<full commit sha reviewed>",
 "round": 1, "decision": "changes",
 "findings": [{"title": "…", "severity": "blocking", "file": "path", "line": 10, "sub_item": 0}]}
-->
```

- `decision` is `approve` or `changes`. A `changes` verdict needs at least one blocking finding, and every blocking finding must point at its sub-issue. You can't know an issue's number before it exists, so put the sub-issue in `sub_items` of the same result and give the finding `sub_item`: its place in that list, counting from 0. The number is filled in for you when the result is applied. (A finding about an issue that already exists gives `sub_issue` with that number instead.)
- `head` is the commit you reviewed. A verdict for an older head doesn't count: a new push means a new review.
- Sanity uses `"decision": "pass" | "fail"`, with `branch` and `head` set to the merged commit on main.
- `orgrove.verdict.parse` reads this block. Anything malformed is treated as no verdict.
