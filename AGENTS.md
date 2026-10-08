# Bifrost CI monitor — agent guide

This repository owns the merge scheduler, CI failure ledger, triage, issue
fixer, Slack integration, and tests for `BrokkAi/bifrost-dev`.
[README.md](README.md) is the operator runbook, including emergency merge
access. [docs/ABOUT.md](docs/ABOUT.md) is the human-facing feature overview.
Put agent-facing implementation notes and contracts in this file, not README
or separate design documents. Keep operator procedures in README.

# Git / version control

We commit and push to `origin/master` directly. This repository has no
pull-request flow: commit on `master` and `git push` as part of finishing a
change. This rule also applies when the current branch is `master` (it always
is).

Do not create a branch, change branches, rebase, or open a pull request unless
the user gives an explicit instruction. Do not run `git checkout -b`. The
instruction "commit" means commit on the current branch; it does not mean create
a branch first. This rule overrides other default branch procedures.

Stage and commit only the files you changed. Do not run `git add -A`. Do not
include unrelated working-tree changes in the commit.

# Operational caveat: cron runs this working tree

Cron executes `monitor.py` straight from this working tree every five minutes
(see `crontab -l`). Two consequences follow:

- Keep `monitor.py` runnable at every save. A change you push is live on the
  next tick, and even an uncommitted mid-edit state can be executed by a tick
  that lands while you are editing.
- Any new column on a table the running monitor may already have created needs
  an additive `ensure_column` migration in `connect_db`. `CREATE TABLE IF NOT
  EXISTS` never adds a column to an existing table, so a bare schema change will
  crash the running monitor on the next poll instead of upgrading it.

# Implementation map

- `monitor.py`: shared configuration, App authentication, subprocess/Mjolnir
  helpers, Slack transport, database migrations, failure parsing/upkeep, and
  the cron entry point for the issue fixer. It retains legacy run-wide repair
  helpers for historical state; new work goes through `issue_fixer.py`.
- `issue_fixer.py`: issue selection, ownership/dossier prompts, one repair
  session per work item, rejected-repair retries, report validation and cleanup.
- `triage.py`: observation-scoped investigations, validated report publication,
  deduplicated failure issues and retirement of exact resolved observations.
- `automerge.py`: queue selection, batch/direct/operator state machines,
  live-session supervision, local/CI gates, exact-head status and merge writes.
- `speculation.py`: one-batch lookahead, candidate checkpoints, same-session
  recovery and ordered promotion, shared by sync and async.
- `mm_service.py`: batch-scoped HTTP interface over shared state; it records
  test assessments/exclusions and reconciles integration-PR publication.
- `local_findings.py`: durable local baseline/flaky observations consumed by
  triage and included in the corresponding issue fixer's dossier.
- `execution_evidence.py`: durable local command receipts and explicit reuse
  links in assessments; evidence intake never changes scheduling or verdicts.
- `skills/mm-*`: agent tools for merge mechanics, state, publication, and
  two-commit checks. `scripts/install-mm-skills.py` installs symlinks and the
  user service. Tool invocation instructions remain in each `SKILL.md`.
- `scripts/apply-mergemarshall-ruleset.sh`: administrator-only normal-policy
  setup. It is never called by cron. Its dry-run and confirmation behavior
  are covered by `test_automerge.py`.

This repository's direct-master workflow does not authorize the supervised
agents to push `bifrost-dev/master`. Those agents publish PRs; only the
supervisor lands their tested heads through GitHub.

# CI host access and Mjolnir operation

The CI daemon runs as `ubuntu` on the running EC2 instance named
`bifrost-ci-agents` in `us-east-1`. Discover its current public address with
the workstation's `bifrost-ci` AWS profile; require exactly one running match.
Do not reuse an old IP or the retired `hel` hostname.

```sh
aws --profile bifrost-ci --region us-east-1 ec2 describe-instances \
  --filters 'Name=tag:Name,Values=bifrost-ci-agents' 'Name=instance-state-name,Values=running' \
  --query 'Reservations[].Instances[].{ID:InstanceId,IP:PublicIpAddress}' --output json
ssh -o BatchMode=yes -o ConnectTimeout=10 ubuntu@CURRENT_PUBLIC_IP
```

Run mj on that host, using `/home/ubuntu/.cargo/bin/mj` and workspace `CI`.
The workstation daemon and remote build targets have separate sessions.
The monitor checkout is `/home/ubuntu/Projects/bifrost-ci-monitor`; set
`BIFROST_GH_BIN=/home/ubuntu/.local/bin/gh` when invoking its Python helpers,
matching cron. The older `/usr/bin/gh` lacks required PR fields.

```sh
/home/ubuntu/.cargo/bin/mj api-info
/home/ubuntu/.cargo/bin/mj sessions --workspace CI --json
/home/ubuntu/.cargo/bin/mj sessions --session SESSION_ID --json
/home/ubuntu/.cargo/bin/mj transcript --session SESSION_ID --role agent --json
/home/ubuntu/.cargo/bin/mj transcript --session SESSION_ID --role tool --after-seq CURSOR --json \
  | jq '{next_after_seq, items: [.items[] | {seq, text, status: .body.call.status}]}'
```

