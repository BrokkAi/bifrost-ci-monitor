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
- `agent_recovery.py`: shared native cancellation and context-reset steps for
  failed triage/fixer turns; each supervisor checkpoints through its own tables.
- `session_policy.py`: future merger/fixer Opus/high launches and straightforward
  DeepSeek Flash/high delegation; existing sessions retain their launch policy.
- `triage.py`: observation-scoped investigations, validated report publication,
  deduplicated failure issues and retirement of exact resolved observations.
- `automerge.py`: queue selection, batch/direct/operator state machines,
  live-session supervision, local/CI gates, exact-head status and merge writes.
- `speculation.py`: one-batch lookahead on actual master, candidate checkpoints,
  landed-master promotion and ordered role handoff, shared by sync and async.
- `supervisor.py`: durable action intents, ordered batch advancement, bounded
  observation, independent maintenance, and threaded idle queue summaries.
- `git_ancestry.py`: shared full-history Git object/result cache and asynchronous
  fetch receipts; workers never decide scheduling or mutate batches.
- `read_budget.py`: invocation-scoped deadlines for shared commands and HTTP reads;
  other supervisors retain their normal timeouts.
- `mm_service.py`: batch-scoped HTTP interface over shared state; it records
  test assessments/exclusions and reconciles integration-PR publication.
- `local_findings.py`: durable local baseline/flaky observations consumed by
  triage and included in the corresponding issue fixer's dossier.
- `execution_evidence.py`: durable local command receipts and explicit reuse
  links in assessments; evidence intake never changes scheduling or verdicts.
- `attest_store.py` and `attest_ci.py`: standalone Attest storage proxy and
  retryable completed-CI artifact intake. Detailed results live in S3; receipts
  retain compact summaries. Intake never changes a merge gate.
  Discovery queues trusted attempts durably before importing one per tick,
  refreshing today's first page and checkpointing one older day/page through
  the seven-day evidence retention window. Pending retries survive discovery
  churn. Untouched attempts import newest first so fresh CI is not blocked by
  historical backfill; failed/interrupted imports yield to untouched work and
  then retry oldest first through additive `last_attempt_at` checkpoints. Additive run
  metadata contains only reviewed run identity, event, branch and repository.
- `performance_findings.py`: bounded, retryable daily Benchmark/UsageBench JSON
  intake and durable benchmark observations consumed by the existing triage lane.
  Benchmark evidence never authorizes a merge or changes a CI gate.
- `merge_failover.py`: consecutive Bedrock 503 detection and durable native Move
  to DeepSeek Flash, retaining the same session, container and execution evidence.
- `merge_heartbeat.py`: threaded supervisor activity reports after five minutes
  without merger prose, then every five minutes until prose resumes. Counts
  distinct tool calls and lists pending/running calls with their ages and
  redacted command/parameter previews, formatted as bounded code blocks. Only
  active calls retain previews; outputs, prompts and credentials stay private.
  `secret_filter.py` shares the web diagnostic scrub without importing web code.
  State and attempts persist across restarts, failed sends retain counts, and
  paginated catch-up must complete before reporting. Uses the existing merger lock and
  transcript observation, with bounded reads. Reports only building/fixing
  sessions before handoff; reporting never changes scheduling or verdicts.
  Historical pending/running statuses do not prove current activity. Reconcile
  call creation positions against durable turn endings, interruption/clear/restart
  markers and the live session activity before reporting. Keep counts/history,
  retire only the reporting entry, and report surviving native background tasks
  separately. Unknown/disconnected activity is unavailable, not confirmed running
  or idle. No age cutoff, cancellation, or session-idleness gate participates.
- `merge_updates.py`: threaded reports from accepted `automerge_skill_events`:
  candidate checkpoints/withdrawals, exact-head rejections/removals, local test
  assessments, integration publication and validated handoffs. A separate durable
  cursor advances after accepted Slack delivery; attempts retain retry backoff.
  Installation checkpoints existing history once, rather than replaying it.
  Only reviewed fields and scrubbed, bounded summaries are exposed; evidence,
  notes, prompts and raw tool payloads remain private. Maintenance retries include
  completed batches so a fast landing cannot lose a handoff report. Notifications
  never mutate a batch revision, scheduling, validation or landing gates.
- `stabilize_state.py`: the shared campaign contract (tables and read API) used
  by the merger, fixer and triage. `stabilize.py`: the campaign engine, cron
  entry point and in-process API for the web UI.
- `skills/mm-*`: agent tools for merge mechanics, state, publication, and
  two-commit checks. `scripts/install-mm-skills.py` installs symlinks and the
  user service. Tool invocation instructions remain in each `SKILL.md`.
- `scripts/apply-mergemarshall-ruleset.sh`: administrator-only normal-policy
  setup. It is never called by cron. Its dry-run and confirmation behavior
  are covered by `test_automerge.py`.
- `containers/Containerfile.ci-tools`, `scripts/install-ci-go.sh` and
  `scripts/install-ci-shellcheck.sh`: the pinned, checksum-verified Go and
  ShellCheck prerequisites for both CI target images. Preserve
  the base image's user and Rust/harness versions. Per-session installations do
  not repair future launches; operator deployment procedures are in README.
- `mm_web/` and `web/`: the dashboard service (`mm-web.service`, FastAPI in the
  uv environment from `pyproject.toml`) and its React UI. Cron entry points and
  `mm_service.py` stay stdlib-only and must not import `mm_web`.

# Web dashboard contracts

`/api/v1` is the single interface for the UI and for agents; anything the UI
shows has a JSON route, and `web/openapi.json` is the committed contract
(`test_mm_web` asserts it equals `app.openapi()`; regenerate it with
`uv run python -m mm_web.export_openapi`, then `npm run api:generate` in `web/`).
Every request reads SQLite through a `mode=ro`, `query_only` connection. The
web process never runs `connect_db`, migrations or other shared-state writes;
reuse existing read helpers (`mm_service.state`, `automerge.active_batch`,
`supervisor.queue_groups`) only after checking that they are pure reads.

Exposure is allowlisted. Curated models declare every field and project nested
payloads; never pass dicts through wholesale. `/sql` and `/schema` expose only
reviewed columns in `mm_web/allowlist.py`; other columns read as NULL. Stored
prompts, briefs, agent reports, recovery and instruction text can embed batch
connection tokens and are never allowlisted. Add a new column there only after
checking its contents. Short free-text fields pass through the token scrub.

Authorization follows GitHub repository permission, looked up with the App
installation token and cached ten minutes per login: write views; maintain or
admin starts or aborts a stabilize campaign. Browsers use the App's OAuth flow
and a signed cookie holding only the login; agents send a GitHub user token or a
view-only host-minted `mmw_` service token. Cookie-authenticated POSTs require
JSON and a matching Origin.

The release form calls `stabilize.py` in process through `mm_web/releases.py`
only; the web keeps no release state of its own and enforces permission before
calling it. `requested_by` is always the authenticated login.

In `web/`, `npm run dev:mock` serves the UI on localhost:5173 from typed
in-memory fixtures (no backend); `npm run dev` proxies `/api` and `/auth` to
localhost:8770. Fixtures are typed against the generated schema, so update them
with the snapshot. Every status has a fixed icon and label; chart colors are
separate from status colors and were checked with the dataviz palette validator.

`entrypoint_heartbeats` records started/finished/skipped for the `monitor`,
`merger`, `triage` and `stabilize` cron ticks through `monitor.record_heartbeat`, which is
best-effort, never raises, stores only exception class names or exit codes, and
is not written by operator commands such as `--check`.

