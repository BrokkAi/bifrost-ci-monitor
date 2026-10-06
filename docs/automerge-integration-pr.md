# Automerge through an integration pull request

Status: approved and implemented in `automerge.py` and `test_automerge.py`; the
supervisor status and desired master ruleset are documented in
`docs/mergemarshall-ruleset.md`. An administrator must run
`scripts/apply-mergemarshall-ruleset.sh` to apply the ruleset.
Replaces the direct-push design in `automerge.py` (commit e7d784e).

## Goal

Land every ready pull request (PR) through the repository's own CI. The agent
combines the ready PRs, resolves conflicts, runs targeted tests, and opens one
integration PR. The repository's existing CI tests that PR. When CI is red, the
agent fixes the integration branch or removes the PR that broke it, and CI runs
again. When CI is green, the exact tested commit is merged.

What was tested is what lands.

## Facts about bifrost-dev (read 2026-10-05)

- Merge commits are allowed. Squash is allowed. Rebase is off.
- No classic branch protection. One ruleset, "Protect `master`", blocks only
  deletion and force-pushes. No required reviews or checks.
- `ci.yml` runs on `pull_request` and `merge_group`. Its `PR verification` job
  always runs and summarizes the other jobs. A `ci-impact` job chooses which
  jobs run.
- Recent PR CI runs took 2 to 130 minutes. Master CI is red at the time of
  writing.

## Identity

Everything acts as the GitHub App `mergemarshall` (app 5203169, installation
168296327). The supervisor gets tokens with `mj github-token --owner BrokkAi`.
Sessions get tokens from mj, without `statuses: write`; only the supervisor's
token has that permission. Trusted rejection comments are those written by
`mergemarshall[bot]`, a configured value, not looked up at runtime.

## Batch selection

Unchanged from the current job, with one policy setting:

- Open PRs with base `master`, not drafts, except PRs rejected at their current
  head (label `automerge-rejected` plus a trusted
  `automerge-rejected-head: <sha>` comment).
- Policy setting `ready`: `non-draft` (bifrost-dev default, because it does not
  require reviews) or `approved` (review decision APPROVED).
- PRs already in an active integration PR are not selected again.

## Priority lane and operator fast-track

Anyone with write access can apply the `mergemarshall-priority` label. When at
least one otherwise eligible PR has that label, the next selection contains
only eligible priority PRs; every other PR waits. The label is not assigned
automatically to CI monitor `ci-fix` PRs. If the selected priority set is one
up-to-date PR, the existing direct-landing path applies.

On each cron tick, if an eligible priority PR is not already in an active
non-priority batch, the supervisor preempts that batch through the same
persisted abort path as `--abort-batch`, with reason `preempted by priority PR
#N`. No source PR is rejected. It starts the priority selection that tick when
the abort completes, or on the next tick if it cannot finish immediately. A
priority batch is never preempted by another priority PR. A batch whose success
status has been posted or whose merge phase has started is allowed to finish
before priority work starts.

An operator can use `python automerge.py --land-now <PR-number>` to fast-track
one PR. This waits up to two minutes for the cron lock, requires the PR to be
open, non-draft, based on `master`, unrejected at its current head, and up to
date with master (`behind_by == 0`). A behind PR is refused with instructions
to update its branch. The normal CI-workflow-change hold applies unless the
operator explicitly adds `--allow-workflow-changes`. The supervisor records a
direct batch with source `operator`, posts
`mergemarshall/verdict: success` on the exact head with description
`fast-track by operator`, and merges with
`gh pr merge <n> --merge --match-head-commit <sha>`. It does not abort or
otherwise alter a batch already in progress; that batch handles any resulting
master movement through its normal freshness/update path. The operator path
does not wait for CI. Slack identifies operator fast-tracks separately.

## Building the integration branch

One mj session per batch, as now (`--workspace CI --target podman --bundle
bifrost --model deepseek-flash --subagents none`).

1. Start branch `mergemarshall/batch-<id>` at current master.
2. Merge each PR head with a merge commit. Never squash or rebase, at either
   level, so GitHub marks each PR merged when the integration PR lands.
