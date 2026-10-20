---
name: mm-autopr
description: Publish a locally tested MergeMarshall branch and reconcile its integration PR against shared batch membership.
---

First record your local assessment with mm-db for the exact committed HEAD,
including checks run there, separately identified reused results with their
original tested SHAs and applicability reasons, and reproduced baseline failures.
The captured base is actual master; nothing is shared with the primary.
Publication is blocked while `predecessor.current` is true (the predecessor is
live). Record local pass, refresh state and call `mm-db ready --revision REV`,
then wait. When the supervisor requests promotion, run `mm-merge --promote` to
merge the recorded master tip and resolve conflicts. Choose reruns from the diff
against the last tested candidate under the validation policy, record a fresh
assessment, then re-register the candidate with `mm-db candidate` and publish.
Reuse unaffected results with their original tested SHAs and applicability reasons.

Then:

```sh
python3 <this skill>/scripts/mm_autopr.py --notes-file /tmp/merge-notes.md
```

The script checks a clean tree, matching recorded test head and pass verdict,
included/excluded ancestry, and the recorded branch. It pushes only that branch.
The supervisor service creates or finds exactly one integration PR using REST,
verifies the published head, and atomically records publication and a durable
readiness receipt containing the tested head, source revision and attempt
generation. This hands the candidate to the supervisor in either CI mode.
It queues title/body and
existing-label updates for automatic delivery and retries; those may still be
pending when this tool returns. Repeated accepted calls return the receipt
without pushing again. After acceptance, stop edits, builds and pushes until
the supervisor gives new work; final messages and session idleness do not gate
supervision. Notes should explain
conflict resolutions and fixes, and include any `known-failure:` diagnosis lines
that should be recorded before the supervisor advances.

After an authorized rebuild, add `--rebuild` for a force push with an explicit
lease against the observed remote head. Otherwise publication uses a normal push.
Finish with `python3 <mm-db skill>/scripts/mm_db.py report` and relevant diagnosis.
The supervisor retains the landing decision and current-source/master checks.

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
