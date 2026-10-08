---
name: mm-db
description: Read and update a MergeMarshall batch's shared state, exclusions, and local test evidence from its agent checkout.
---

Use `python3 <this skill>/scripts/mm_db.py` in the batch checkout. Configure once
using the connection JSON supplied by the supervisor:

```sh
python3 <this skill>/scripts/mm_db.py configure --connection-file /tmp/mm-connection.json
python3 <this skill>/scripts/mm_db.py state
python3 <this skill>/scripts/mm_db.py pr --pr N
python3 <this skill>/scripts/mm_db.py issue --issue N
python3 <this skill>/scripts/mm_db.py findings
```

The connection is stored privately inside the Git directory. Keep its token out
of reports and PR bodies. `state` returns the current source list and revision.
Pass that revision to each mutation; on a stale-revision error, read state again
and reconsider the requested update.

```sh
python3 <this skill>/scripts/mm_db.py exclude --revision REV --pr N --head SHA --kind removed --reason 'source head changed'
python3 <this skill>/scripts/mm_db.py exclude --revision REV --pr N --head SHA --kind rejected --reason 'isolated regression' --evidence-file /tmp/evidence.md
python3 <this skill>/scripts/mm_db.py tests --revision REV --head SHA --verdict pass --tests 'commands actually run' --baseline 'reproduced failures or none'
python3 <this skill>/scripts/mm_db.py comment --revision REV --issue N --body-file /tmp/comment.md
python3 <this skill>/scripts/mm_db.py finding --revision REV --kind baseline --head BASE_SHA --identity 'exact failing test' --command 'actual check command' --evidence-file /tmp/evidence.md
python3 <this skill>/scripts/mm_db.py finding --revision REV --kind flaky --head TESTED_SHA --identity 'exact flaky test' --command 'actual check command' --evidence-file /tmp/evidence.md
```

Record unresolved baseline failures and flaky product tests for later repair with
`finding`. Include the failure output, settings/environment, exact committed
tested SHA, reusable log paths, and any subsequent pass/fail outcomes. Finding
evidence is limited to 12,000 characters (40 KiB after JSON encoding with the
identity and command); reference full logs when longer. Baseline
findings must name the batch's captured base SHA; candidate-only failures need
normal fix/reject investigation first. A passing rerun does not erase a product
flake. Use `findings` to reuse an existing observation. Do not record already
fixed interactions, infrastructure incidents, or known container limitations as
unresolved product failures. Reuse existing evidence; registration needs no new
test run.

Local findings are durable diagnostic records in the shared database. Triage
checks their applicability to current master, deduplicates product issues, and
routes them to the normal issue fixer. They retain local provenance and never
stand in for master CI run evidence or authorize a sync baseline. Recording one
does not change membership, invalidate test evidence, or change the batch
revision. Diagnostic intake remains available after publication/completion;
membership, test and publication mutations retain their lifecycle guards.

Removal updates the supervisor immediately. Rejection records the exact-head
marker and evidence for delivery by the supervisor's App identity; use it only after
establishing a new failure attributable to that source head. Changed or closed
PRs are removed without rejection. Diagnose the available failure groups before
recording the combined rejection set. Record all established exact-head exclusions,
refreshing the revision between mutations, then read state again for the remaining
membership. Removing a prerequisite also removes its descendants; do not reject
them unless they are independently broken. Rebuild the recorded remainder once,
preserving applicable fixes and conflict resolutions. Do not rebuild and retest
between individual exclusions from the same diagnosis pass. Test evidence is your
explicit assessment; this helper does not select tests or infer a verdict.

After changes, select reruns from the diff against the last tested candidate,
covering affected behavior and shared dependencies/interactions. Retain justified
results for unaffected areas. Record a fresh assessment at the current source
revision and final committed HEAD; an earlier passing assessment does not transfer
automatically. In the one-line `--tests` summary, distinguish commands run at that
HEAD from reused results, identifying their original tested SHAs and why they
remain applicable. Keep detailed commands, logs, and reuse reasons in the private
progress note.

The state response lists pending GitHub writes. A recorded rejection or comment
is accepted even if GitHub delivery is pending; continue the batch. The supervisor
retries delivery. A rejection is tracked by its exact head and does not change
the PR's draft state. Use `pr` and `issue` to read GitHub
metadata and comments through the service. Do not run `gh` to post comments,
change labels, mark PRs draft, or otherwise mutate GitHub directly.

After publication, `report` renders the recorded local verdict, test summaries,
tested SHA, publication, and ejection markers for your final message. Add any
required `known-failure:` diagnoses yourself. An unavailable service is an error;
do not substitute a local database for the shared state.