A merge batch ID is not its mj session ID. Resolve it through
`automerge_batches.session_id` in `monitor.DB_PATH` or match the batch's exact
persisted title in `mj sessions`. Page transcripts with `next_after_seq`.
Transcript cursors and `mj events` cursors are different sequence spaces.
Read the agent's own output before reporting findings. For tool activity,
project only safe summary text/status; raw bodies, inputs and presentation
fields can contain connection tokens. `api-info` reports a credential file's
path; never read or print that file's contents.

For authorized steering, `mj message --session SESSION_ID 'guidance'` uses the
same route as `send_message`. Automation uses `monitor.send_session_message`
with a persisted request ID through `/sessions/SESSION_ID/message`; the CLI
generates a fresh ID per invocation, so it is unsuitable for ambiguous retries.
Reuse the original request ID after a lost reply. Inspect command `--help` for
installed flags. Typed `/clear` and the ordered restart brief continue to use
`mj prompt --command-id ID --prompt-file FILE` and their durable recovery boundary.
`mj interrupt-turn` cancels the current turn, `mj stop-task --session SESSION_ID
TASK_ID` stops a listed background task, and `mj suspend --session SESSION_ID
--acknowledge-unpublished-work --json` preserves recovery state while releasing
the environment. Coordinate supervised lifecycle changes through the existing
supervisor lock and cleanup path. Use suspension to preserve work; `mj destroy`
permanently deletes the environment and recovery archive.

# State, authentication, and process ownership

All three entry points share `monitor.DB_PATH` and its additive
`known_failures` ledger. Merger tables include `automerge_batches`,
`automerge_relayed_messages`, `automerge_blocked_notifications`, and
`automerge_skill_events`. `automerge_github_outbox` holds App-owned GitHub
comments, labels, integration-PR metadata, and changed-head draft requests.
It also holds dependency promotion requests. `automerge_pr_inventory` retains
observed branch identities and head history, `automerge_pr_dependencies` retains
relationships for each exact dependent head, and `automerge_commit_ancestry`
caches only successful comparisons of immutable commit pairs.
The merger agent records intents with `mm-db` and publishes through `mm-autopr`;
it must not make direct `gh` writes. The supervisor retries outbox delivery after
ambiguous or failed responses. Queue selection honors recorded exact-head
rejections and pending drafts before GitHub reflects them. Delivery progress must
not change the agent's batch revision. The integration PR create and exact-head
merge remain synchronous. Fixer tables are
`issue_repairs` and `issue_repair_messages`; triage uses `triage_jobs`,
`triage_observations`, and `triage_publications`. Historical `invocations`
remain readable. An active legacy invocation blocks new issue repairs until
it is retired. Never replace or discard the live database to fix scheduling.

`local_findings` retains merge-agent baseline and flaky product-test observations
with their actual tested commit, command, evidence, and originating batch/session.
Use `mm-db findings` to inspect them and `mm-db finding --revision REV --kind
baseline|flaky --head SHA --identity TEST --command 'actual command'
--evidence-file FILE` to record one. Baselines must name the captured base SHA.
Reuse collected evidence; registration does not require another test run. Record
unresolved product failures, including flakes that passed a rerun; omit repaired
interactions, infrastructure and known container limitations. Exact-head source
rejection still uses `mm-db exclude --kind rejected`. These are distinct records.
Local findings do not change the batch revision or invalidate its assessment,
and may be registered after publication/completion. Triage investigates them
alongside CI observations and publishes through its existing ownership/dedup
path; the fixer receives the local evidence in the linked issue's dossier.
Keep local findings out of the master CI ledger and sync baseline authorization;
do not invent run IDs, workflow/job mappings, or proof of recovery.

The tooling owns `mergemarshall:in-progress` on selected source PRs, including
batch expansions and direct/operator records. Membership changes checkpoint
label intents atomically; removal, completion, abort, and failed launches queue
cleanup. Delivery reconciles against current live membership and the current
open head, so delayed cleanup cannot untag a reselected PR. The worker creates
the repository label if absent. This label is informational: failures retain
backoff/retries without blocking selection, validation, landing, or changing
agent revisions. These tooling intents are omitted from agent pending writes.

New rejection and integration labels are `mergemarshall:rejected` and
`mergemarshall:batch`. Read the old `automerge-rejected` and `mergemarshall-batch`
names as aliases during migration across selection, operator landing, fixer
retries, and outcome reporting. Stale-rejection cleanup removes whichever old/new
labels are present. Queued writes resolve the current canonical names at delivery.
The worker creates the new repository labels when needed; no historical label or
comment migration is required. Machine-readable markers now use
`mergemarshall:rejected-head`, `mergemarshall:ejected-pr`,
`mergemarshall:local`, and `mergemarshall:verdict`; accept the previous
`automerge-*` forms when reading live sessions and stored evidence. Reconcile
trusted rejection comments across both spellings before posting, so a delivery
retry never duplicates an accepted legacy comment.

Each entry point takes its own non-blocking file lock. Operator abort and
fast-track commands wait up to 120 seconds for the merger's same lock.
Triage publication also coordinates with the fixer lock before deciding
whether an issue should be created. Preserve these ownership boundaries when
changing selection/publication; do not make independent workers decide the
same claim from separate snapshots.

