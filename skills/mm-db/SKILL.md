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
python3 <this skill>/scripts/mm_db.py executions
```

The connection is stored privately inside the Git directory. Keep its token out
of reports and PR bodies. `state` returns the current source list and revision.
Pass that revision to each mutation; on a stale-revision error, read state again
and reconsider the requested update.

Supervisor messages name their `prompt_command_id`, `attempt_generation` and
`source_revision`. Check these against `state` before acting and discard stale
instructions. Large feedback is retained in `supervisor_instruction.text` in
state; read it when the message directs you there, checking its `id` matches.
Message acceptance means queued for delivery, not that you have processed it.

Checkpoint a clean committed candidate before expensive validation. The helper
pushes only the recorded branch and records its head/tree without opening a PR:

```sh
python3 <this skill>/scripts/mm_db.py candidate --revision REV
python3 <this skill>/scripts/mm_db.py candidate --revision REV --withdraw
```

Withdraw before edits. After rebuilding, checkpoint with `--rebuild` for an
explicit observed lease. Re-read state between mutations. `attempt_generation`
fences old attempts. If `predecessor` is present, merge only your own `sources`
on its pinned base; never redo its membership. After local pass, refresh state
and run `ready --revision REV`, then `report` and finish without publishing.
`ready` hands the exact checkpointed passing head to the supervisor. A stale
predecessor blocks mutations until the supervisor clears/restarts this attempt
in the same environment. Independently
established rejections survive that restart and every abort.

`role` becomes `primary` as soon as the predecessor lands. Keep running your
existing checks and use `ready` while `predecessor` remains present. The
supervisor can start your successor from your checkpoint during validation.
After its explicit `mm-merge --promote` instruction, register the new head
without withdrawing only if incorporation leaves the tree unchanged. The
service verifies ancestry and retains your successor's pinned checkpoint.
For source edits or a changed tree, withdraw and replace it normally.

Run your chosen local builds/tests through the execution wrapper:

```sh
python3 <this skill>/scripts/mm_db.py run -- eatmydata cargo nextest run -E 'test(changed_behavior)'
python3 <this skill>/scripts/mm_db.py run --script /tmp/check.sh
python3 <this skill>/scripts/mm_db.py execution --receipt /path/to/receipt.json
```

`run` records the actual command/arguments, committed head/tree, selected build
settings and host metadata, start/end times, duration, exit status, tracked-source
changes, and stdout/stderr paths. It returns the check's exit status. Output and
`receipt.json` default to a unique directory under the common Git directory's
`mm-executions`; `--output DIR` chooses another new directory. `--script` retains
a copy of the Bash script. Keep credentials out of commands and scripts; the
helper never copies the complete process environment.

Receipts are uploaded at start and completion. An unavailable service retains
the local receipt and command result; retry with `execution --receipt FILE`
without rerunning the check. `executions` reads this batch's durable history,
including old attempts, failed and interrupted checks. Evidence intake changes
no batch revision, assessment, readiness, or landing policy, and remains
available after handoff/completion. New checks still respect the handoff fence.

```sh
python3 <this skill>/scripts/mm_db.py exclude --revision REV --pr N --head SHA --kind removed --reason 'source head changed'
python3 <this skill>/scripts/mm_db.py exclude --revision REV --pr N --head SHA --kind rejected --reason 'isolated regression' --evidence-file /tmp/evidence.md
python3 <this skill>/scripts/mm_db.py tests --revision REV --head SHA --verdict pass --tests 'commands actually run' --baseline 'reproduced failures or none'
python3 <this skill>/scripts/mm_db.py tests --revision REV --head SHA --verdict pass --tests 'selected and reused checks' --baseline none --execution ID --reuse-execution OLD_ID 'covered code, inputs and settings unchanged'
python3 <this skill>/scripts/mm_db.py ready --revision REV
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
them unless they are independently broken. If no sources remain, finish the turn
with the evidence already collected; do not rebuild, test or publish an empty
batch. The supervisor ends it and selects new work in a fresh batch/session.
Otherwise rebuild the recorded remainder once,
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

By default, the assessment snapshots completed executions at its exact head and
captured base for the current source revision/attempt. Repeated `--execution ID`
selects specific records; `--reuse-execution ID REASON` links older results with
an explicit applicability reason. Dirty-tree and unfinished records cannot be
linked as committed-tree evidence. Exit codes are observations: you still assess
baseline failures and flakes and decide the verdict. Receipts appear in the
integration PR, final report, and readiness handoff. Existing evidence without
execution receipts remains valid; do not rerun checks solely to create receipts.

The state response lists pending GitHub writes. A recorded rejection or comment
is accepted even if GitHub delivery is pending; continue the batch. The supervisor
retries delivery. A rejection is tracked by its exact head and does not change
the PR's draft state. Use `pr` and `issue` to read GitHub
metadata and comments through the service. Do not run `gh` to post comments,
change labels, mark PRs draft, or otherwise mutate GitHub directly.

Successful `mm-autopr` publication is the foreground handoff; `ready` is only for
a speculative successor. Both record a durable receipt returned as `state.ready`.
After acceptance, stop edits, builds, and pushes until the supervisor requests
new work. Membership, assessment, candidate and publication mutations are fenced;
read-only operations and diagnostic findings remain available. Repeating the
accepted handoff after a lost reply returns the same receipt. Changed inputs or
new supervisor instructions invalidate it and require another explicit handoff.

After handoff, `report` renders the recorded local verdict, test summaries,
tested SHA, publication, and ejection markers for your final message. Add any
required `known-failure:` diagnoses yourself and include them in the handoff
notes (`ready --notes-file FILE` or mm-autopr's `--notes-file`). The report is
informational; supervisor processing does not depend on a final message or
`mj wait` completion. An unavailable service is an error; do not substitute a
local database for the shared state.