Overview Needs attention is a read-only projection of the shared ledger, latest
fixer outcomes and active repairs, plus failed outbox deliveries, cron heartbeat
health, recovering sessions and blocked PR dependencies. Its two-hour unhandled
failure and 48-hour unconfirmed-fix filters are display heuristics, not proof of
issue closure or recurrence. Missing CI confirmation never authorizes reopening
a ticket; triage verifies a new failure's cause before reusing/reopening it.
For completed master runs, explicit passing steps also retire matching ledger
failures when their job was later cancelled. Missing/interrupted test output
does not prove recovery. Campaign qualification retains its complete-job boundary.

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
installed flags. Message request IDs must fit mj's 1-64 ASCII identifier contract;
the shared client validates this before submission. Correction IDs retain their
stable identity through retries and fit within that limit. Typed `/clear` and the ordered restart brief continue to use
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
Reviewed supersession records in `pr_dependencies.py` can replace a closed,
unmerged preparation at one exact head with one ready, open consolidated head
that contains it. The adapter refreshes both PRs and authenticates rejection
markers and outbox state; unavailable reads defer instead of authorizing an
alias. Only incorporation edges proven inside that audited expansion disappear.
External prerequisites, ordinary cycles, rejected ancestors and normal review,
tests and final exact-head landing gates remain authoritative. Absorption (below)
now handles most closed consolidations without a record. A changed
replacement head needs a new reviewed source record. Once the retired head is
actually in master, ordinary landed ancestry applies without supersession.
Within one dependency discovery, authenticate each exact reviewed source record
once and share the completed rejection proof across its descendant edges. The
cache is local to that discovery; the next discovery rereads mutable metadata
and comments. Failed/unavailable reads never populate it, and final landing
checks retain their own fresh discovery.

Each production comparison is committed immediately, including negative results
and pinned speculative views; exceptions and incomplete fetches are never facts.
Discovery reuses those pairs for unchanged heads and checks new historical heads
against existing dependents. Batch rechecks discover only their source closure;
priority checks discover only the priority closure. Retain hidden inherited and
rejected-head checks. Production ancestry uses a separate bare Git object cache
under the merger state directory, with full commit history and filtered blobs.
Missing history starts a fetch worker and leaves the action pending; unavailable
historical objects use GitHub's comparison API. Reject shallow history. Workers
receive App authentication only in their environment and write sanitized receipts.
Operator `--check` remains read-only and does not warm or write this cache.
All commit-derived reads, including skill-service verification, use that cache.
Fetch branch and PR refs together with full history and filtered blobs; fetch
detached historical tips in batches only when still missing. This bare cache uses
`fetch`, never `pull`, and never changes agent checkouts. Fetch workers maintain
Git commit-graphs so repeated immutable ancestry comparisons stay inexpensive.
Queue ancestry reads reuse a complete `rev-list` walk per immutable descendant
within a cache client, keeping at most four walks in memory. Positive object
presence is remembered by type; missing objects are never remembered. Failed,
interrupted, malformed or incomplete walks produce no ancestry facts. Individual
positive and negative pair results retain their existing durable checkpoints;
mutable ref reads and every dependency/rejection/landing gate remain fresh.
Read current master and published batch heads freshly with `ls-remote`, including
final landing gates.
Current-ref failures expose only sanitized categories (authentication, repository
access, transport, rate limit, missing branch or invalid/unknown response), never
Git stderr. Explicit authentication failure permits one renewal through the same
App-token provider and a fresh probe within the original read budget. Other
failures are not retried inline and never supply a stale head or negative fact.
Current-ref reads capture redacted curl headers privately, with response bodies
disabled, because Git can hide an HTTP 401 behind a credential-prompt error.
Only received numeric status lines participate in classification; trace text
never leaves the subprocess error handler or enters logs, receipts or state.
Failed current-ref reads retain only the numeric process exit and recognized
curl/HTTP error codes alongside their category. RPC/HTTP2 failures are transport
errors; these diagnostics never authorize authentication renewal or inline retry.
Cache immutable tree identities, behind counts, merge-base changed paths, and
base-pinned classifier results across polls/batches. Changed-path reads include
rename endpoints without loading file blobs; retrieve only the pinned classifier
and its literal relative module imports when needed, all from the same base.
Missing imports retain the judgment policy and never cache a verdict. A cache
request lock deduplicates fetch launches across cron
and service clients. Missing objects or incomplete reads are never cached as
negative facts. Authentication/network fetch failures defer without REST fanout;
only confirmed unavailable historical tips permit the API fallback. Legacy
in-flight fetch requests/receipts remain readable. PR/issue metadata, CI state,
labels, comments and GitHub writes remain API operations.
The skill service marks unfinished cache reads as retryable HTTP 503 responses;
its client retries the identical payload within the original command timeout.
Ordinary transport/service errors and revision conflicts are not auto-retried.

`automerge_supervisor_actions` records intents bound to current batch inputs.
Recompute actions after each transition and obsolete changed inputs; batch phases,
readiness, and the existing landing gates remain authoritative. A priority closure
read earlier in a tick is discarded when any batch reaches a terminal state, and
priority is checked again before successor reconciliation or primary selection
uses it; a landing must never re-batch its own merged sources. Recovery and
pending guidance precede priority checking; no priority fences, aborts or resets
a batch. Advance the immediate lane, then the primary, then its successor, then
the campaign lane. Promote and deliver the next instruction in the same tick. Observation uses short slices
within the poll budget, with no agent work deadline or idleness gate. Deferred
reads retain their action without an error alert; other independent actions may
proceed. Repeated observations are new polls of the same action, not repeated
message writes. Maintenance and notification retries follow batch advancement.
Priority dependency discovery checkpoints informational notices even while an
existing batch has no candidate; delivery follows advancement through the same
outbox and preserves retry backoff. Failed notice delivery never fences
selection or landing.
When priority discovery returns eligible work, selection reuses that validated
dependency closure within the same tick: immediate roots go to the immediate
slot and high roots to the next free ordinary slot. Unrelated queue discovery
must not delay admitting immediate work.
An empty eligible queue with no active batch posts a categorized open-PR summary
in the last primary thread (or a new idle thread); checkpoint accepted replies in
`automerge_queue_summaries` and suppress unchanged snapshots. This needs Slack chat.
The merger agent records intents with `mm-db` and publishes through `mm-autopr`;
it must not make direct `gh` writes. The supervisor retries outbox delivery after
ambiguous or failed responses. Queue selection honors recorded exact-head
rejections and pending drafts before GitHub reflects them. Delivery progress must
not change the agent's batch revision. The integration PR create and exact-head
merge remain synchronous. Fixer tables are
`issue_repairs` and `issue_repair_messages`; triage uses `triage_jobs`,
`triage_observations`, `triage_publications`, and `triage_product_announcements`.
`triage_finding_receipts` checkpoints classification per finding, so independent
product causes in one failed step can publish distinct issues and retry safely.
Historical `invocations`
remain readable. An active legacy invocation blocks new issue repairs until
it is retired. Never replace or discard the live database to fix scheduling.

