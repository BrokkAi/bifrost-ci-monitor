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
Use the archived work as evidence; never transplant old merge commits from the
invalidated attempt. Read the generated brief and current
state; the archived progress note's next action is obsolete.

The captured base is actual master; nothing is shared with the primary. Merge
only your own recorded sources. `predecessor.current` means the predecessor is
live, independently of its candidate. After local pass, call `mm-db ready` and
wait without publishing while it is live. Taking the primary scheduling role
does not stop checks or change your checkout.

When the supervisor requests promotion, run `--promote` to merge the exact
recorded master base into your committed candidate. Resolve conflicts and commit
with the prepared trailer. Choose reruns from the diff against the last tested
candidate under the validation policy, then record a fresh assessment and
re-register the candidate with `mm-db candidate`. The helper summarizes changed
paths and crates. Reuse unaffected results only with their original tested SHAs
and applicability reasons; promotion always requires a fresh assessment. The
supervisor withdraws the old checkpoint when it updates the base. Withdraw any
new checkpoint before further edits. Publish through mm-autopr after validation.

Only when `mm-db state` names a stabilize campaign branch in `target_ref`, apply
these campaign instructions: use the captured `base_sha` for assembly,
comparisons and updates; publish against that campaign branch; never merge master
into the batch; incorporate only campaign base updates requested by the supervisor.
Integration batches use local validation in async mode. The supervisor may land a
single up-to-date campaign PR directly under the same async policy as master;
campaign stage suites then validate the new branch tip. Campaign gate snapshots
may omit prerequisites already satisfied by merged campaign fixes, which the
supervisor verifies before selection. Verdict, exact-head and freshness gates
still apply to the campaign landing branch.
