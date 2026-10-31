# Verdict format (QA, Security, Sanity)

A reviewing role ends its run with **one** issue comment. The comment has a short human summary, followed by exactly one machine block:

```
<!-- orgrove:verdict
{"role": "qa", "issue": 123, "branch": "agent/123-fix-x", "head": "<full commit sha reviewed>",
 "round": 1, "decision": "approve",
 "findings": [{"title": "…", "severity": "blocking", "file": "path", "line": 10, "sub_issue": 456}]}
-->
```

- `decision` is `approve` or `changes`. A `changes` verdict needs at least one blocking finding, and every blocking finding needs its `sub_issue` number. Raise the sub-issue first, then write the verdict.
- `head` is the commit you reviewed. A verdict for an older head doesn't count: a new push means a new review.
- Sanity uses `"decision": "pass" | "fail"`, with `branch` and `head` set to the merged commit on main.
- `orgrove.verdict.parse` reads this block. Anything malformed is treated as no verdict.