Host-side GitHub operations go through `monitor.run_gh`, which obtains an
installation token with `mj github-token --owner BrokkAi`, caches it for at
most 30 minutes, and refreshes on authentication failure. Production requires
the App token. `REQUIRE_APP_TOKEN=False` is only for local development with
ambient auth. Do not add a fallback that silently uses an operator's login.

The supervisor App is `mergemarshall`, ID 5203169, with trusted bot login
`mergemarshall[bot]`. Mjolnir injects narrower session tokens without
status-writing permission. Only the supervisor can post the required
`mergemarshall/verdict`. Do not grant the App checks-write: an App check run
with that name could also satisfy the GitHub rule. Never print tokens,
batch connection files, secret contents, or complete stored prompts.

The normal master ruleset requires PRs, zero approvals, the App-bound verdict,
an up-to-date branch, and deletion/force-push guards, with no bypass actors.
The policy script finds a master-targeting ruleset with both guards, preserves
its name and targeting, and refuses ambiguous/unrecognized matches. Creation
and updates require confirmation. It replaces rules/bypass actors; never run
it automatically to undo an operator's emergency configuration.

# Session lifecycle and ambiguous launches

Modern fixer, triage, and integration sessions stay live while their work is
active, including idle waits and follow-up turns. A merger tick observes for
at most 50 seconds, then leaves the session for the next tick. Do not restore
the old one-hour turn cutoff or suspend/resume between attempts or CI polls.
Legacy timeout helpers in `monitor.py` do not describe the current lifecycle.
Manual abort and immediate-priority preemption deliberately interrupt work.
After a verified terminal outcome, checkpoint/suspend through the existing
cleanup path; supervision errors leave sessions available for later polls.

Integration completion uses a durable `ready_json` handoff in the shared DB.
Successful `mm-autopr` records it atomically with publication; speculative
successors call `mm-db ready` for their checkpointed passing head. Receipts bind
the tested full head, local assessment, source revision and attempt generation.
The supervisor consumes them in both CI modes without requiring `mj wait`,
session idleness or a final assistant message. It preserves all normal freshness,
ancestry, exact-head and CI gates and suspends after confirmed landing. Agent
mutations are fenced after handoff; accepted retries are idempotent. Changed
inputs, recovery, promotion and new instructions invalidate readiness. Follow-up
guidance uses Mjolnir's `send_message` API with persisted request IDs, reaching
busy workers at tool boundaries and waking idle workers. Mjolnir handles older
workers with its queued-turn fallback. A receipt means accepted into its outbox,
not read by the agent; never resend an accepted message awaiting delivery. The
legacy `prompt_delivered` flag records acceptance only after the API returns.
Every merger message names its `prompt_command_id`, attempt and source revision;
agents check those against mm-db state and discard stale messages after recovery
or newer instructions. An old active-turn flag cannot establish acceptance.
Feedback exceeding mj's 64 KiB message limit stays intact in
`supervisor_instruction.text` in mm-db state; the message directs the agent to
fetch that matching instruction. Read-only state and diagnostic findings remain
available after handoff. On upgrade, matching durable publications
and already parked successors acquire receipts once; incomplete assessments do not.

Integration sessions use Opus 5.5 (`opus`) on `bedrock-podman` with Mjolnir Luna 6
(`global.openai.gpt-6-luna`, high effort) subagents. The CI Mjolnir configuration
allows up to 16 concurrent subagents per session. The primary coordinates the
checkout and branch and owns Git operations, batch mutations, test assessment,
and publication; subagents edit assigned files. Work against the captured base
until the supervisor requests an update; it owns final master/source freshness
checks. Follow the batch skills and revision checks.

Keep a short private progress note in the Git directory with HEAD, pending edits,
decisions and evidence/log paths, unresolved work, running command/session IDs,
and the next action. Update it at meaningful milestones. After compaction or an
interrupted turn, reconcile it once with HEAD, working-tree status, batch revision,
and running commands, preserving pending edits and reusing matching evidence.
Investigate actual mismatches; reopen settled work for changed inputs, missing
evidence, or contradictory evidence, and record that reason. Keep credentials
out of the note.

Persist the exact launch title/intent before a request may create a session.
On an ambiguous result, list the CI workspace and adopt the matching title;
never duplicate a session because creation timed out or a response was lost.
Merger launch-grace logic is separate from an agent work deadline. Absence
must be established before creation is retried. Preserve the original error
while awaiting discovery.

Fixer prompts must fit both 65,536 Unicode characters and 96 KiB after JSON
encoding, below mj's 128 KiB request cap. `render_prompt` omits general PR
inventory first, then older comments, then shortens the issue body with
explicit omission/excerpt metadata. Target and rejection evidence take
precedence. `bounded_stored_prompt` also handles jobs saved by older code.
Only recognized pre-creation prompt-validation/request-size API rejections
return to `selected`; unknown failures stay `launching`. Create/write the
local prompt file before recording a launch attempt so filesystem failures
cannot strand an unsubmitted job. These boundaries are covered by
`test_issue_fixer.py`.

# Issue fixer contracts