3. Resolve every conflict. A conflict is never a reason to send a PR back.
4. Every commit the agent creates carries `Automerge-Batch: <id>`.
5. Run targeted tests according to the persisted CI mode described below.
6. Push the branch and open or update the integration PR according to the
   selected mode: async does so only after the local gate passes; sync opens
   it after the build so the supervisor can start CI:
   - title `Merge batch: #182 #187 #191`;
   - body lists each PR with the head commit included and the agent's
     conflict-resolution and fix notes;
   - label `mergemarshall-batch`.

## Direct single-PR landing

If selection returns exactly one eligible PR and GitHub compare
`master...<head-sha>` reports `behind_by == 0`, the supervisor creates a durable
batch record of kind `direct` and does not start an Mjolnir session or create an
integration branch. A behind PR or a queue with multiple eligible PRs follows
the ordinary integration-batch path. Direct records capture `CI_MODE` when
created and are resumed from SQLite after a restart.

Before landing, both modes recheck that the source PR is open, non-draft, based
on `master`, still at the selected head, and not rejected at that exact head.
The supervisor also verifies it is still up to date with master and scans the
PR diff for `.github/workflows/` and `.github/actions/` changes. Such changes
remain pending for a person because pull-request CI executes workflow
definitions supplied by the PR.

- In `async`, the direct PR lands as soon as the common source and workflow
  checks pass. The supervisor does not query CI.
- In `sync`, the supervisor waits for `PR verification` from the latest
  `.github/workflows/ci.yml` `pull_request` run whose head SHA is exactly the
  selected PR head. Green CI lands. Red CI lands only when the supervisor's
  existing same-job test-identity and failed-step comparison shows that the
  failing jobs are no worse than the baseline for the captured master tree.
  If a red result is worse, the supervisor itself rejects that source head:
  it posts the trusted `automerge-rejected-head: <full sha>` comment with the
  failing jobs, tests, steps, CI run, baseline, and comparison evidence, then
  applies `automerge-rejected`.

After every gate passes, the supervisor posts `mergemarshall/verdict: success`
on the source PR head and runs
`gh pr merge <n> --merge --match-head-commit <sha>`. The persisted
`direct_merging` phase makes restart recovery safe: if GitHub already merged the
PR, the next tick observes that state and records completion; otherwise it
retries the same exact-head merge without duplicating the status. If master
advances and GitHub refuses the merge, the direct attempt ends without
rejecting the PR; the next cron tick selects it through the normal batch path.

For a direct PR held because it changes CI workflow or action files, a
maintainer reviews the diff and CI evidence, then an authorized operator posts
success on the exact reviewed PR head through the supervisor App status path
and merges with `--match-head-commit`. The automerge job does not post success
automatically for this hold.

## CI modes

The Bifrost module setting `CI_MODE` defaults to `"async"`. When a batch is
created, its selected mode is stored in that batch's `ci_mode` column. The
stored value controls the batch across restarts even if `CI_MODE` changes on a
later cron tick. Existing batches migrated without a mode keep `sync` behavior.

- **`sync`** waits for the integration PR's verified `PR verification` check.
  On red CI, the supervisor selects a run for the exact base tree and compares
  failed jobs, tests, and steps. It resumes the session to fix or remove
  responsible PRs. It lands when green, or when every failure is no worse than
  that baseline. A red master can therefore be handled without blocking a
  batch whose integration failures are all present at the base. Sync batches
  allow at most four CI rounds.
- **`async`** does not wait for or query GitHub CI and has no baseline workflow
  run or CI-round limit. The session runs targeted local tests selected from
  `AGENTS.md`, `ci-impact`, and `.github/workflows`. It reruns any failing test
  at the exact batch base in a separate worktree. Failures reproduced there
  are baseline; any new failure must be fixed or its responsible PR removed
  and rejected at the tested head. The final agent message must include
  `automerge-local: pass|fail`, `Tests run: ...`, and
  `Baseline failures: ...`. Only `pass` can proceed to publication.