An accepted fixer `deferred` report can checkpoint `issue_repair_deferrals` when
its complete captured issue requirements, observed source identities and PR head
still match fresh reads. These receipts record hashes only and never claim PR
ownership or prove resolution. While the exact PR head remains open, ready and
unrejected, another run of the same reviewed inputs or retirement/reappearance
of covered ledger rows does not launch another fixer. New source/identity/platform
or local command inputs, changed issue requirements, and changed PR head, target,
draft/rejection/closed/merged state admit a fresh review. A lifecycle review gets
its own durable work key even without a new CI run. Missing/truncated historical
inputs cannot establish coverage; ordinary issue/PR links alone never suppress
work. Retain the original reports, sessions and validation; do not close issues
or alter merge/test gates when recording a deferral.

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
batch membership changes and direct/operator records. Membership changes
checkpoint label intents atomically; removal, completion, abort, and failed
launches queue cleanup. Delivery reconciles against current live membership and
the open head, so delayed cleanup cannot untag a reselected PR. The worker creates
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
Terminal suspension retries checkpoint `suspend_retry_after` before submission,
with exponential backoff capped at fifteen minutes. Completion verification
continues on each poll and clears the checkpoint after confirmed suspension.
This applies only to terminal cleanup; active session lifecycle and scheduling
are unchanged. Native checkpoint refusals for unresolved Git conflicts retain
the original index, merge state, files and environment for a native tool repair.
Triage publication also coordinates with the fixer lock before deciding
whether an issue should be created. A GitHub command failure for one finding
leaves it pending while other independent findings publish and checkpoint.
The cached report stays `publishing` with the error until those writes succeed;
its combined Slack issue announcement waits for the complete report. Preserve
these ownership boundaries when changing selection/publication; do not make independent workers decide the
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
Manual abort deliberately interrupts work; priority never does.
After a verified terminal outcome, checkpoint/suspend through the existing
cleanup path; supervision errors leave sessions available for later polls.
Fixer terminal errors, stops and interruptions persist `issue_repairs.recovery_json`
and recover in the original session/checkout through `agent_recovery.py`, sharing
triage's native cancellation, typed clear and durable-divider controls. Preserve
HEAD, source edits, worktrees, caches and logs; restart with the bounded original
issue prompt and instructions to reconcile saved progress and reuse validation.
`report_after_seq` fences pre-reset reports. Both recovery columns migrate
additively in `connect_db`. Recovery is silent while progressing; quota/input
blocks and failed supervision/recovery alert once per episode without making
Slack delivery a scheduling gate. A valid fixer-result can also precede native
completion of a background-task continuation. While native wait still reports
running, send one durable same-session handoff reminder per canonical report and
recovery boundary through `issue_repair_messages`. Retry ambiguous sends with the
same request ID. This reminder does not accept the report, cancel work, or inspect
session idleness; normal native completion and validation still own cleanup.
A repeated ended-turn result cannot start
another reset. Accepted report corrections are quiet on subsequent polls;
a new completed turn may receive another correction. No session-idle checks,
new environment or elapsed-work deadline participates in recovery.

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

Reported completion or a blocked merger turn without a valid handoff queues a continuation in the same
checkout, retaining the current supervisor instruction and applicable evidence.
Record its Mjolnir turn ID in `automerge_skill_events` atomically with the new
instruction; repeated polls of that outcome cannot replace or resend accepted
guidance. A new ended turn may receive another continuation. Outcomes without
turn IDs are deduplicated by outcome within the session/attempt. Errors, stopped
sessions, quota limits and input requests also produce an actionable channel
alert; provider/input/lifecycle blockers may need operator intervention. CI-wait,
merge and waiting-for-predecessor phases without a valid receipt queue handoff
recovery without inspecting session idleness. Waiting successors re-read the row
after dependency discovery, which may refresh captured metadata and invalidate
their handoff even when all source heads remain unchanged. Recovery retains the
checkout and execution evidence; a metadata refresh alone does not require new
tests. Already-confirmed GitHub landing can still be reconciled.

Every new integration and issue-fixer session uses Opus (`opus`) at high effort
on `bedrock-podman`, with Mjolnir straightforward subagents using DeepSeek Flash
(`deepseek-flash`) at high effort. There are no subtle subagents. This applies
regardless of source count or lane, including speculative successors. Persist
`opus-high-dsf-high` in `automerge_batches.agent_configuration`; rebuild,
exclusion, recovery and promotion retain it. Existing live rows keep their
`single-pr`, `multi-pr`, `flash-none`, `flash-luna` or `flash-max` policy and prompt
delegation guidance. Do not Move or reconfigure active sessions for this change.
After exact-title lookup establishes absence and a new launch is allowed, a
legacy row creating a session also checkpoints `opus-high-dsf-high`. Adoption of
an existing session keeps its persisted configuration. Historical schema defaults
remain `multi-pr` for old rows.
The CI daemon has `[subagents.straightforward]` set to `deepseek-flash`/`high`,
no `[subagents.subtle]`, and `deepseek = true` under `eligible_profiles`.
Straightforward-only delegation requires Mjolnir 2.47.0 or later; 2.46.0
rejects these launches before creating a session.
Launches explicitly override straightforward model/effort; omitting a subtle
override would otherwise inherit the global subtle choice. Triage remains
DeepSeek Flash with `--subagents none`.
Opus merger sessions retain Bedrock failover:
Two distinct consecutive agent messages beginning `API Error: 503` and naming
Bedrock trigger automatic same-session failover to DeepSeek Flash. Normal agent
output or completed tool work resets the count; quoted errors, tool HTTP errors,
retry prompts and repeated observations do not count as another provider failure.
Read to the transcript tail before acting, independently of Slack delivery.
`automerge_provider_failovers` checkpoints the cursor, error identities, native
Move operation and continuation. Profile-only in-place Move retains the target,
mounts, resource allocation, Git directory, logs, caches and recorded delegation.
Confirm Flash configuration before queuing the normal durable continuation;
persist `flash-dsf-high` for retained straightforward Flash/high delegation on
new Opus sessions, or `flash-luna` for legacy Luna delegation (`flash-none` for
an operator Move that disabled subagents). Native Move owns cancellation/recovery;
observe its
read-only journal after a CLI timeout, without idleness or new work deadlines.
Never resubmit an ambiguous Move with no matching journal. Report that uncertainty
or failed native recovery for operator reconciliation. Refuse automatic transfers
that cannot keep the environment in place; private evidence must be preserved
before an operator changes environments. Valid handoffs continue through normal
landing gates. Priority/other batches and maintenance remain independent while
Move runs; continuation and its checkpoint commit atomically. A completed failover
posts one threaded notice, retrying delivery without gating agent progress.
The CI Opus profile enables one-hour prompt caching via
`ENABLE_PROMPT_CACHING_1H = "1"`. The CI Mjolnir configuration allows up to
16 concurrent subagents per session. The primary coordinates the
checkout and branch and owns Git operations, batch mutations, test assessment,
and publication; subagents edit assigned files. Work against the captured base
until the supervisor requests an update; it owns final master/source freshness
checks. Follow the batch skills and revision checks.

Merger agents post a brief plan after reading batch state and concise ordinary
assistant messages at meaningful milestones: source assembly, conflict decisions,
and validation results. During extended command waits, report the current check,
its observed progress or blocker, and the next step. These messages are relayed
to Slack; tool calls and private progress notes alone do not reach it. Keep updates
evidence-based and omit routine tool noise and credentials.

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
Fixer launches recognize only specific pre-creation prompt-size and missing
subagent-choice rejections as retryable, including old `launching` rows that
retained such a rejection. Always perform the exact-title lookup first; other
422 responses and transport failures remain ambiguous.
Merger launch-grace logic is separate from an agent work deadline. Absence
must be established before creation is retried. Preserve the original error
while awaiting discovery.

Fixer prompts must fit both 65,536 Unicode characters and 96 KiB after JSON
encoding, below mj's 128 KiB request cap. `render_prompt` omits general PR
inventory first, then older comments, then shortens the issue body and long
observation diagnoses/evidence with explicit omission/excerpt metadata.
Retain observed identities, tested SHAs, commands and provenance. Target and
rejection evidence take precedence. `bounded_stored_prompt` also handles jobs
saved by older code.
Only recognized pre-creation prompt-validation/request-size API rejections
return to `selected`; unknown failures stay `launching`. Create/write the
local prompt file before recording a launch attempt so filesystem failures
cannot strand an unsubmitted job. These boundaries are covered by
`test_issue_fixer.py`.

# Issue fixer contracts

Select open `buildfailure` issues without someone else's assignment,
`agent-in-progress`, or `Escalated`; exclude the generated aggregate issue.
There is no pending queue snapshot: each poll with a free slot reads GitHub and
SQL-orders current work, rejected owned repair PRs first, then oldest issue number.
`BIFROST_FIXER_CONCURRENCY` overrides `CONFIG_DIR/fixer-concurrency`; absent both,
the limit is one. Read it each tick. Supervise every active repair before admitting
new work, and isolate job and admission errors so one failed repair cannot starve
the others. An issue that fails before its repair row is created is retried on a
later poll; other eligible issues can still fill every free slot this poll.
Active jobs, ambiguous launches and completed jobs awaiting suspension reserve
slots and their issue number, even when the issue's evidence/work key changes.
Slack-only terminal retries do not reserve slots. Lowering the limit lets existing
repairs finish. Selection and admission stay under the existing fixer lock.
One active job addresses only its selected issue and linked observations.