Select open `buildfailure` issues without someone else's assignment,
`agent-in-progress`, or `Escalated`; exclude the generated aggregate issue.
There is no pending queue snapshot: each idle poll reads GitHub and SQL-orders
current work, rejected owned repair PRs first, then oldest issue number.
One active job addresses only its selected issue and linked observations.

The dossier contains the issue, recent comments, its own ledger observations
and diagnosis provenance, and an open-PR inventory. The agent judges PR
relevance. Never add a directive to fix every red test or dump all issues.
It reads Bifrost's AGENTS, claims with `agent-in-progress` and a comment naming
session/branch, requests `brokk-service` assignment if assignable, and accepts
the user-approved label/comment claim when assignment is unavailable.
`mergemarshall[bot]` cannot be assigned. Refresh ownership before claiming
and publishing; a failed claim forbids beginning work. Standing down releases
only its own claim.

Prefer a straightforward production fix or mechanical test update; otherwise
revert the introducing change. A nontrivial revert or irreconcilable task is
documented on the same issue, escalated with `Escalated` and assignment to
`DavidBakerEffendi`, and its own claim released. Unrelated failures are test
limitations, not additional repair tasks. Submitted work uses a `ci-fix` PR,
`Fixes #N`, `CI-Repair-Issue: N`, and relevant run trailers. Merge current
master before readying it; keep it draft while editing. The fixer neither
merges its PR nor pushes master.

Infrastructure is operational work, not a Bifrost product defect. The fixer
reports `infrastructure` with no PR, records evidence on the misplaced ticket,
and releases its own claim. The supervisor closes that ticket as not planned
only after checking it has no remaining owner/claim, then posts the cached
summary at channel level. Do not assign David or create another product ticket
for runner/provider/quota/network failures. Flaky product tests remain defects.

A rejection retry requires the recorded fixer association and a trusted
`mergemarshall:rejected-head` comment exactly matching the current head. Stale
labels and another author's PR are not retries; another person's assignment
still blocks selection. The new session uses the existing branch/PR, makes it
draft before pushing, appends corrections and readies it again. Each rejected
head is handled once; a later head can be repaired again. Never interrupt an
active repair just to prioritize another one.

# Automerge contracts

## Selection and membership

Read all open PRs, including non-master bases, excluding integration PRs and
heads rejected by a trusted exact-head marker. `READY_POLICY=non-draft` is
the default; `approved` additionally requires `APPROVED`. A stale rejection
label on a new head is not a permanent exclusion.

Dependency discovery belongs to the supervisor (`pr_dependencies.py` and the
GitHub adapter in `automerge.py`). Resolve base branches against head branches
in the same repository, including closed prerequisites when needed. Verify
ancestry and infer inherited work from observed/current/rejected PR heads even
when the dependent already targets master. Descriptions are not dependency
authority. Capture prerequisite numbers and full head SHAs in SQLite and batch
source JSON; retain relationships across retargeting and restarts. A new
descendant head retains prior relationships; unrelated replacement work does not.

Admit each source only when every prerequisite is in current master by actual
commit ancestry, or is ready at its exact captured head and included earlier in
the same batch. Apply the closure at selection, priority/preemption, expansion,
retries, direct landing, and `--land-now`. Changed, draft, rejected, withdrawn,
or closed-unmerged prerequisites block descendants while unrelated work proceeds.
A repair unblocks descendants after they contain the new eligible prerequisite
head. Ambiguous branches/shared heads, cycles, and unresolved relationships are
reported through dependency block notices and `--check`.

Dependency notices link the affected PR and prerequisite, state the author's
next action, and describe the scope of that block without claiming the queue
is progressing. Keep exact-head and reason identities internal to notification
deduplication; pending legacy notices use the same readable delivery format.

Ejection removes dependent descendants with exclusion kind `blocked`; only the
standalone-broken prerequisite receives a rejection. Recheck captured dependency
heads before landing. Global recorded rejections also prevent a candidate from
importing a rejected head outside the batch; its eligible repaired PR must be
included, or that ancestry must already be in master.

Queue `promote_dependency` intents when all captured prerequisites land, including
combined integration merges. Verify prerequisite ancestry in current master and
the unchanged dependent head before retargeting it to master. Promotion is
independent of readiness and GitHub's indirect-merge bookkeeping. Reconcile
already accepted retargets after lost replies/restarts through the outbox. Never
rewrite author branches, rebase, or require authors to promote/poll their PRs.

Priority labels are case-insensitive. `mergemarshall:high` and
`mergemarshall-priority:high`, plus legacy `mergemarshall-priority`, select the
high tier. `mergemarshall:immediate` and
`mergemarshall-priority:immediate` select immediate. If either tier is ready,
select all eligible high/immediate PRs and their prerequisite closure. Ordinary
PRs enter that lane only as prerequisites. `high` waits for
current work. A newly eligible immediate PR preempts an active batch through
the durable abort path, even if the batch is high, unless already included;
never preempt after success is posted or merging starts. Source PRs from an
abort remain eligible. `ci-fix` is ordinary unless labeled explicitly.