If master is already red, async mode uses those local exact-base test results
  to distinguish baseline failures from new ones; it does not wait for master
  CI. After the supervisor's common pre-merge checks pass, it posts
  `mergemarshall/verdict: success` on the locally tested integration head with a
  description such as `async: local targeted tests passed; CI runs after merge`,
  then merges with `--match-head-commit`. The integration PR and master CI run
  normally after merge. The existing CI monitor handles any resulting breakage
  by opening `ci-fix` PRs, which enter later batches. The Slack outcome links
  the integration PR so people can watch its CI.

Both modes use the same pre-merge freshness, source-PR state/head, included and
excluded ancestry, and CI-workflow-change human-review checks. A change under
`.github/workflows/` or `.github/actions/` remains held for a person in either
mode.

## Sync mode: waiting for CI (supervisor, not the agent)

The supervisor accepts `PR verification` only from the GitHub Actions run whose
path is `.github/workflows/ci.yml`, whose head SHA is the tested head, and whose
event is `pull_request`. It follows the latest attempt and matches the check run
to that workflow run's check suite. The agent session is suspended while CI
runs, so no agent time is spent waiting. While CI or a supervisor decision is
pending, the supervisor posts `mergemarshall/verdict: pending` on that exact head.

## Sync mode: when CI is red

1. The supervisor collects the failed jobs and the log tails of failed steps
   (`gh run view --log-failed`) for that head commit.
2. Baseline comes from the most recent CI run that tested the batch's exact
   base tree. If the base is a recorded merge commit from an earlier
   integration batch, use that batch's final integration-PR CI result only
   when GitHub confirms that the base commit and its tested head have the same
   tree. Otherwise use the newest `ci.yml` run on `master` for the exact base
   commit. A pending run keeps the agent suspended while the supervisor waits.
   If the run is missing or cancelled, first use any matching open CI ledger
   identities whose last-seen SHA equals the base or is an ancestor of it.
   The ledger is parser-derived evidence for this comparison. If no such entry
   applies, dispatch `ci.yml` on `master` only while master still points at the
   base SHA. Persist the dispatch intent and a
   10-minute grace deadline before dispatching; on each later tick, first list
   dispatch runs created since that intent and accept only a run whose head SHA
   equals the base SHA. Do not retry while the grace period is active and no
   matching run has appeared. After it expires, reconcile the runs and retry
   only if master still points at the base SHA. If no usable baseline can be
   established, fail closed, notify Slack once, and leave the batch waiting for
   the next tick to re-evaluate. The selected baseline run's
   failed jobs and failed-step logs are supplied to the agent and parsed by the
   supervisor. The supervisor compares failing test identities as well as jobs;
   each job's failing test identities and failed-step names must independently
   be subsets of that same job's baseline failures. A job with no baseline
   failure is always worse. For a failed job with no parseable test identity
   (such as build, lint, or crash failures), its failing step name must still
   match a step that failed in that same baseline job.
3. It resumes the same session and sends the failures with `mj prompt`.
4. The agent either:
   - fixes the interaction with a new commit on the branch, or
   - ejects the PR or PRs responsible. Ejecting means rebuilding the branch
     from its base without them and force-pushing the integration branch (only
     that branch). Never eject with a revert commit: the ejected PR's commits
     would still reach master and GitHub would mark it merged.
5. Each ejected PR is rejected at the exact head that was tested (label plus
   trusted comment with the failing jobs and evidence). A new push re-admits it.
6. Targeted tests again, then push. CI runs again.

Limits: at most 4 CI rounds per sync batch. After that, the batch closes without
landing, the integration PR is closed with a summary, and Slack is notified.

## Operator abort

`python automerge.py --abort-batch <batch-id> [--reason TEXT]` waits up to two
minutes for the cron supervisor's same non-blocking lock before changing the
batch. It interrupts an active Mjolnir turn, suspends that session with
`--acknowledge-unpublished-work`, posts `mergemarshall/verdict: failure` on the
integration PR's current head, and closes the integration PR with the supplied
reason. It does not delete the remote branch. It removes any rejection labels
applied to this batch's source PRs, writes no rejection markers, marks the
batch aborted, and releases the queue. The abort phase is persisted so cron can
finish the operation after a process restart; an aborted Slack outcome is
retried independently if delivery fails.

