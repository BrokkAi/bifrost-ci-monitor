# Bifrost CI Auto-fixer

`monitor.py` polls the shared failure ledger and runs one issue-scoped repair at
a time through `issue_fixer.py`. Triage diagnoses failed CI, Hourly CI, and Nightly
CI runs and creates individual `buildfailure` issues. The fixer selects an open,
unassigned issue without `agent-in-progress`; the aggregate known-failures issue
is never a repair target. Issues assigned to another person are left to them.

Each session handles only its selected issue. It reads Bifrost's `AGENTS.md`,
claims the issue with `agent-in-progress`, and posts a MergeMarshall claim comment
containing its session and branch. The requested assignee is `brokk-service`.
The agent checks its assignability and assigns it when repository access permits;
otherwise the user-authorized label/comment claim suffices. GitHub rejects
assigning `mergemarshall[bot]` itself, so it is not used as an assignee.
It refreshes ownership before
claiming and publishing. On standing down without a submitted repair, it removes
only its own claim. A failed label/comment claim does not permit work to begin.

The agent chooses a straightforward production fix or mechanical test update,
otherwise reverts the introducing change. If subsequent work makes the revert
nontrivial, it records the evidence and escalates on the same issue. Unrelated CI
failures are validation limitations, not extra repair tasks. The agent may also bail
out on a particularly tricky issue or irreconcilable requirements: it documents
the blocker, adds `Escalated`, assigns `DavidBakerEffendi`, and releases its own
claim. Escalated issues are never selected automatically. Fixes go through a
`ci-fix` PR, with `Fixes #N`, `CI-Repair-Issue: N` and relevant run trailers. The
agent merges current master before opening/readying the PR, uses draft status
while working, and never merges its own PR or pushes master.

## Rejected repair PRs

Each idle poll reads current GitHub candidates and selects one with SQL ordering:
rejected fixer PRs first, then oldest issue number. There is no stored pending queue.
The scheduler requires a recorded fixer PR association and a trusted merger
comment whose `automerge-rejected-head` exactly matches the current PR SHA. A
stale label or another author's PR does not trigger a retry. Another person's
assignment prevents a retry, too; the existing MergeMarshall claim is allowed
only for its own recorded repair.

The retry receives the same issue, PR, branch, rejected head and rejection
comments. It checks out the existing branch, makes the PR draft before pushing,
appends corrections, merges master, validates and readies the same PR. A changed
head re-enters the merge queue. Each rejected head is handled once; a rejection
of a later head can launch another repair. This priority does not interrupt an
already running repair.

## Dossier

The prompt includes the target issue body, recent comments, only that issue's
linked failure observations and diagnosis provenance, plus a compact inventory
of open PRs targeting master. The agent judges PR relevance. A rejection retry
also includes the merger's rejection evidence. There is no dump of every open
issue or directive to fix every red test.

Bodies are excerpts with explicit truncation flags. The prompt is capped at
65,536 Unicode characters and its JSON-encoded request at 96 KiB, leaving room
within mj's 128 KiB request limit. Both independent limits are checked. If
necessary, general PR inventory entries are omitted first, followed by older
comments; counts tell the agent what to retrieve with `gh`. Target issue and
rejection evidence take precedence.

## Lifecycle and inspection

The `issue_repairs` table in `$HOME/Projects/bifrost-ci/activity.db` stores one
job per issue evidence snapshot or exact rejected PR head, its prompt, branch, session, transcript
cursor and outcome. Multiple issues from the same CI run can have independent
sessions. Historical run-wide `invocations` remain readable; they are no longer
scheduled. A still-active legacy invocation blocks new work until retired.

Repairs use workspace CI, target podman, bundle bifrost, `deepseek-flash`, no
subagents, 32 CPUs and 28 GiB RAM. New jobs branch from current master; rejection
retries use the existing PR branch. The session stays live through the work and
has no wall-clock deadline. Each cron tick observes it without suspending or
restarting the agent. After the final outcome is captured and the submitted PR
is verified, the session is checkpointed and suspended. Supervision failures
are recorded for the next poll, leaving the session intact.