On interruption, removals, or rebuild/retest requests, rescan membership.
Allow three expansions per batch; an empty scan consumes none. Priority
batches admit only priority PRs and their prerequisites; removed heads never reenter the same
batch. Commit membership/counter with the follow-up prompt so restarts cannot
reset the limit. A finished passing tree is not rebuilt just for new arrivals.
A source head changed during processing is made draft. Remove changed/closed
sources without rejection and let the author ready the new head. If removal or
ejection leaves no sources, end the batch; never fill an empty batch with arrivals.
During running turns, each supervisor tick checks source eligibility and starts
cleanup immediately when every source is ineligible or already excluded. Fence
agent writes, stop queued turns/background checks/subagents, and suspend through
the durable cleanup path before releasing the batch. Normal selection creates a
new batch/session on a later tick. If sources remain, retain useful agent work
and apply external removals at the existing turn/rebuild boundary.

## Integration and test evidence

### Candidate checkpoints and speculative successors

After merging/fixing a clean committed candidate, checkpoint with `mm-db candidate`
before expensive validation. This pushes only the recorded ref and atomically
records head, tree, source revision and attempt generation; it creates no PR.
Withdraw the checkpoint before further edits and register a replacement when
ready. Repeated accepted registrations/withdrawals retain their identities.
The supervisor permits one successor, selected with dependency closure against
the pinned candidate, excluding every live batch's reservations. Dependency
publication continues to use actual master. New batches persist current CI_MODE.

A successor's membership contains only its new source PRs. It may resolve,
validate and independently reject those exact heads. An interaction with the
unlanded predecessor does not establish a standalone defect. It records local
pass and waits without publishing; the service and landing gates forbid
publication/landing before landed ancestry incorporation. Source changes, priorities and aborts
retain the normal policies. Never discard established rejection intents on
reset, abort or failed launch.

When the predecessor lands, the successor takes the primary scheduling role
immediately (`role_promoted`), even with validation still running. Prove the
pinned predecessor tree landed; retain the same session, checkout, captured
base, source revision, checkpoint, evidence and running work. Its checkpoint
can now start the next speculative successor. Permit one primary and one
speculative successor; an unpromoted successor cannot start another batch.
`mm-db state` exposes `role`. A primary can still have `predecessor` metadata
until ancestry incorporation: the agent continues its existing validation and
uses `mm-db ready` as before. Passing evidence gates publication, not role
promotion. A changed actual master tree uses the existing same-session recovery.

Slack start headings mark successors as `SPECULATIVE` and name the predecessor
whose landing gates publication/merge. PR-list replies, relayed progress, blocked
alerts and terminal summaries retain that distinction until role promotion.
After role promotion, new messages use normal batch wording. Message decoration
must not change stored agent evidence, relay identities/cursors or delivery retries.

Invalidation fences old mutations immediately. Persist a new attempt generation,
stop queued prompts/turns/background tasks and child sessions. Cancellation
accepts an already-ended turn; cleanup never infers turn
activity or waits for session idleness. Existing task/child cleanup and the
clear boundary (or suspension for aborts) own termination. Then issue typed
`/clear` with a stable command ID. Wait for its durable context divider before
submitting a deterministic brief, also with a stable ID. Lost replies retry the
same command; confirmed failed clears use a new ID. The session, container,
checkout and caches survive. `mm-merge --restart` archives the old tip, pending
source edits and progress note once, then assembles from the replacement base
and original own heads. Preserve Git rerere with automatic staging disabled;
review reused resolutions. Never transplant old merge commits or retain the
invalidated predecessor's ancestry. No agent-written handoff or cache transfer
is needed. Read final reports only after this attempt's context divider.

After the passing handoff, recheck master and queue `mm-merge --promote` to
incorporate the actual landed commit. Keep the checkpoint available while this
approved ancestry incorporation is pending, provided its original source inputs
and attempt generation still match. If the committed tree is unchanged, register
the new head without withdrawing: the service verifies ancestry and preserves
the logical checkpoint ID with its verified equivalent heads. A pinned successor
can keep its work through this step. Different trees, source changes, exclusions,
withdrawals and recovery invalidate it normally. Once landed, the parent's
exact tested head must equal its current checkpoint head; a verified older
equivalent head may remain the successor's pin. Evidence may
be reused only with unchanged tree and applicable validation inputs/settings;
always record a fresh assessment for the resulting committed HEAD/source set.
A differing master tree starts a fresh attempt in the same environment. Normal
sync/async freshness, CI and landing gates apply after promotion. Completed
notification retries must not occupy the foreground selection slot.

### Merge and validation policy

One session creates `mergemarshall/batch-<id>` from the captured master base,
retaining source heads through merge commits. Every agent-authored commit has
`Automerge-Batch: <id>`. Resolve conflicts preserving both intents; a conflict
alone is never rejection. Append fixes for interactions or mechanical stale
tests. Eject standalone-broken PRs or changes requiring someone else's design
to be substantially rewritten. Ejection rebuilds from the base without those
heads; never use a revert that would retain their ancestry. Force-push only
the exact batch ref, with `--force-with-lease`.

The supervisor runs Bifrost's `scripts/public/ci-impact.mjs` from the captured
base on the union of exact source-head diffs. `docs` instructs the agent to run
no tests, builds, or baseline reproductions. Every other mode (including an
unavailable classification) tells the agent to use its best judgment to choose
useful local checks and expand testing when failures or specific unresolved
concerns warrant it. A `full` result does not require the full CI suite locally.
The agent uses Bifrost's AGENTS and workflow definitions for commands and
environment conventions, without rerunning ci-impact. Select useful targeted or
broad/grouped checks from the diff, dependencies, interactions, conflict
resolutions, existing results, and specific unresolved concerns.