When a submitted master repair has merged but its issue remains open with its
exact trusted MergeMarshall claim, a new completed product investigation naming
that issue may resume the original session. The investigation must have started
after the previous repair completed; a newer repair, human assignment, escalation,
released/replaced claim, or unmerged PR prevents continuation. This also covers
intermittent failures whose ledger observation was retired by a later passing run.
Confirm the preserved session is still suspended; defer if an operator has already
resumed it rather than cancelling their useful work.
The continuation authorizes investigation and verification against current source;
it never reopens a closed issue or establishes that a historical cause remains.
The merged PR ends any earlier rejected-head retry: archive its retry PR/head,
then clear those fields before rendering the continuation. The follow-up keeps
the same branch but may publish a new PR. Completion also upgrades older resumed
jobs using their matching durable follow-up archive and recovery identity;
ordinary rejected-PR retries retain their exact PR/head fence.
Archive the prior completion, prompt and recovery privately in
`issue_repair_followups`, checkpoint the new recovery atomically, and use the
existing cancellation/typed-clear path with its report boundary. Preserve the
session, checkout and evidence. Deduplicate each investigation's handoff, respect
fixer slots, and isolate follow-up read failures from unrelated admission.

The dossier contains the issue, recent comments, its own ledger observations
and diagnosis provenance, an open-PR inventory, and up to three completed
reports for this exact issue. Prior reports project only job/session identity,
original checkout base and target, completion, outcome, PR and a bounded summary;
never copy stored prompts or recovery state. They retain evidence after comments
roll out of the recent window. Reuse matching validation after reconciling
current heads, commands, inputs and scope; newer observations alone do not
invalidate unchanged checks. Reports never establish a current pass, ownership
or issue closure, and do not suppress admission. Prompt compaction drops older
prior reports after comments and before target evidence, with omission counts.
The agent judges PR
relevance from committed Git changes and applicable local test evidence.
Issue/PR links, states and descriptions are metadata, not proof of coverage or
resolution, and do not suppress issue selection. Defer to an existing PR only
after verifying it addresses the selected issue's outstanding failures.
Never add a directive to fix every red test or dump all issues.
It reads Bifrost's AGENTS, claims with `agent-in-progress` and a comment naming
session/branch, requests `brokk-service` assignment if assignable, and accepts
the user-approved label/comment claim when assignment is unavailable.
`mergemarshall[bot]` cannot be assigned. Refresh ownership before claiming
and publishing; a failed claim forbids beginning work. Standing down releases
only its own claim.
After verifying that every cause is already fixed on the current target, the
fixer posts current source and applicable passing-test evidence, rechecks
ownership, closes the issue as completed and releases its own claim. Reporting
`resolved` while leaving the verified ticket open admits redundant sessions as
CI observations change. Prior reports, skipped tests and PR links alone never
authorize closure; pending repairs and other owners remain authoritative.

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
Runner memory/disk exhaustion, including an OOM-killed compiler and exhausted
swap, is infrastructure unless evidence identifies a source, compiler or harness
defect. A missing benchmark report alone is not a product regression. Triage and
fixer prompts share `monitor.RESOURCE_FAILURE_GUIDANCE`; do not create a product
ticket merely to request a larger runner. Preserve runner allocation and the
build/benchmark recipe, comparison thresholds and baseline. Capacity, spending
and IAM changes require separate operator authorization; ordinary CI repair
authority does not supply it. Complete independent diagnosis and report the
precise decision when that boundary blocks recovery.

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
the same batch. Apply the closure at selection, priority admission, rebuild
rechecks, retries, direct landing, and `--land-now`. Changed, draft, rejected,
withdrawn or closed-unmerged prerequisites block descendants while unrelated
work proceeds.
A repair unblocks descendants after they contain the new eligible prerequisite
head. Absorption is the exception for closed work: an open PR drops its edge to a
closed unmerged PR (rejected heads included) when it contains that captured head
and the head has not landed, whether or not it is stacked on the closed PR's
branch. Its batch tests the full tree; `Graph.absorbed` records the edge. A
reviewed supersession record for that exact closed head takes precedence. The
absorbing PR inherits the closed PR's captured and base-branch prerequisites, so
absorbed work never skips them. The supervisor never retargets a stacked PR
before it lands; afterwards the absorbed heads are landed prerequisites and the
normal `promote_dependency` retarget records the merge. Absorbed and ambiguous
parents stay in discovery.
Batch rechecks (`roots`, including the final landing recheck) read absorbed parents
fresh, because a cached closed record can be stale; queue selection keeps the
cached record to bound GitHub reads. Two open PRs at one unlanded head report
`ambiguous shared head`, not a cycle.
Ambiguous branches/shared heads, cycles, and unresolved relationships are
reported through `--check` and App-owned GitHub notices. The supervisor applies
`mergemarshall:dependency-blocked` to ready PRs with ineligible prerequisites
and comments on the blocked PR with links and the needed action. When the
prerequisite's captured head has a recorded rejection, the comment says so and
gives the author routes (close a replaced prerequisite, or push a new prerequisite
head); shared heads ask the authors to close one PR. It reads only recorded state
and never changes eligibility. The existing
inventory's current block reason owns label reconciliation; changed, closed,
draft, or unblocked heads lose the label. Exact-head/reason outbox markers
deduplicate comments after lost replies. Retry these informational writes only
after fresh dependency discovery, retaining backoff without changing batch
revisions or creating Slack alerts. Retire pending legacy dependency Slack
notices without sending them. Successful dependency-notice delivery checkpoints
the reconciled dependency label in cached PR metadata atomically with its outbox
receipt, including labels already absent on GitHub. Preserve other metadata and
Git facts; closed cached PRs must not rearm completed cleanup on every discovery.

Ejection removes dependent descendants with exclusion kind `blocked`; only the
standalone-broken prerequisite receives a rejection. Recheck captured dependency
heads before landing. Global recorded rejections also prevent a candidate from
importing a rejected head outside the batch; its eligible repaired PR must be
included, that ancestry must already be in master, or the rejected PR must be
closed and absorbed: its inventory record is closed unmerged
(`_rejected_pull_absorbed`).

Queue `promote_dependency` intents when all captured prerequisites land, including
combined integration merges. Verify prerequisite ancestry in current master and
the unchanged dependent head before retargeting it to master. Promotion is
independent of readiness and GitHub's indirect-merge bookkeeping. Reconcile
already accepted retargets after lost replies/restarts through the outbox. Never
rewrite author branches, rebase, or require authors to promote/poll their PRs.

Priority labels are case-insensitive. `mergemarshall:high` and
`mergemarshall-priority:high`, plus legacy `mergemarshall-priority`, select the
high tier. `mergemarshall:immediate` and
`mergemarshall-priority:immediate` select immediate. High is an ordering rule
for the ordinary slots: when any high PR is ready at selection, that batch admits
only high PRs and their prerequisite closure, and it waits for current work like
any other batch. Immediate work runs in its own lane (see the lanes paragraph in
the campaign section): the immediate batch admits only immediate PRs and their
prerequisite closure, excludes every live reservation, never has a successor,
and lands through the normal gates. Running batches are never aborted, discarded
or reset for priority; they merge the landed result through promotion or the
normal master update. A labeled PR already reserved by a live batch stays there,
and an immediate PR whose prerequisite is held by another live batch waits for
it. Source PRs from an abort remain eligible. `ci-fix` is ordinary unless labeled explicitly.

