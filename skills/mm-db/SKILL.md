---
name: mm-db
description: Read and update a MergeMarshall batch's shared state, exclusions, and local test evidence from its agent checkout.
---

Use `python3 <this skill>/scripts/mm_db.py` in the batch checkout. Configure once
using the connection JSON supplied by the supervisor:

```sh
python3 <this skill>/scripts/mm_db.py configure --connection-file /tmp/mm-connection.json
python3 <this skill>/scripts/mm_db.py state
```

The connection is stored privately inside the Git directory. Keep its token out
of reports and PR bodies. `state` returns the current source list and revision.
Pass that revision to each mutation; on a stale-revision error, read state again
and reconsider the requested update.

```sh
python3 <this skill>/scripts/mm_db.py exclude --revision REV --pr N --head SHA --kind removed --reason 'source head changed'
python3 <this skill>/scripts/mm_db.py exclude --revision REV --pr N --head SHA --kind rejected --reason 'isolated regression' --evidence-file /tmp/evidence.md
python3 <this skill>/scripts/mm_db.py tests --revision REV --head SHA --verdict pass --tests 'commands actually run' --baseline 'reproduced failures or none'
```

Removal updates the supervisor immediately. Rejection also posts the exact-head
marker and evidence through the supervisor's App identity; use it only after
establishing a new failure attributable to that source head. Changed or closed
PRs are removed without rejection. Rebuild the branch after exclusions, preserving
relevant conflict resolutions. Test evidence is your explicit assessment; this
helper does not select tests or infer a verdict.

After publication, `report` renders the recorded local verdict, test summaries,
tested SHA, publication, and ejection markers for your final message. Add any
required `known-failure:` diagnoses yourself. An unavailable service is an error;
do not substitute a local database for the shared state.