`monitor.py` retains the shared GitHub App authentication, Slack transport,
ledger and CI helpers used by all three components. Every host-side `gh` call
uses the installation token from `mj github-token`; sessions use mj's injected
token. Configuration and data stay on the host; do not commit secrets or DBs.
The pinned `Known CI failures on master` issue is generated from the SQLite
ledger. Completed CI or evidence-backed triage can retire the exact failure observation;
a fixer PR merely links its target issue's observations to proposed work.

With the Slack bot transport, each repair gets a thread and completed agent
messages are relayed with persisted cursors and stable-ID deduplication. The
webhook transport gets engagement and outcome messages. See MORNING-SETUP.md.

Inspect current work:

```sh
sqlite3 ~/Projects/bifrost-ci/activity.db \
  "SELECT issue_number,status,session_id,retry_pr_number,repair_pr_number,last_error
   FROM issue_repairs ORDER BY created_at DESC LIMIT 10;"
```

An ambiguous `mj new` result is reconciled by the job's exact persisted title;
the poller adopts a matching session instead of starting a duplicate. If it
remains `launching`, inspect workspace CI before resetting that job to `selected`.
Never retry creation while the original request may still be provisioning.
Explicit prompt-validation or request-size rejections return the job to
`selected` for retry; they do not wait for session discovery. The original error
is retained while an ambiguous launch is pending. Selected jobs saved by an older
scheduler are compacted to the current prompt limits before launch.

## Agent selection

The CI monitor sessions use `--model deepseek-flash --subagents none` and the
label DeepSeek Flash (mj). Automerge uses `deepseek-flash` too, set separately, with
`--subagents none` and the label DeepSeek Flash (mj). The executables have
absolute defaults: `mj` at `$HOME/.cargo/bin/mj` and `gh` at `/usr/bin/gh`.
Override them with `BIFROST_MJ_BIN` and `BIFROST_GH_BIN`. Both scripts check
that the required executables exist and are executable at startup, then post a
once-per-reason blocked notice if one is missing. `BIFROST_CI_MONITOR_STATE`,
`BIFROST_CI_AUTOMERGE_STATE`, and `BIFROST_CI_CONFIG_DIR` override the state
and secrets directories, which otherwise live under the current user's home.

The installed mj must support transcript --finished-only. A transcript error
is recorded on the active repair for a later retry. Upgrade Mjolnir from
~/Projects/mjolnir before enabling repair launches.

Runtime state and secrets stay local. Do not commit the Slack webhook, bot
token, SQLite database, cron output, or session data.

## Slack setup with the Slack CLI

The Slack CLI is used to create and install the app. Incoming webhooks are
channel-bound, so the final channel authorization is performed in Slack app
settings. Slack CLI has no command that creates or returns a channel-bound
webhook URL; Slack's supported flow is the Incoming Webhooks channel picker,
or a custom OAuth flow whose response contains incoming_webhook.url.

1. Authenticate the CLI if needed:

       slack auth login --team brokkworkspace

2. Request a Slack service token:

       slack auth token --team brokkworkspace

   Run the displayed /slackauthticket ... command in Slack, approve the
   modal, and enter the resulting challenge code back into the CLI. Keep the
   resulting service token private.

3. Create the app from the checked-in manifest:

       export SLACK_SERVICE_TOKEN='paste-the-service-token-locally'
       manifest=$(python3 -c 'import json; print(json.dumps(open("slack/manifest.json").read()))')
       slack api apps.manifest.create --token "$SLACK_SERVICE_TOKEN" --json "{\"team_id\":\"T08PB1S0VL2\",\"manifest\":$manifest}"

   The created app is A0BPC1HK4M6. Never commit the service token.

4. Install the app into brokkworkspace:

       slack app install --team T08PB1S0VL2 --app A0BPC1HK4M6 --token "$SLACK_SERVICE_TOKEN"

5. Open settings and add the webhook:

       slack app settings --app A0BPC1HK4M6

   In the app settings, choose Incoming Webhooks, add a webhook to
   #github-brokk-desktop, and copy the generated URL. The URL is a secret
   and Slack may revoke it if it is exposed.

6. Store and test the webhook without putting it in Git:

       ./monitor.py --configure-slack
       ./monitor.py --test-slack