## Human review for CI workflow changes

The supervisor inspects the integration PR's changed files before landing. If
any path is under `.github/workflows/` or `.github/actions/`, it leaves
`mergemarshall/verdict` pending with `needs human review: CI workflow changes`, sends
one Slack notice, and keeps the batch held. This is necessary because
`pull_request` CI runs the workflow definitions from the PR being tested.

A maintainer has two paths:

- To land the workflow change, review the integration PR's workflow/action diff
  and its CI evidence. An authorized operator then posts
  `mergemarshall/verdict: success` on the exact reviewed head through the supervisor
  App's status-writing path and merges with
  `gh pr merge <n> --merge --match-head-commit <sha>`. The automerge job does
  not post that success automatically.
- To land the other batch PRs first, mark the workflow-changing source PR as a
  draft. The existing source-state gate removes it and asks the batch session
  to rebuild the integration branch without it. After the other PRs land, the
  source PR can be marked ready again for separate human handling. Alternatively,
  its owner can push a follow-up restoring the workflow/action files to their
  master contents; the changed head triggers the normal rebuild, and automatic
  landing resumes once the integration diff no longer changes those paths.

## Sync mode: when CI is green

1. Up to date: if master has moved since the branch's base, the agent merges
   master into the branch (merge commit), runs targeted tests, and pushes, and
   CI runs again. The desired master ruleset also requires an up-to-date branch;
   the supervisor checks freshness before posting success and treats a GitHub
   refusal caused by a master advance as a request to update and re-test.
2. Re-check each included PR: still open, not draft, base master, head
   unchanged. If any changed, it is removed (rebuild, not reject) and CI runs
   again.
3. Once CI and every pre-merge check pass, the supervisor posts
   `mergemarshall/verdict: success` on the integration PR's exact tested head. The
   description is `green` or `not worse than master: N baseline failures`; the
   target links to the Slack thread or integration PR. Success is never posted
   on an untested commit.
4. Merge with `gh pr merge <n> --merge --match-head-commit <tested sha>`. The
   head check makes GitHub refuse the merge if the head moved. If GitHub refuses
   because master advanced, the supervisor changes the tested head's verdict
   back to pending, asks the agent to merge master into the batch branch, and
   waits for fresh CI before posting success again.
5. Confirm every included PR now shows as merged. Comment on any that does not.
6. Slack outcome: landed PRs, ejected PRs with reasons, CI rounds used.

## Interaction with the CI fixer

`ci-fix` PRs are ordinary queue members. While master is red, the next batch is
the route by which a fix lands.

## Review of agent-written code

Conflict resolutions and interaction fixes are written by the agent and land
without human review. bifrost-dev requires no reviews today, so this changes
nothing there. For other repositories, the product default needs a decision:
land as is, or require review when the agent's own changes exceed a size
threshold. Not part of this change.

## Repository rules

The desired ruleset "Protect `master`" requires pull requests (zero approvals), requires
`mergemarshall/verdict` from the GitHub App mergemarshall (app id 5203169), requires
branches to be up to date, and blocks force-push and deletion. It has no bypass
actors. This prevents direct pushes and self-merges by people; source PRs land
through the automerge queue and its tested integration PR. The existing
deletion and force-push blocks remain. The ruleset setup is not automatic; an
administrator applies this desired configuration using the script below.

An administrator can inspect and apply the ruleset with
`bash scripts/apply-mergemarshall-ruleset.sh --dry-run` and then
`bash scripts/apply-mergemarshall-ruleset.sh`. The script shows the complete JSON,
requires explicit confirmation, and creates or updates by ruleset name using
the administrator's own `gh` authentication. It is never called automatically
and is not used by `automerge.py`.

## Sync-mode landing when the base tree is already red