Delegate bounded investigation, review, or implementation to Luna. Divide large
merges into independent PR groups or conflict clusters with explicit ownership;
the primary combines and assesses the work. Builds are expensive: normally
consolidate changes and validation through the primary. Subagents propose the
smallest useful check and build only when assigned a check that resolves a
specific decision. Concurrent builds are appropriate when independent useful
checks justify their cost and use the existing mbx configuration. Throttling
controls resource contention; avoid duplicate or speculative diagnostic builds.
The supervisor's next-batch lookahead is an independent useful build.

Prefer blame, history, focused diffs, and tracing the failing assertion through
data/control flow; inspection is almost always faster here than rebuilding Rust
bisect variants. Before an expensive experiment, record the pending decision,
smallest useful check, what results would change, and reusable evidence or built
trees. Use bisect or subtraction/rebuild experiments when inspection cannot
distinguish specific alternatives and the build justifies its cost. Attribution
prefers captured base versus that base plus suspected exact heads through real
merges with consistent settings; account for an older PR-head base. Patch
reversals may suggest hypotheses; acceptance/rejection requires evidence from
the required committed trees. On contradictory results, reconcile tested trees,
commands, and settings first. Record sufficient fix/eject conclusions in the
progress note.

When validation fails, preserve the failing committed candidate and logs while
diagnosing the available failures. Collect failures from selected checks, avoiding
fail-fast behavior where practical, and continue useful independent checks when
others are blocked. Group failures by likely cause and delegate independent
investigations against the same candidate and captured source heads. Assess every
observed group as reproduced baseline, interaction/mechanical fix, independently
broken source, or unresolved failure needing evidence. This covers observed
failures and specific concerns, not separate testing of every PR or hypothetical
defects. Before changing the integration tree or ending the turn, consolidate
established rejections and applicable fixes. Resolve attribution questions before
rejecting a source. Record the combined exact-head exclusion set through mm-db,
refreshing revisions between mutations, then read the remaining membership;
dependent descendants are removed without independently rejecting them. If sources
were excluded, rebuild the recorded remainder once, preserving applicable
fixes/conflict resolutions. Append fixes for retained PRs and validate the result;
reproduced baseline failures alone require no rebuild. Do not rebuild and retest
between individual exclusions from the same diagnosis pass. Build blockers can
hide more failures; record blocked checks and reassess after the combined rebuild.

After exclusions, fixes, conflict resolutions, or base updates, choose reruns from
the actual diff against the last tested candidate. Cover affected behavior,
shared dependencies/interactions, and previously failing checks addressed by the
changes. A membership or HEAD change alone does not require another full suite.
Reuse results for areas whose covered code, dependencies, test inputs, and settings
remain unaffected, unless contradictory evidence appears. Expand when impact
cannot be bounded or a specific concern warrants it, and record that reason.
Keep original tested SHAs, commands, logs, and reuse reasons in the progress note.
Fixture/golden generation is editing; validate affected checks on the resulting
committed tree without blessing enabled. Record a fresh assessment for the final
HEAD and current source set, distinguishing checks run there from reused results
with their original tested SHAs and applicability reasons.

Reproduce only failures observed in the candidate's selected checks at the exact
base, reusing existing exact-tree evidence where available. Passing tests and
ledger failures absent from the candidate need no baseline runs. Test committed
trees without temporary source edits or validation shims. Baseline build failures
may block dependent checks; report those as blocked and run unaffected useful
checks. Baseline summaries contain candidate failures reproduced at base;
repaired tests belong in fix notes. The final assessment describes the resulting
committed candidate. When selected checks pass or only reproduced baseline
failures remain, proceed to the mode's publication/reporting step. No local test
command registry is implemented.

Run selected local builds/tests through `mm-db run -- COMMAND...` or
`mm-db run --script FILE`; configured `mm-compare` records both sides automatically.
`automerge_executions` retains command/arguments, committed head/tree, safe build
settings and host metadata, start/end times, duration, exit status, source changes,
output paths, source revision and attempt generation. The helper retains output,
script snapshots and retryable `receipt.json` under the common Git directory by
default. Upload start/completion with stable execution IDs; late starts cannot
overwrite finished results. Completed evidence is immutable. Retry delivery with
`mm-db execution --receipt FILE` without rerunning checks. `mm-db executions`
reads the batch's entire history. Intake remains diagnostic after handoff and
completion and never changes a batch revision, assessment, readiness or CI gate.
Assessments snapshot completed clean-tree checks at the assessed head and captured
base in the current source revision/attempt by default. Explicit `--execution ID`
selects checks; `--reuse-execution ID REASON` retains original tested inputs with
an applicability reason across changed heads/source sets or attempts. Preserve
these snapshots through publication and readiness. Existing summaries/evidence
remain accepted without receipts; never rerun merely to backfill observations.