The first command hides the URL while reading it and writes a mode-0600
file under `$HOME/.config/bifrost-ci-monitor/` by default.

## PR automerge

Automerge agents have four scripted skills, maintained in this repository:
`mm-merge` attempts an octopus merge of verified source heads, then supports
manual sequential merges when it fails; `mm-db` reads shared batch state and
records exact-head exclusions and explicit local test assessments; `mm-autopr`
pushes the tested branch and reconciles one integration PR through REST;
`mm-compare` runs a supplied Bash check at two commits in detached worktrees and
returns raw output diffs and exit codes. Check selection and diagnosis remain
the agent's responsibility. The CI command registry is deferred.

Install them into enabled Mjolnir profile homes and start the batch state service:

```sh
python3 scripts/install-mm-skills.py --service-listen <host-private-IP>
```

Mjolnir stages those skills into container sessions. The user systemd service
`mm-skills.service` listens on port 8769 at the specified private interface;
rootless Podman agents reach it through `host.containers.internal`. The supervisor
supplies a token scoped to that batch in initial and follow-up prompts. Its key
is generated mode 0600 under the automerge state directory. Agent updates check
a state revision; finished batches refuse mutations. The additive
`automerge_skill_events` table stores assessments, exclusions, and publication
evidence. Exclusions update the existing supervisor membership immediately;
publication does not merge or bypass the common landing checks.

Agents, including the CI repair agent, open pull requests instead of pushing
to master. `automerge.py` batches eligible PRs into one integration PR and
merges after the selected CI mode's gate and common pre-merge checks pass. Run
it from cron every minute. It has a separate non-blocking lock and persists its
phase, mode, integration PR number, and Mjolnir session in its own tables in
`$HOME/Projects/bifrost-ci/activity.db` by default (`BIFROST_CI_DB` overrides
the database path). After a restart it reattaches during
building, CI wait, repair, and merge phases.

`CI_MODE` is a module setting with Bifrost's default set to `"async"`; set it
to `"sync"` for CI-gated batches. Each batch stores its mode when created and
keeps it across restarts and later setting changes. Existing batches migrated
without a mode remain `sync`.

The default queue includes every open, non-draft PR based on `master`, except
one rejected at its current head. The optional `READY_POLICY="approved"`
setting also requires an approved review. Integration PRs are excluded from
the source queue. If any eligible PR has `mergemarshall:high` or
`mergemarshall:immediate`, the next selection contains only eligible high and
immediate PRs; all others wait. The `mergemarshall-priority:` prefix is also
accepted for both tiers, and the legacy `mergemarshall-priority` label counts
as high. `ci-fix` PRs have no automatic priority. A single up-to-date high or
immediate PR uses the direct-landing path.

`high` waits for the current batch to finish. `immediate` aborts an active
batch before publication, even if that batch is already high priority, then
selects all currently eligible high and immediate PRs. The aborted batch's
source PRs remain eligible. A batch that has entered the merge phase or posted
success finishes first.

When an agent turn is interrupted, a source PR is removed, or a retry requires
rebuilding/retesting, the supervisor rescans for newly ready PRs. Each batch can
expand **three times**; scans with no additions do not consume an expansion.
After that its membership can only shrink until it lands. Priority batches
only admit new priority PRs, and previously removed PRs cannot reenter that
same batch. The counter and updated membership are committed with the retry
prompt, so restarting the poller does not reset the limit. `--check` shows the
counter. A completed, passing tree is not rebuilt just to collect new arrivals.

If a selected source PR's head changes while the batch runs, the supervisor
marks it **draft**. The author must mark it ready again when finished. The
agent's current turn continues; removal and any expansion take effect at the
next attempt. Changed heads are also drafted on the single-PR direct path.
An interrupted agent turn continues in the same live session with the refreshed
source list; it is not suspended and restored between attempts.

When exactly one PR is eligible and GitHub compare reports
`behind_by == 0` against current `master`, the supervisor records a `direct`
attempt and lands that PR without an Mjolnir session or integration branch.
If it is behind, or more than one PR is eligible, the normal batch path runs.

