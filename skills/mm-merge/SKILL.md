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