Persist classification in `validation_impact_json` with the exact base and
heads. Reclassify when the source set, base, or candidate changes; reuse the
result for unchanged retries. Fetch the classifier and diffs through the normal
App-authenticated GitHub runner, then import `classifyChangeSet` in host Node.js.
Include both paths of renames. Missing/truncated diff data or classifier errors
use the judgment policy, never the docs shortcut. Before accepting a docs
report with no tests, independently classify the actual integration head.

Prompted Cargo commands use `eatmydata`; do not export session-wide
`LD_PRELOAD`. mbx owns Cargo build storage: do not set `CARGO_TARGET_DIR`,
invent/clean target directories, or bypass the cache. The root-only
`unwritable_workspace_root_reports_the_ways_out` failure is a known container
limitation only for that exact assertion; in async, reproduce it at the base
and report it. Other failures require normal investigation.

Publish one ready integration PR named `Merge batch: #...` with
`mergemarshall:batch`, exact source heads, tested head, and conflict/fix notes.
Async publication requires local pass first. Publication retries can reuse
evidence only while committed HEAD, remote head, and clean working tree still
describe the same tested candidate. Look up the PR using the bare head branch
name; reconcile accepted writes whose replies were lost.

## CI modes and landing

Persist `CI_MODE` at batch creation; the Bifrost default is async. Migrated
old batches without a mode remain sync. A later setting edit never changes
an active batch's mode.

Async requires a recorded passing local assessment and accepted publication
handoff for the tested full HEAD and current source revision. The human report
renders `mergemarshall:local: pass|fail`, `Tests run:` and `Baseline failures:`;
its arrival is not a scheduling gate. `Tests run: none` is accepted only for a
supervisor-confirmed docs candidate; baseline/test summaries remain required.
Any new failure must be fixed or the exact responsible
head removed/rejected; a baseline requires a local rerun at the captured base.
The supervisor does not wait for/query CI or dispatch baseline runs. There is
no sync-style CI-round limit. Master CI after landing is handled by triage
and the issue fixer. Agent reports and PR-produced tests are trusted evidence
under the project's current contributor policy.

Sync accepts `PR verification` only from `.github/workflows/ci.yml`, the exact
tested head, `pull_request`, latest run attempt and matching check suite.
Before querying CI or a baseline, compare the captured base and PR base with
current master; an advance requires merging current master and retesting.
On red, compare failed tests AND failed steps independently within each same
failed job against the exact base. A newly failing job is worse. The agent's
`mergemarshall:verdict` is advice, not authorization. Choose the newest master CI
for the exact base, or prior integration CI only with identical Git trees.
Pending baseline CI waits. Missing/cancelled CI can use open parser-derived
ledger identities last seen at/equal-to-an-ancestor-of the base; otherwise
dispatch master CI only while master is still at the base. Persist intent and
a 10-minute discovery grace before retry. Unavailable baseline or integration
CI data remains pending with a deduplicated top-level Slack alert and automatic
retries. Fix/eject in the same live session; at most four
sync CI rounds are allowed.

Before success, recheck current master, source eligibility/head, included
ancestry, excluded-head absence, and the exact tested integration head. The
excluded-head check permits a head already present in current master. A
confirmed ancestry mismatch queues a rebuild from selected source heads and
fresh tests. Unavailable compare data leaves the verdict pending, sends a
top-level Slack alert, and retries verification on later ticks. The
current implementation has no workflow/action-change human-review gate.
Do not resurrect it from historical docs, old database columns, or pending
status descriptions. Only the supervisor posts success, then performs a
merge commit with `--match-head-commit`. A master advance requires updating
and retesting; an accepted merge command must still be confirmed via PR state.
GitHub's indirect source-PR merged state may lag: report it in the one outcome
summary, without per-PR provisional warnings.

## Direct, operator, and retry paths

Exactly one eligible PR with `master...head` reporting `behind_by == 0` uses
a persisted `direct` record without session/integration branch. Async lands
after common source checks without a new local test run; sync applies exact
PR CI and baseline comparison. A worse sync result is rejected by the
supervisor with exact-head evidence. Master movement ends/falls back without
rejection. Durable `direct_merging` observes an already accepted merge after
restart rather than duplicating work.

`--land-now` uses the direct async/operator path, requires an up-to-date source
PR, and does not wait for CI/run tests or abort another batch. It posts
`fast-track by operator` through the normal App path. The removed
`--allow-workflow-changes` flag must not be documented as current.

Verdict/merge failures for a still-current candidate persist
`github_write_retry_attempts` and `github_write_retry_after`: delay 1, 2, 4,
8, then 10 minutes repeatedly. The first failure gives a top-level Slack
alert. Leave the PR open; retry until merged or explicitly aborted. Recheck
candidate freshness at retries. Do not terminate/close a tested candidate
merely because GitHub refused one write or confirmation was ambiguous.

Abort is resumable: interrupt/suspend, failure verdict, close the integration
PR, preserve branch, then release nonrejected sources to the queue. Established
exact-head rejections and their pending outbox writes survive every abort;
never cancel them or remove their labels as abort cleanup. Direct abort leaves its source PR open.
Retry the Slack outcome independently when delivery is unavailable.

# Failure ledger, triage, and Slack