Direct attempts repeat the source state, head, and rejection-marker gates before
merging. Async mode lands immediately after those checks.
Sync mode waits for `PR verification` from the exact `.github/workflows/ci.yml`
`pull_request` run at that head; green lands, and red lands only when the
supervisor's same-job test and step comparison proves it is no worse than the
captured master baseline. A worse red result is rejected by the supervisor at
that exact head with `automerge-rejected-head: <full sha>`, evidence, and the
`automerge-rejected` label. If master advances before the merge, the direct
attempt ends without rejecting the PR;
the next tick sends it through the normal batch path.

An operator can fast-track one PR with
`python automerge.py --land-now <PR-number>`. It waits up to two minutes for
the cron lock, requires an open, non-draft PR based on master and up to date
with master, and applies the rejection gate. A PR behind master is refused
with a prompt to update its branch. The supervisor posts `mergemarshall/verdict`
success on the exact head with description `fast-track by operator`, then
merges with `--merge --match-head-commit`. It records a direct batch with
source `operator` and does not abort or modify a batch already in progress; an
active batch handles any resulting master movement through its normal update
path. This operator path does not wait for CI. Slack marks operator
fast-tracks separately.

For regular batches, one DeepSeek Flash (`deepseek-flash`, no sub-agents)
session starts from current master, merges source heads with merge commits,
resolves conflicts, runs targeted checks using `ci-impact` and repository
guidance, then opens or updates one integration PR. Its title lists its source
PRs and it carries the
`mergemarshall-batch` label.

Run `python automerge.py --check` (or `--once`) to inspect the next tick's
selection and plan. It reads queue state and GitHub but does not create a batch,
remove labels, start Mjolnir, post Slack, or write to GitHub.

### Sync mode

The agent session stays live and idle while the supervisor polls `PR verification`
every minute. It accepts that check only from `.github/workflows/ci.yml` for
the exact tested head and `pull_request` event, matching the latest attempt's
check suite. On red, the supervisor sends failed-step logs for the integration
head and the selected baseline run for the exact batch base back to the same
session. If the base is a previous integration merge, its final PR CI is used
only when its tested head and the base have identical Git trees. Otherwise the
baseline is the newest `ci.yml` run on master for that exact SHA. Pending CI is
waited on; when CI is missing or cancelled, the supervisor first checks for
open ledger failures whose last-seen commit is equal to or an ancestor of the
base. Those parser-derived identities count as baseline evidence. If none
qualify, CI is dispatched on master only while master still points to the base.
If master advances, the supervisor asks the agent to merge current master and
retest before waiting for a baseline on the old base. After dispatch, the supervisor waits on a persisted
10-minute grace period for the run to appear before retrying. If no baseline
can be established, the batch stays waiting, sends a top-level Slack alert, and
retries automatically. Unavailable integration CI run data receives the same
alert and retry treatment. The
supervisor compares failed tests and failed steps independently within each
same failed job against that baseline. The
agent can append fixes or eject a responsible PR by rebuilding the branch
without it; ejection never uses a revert commit. Force-push is permitted only
for rebuilding `mergemarshall/batch-<id>`, using that exact branch ref. Sync batches
allow at most four CI rounds.

The supervisor decides whether red CI is not worse than the batch base by
comparing failed jobs, test identities, and failed step names. The agent's
`automerge-verdict` is advice only. Before merge, the supervisor checks that
the integration PR is based on current master, every constituent PR is still
open, non-draft, based on master, and at its tested head, and every recorded
source head is present while no ejected head remains in the integration tree.
An excluded head already on current master is accepted as part of that base.
A confirmed ancestry mismatch rebuilds the integration branch from the selected
source heads and retests it. If GitHub compare data is unavailable, the merge
remains pending, a top-level Slack alert is sent, and verification retries on
the next tick.
Once all sync gates pass, it posts the required `mergemarshall/verdict` success
status on the exact CI-tested integration head, then runs
`gh pr merge <n> --merge --match-head-commit <tested-sha>`. A GitHub refusal
caused by master advancing returns the status to pending, merges master into
the batch branch, and requires fresh CI before another success status. The
supervisor posts pending while CI runs and failure when a batch closes without
landing. The bot never pushes master.