A rebuild rescan removes changed or closed sources but never adds arrivals. New
arrivals wait for the speculative successor or the next batch. The expansion
counter remains in the schema for historical rows. A finished passing tree is
not rebuilt just for new arrivals.
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
The checkpoint is a milestone and a launch gate, not a base: once the primary
has one, the supervisor permits one successor, selected with dependency closure
against actual master, read freshly at launch, and excluding every live batch's
reservations. The successor captures that master commit as its own base, exactly
like a primary, so a dependent whose prerequisite is reserved by a live batch
waits for it to land. Each batch has its own captured base; there is no shared
epoch, and an independent landing between launches simply gives the later batch
a newer base. New batches persist current CI_MODE.

A successor's membership contains only its own source PRs, and its tree contains
nothing of the primary. It resolves, validates and independently rejects those
exact heads against its captured base, records local pass with `mm-db ready`,
and waits without publishing; the service and landing gates forbid
publication/landing while a predecessor is live. Nothing the primary does
(ejection, rebuild, candidate withdrawal or replacement, failed tests, abort or
terminal failure) invalidates, resets or restarts the successor. Source changes
and aborts of the successor itself retain the normal policies. Never discard
established rejection intents on abort or failed launch.

When the predecessor ends, landed or not, the successor takes the primary
scheduling role immediately (`role_promoted`), even with validation still
running, retaining the same session, checkout, source revision, evidence and
running work. If master moved, it incorporates current master through promotion
(below); otherwise it continues as primary with no extra step. Permit one
primary and one speculative successor; an unpromoted successor cannot start
another batch, and the promoted batch's next checkpoint can. `mm-db state`
exposes `role`. Passing evidence gates publication, not role promotion.
On upgrade, a live successor still pinned to a predecessor candidate is reset
once, through the existing recovery path, onto actual master; an in-flight
legacy incorporation completes under its recorded promotion.

Slack start headings are `*$project* new batch:`, `*$project* promoted batch:`,
`*$project* speculative batch:`, or `*$project* immediate batch:`. Derive `$project` from the repository part
of `monitor.REPO_NAME` using `monitor.slack_project_prefix()`; never hardcode a
display name. Triage and fixbot thread headings use the same project prefix.
Omit model/harness boilerplate. Speculative headings explain that the batch runs
alongside the current one on the same master and publishes after that batch ends. Batch and predecessor IDs belong in
the first thread reply, alongside the PR list for multi-PR batches, rather than
channel-level messages. Start replies use the existing durable reply retry;
standalone alerts and outcomes attach diagnostic replies on a best-effort basis
without duplicating an accepted root message. PR-list replies, relayed progress, blocked
alerts and terminal summaries retain that distinction until role promotion.
Role promotion starts a new channel-level primary thread. Persist the original
thread as `speculative_thread_ts` and use the new `thread_ts` for subsequent
progress, routine notices and the outcome. `start_notification_role` and the
existing start/reply flags checkpoint this transition and its retries; migration
also moves already-promoted live batches, without announcing completed history.
Preserve transcript cursors and relay identities across the move. If the new
root is unavailable, retain unrelayed progress for delivery after it is accepted;
notification failure does not stop agent work or change its test evidence.

Invalidation (a fresh attempt in the same environment) is never triggered by a
sibling batch or by master moving; those are handled by promotion and the normal
master update. It remains the protocol for the upgrade reset above, for
operator-requested rebuilds from a different base, and for genuine recovery.
When it runs: invalidation fences old mutations immediately. Persist a new attempt generation,
stop queued prompts/turns/background tasks and child sessions. Cancellation
accepts an already-ended turn; cleanup never infers turn
activity or waits for session idleness. Parked children have no live worker:
suspend them directly without queue/turn/task commands or waking them. Existing
task/child cleanup and the clear boundary (or suspension for aborts) own
termination. Then issue typed
`/clear` with a stable command ID. Wait for its durable context divider before
submitting a deterministic brief, also with a stable ID. Lost replies retry the
same command; confirmed failed clears use a new ID. The session, container,
checkout and caches survive. `mm-merge --restart` archives the old tip, pending
source edits and progress note once, then assembles from the replacement base
and original own heads. Preserve Git rerere with automatic staging disabled;
review reused resolutions. Never transplant old merge commits from the
invalidated attempt. No agent-written handoff or cache transfer
is needed. Read final reports only after this attempt's context divider.

After role promotion, and whenever current master differs from the captured
base before landing, the supervisor queues `mm-merge --promote` (or the normal
master update) to merge the fresh master tip into the committed candidate.
Queue promotion immediately even while building or fixing, without waiting for
passing validation or a handoff; deliver through the durable same-session message
path without cancelling ongoing work. Each target tip is checkpointed once.
The base becomes that master commit and the source revision changes, which
invalidates the earlier handoff and withdraws the checkpoint; the agent
registers the post-merge head. The agent resolves conflicts, reruns by the
master-merge narrow default in the validation policy (below), reusing unaffected
results with their original tested SHAs and applicability reasons, and records a
fresh assessment for the resulting HEAD. Promotion always
revalidates by that policy: there is no unchanged-tree shortcut, no
equivalent-head alias and no fresh attempt for a differing master tree. Normal
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

For new merger and fixer sessions, delegate only straightforward work with a
clear method to DeepSeek Flash/high. Keep ambiguous diagnosis, design choices,
subtle correctness decisions, integration and final assessment with the Opus
primary; no subtle task type is available. Use bounded tasks and explicit file
ownership. Existing sessions retain their recorded delegation policy.
The primary combines and assesses the work. Builds are expensive: normally
consolidate changes and validation through the primary. Subagents propose the
smallest useful check and build only when assigned a check that resolves a
specific decision. Concurrent builds are appropriate when independent useful
checks justify their cost and use the existing mbx configuration. Throttling
controls resource contention; avoid duplicate or speculative diagnostic builds.
The supervisor's next-batch lookahead is an independent useful build.

Attribute new failures by inspection, not by building: map each failure to the
source diffs that touch its code path, assertions, goldens or fixtures with blame,
history, focused diffs and tracing the assertion through data/control flow. Merge
order and the first merge touching the relevant files are evidence. Do not build
to decide which source caused a failure (no bisection, merge-chain prefixes, base
plus suspected heads, standalone heads or subtraction rebuilds). The failure path
builds only a base-side run when the baseline rules below require one and
validation of the rebuilt candidate. When in doubt, throw it out: reject the one
head inspection implicates, every head it cannot distinguish between, or the heads
whose diffs reach the failing code. Evidence names the failing tests, candidate
and base results, implicated paths/commits and mechanism, and says when
attribution is by inspection only. A failure that persists after the rebuild is
attributed again against the remaining heads. Rejection comments tell authors to
push an empty commit for a wrong rejection: the new head re-enters the queue and is
retested in a batch, whereas merging master could take the untested direct path.
Record fix/eject conclusions in the progress note.

When validation fails, preserve the failing committed candidate and logs while
diagnosing the available failures. Collect failures from selected checks, avoiding
fail-fast behavior where practical, and continue useful independent checks when
others are blocked. Group failures by likely cause and delegate independent
investigations against the same candidate and captured source heads. Assess every
observed group as reproduced baseline, interaction/mechanical fix, or a
source to reject; never hold a batch for more attribution evidence. This covers
observed failures and specific concerns, not separate testing of every PR or
hypothetical defects. Before changing the integration tree or ending the turn,
consolidate established rejections and applicable fixes, settling attribution by
inspection. Record the combined exact-head exclusion set through mm-db,
refreshing revisions between mutations, then read the remaining membership;
dependent descendants are removed without independently rejecting them. If sources
were excluded, rebuild the recorded remainder once, preserving applicable
fixes/conflict resolutions. Append fixes for retained PRs and validate the result;
reproduced baseline failures alone require no rebuild. Do not rebuild and retest
between individual exclusions from the same diagnosis pass. Build blockers can
hide more failures; record blocked checks and reassess after the combined rebuild.