Decision for bifrost-dev: "not worse than the batch base tree", compared test by test.

- A batch lands when every CI failure on the integration PR also fails at the
  branch's base commit. The supervisor compares deterministic failing test
  identities, not only job names, so a new failure inside a job that was
  already red still blocks landing.
- The supervisor hands the agent the failed-step logs for both the
  integration PR and the selected baseline run described above. It does not
  substitute a run from a different base SHA.
- If a failure cannot be shown to be a baseline failure, it counts against
  the batch. Unparseable failed jobs count as worse unless the same job and
  failed step also failed at the baseline.
- The agent may include `automerge-verdict: not-worse` and a list of baseline
  failures as advice for the hand-back. This verdict is not a landing
  condition; the supervisor's job, test, and step comparison decides.
- The Slack outcome lists the baseline failures that the batch landed with.

This not-worse policy trusts test output produced by PR code. A PR could modify
tests or their runner to fake its reported failures. This is accepted for the
current contributor set of Bifrost agents and people; strict green-only mode
does not rely on this test-level comparison and does not have this issue.
Async mode also trusts local targeted-test output produced by PR code, which a
PR could fake; that is accepted for the same current contributor set.

The baseline run selection above is the approved policy for bifrost-dev; the
former open question about missing or pending master CI is resolved.

## Shared known-failures ledger

`monitor.py` and `automerge.py` maintain an additive `known_failures` table in
the shared SQLite database. A primary key is `(workflow, job, identity kind,
identity)`, where identity is either a deterministic failed-test identity
from the automerge log parser or, when no test can be parsed, the failed step
name. The table keeps first/last seen commit, run ID and URL, open/fixed state,
fix commit, related repair PR or escalation issue, and an optional short
diagnosis with its source. Job names omit RunsOn's per-run labels while
retaining real matrix values; the aggregate `PR verification` job is excluded.
Agent `known-failure:` lines can annotate only an
identity the supervisor has already observed; agent text alone never creates
ledger entries.

Both cron jobs call one shared upkeep function, guarded by a persisted
five-minute timestamp so only one job processes runs in that interval. It
reads completed master runs for CI, Hourly CI, and Nightly CI, processes each
run once, skips cancelled runs, and downloads logs only for failed jobs. The
initial backfill is limited to the five newest completed runs per workflow
from the last 24 hours; older history is never traversed. If a failed-job log
is unavailable, failed-step names from that run's job metadata still enter the
ledger and processing continues with other jobs. A failure is fixed when a
later completed run has the same job passing or its
parsed identity absent from that job's failures. Repeated upkeep errors are
logged and reported to Slack once per reason without stopping either main job.
An upkeep pass makes three workflow-run-list calls, one run-details call per
new completed run, and one failed-log call per failed job. It refreshes each
distinct linked PR/issue state once. The generated issue is searched for only
when no issue number is stored; create/edit/pin calls happen only on the first
render or when the rendered body changes.

The monitor repair prompt omits failures already linked to open work and asks
the agent to focus on new failures. Automerge build, test-feedback, rebuild,
and update prompts include up to 40 open entries and the count of the rest.
When a sync base CI run is missing or cancelled, an open CI ledger entry counts
as baseline only if its last-seen commit equals the batch base or GitHub proves
it is an ancestor. These identities have the same trust level as sync
not-worse comparison: they come from the same deterministic parser, not from
the generated issue or agent text.

The `Known CI failures on master` issue is a pinned generated view of the open
table; SQLite is authoritative and the issue is never parsed back. The bot
creates it with the `known-ci-failures` label, rewrites the body only when its
rendered content changes, and continues if pinning is denied. The repository
administrator must create that label before first use.

Strict "green only" remains the intended default for other repositories.

## Reused from the current job

Batch selection and rejection markers, launch identity and ambiguous-launch
handling, transcript relay, Slack notices, locking, restart re-attachment.
Replaced: the agent prompt, full-suite run, and direct push. New: the CI wait
loop, the failure hand-back, and merging the integration PR.