If GitHub refuses the verdict status or merge while the candidate is still
current, the supervisor leaves the PR open and retries after 1, 2, 4, 8, then
10 minutes, continuing every 10 minutes until it lands or an operator aborts
the batch. The first failure sends a top-level Slack alert. A successful merge
command is confirmed against the PR state before the batch is marked landed.

After landing, constituent PR states appear in the single batch summary. The
bot does not post per-PR warnings for provisional unmerged status: GitHub's
indirect merge status can lag, and excluded PRs intentionally remain open.

Sync not-worse mode trusts test output produced by PR code, which could fake
its reported failures. This is accepted while Bifrost PRs are authored by the
team's agents and people. Strict green-only mode does not have this issue.

### Known CI failures ledger

The additive `known_failures` table shares the monitor's SQLite database. Its
key is workflow, job, and either a deterministic test identity parsed from a
failed-job log or a failed step name when the log has no parseable test. The
same parser powers sync not-worse comparisons. Job names omit RunsOn's
per-run labels while retaining real matrix values. The aggregate
`PR verification` job is excluded. Each completed master run is recorded once;
cancelled runs are ignored. A later passing job, or a failure
whose parsed identity no longer appears, closes an open ledger row.

All three cron entry points share a persisted five-minute upkeep guard. They fetch
recent completed runs for CI, Hourly CI, and Nightly CI, and fetch logs only for
failed jobs. The first backfill is limited to the five newest completed runs
per workflow from the last 24 hours; older runs are never traversed. If a
failed-job log is unavailable, failed-step names from the job metadata still
enter the ledger and other jobs continue normally. The repair and automerge
prompts include up to 40 open identities and a count of additional rows. The
monitor omits rows already linked to an open repair PR or escalation issue.
Agents may return `known-failure:` lines
with a one-line diagnosis; a diagnosis is stored only when the corresponding
identity already exists in the ledger. The bot maintains and pins one issue
named `Known CI failures on master`; its rendered table is informational and
is never read back as data.

### Independent failure triage

`triage.py` runs every minute alongside the merger. It reads the shared ledger
and starts one `mj new` session in `CI`, on the `podman` target with the `bifrost`
bundle, using `deepseek-flash`, no subagents, **2 CPUs and 4 GiB RAM**. The session
reads logs, source, history, issues and repair PRs; it does not build or fix code.
It has no runtime deadline and stays live throughout its investigation.

Up to 40 new observations are grouped into one investigation. An observation is
identified by workflow, job, failure identity, failed steps and failing commit;
repeated runs of the same failure on the same commit do not launch more agents.
Changed commits or failure identities become new work. Diagnoses must distinguish
evidence from hypotheses. The agent drafts one issue per cause, searches existing
open and closed issues, and identifies stale failures already fixed on master.

The poller validates the final JSON report, creates or updates `buildfailure`
issues, reopens matching closed issues, and records their links and diagnoses in
the ledger. Persisted publication markers recover successful GitHub writes whose
responses were lost. Failures fixed or superseded during the investigation are
skipped. A resolved finding (`issue: null`, with concrete evidence) retires only
the captured observation when its commit, failed steps, and run still match.
The row and diagnosis remain in SQLite as history; the aggregate issue and repair
prompts show only open rows. A later failed run reopens the row and can trigger
fresh triage even on the same commit. Completed historical reports are reconciled
on polling; `python3 triage.py --reconcile-resolved` applies that cleanup and
refreshes the issue immediately without launching a session.

Triage issue links are separate from human escalation links: a triage ticket does
not suppress the fixer. The fixer is instructed to reuse it. Publication waits
for the fixer's lock and any active repair invocation before deciding whether a
new ticket is needed. A completed triage session is checkpointed for the existing
archive policy, after its findings have been published.

State lives in the shared SQLite database, in `triage_jobs`,
`triage_observations` and `triage_publications`. `python3 triage.py --check` shows
sessions, pending observations and errors without contacting GitHub or mj. An
ambiguous launch is recovered by its unique session title; it is never blindly
launched twice. If no matching session was created, an operator can explicitly
retry with `python3 triage.py --retry-launch <job-id>`. Other API failures retry
on later polls. Logs use the `bifrost-ci-triage` journal tag on the runner.

### Async mode