After exclusions, fixes, conflict resolutions, or base updates, choose reruns from
the actual diff against the last tested candidate. After ejecting sources from a
fully tested candidate, the default is a narrow rerun: rebuild the remainder,
rerun the checks that failed and the tests of the crates the ejected diffs touched,
reuse every other result from the tested candidate with its original tested SHA
and an applicability reason, record the fresh assessment for the rebuilt HEAD,
and publish. An ejection's diff is bounded by construction (it is the ejected
heads' changes, reversed), so an ejection alone never justifies the full suite;
expand only for a specific concern, and state it. Interactions that the rebuild
would have exposed are covered by the build and the touched-crate tests; this is
an accepted trade for queue latency. After merging master into a tested candidate
(promotion or a master update), the default is also a narrow rerun: rebuild,
rerun the tests of the crates the incoming master diff touches (old captured base
to new base), the areas of any conflict resolutions, and checks that failed on
the last tested candidate; reuse every other result with its original tested SHA
and an applicability reason, record the fresh assessment, and publish. Master's
changes were validated before they landed, so a master merge alone never
justifies the full suite, and a master diff touching a widely used crate calls
for that crate's tests, not every suite; expand only for a specific concern, and
state it. Checks the candidate has never run follow the normal policy. Cover affected behavior,
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

Classify only failures observed in the candidate's selected checks; passing tests
and ledger failures absent from the candidate need no baseline work. A candidate
failure is baseline without a base run when inspection shows no batch source diff
reaches its code path, assertions, goldens or fixtures, and existing evidence ties
the test to master: an open ledger entry in the prompt, a baseline finding at the
base or an ancestor, or an open `buildfailure` issue for that test confirmed with
`mm-db issue`. The baseline summary names that evidence. Run failing tests at the
exact base only when a batch diff reaches the failing code or no such evidence
exists; that run covers only the base side through `mm-db run` and reuses the
candidate's recorded results (no candidate rerun or mm-compare). Test committed
trees without temporary source edits or validation shims. Baseline build failures
may block dependent checks; report those as blocked and run unaffected useful
checks. Baseline summaries list candidate failures that are baseline by a base run
or existing master evidence; repaired tests belong in fix notes. Register baseline
findings only for failures reproduced at the base and not already tracked by an
open issue or ledger entry. The final assessment describes the resulting
committed candidate. When selected checks pass or only baseline failures remain,
proceed to the mode's publication/reporting step. No local test command registry
is implemented.

