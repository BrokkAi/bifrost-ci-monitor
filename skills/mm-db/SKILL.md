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
```

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
