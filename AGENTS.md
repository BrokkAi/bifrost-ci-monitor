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
- `mm_service.py`: batch-scoped HTTP interface over shared state; it records
  test assessments/exclusions and reconciles integration-PR publication.
- `skills/mm-*`: agent tools for merge mechanics, state, publication, and
  two-commit checks. `scripts/install-mm-skills.py` installs symlinks and the
  user service. Tool invocation instructions remain in each `SKILL.md`.
- `scripts/apply-mergemarshall-ruleset.sh`: administrator-only normal-policy
  setup. It is never called by cron. Its dry-run and confirmation behavior
  are covered by `test_automerge.py`.

This repository's direct-master workflow does not authorize the supervised
agents to push `bifrost-dev/master`. Those agents publish PRs; only the
supervisor lands their tested heads through GitHub.

# State, authentication, and process ownership

All three entry points share `monitor.DB_PATH` and its additive
`known_failures` ledger. Merger tables include `automerge_batches`,
`automerge_relayed_messages`, `automerge_blocked_notifications`, and
`automerge_skill_events`. Fixer tables are
`issue_repairs` and `issue_repair_messages`; triage uses `triage_jobs`,
`triage_observations`, and `triage_publications`. Historical `invocations`
remain readable. An active legacy invocation blocks new issue repairs until
it is retired. Never replace or discard the live database to fix scheduling.

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
`automerge-rejected-head` comment exactly matching the current head. Stale
labels and another author's PR are not retries; another person's assignment
still blocks selection. The new session uses the existing branch/PR, makes it
draft before pushing, appends corrections and readies it again. Each rejected
head is handled once; a later head can be repaired again. Never interrupt an
active repair just to prioritize another one.

# Automerge contracts

## Selection and membership

Select open, non-draft PRs based on master, excluding integration PRs and
heads rejected by a trusted exact-head marker. `READY_POLICY=non-draft` is
the default; `approved` additionally requires `APPROVED`. A stale rejection
label on a new head is not a permanent exclusion.

Priority labels are case-insensitive. `mergemarshall:high` and
`mergemarshall-priority:high`, plus legacy `mergemarshall-priority`, select the
high tier. `mergemarshall:immediate` and
`mergemarshall-priority:immediate` select immediate. If either tier is ready,
select all eligible high/immediate PRs and no ordinary PRs. `high` waits for
current work. A newly eligible immediate PR preempts an active batch through
the durable abort path, even if the batch is high, unless already included;
never preempt after success is posted or merging starts. Source PRs from an
abort remain eligible. `ci-fix` is ordinary unless labeled explicitly.

On interruption, removals, or rebuild/retest requests, rescan membership.
Allow three expansions per batch; an empty scan consumes none. Priority
batches admit only priority PRs, and removed heads never reenter the same
batch. Commit membership/counter with the follow-up prompt so restarts cannot
reset the limit. A finished passing tree is not rebuilt just for new arrivals.
A source head changed during processing is made draft. Remove changed/closed
sources without rejection and let the author ready the new head.

## Integration and test evidence

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
environment conventions, without rerunning ci-impact. Run chosen checks, then
rerun failures at the exact base to establish baseline evidence. Do not use
temporary source edits, validation shims, or a different tree as proof. A
baseline build failure can block dependent checks; report those as blocked
and run unaffected checks. No local test command registry is implemented.

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
`mergemarshall-batch`, exact source heads, tested head, and conflict/fix notes.
Async publication requires local pass first. Publication retries can reuse
evidence only while committed HEAD, remote head, and clean working tree still
describe the same tested candidate. Look up the PR using the bare head branch
name; reconcile accepted writes whose replies were lost.

## CI modes and landing

Persist `CI_MODE` at batch creation; the Bifrost default is async. Migrated
old batches without a mode remain sync. A later setting edit never changes
an active batch's mode.

Async requires one standalone `automerge-local: pass|fail` line, `Tests run:`
and `Baseline failures:` in the final agent report, with the tested full HEAD.
Only pass proceeds. `Tests run: none` is accepted only for a supervisor-confirmed
docs candidate; baseline/test summaries remain required. Any new failure must be fixed or the exact responsible
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
`automerge-verdict` is advice, not authorization. Choose the newest master CI
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
PR, preserve branch, remove batch-applied rejection labels without rejecting
sources, then release the queue. Direct abort leaves its source PR open.
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
findings may contain issue drafts. Infrastructure publishes one top-level Slack
notice; issue bodies and Slack text are prepared once and cached in
`triage_jobs.report_json`. Infrastructure checkpoints
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
`test_automerge`, `test_merge_retries`, `test_mm_skills`, `test_issue_fixer`,
`test_repair_dossier`, `test_triage`, and `test_monitor`. For Python changes,
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