Run selected local builds/tests through `mm-db run -- COMMAND...` or
`mm-db run --script FILE`; configured `mm-compare` records both sides automatically.
When the tested checkout has `attest.toml`, the wrapper invokes standalone
`attest exec -- COMMAND` in shadow mode. Attest owns child-scoped runner
interception, adapter registration, native argument parsing and per-test results;
Merge Marshall only supplies authenticated storage and retains compact summaries
in the command receipt. Nextest is the first adapter, not a language boundary.
Native calls inside scripts are captured without agent suite selection. Actual
flags/profile/environment remain authoritative. Unsupported calls or failed
preflight run natively; report failures after execution never rerun tests or
replace the native exit status. Multiple calls retain bounded `attest_runs` and
capture diagnostics; legacy singular `attest` summaries remain readable. Suite
labels do not establish equivalence: Attest compares source, test identity and
adapter-defined build variants and declared runtime test inputs. Its single
compatibility policy records platform and tool versions as diagnostics;
environment-certification jobs use native runners. Format 3 adds required runtime
inputs gathered from project-wide env_inputs declarations. Native capture preserves
actual inherited values, including script overrides, without imposing suite defaults.
Formats 1/2 remain readable but cannot supply passes without recorded runtime inputs.
Consumers accept summary formats 1/2/3, request CLI 0.2.1, retain
summaries and leave compatibility decisions to Attest. A missing declared tool
prevents that run from producing reusable passes, while it may still consume
compatible evidence. Tool versions remain diagnostic. Keep explicit S3 storage;
the newer default filesystem store does not replace the configured proxy.
For Bifrost integration validation, `mm-db attest SUITE --filter EXPR --
-p CRATE --lib` (or `--test BINARY`) delegates to that checkout's
`scripts/merge-nextest.py` when available. Bifrost owns recipe construction and
selection validation; the monitor only forwards arguments through normal
execution evidence and shadow capture. This shares CI features/profile/filter
while retaining local Cargo settings and narrow package/binary selection.
Older checkouts retain the original configured-suite path without selectors;
use an explicit matching native command there to avoid CI-only build settings.
Keep push/contract checks featureless, Python limited to policy integration,
and release-tooling on its existing semantic-pack checks. This guidance is for
merger-to-CI reuse on matching contents, not developer-wide recipe enforcement.
Prompt and execution-skill guidance must carry Bifrost's ad hoc test-only
`BIFROST_OPEN_PACKS_DISABLE="${BIFROST_OPEN_PACKS_DISABLE-1}"` default, preserving
explicit zero/empty overrides. Scope it to test invocations, including tests
inside scripts; never apply it to session-wide build/acquisition/benchmark
environments or rewrite the inputs of existing evidence. Attest continues to
compare the actual effective inputs rather than imposing suite defaults.
Capture removes only its hook directory from the effective child PATH, retaining
script-added toolchains and deliberate PATH removals during native execution.
Automatic capture never skips tests, changes assessments or authorizes a merge.
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
head removed/rejected; a baseline needs a local base-side run or existing master
evidence under the baseline rules above, never a candidate rerun.
The supervisor dispatches no baseline runs and reads one CI result: the
integration PR's `quick policy` job (`QUICK_POLICY_CHECK`) at the tested head.
It holds the merge while that job is pending and lands only after success;
any other conclusion, including skipped or cancelled, returns the batch to its
live session with the job URL and failed step names. That job holds Bifrost's
open-core classification and projection dry run, which the release gate
repeats on master (bifrost-dev#6083). Docs batches keep the gate. The direct
path has no integration PR and source PRs get no CI, so it is not gated.
There is no sync-style CI-round limit. Master CI after landing is handled by triage
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
shared persisted five-minute upkeep. `FAILURE_LEDGER_WORKFLOWS` tracks push `CI`,
`Release gate`, `Nightly CI` and `Hourly CI`. Release gate replaced the old hourly
release lane in Bifrost #5409; #5428 later restored Hourly CI as a Windows/x86
regression detector. Include its failures in ordinary triage while keeping release,
campaign and legacy run-wide polling gates unchanged. Retain historical Hourly CI
rows under their original workflow identity; neither the rename nor detector
restoration proves recovery.
Process each completed master run once, ignore cancelled runs, fetch logs only
for failed jobs, and use failed-step
metadata if logs are unavailable. First backfill is at most five completed
runs per workflow in the last 24 hours. A later passing job closes its rows.
Within a red job, retire an absent identity only when its recorded failed
steps passed, or completed test results from those steps establish its absence.
`FailureReport.successful_steps` carries per-step passes; `incomplete_jobs`
marks runner loss/acquisition (including a RunsOn Spot interruption message in a
failed step's log), unfinished/timed-out steps, or unavailable logs.
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

Benchmark intake runs in the existing triage poll after active-session supervision,
once per five minutes, at most one new run per source. Its explicit sources are
`BrokkAi/bifrost-dev` master `benchmark.yml` and `BrokkAi/usagebench` main
`benchmark.yml`, for scheduled/manual completed runs in the last three days.
Validate same-repository origin, workflow path, branch, attempt and artifact
provenance; read bounded JSON ZIP members without extracting or executing them.
The shared `monitor.run_gh(binary=True)` retains App authentication and renewal
while preserving archive bytes and excluding binary bodies from errors.
`performance_runs` checkpoints intake/errors; failed imports retry independently,
and unseen runs may advance past a bad import.
An authenticated Bifrost artifact that lacks its report, comparison or provenance
records missing measurement coverage, including failed workflow steps, rather
than retrying a parsing error forever. Ambiguous members, malformed complete
reports and provenance mismatches remain invalid evidence. Recheck the attempt
before recording partial-artifact coverage; it never proves a regression or pass.
`performance_findings` retains scenario/case identity, baseline/configuration context, original and latest run,
harness and actual engine revisions, compact measurements and triage ownership.
Repeated same-head observations share an investigation. Explicit comparable
passes retire only their cases; missing/partial output and baseline changes do
not prove recovery. A recurrence after recovery starts a new durable episode.
Triage resolution records that an investigation needs no additional product repair;
it does not mark a benchmark measurement as passing. In particular, private-engine
recovery does not retire a still-failing public UsageBench observation. Keep its
classification deduplicated for the same tested engine and episode; a changed
engine or measured recovery followed by recurrence gets fresh review.

Reuse the benchmark's comparison flags/thresholds and preserve environment-variance
context for agent review. No measured result is distinct coverage evidence, never
an invented slowdown. UsageBench expected failures/unsupported cases are not new
regressions. It tests the public engine; never use the harness SHA as its tested
Bifrost SHA or assume a public failure exists in private master. Performance
product tickets use `performance-regression`, separate from fixer `buildfailure`
admission. Actual compiler/product defects found in coverage investigations retain
the normal buildfailure route. The optional issue repository is allowlisted:
Bifrost defects use the normal repository, and only UsageBench observations may
publish harness/ground-truth defects to `BrokkAi/usagebench`. Durable publications
pin both repository and issue number through an additive migration; retries cannot
retarget them. Closed UsageBench issues require current main-head verification.
Existing CI/local/campaign observations keep their original shapes and behavior.

Triage groups up to 40 new observations by captured workflow/job/identity,
failed steps and commit, not repeated runs of the same observation. Its
2-CPU/4-GiB session reads logs/source/history but does not build or fix code.
Validate final JSON, deduplicate causes against open/closed issues, reopen
matching issues, and persist publication markers to recover lost replies.
One master CI step-level observation may support several product findings, each with its
own cause and ticket; failure IDs remain unique within each finding. Test, local
and performance observations and campaign branches retain one finding each. Shared steps cannot mix
product with resolved/infrastructure outcomes. A linked escalation from one
cause does not override the other findings' reviewed ticket choices. Publication
receipts and announcements use finding indices, not the step's single legacy
summary link, and ordinary issue selection admits each unclaimed product ticket.
Each published investigation announces its product issues in one Slack thread,
with actual ticket titles and links for created, updated or reopened issues.
List each ticket once and split long lists into replies within Slack's message
limit. Omit investigation/session IDs from these headings and omit resolved or
superseded findings without a published ticket. Persist the announcement in
`triage_product_announcements` atomically with job completion, caching its text,
accepted-message count and root thread. Delivery retries do not repeat GitHub
publication, occupy the investigation slot or block cleanup. Retry only remaining
messages; an accepted send whose reply is lost can still be duplicated. Upgrade
does not announce completed history. Infrastructure uses its separate incident
thread, including in reports that also contain product findings.
Triage terminal errors, stops and interruptions persist a recovery in
`triage_jobs.recovery_json` and restart the original observations in the same
session/checkout. Clear queued work, interrupt the turn and stop background
tasks, then use typed `/clear` and its durable `context-cleared:` divider before
the restart brief. Persist stable command IDs before submission and reuse them
after ambiguous replies; only a confirmed failed clear receives a new ID.
`report_after_seq` fences reports from before that divider. No session-idle
test or agent deadline participates. Quota/input outcomes await provider or
operator action instead of clearing work. Routine automatic recovery is silent;
quota/input blocks and failed recovery steps alert Slack once per episode, with
session IDs in thread replies. Slack failure must not stop recovery. Repeated
polls of an accepted report correction are quiet; a newly completed turn can
receive another correction even when its
invalid text is unchanged.
Every finding declares `outcome: product|infrastructure|resolved`. Only product
findings may contain issue drafts. Infrastructure publishes a short top-level
Slack notice and the details in its thread; issue bodies and Slack text are
prepared once and cached in
`triage_jobs.report_json`. Infrastructure findings in one report share a thread;
nearby reports reuse that channel's thread until a 15-minute gap between successful
notices. `triage_infrastructure_threads` persists the window across restarts, while
cached report thread IDs keep publication retries together even after the window.
Each reply includes the finding's job summary. Publication's existing fixer lock serializes
thread selection. Local findings have no CI run URL; render their actual tested
commit, command and batch/session provenance in the diagnostic reply instead.
Before claiming new baseline findings, triage requires ancestry on the recording
batch's current target (master if absent or the campaign branch is missing); unlanded
commits defer while a live candidate can carry them or reads are unavailable,
otherwise retire quietly as `unlanded`, leaving flaky and already-triaged findings unchanged.
Infrastructure checkpoints
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
produce a duplicate notice. Product issue drafts must explicitly include
`existing_number`: a positive issue number for reuse or null for a new issue.
Omission is ambiguous and requests a same-session correction before any issue
write, including for cached reports awaiting publication. Existing issue links
remain investigation metadata; they do not prove that different failures share
a cause. Reopening a closed issue also requires `issue.verified_head` and
diagnosis/evidence establishing that the cause remains at that current master
or affected stabilize Git head. Fresh Git-protocol reads verify the head before
commenting or reopening; missing/stale verification requests correction in the
same investigation without consuming observations. Unavailable Git reads defer
publication. Closure metadata alone never proves recovery, and historical
baseline failures alone never justify reopening a repaired issue. Reconcile
current source/CI facts and reuse existing evidence without repeating builds.
Old in-flight reports lacking outcome must classify
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

# Stabilize campaigns

A campaign cuts `stabilize/<id>` from one master commit so other landings cannot
keep it red. One campaign is live at a time. `stabilize_state.py` is the shared
contract: `stabilize_campaigns`, `stabilize_issues`, `stabilize_gate_merges` and
the campaign-scoped ledger `stabilize_failures`. `stabilize.py` owns every write
to them except `stabilize_state.classify`, which triage calls under its
publication lock. Its internal tables (`stabilize_runtime`, `_dispatches`,
`_runs`, `_issue_identities`, `_fix_commits`, `_messages`, `_writes`,
`_gate_intents`) are private to the engine. With no campaign rows, the merger,
fixer and triage behave as before.

Stages run push (`CI`), hourly and nightly in order. Nothing triggers on the
branch by itself, so the engine dispatches each suite with `workflow_dispatch`.
Stage S passes only when every stage up to S has a completed run with real
successful jobs at one tip. Each landing starts a round at the new tip and
dispatches all stages up to S there. Persist dispatch intent first and discover
runs by branch, event, exact tip, intent time and run-ID floor. Never resubmit
an ambiguous dispatch; an undiscovered one pauses after the ten-minute grace.
Cancelled runs, and runs classified as infrastructure or incomplete, are the
only redispatches (each at most twice per stage and tip; a cancelled run is often a job time limit, so the cancelled cap pauses with that reason). Caps count only dispatches in the
current resume epoch (`stabilize_dispatches.resume_epoch`), so each resume grants
two fresh redispatches and keeps the earlier dispatch rows. Eight rounds in one
stage, or blockers that are only escalated issues, pause the campaign. The
escalated-only pause (`stabilize_runtime.pause_kind`, including legacy rows with
that reason text) keeps refreshing issue states and resumes automatically once an
issue at or before the current stage is `open` or `pr_ready`, with one linked thread
reply; stage work waits for the next tick. Every other pause needs `resume`. A
refused dispatch (HTTP 403; the installation and the host mj `token_permissions`
both need `actions: write`) pauses once with GitHub's status and message, and is
retried only after `resume`, which never replays an ambiguous dispatch.

Branch runs use the master ledger's parser and proof rules but write only
`stabilize_failures`. Incomplete jobs can add identities, never retire them.
Seeds are open triage-linked master rows, excluding infrastructure, whose
`first_seen_sha` is an ancestor of the anchor by the shared Git cache;
unavailable ancestry defers. Seeds keep their master provenance.

Issue states are `open`, `pr_ready`, `landed`, `escalated`, `closed` and
`dropped`. A PR whose current head has a trusted or recorded exact-head
rejection keeps its issue `open`. An `open` or `escalated` campaign issue assigned to anyone
other than the fixer account (`issue_fixer.ASSIGNEE`; `mergemarshall[bot]` is
ignored) gets one durable comment per campaign, issue and assignee set, asking
for a PR against the campaign branch with `Fixes #N` and `CI-Repair-Issue: N`,
or for the assignee to unassign so the fixer can take it. No Slack message.
`Fixes #N` closes issues only on the default
branch, so the engine closes landed campaign issues when the gate merge that
contains their fix lands in master. Dropping needs later-tip proof for every
identity.

The round gate (`merge_gate_open`) opens when the campaign is running and no issue
at or before the current stage is `open`. It does not require a `pr_ready` issue,
so branch PRs with no campaign issue (such as workflow changes) merge through the
normal lane; the "merge gate open" Slack notice still needs a `pr_ready` fix. The
merger checks it at selection only. Campaign batches persist
`automerge_batches.target_ref` (NULL means master) and contain one target. They
run in an independent campaign lane: at most one live campaign batch, alongside
master's primary, its speculative successor and the immediate lane, with no
ranking between lanes. `automerge_batches.lane` (additive; NULL means ordinary)
marks the immediate slot, which holds at most one live batch, admits only
immediate PRs and their prerequisite closure, excludes every live reservation,
and never has a successor; an up-to-date single PR may take its direct path.
`active_batch` is ordinary-master-only; `active_immediate_batch` and
`active_campaign_batch` supply the other slots. Each tick advances the immediate
lane, then the master primary, then its successor, then the campaign lane; a
deferred read, error or stall leaves only that batch's action pending. With one
live row, master keeps the whole 50 s budget as before; with more, each batch
not yet advanced keeps a 10 s reservation (one observation slice plus commands),
reclaimed when unused. Successor guidance and
recovery keep their place ahead of priority discovery. Per-PR comment and review
reads run only for the lane's own source closure after structural partitioning;
historical rejection, ancestry and supersession fences still read what they
need, so an unrelated read failure in one lane cannot block the other's
selection or landing recheck. Selection reservations stay global, so a source
cannot enter two lanes if its base or labels change. The idle queue summary and
`--land-now` are master-only; abort resolves any lane by batch ID, and `--check`
shows every slot. Campaign batches always use async mode
(one up-to-date PR may take the async direct path into the branch; the stage
suites at the new tip are its check) and never have or follow a speculative
successor. Base
capture, freshness, behind counts, verdicts, landing, direct paths, prompts and
skills use the batch target. Campaign agents never merge master into a stabilize
batch. PRs based on a finished or aborted campaign branch are excluded quietly
and listed in `--check` as `campaign_exclusions`.

A passed stage records an immutable snapshot branch at its tip and opens a
ready PR from it to master labeled `mergemarshall:high`; the normal merger lands
it. Only one gate PR is open at a time. Gate snapshots satisfy prerequisites
proven merged into their campaign branch and ancestral to the gate tip; these
are omitted from batch source dependencies so mm-merge does not demand them
again. For a gate source only, a globally rejected ancestor head is acceptable
when a later head of the same PR descends from it, was merged into that
campaign branch, and is an ancestor of the gate tip. Unavailable reads defer.
A gate whose tip is already in master (for example, a stage that passed with no
campaign fixes) needs no PR: it is recorded as merged with no PR number, its
landed issues close, and the stage advances. GitHub refuses PRs with no new
commits, so this check runs before publication and again after PR races.
A rejected gate PR pauses the campaign. Completion is the nightly gate landing
in master; remaining branch PRs are reported, never retargeted.

Abort salvages by default (`abort(..., salvage=True)`, CLI `--no-salvage`, web
checkbox): one extra gate with stage `salvage` at the branch tip read at abort
(else the last observed tip), unless the tip is the anchor or already has a gate.
It reuses `open_gate`, `observe_gates` and `land_gate`, so the merger's gate rules,
the already-in-master shortcut and issue close receipts apply; the merge batch
tests it like any PR although the campaign suites did not all pass. Cron keeps
ticking an aborted campaign only while it has an unfinished gate, including a
stage gate opened before the abort. Gates of an aborted campaign never advance
stages or complete it; a failure that would pause marks its unfinished gates
`abandoned` instead. PRs still based on the aborted branch are not retargeted. Reviewed
supersession records apply to master-targeted sources only: a gate does not
qualify a reviewed replacement that was merged into its campaign branch.

The fixer sorts open campaign issues first (`issue_priorities`), admits only
owned exact-head rejection retries for `pr_ready` issues and nothing for
`landed`. It persists `issue_repairs.target_branch` (NULL means master),
branches from the campaign tip, merges only `origin/<target>`, and opens PRs
with `--base <target>`, `Fixes #N` and `CI-Repair-Issue: N`. A stored target
survives campaign end. Triage reads `stabilize_state.observations` beside the
ledger and local findings; campaign IDs scope fingerprints, while cause matching
spans refs, so a matching master issue is updated or reopened instead of
duplicated. Branch product issues carry `mergemarshall:stabilize`. Issue
evidence and dossiers render `Red since` from first-seen SHA, time and run.

Each campaign posts one Slack root, `<project prefix> stable release branch
started from <linked 7-character anchor>.`, with no branch, campaign ID or full
SHA at channel level. Its first thread reply, checkpointed once, links the
branch and names the campaign ID and requester; the seeded-issue reply follows.
Thread messages show commits as linked 7-character SHAs (`slack_commit`) and
issue/PR numbers as links (`slack_number`). Each stage change (including a
no-PR gate) starts a new channel-level stage thread, `<prefix> stable release
branch from <anchor>: <stage> stage started (<previous> passed at <tip>).`,
checkpointed in `stabilize_stage_threads`; its first reply links the previous
thread. Later replies go to the newest thread, already-queued replies keep their
pinned thread, and `stabilize_campaigns.thread_ts` moves only after Slack accepts
the new root. `get` adds `stage_threads`.
Delivery is durable and never gates the engine. `create` and `resume` run the
campaign's first tick inline under the engine lock (`run_campaign_tick`, shared
with cron): Slack root, seeding and dispatch happen before they return. That
tick is best-effort; failures are left for cron and never fail the call. An
ambiguous ref write still posts the placeholder root. The web UI calls `resolve`, `create`, `get`,
`list_campaigns`, `abort` and `resume` in process. `resolve` is read-only (REST and
SQLite). `create` takes a full SHA, requester and idempotency key, waits up to
120 seconds for the engine lock, and returns `starting` with `start_error` when
branch creation is ambiguous. `resume(campaign_id, requested_by)` clears a
pause. Issue readiness calls the merger's own `_queue_ready` predicate in
dry-run mode, so the round gate and selection agree on drafts, policy, pending
draft intents and rejections. Gate close receipts bind the fix and gate and
are cancelled if the failure recurs after the gate tip. Errors derive from `StabilizeError`: `InvalidRef`,
`CampaignNotFound`, `CampaignAlreadyLive` and `LockTimeout`. Terminal statuses
are `completed` and `aborted`, with `finished_at`.

# Validation

This is a Python repository. Pick the relevant existing modules:
`test_automerge`, `test_merge_retries`, `test_membership_labels`, `test_mm_skills`, `test_issue_fixer`,
`test_repair_dossier`, `test_triage`, `test_stabilize`, and `test_monitor`. Dashboard changes use
`uv run python -m unittest test_mm_web` and `npm run build` in `web/`. Dependency changes also
use `test_pr_dependencies`, with temporary Git histories and mocked external
writes, and `test_git_ancestry` for cache/ref/diff/blob behavior. Never validate
scheduling against the live queue. For Python changes,
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
