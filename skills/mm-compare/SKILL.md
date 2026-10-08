---
name: mm-compare
description: Run one supplied Bash check at two Git commits in detached worktrees and return raw output, exit codes, and diffs.
---

Supply the check yourself; this helper makes no test selection or diagnosis:

```sh
python3 <this skill>/scripts/mm_compare.py --a BASE_SHA --b CANDIDATE_SHA --script /tmp/check.sh
```

Use this helper when fresh runs at both commits are needed. Reuse existing
exact-tree evidence when available; baseline checks cover failures observed in
the candidate. The primary schedules expensive comparisons and may assign a
specific check to a subagent.

It snapshots the supplied script, runs both sides concurrently in temporary
detached worktrees, saves stdout/stderr and their diffs, and removes the worktrees.
The active checkout is untouched. Results survive in the printed output directory.
Pass `--sequential` when checks use shared resources that cannot run concurrently.
Do not run toolchain installation in parallel. Cargo continues through the existing
mbx configuration; this helper does not redirect build storage.

The result reports each resolved commit, exit code, and whether the check changed
tracked source. Gate evidence must describe committed trees. Raw diffs may include
temporary paths, timestamps, and ordering differences; inspect actual failures
before classifying them. Do not infer baseline equivalence from equal exit codes.

Each side also retains an execution receipt with its command, commit/tree,
settings/environment, timing, exit status, and output paths. In a configured batch
checkout, both receipts are automatically uploaded to the shared execution
history without changing the batch revision. Use their printed execution IDs
in `mm-db tests --execution ID` or `--reuse-execution ID REASON`. Failed uploads
retain the receipt files for `mm-db execution --receipt FILE`; retry delivery
without repeating the comparison. Outside a configured batch, receipts remain
local alongside `result.json`.
