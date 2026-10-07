---
name: mm-autopr
description: Publish a locally tested MergeMarshall branch and reconcile its integration PR against shared batch membership.
---

First record your local assessment with mm-db for the exact committed HEAD,
including commands actually run and reproduced baseline failures. Then:

```sh
python3 <this skill>/scripts/mm_autopr.py --notes-file /tmp/merge-notes.md
```

The script checks a clean tree, matching recorded test head and pass verdict,
included/excluded ancestry, and the recorded branch. It pushes only that branch.
The supervisor service creates or updates exactly one integration PR using REST,
generates its source table/title from current membership, applies the existing
label, verifies the published head, and records publication. Repeated calls reuse
the PR. Notes should explain conflict resolutions and fixes.

After an authorized rebuild, add `--rebuild` for a force push with an explicit
lease against the observed remote head. Otherwise publication uses a normal push.
Finish with `python3 <mm-db skill>/scripts/mm_db.py report` and relevant diagnosis.
The supervisor retains the landing decision and current-source/master checks.