The ledger key is workflow/job plus deterministic test identity, or failed
step when no test can be parsed. Normalize RunsOn run labels while keeping
real matrix values; exclude aggregate `PR verification`. All three jobs use
shared persisted five-minute upkeep. Process each completed master run once,
ignore cancelled runs, fetch logs only for failed jobs, and use failed-step
metadata if logs are unavailable. First backfill is at most five completed
runs per workflow in the last 24 hours. A later passing job closes its rows.
Within a red job, retire an absent identity only when its recorded failed
steps passed, or completed test results from those steps establish its absence.
`FailureReport.successful_steps` carries per-step passes; `incomplete_jobs`
marks runner loss/acquisition, unfinished/timed-out steps, or unavailable logs.
Partial logs can add failures, but cannot prove prior failures disappeared.
A failure in checkout/build does not resolve unexecuted tests. `known-failure:` text can diagnose only an
existing observed identity; it never creates evidence.

SQLite is authoritative. The pinned `Known CI failures on master` issue is a
generated view, never parsed back as state. Update only changed renders and
continue if pinning is denied. Update its stored issue number through REST;
failed idempotent updates retain the prepared body in
`known_failure_state.issue_pending_body` and retry on the next poll, including
when run ingestion is rate-limited. Retry that exact body before rendering a
new view; never repeat an investigation or CI-log parse just to retry a write.
Title search is only for initial setup. Triage issue links do not suppress the fixer;
it reuses those issues. A repair PR associates observations with proposed
work, but is not evidence that failures are fixed.

Triage groups up to 40 new observations by captured workflow/job/identity,
failed steps and commit, not repeated runs of the same observation. Its
2-CPU/4-GiB session reads logs/source/history but does not build or fix code.
Validate final JSON, deduplicate causes against open/closed issues, reopen
matching issues, and persist publication markers to recover lost replies.
Every finding declares `outcome: product|infrastructure|resolved`. Only product
findings may contain issue drafts. Infrastructure publishes a short top-level
Slack notice and the details in its thread; issue bodies and Slack text are
prepared once and cached in
`triage_jobs.report_json`. Infrastructure findings in one report share a thread;
nearby reports reuse that channel's thread until a 15-minute gap between successful
notices. `triage_infrastructure_threads` persists the window across restarts, while
cached report thread IDs keep publication retries together even after the window.
Each reply includes the finding's job summary. Publication's existing fixer lock serializes
thread selection. Infrastructure checkpoints
handled observations in the existing `triage_observations` table. Handled
infrastructure remains open in the CI ledger until normal completed-run upkeep
observes recovery; it is never treated as resolved merely because it was reported.
Persist the accepted classification in the additive `known_failures.triage_outcome`
column. Keep it only for the same open commit/failed-step observation; reset
on changed SHA, changed steps, or reopening. On upgrade, triage backfills matching
open observations from explicit outcomes in completed cached reports, once,
without investigation or publication. Do not infer infrastructure from old
unclassified reports. Merger prompts label infrastructure as diagnostic only:
never reproduce/repair it, reject a source PR for it, or treat it as a product-test
baseline. Exclude classified infrastructure from the sync ledger-baseline fallback;
the sync CI failure state machine itself still needs separate infrastructure handling.
Publication outages leave the cached report available for later polls without
occupying the investigation slot or allowing duplicate investigation of its
captured fingerprints. Accepted findings are checkpointed independently so a
later finding's failed write does not republish earlier Slack messages. Failed
Slack sends retry the cached text; an accepted send whose reply is lost can still
produce a duplicate notice. Old in-flight reports lacking outcome must classify
their existing findings in a correction turn before any issue write, using their
captured evidence rather than repeating investigation.
Skip findings fixed/superseded during investigation. A resolved finding can
retire only its unchanged captured commit/steps/run; a later failure reopens
the ledger. `--reconcile-resolved` reconciles historical completed reports
without a new agent. `--retry-launch` first requires no exact-title session.

Bot transport messages use persisted cursors and stable-ID deduplication;
webhooks provide engagement/outcome messages only. Keep delivery failures
separate from state-machine progress. Blocked alerts deduplicate by reason;
GitHub write and CI/baseline/ancestry lookup failures are deliberately top-level.
Never manually send extra
Slack/GitHub messages without user authorization.

# Validation

This is a Python repository. Pick the relevant existing modules:
`test_automerge`, `test_merge_retries`, `test_membership_labels`, `test_mm_skills`, `test_issue_fixer`,
`test_repair_dossier`, `test_triage`, and `test_monitor`. Dependency changes also
use `test_pr_dependencies`, with temporary Git histories and mocked external
writes. Never validate scheduling against the live queue. For Python changes,
run the affected suites, `python3 -m py_compile` on changed files, and
`git diff --check`. Do not run Bifrost/Mjolnir Rust crate suites for monitor
changes. For documentation-only changes, verify local links, described CLI
flags, shell syntax and SQL examples against temporary state; no repair or
merge should run as a documentation check.

Fixtures must isolate state and mock external writes/session creation. Some
legacy tests can attempt real `mj` subprocess reads; do not run them against
the production host just to validate unrelated docs. Never execute README's
recovery writes, mint credentials, change rulesets, pause live cron, or merge
a PR merely to test the runbook. Operator documentation is not permission to
perform the incident actions.