The session runs targeted tests locally, using `AGENTS.md`, `ci-impact`, and
`.github/workflows` to choose the affected checks. It reruns any failing test at
the exact batch base in a separate worktree. Failures reproduced at the base
are baseline; new failures must be fixed or the responsible PR must be removed
and rejected at its tested head. The final agent message includes
`automerge-local: pass|fail`, `Tests run: ...`, and
`Baseline failures: ...`. Only `pass` proceeds to publication.

The supervisor does not wait for or query CI, run baseline workflows, or apply
the four-round sync limit. It performs the same master-freshness, source state
and head, and ancestry checks as sync mode. After they pass, it
posts `mergemarshall/verdict` success on the locally tested head with a description
such as `async: local targeted tests passed; CI runs after merge`, then merges
with `--match-head-commit`. The integration PR and master CI run normally after
merge. If master is red, the agent reruns targeted failures at the batch base
locally; it does not wait for master CI. Any breakage after merge is handled by
the existing CI monitor, which opens `ci-fix` PRs for later queue batches. The
Slack outcome links the integration PR so people can watch its CI.

Async mode also trusts test output produced by PR code: a PR could fake its
local results. This is accepted for Bifrost's current contributors, the team's
agents and people.

The desired master ruleset requires a pull request with zero approvals, the
`mergemarshall/verdict` status from mergemarshall (GitHub App ID 5203169), and an
up-to-date branch; it blocks force-push and deletion and has no bypass actors.
People cannot push directly to master or self-merge; changes land through the
queue. See [the ruleset guide](docs/mergemarshall-ruleset.md). An administrator
applies it manually with `bash scripts/apply-mergemarshall-ruleset.sh`; inspect the
JSON without making changes using `bash scripts/apply-mergemarshall-ruleset.sh
--dry-run`. The script is never run by cron or by `automerge.py`.

Each agent turn has a one-hour budget. On expiry the supervisor interrupts the
turn and notifies Slack. Finished messages and the GitHub outcome are reported
in the batch Slack thread. A failed or ambiguous `mj new` holds the queue until
the session listing proves the exact-title session is absent.

An operator can abort an active integration batch with
`python automerge.py --abort-batch <batch-id> --reason "<reason>"`. The command
waits up to two minutes for the same lock used by cron, interrupts and suspends
its session, posts a failure status on the integration head, and closes the
integration PR with the reason. It leaves the branch in place, removes any
rejection labels applied by that batch, rejects no source PRs, and releases the
queue. The aborted outcome is posted in Slack and retried on later ticks if
Slack is unavailable.
For a `direct` record there is no integration PR to close: abort marks the
attempt, posts failure on its source head, and leaves the source PR open and
eligible.

The supervisor uses the GitHub App token from `mj github-token --owner
BrokkAi`; sessions receive their own Mjolnir GitHub token, which cannot post
the required verdict status.

Create these labels in GitHub before enabling the job; the job does not create
labels:

- `ci-fix` — labels CI repair pull requests.
- `buildfailure` — labels issues filed for blocked or unrevertable failures.
- `automerge-rejected` — marks a PR rejected at its current head, paired with a
  trusted bot comment containing `automerge-rejected-head: <full sha>`.
- `mergemarshall-batch` — marks the integration PR.
- `mergemarshall:high` — next batch contains only high and immediate PRs.
- `mergemarshall:immediate` — interrupts an active batch before publication and
  selects all high and immediate PRs. `mergemarshall-priority:high` and
  `mergemarshall-priority:immediate` are accepted aliases; the old
  `mergemarshall-priority` label remains a high alias.
- `known-ci-failures` — labels the generated master-failure ledger issue.

## Running and inspecting

    ./monitor.py --check
    ./monitor.py --init-db
    sqlite3 "${BIFROST_CI_DB:-$HOME/Projects/bifrost-ci/activity.db}" 'select workflow_run_id,sha,status,exit_code,started_at,finished_at from invocations order by started_at desc;'
    crontab -l

The installed cron entry uses absolute paths and a non-overlapping process lock.
Slack delivery is fail-open after configuration: an outage is logged but does
not stop session supervision.

## License

Copyright 2026 Brokk AI. Licensed under the Apache License, Version 2.0.
See LICENSE.
