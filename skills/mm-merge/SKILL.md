---
name: mm-merge
description: Assemble a MergeMarshall integration branch from verified source heads, trying an octopus merge before resolving conflicts manually.
---

Read the shared batch state with mm-db. Run:

```sh
python3 <this skill>/scripts/mm_merge.py
```

The script verifies the branch and clean tree, fetches all source heads into
named refs, checks their exact SHAs, and attempts one octopus merge with the
batch trailer. It verifies that every captured prerequisite is contained in
its dependent head and included in the batch or captured base. The supervisor
orders prerequisites before dependents. It skips heads already present. A changed source head stops the
operation; record its removal with mm-db and rebuild without rejecting it.
Removing a prerequisite also removes its descendants. Refresh mm-db state and
use that recorded source set; blocked descendants are not standalone rejections.

If the octopus attempt fails, the script restores the state before that attempt
and exits 2. Resolve manually using `--manual`: this merges the remaining heads
one at a time and stops at the first conflict. Read the PR intent, resolve and
stage the files, then `git commit --no-edit` to retain the prepared batch trailer.
Run `--manual` again to continue. Conflicts are not grounds for rejection.

Use `--rebuild` only when the supervisor or recorded exclusions require rebuilding
from the captured base. It resets this batch branch after requiring a clean tree.
Preserve any needed resolutions/fixes; an octopus retry does not reconstruct
agent-authored fixes. Source history remains reachable in either merge topology.
Publication is a separate mm-autopr operation after the local test gate passes.

The helper enables Git rerere with `rerere.autoupdate=false`. Review reused
resolutions before staging them.

After the supervisor creates a new attempt, run `--restart` (combine with
`--manual` when useful). It saves the old tip, pending source files/patches and
progress note under the Git directory's `mm-recovery/attempt-N`, then resets to
the recorded replacement base and assembles your original own heads. Repeating
it in the same attempt preserves current work. Caches and logs remain available.
Use the archived work as evidence; never transplant old merge commits or retain
the invalidated predecessor's ancestry. Read the generated brief and current
state; the archived progress note's next action is obsolete.

After promotion, run `--promote`. This merges the exact landed base and reports
whether the resulting tree matches earlier passing evidence. Reuse requires
applicable test inputs/settings as well as an unchanged tree. Record a fresh
assessment at the resulting committed HEAD before publication. If the helper
encounters conflicts, resolve/commit them and reassess affected checks.
