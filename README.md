# Bifrost CI Auto-fixer

This monitor polls the CI, Hourly CI, and Nightly CI GitHub Actions workflows
for BrokkAi/bifrost-dev every five minutes. CI push runs follow master; Hourly
CI and Nightly CI also include their scheduled and manually dispatched runs.
When a settled latest run is red, the monitor starts a Mjolnir container session
at the current master SHA fetched from GitHub.

The agent diagnoses each failure independently and follows one of four paths:

- FIX: make a small repair, test it, commit it with the trailer
  CI-Repair-Run: <run-id>, push the session branch, and open a `ci-fix` PR for
  automerge. The PR body records the failing run and tests, introducing commit,
  classification, and evidence.
- REVERT: revert a change when a direct fix is too involved, document the
  regression, add the same run trailer, and open a `ci-fix` PR. The issue
  comment and final Slack message link to the revert PR.
- BLOCKED REVERT: make no changes or commits, file a buildfailure issue, and
  ping the team when a revert conflicts with later dependent work.
- ESCALATE: make no changes or commits, file an issue, and ping the team for
  flaky, infrastructure, or unpinnable failures.

The repair agent publishes only its `ci-repair/<run-id>-<attempt>` branch and
PR; it never force-pushes, writes directly to master, or merges its own PR. If
one invocation makes both fixes and reverts, it puts all of its commits in one
PR. The automerge agent batches open, ready PRs into one integration PR. The
repository's own CI verifies that exact integration head before the bot merges
it; red batches are repaired or have responsible PRs removed and rebuilt.
Conflicts are resolved by automerge, and created commits keep their run trailer
for auditability. The monitor detects repair publication by looking up the PR
for the session branch.

Every host-side `gh` call from the monitor or automerge uses `GH_TOKEN` from
`mj github-token --owner BrokkAi`; repair sessions continue to use their own
Mjolnir-injected token. The shared `REQUIRE_APP_TOKEN` setting in `monitor.py`
defaults to `True`, so polling stops and Slack receives one blocked notice per
reason if the app token cannot be obtained. For local development only, setting
it to `False` allows ambient `gh` authentication and logs that fallback
explicitly. The supervisor caches the token for at most 30 minutes and asks
Mjolnir for a fresh one after a GitHub 401 response.

Both cron jobs update the shared `known_failures` ledger at most once every
five minutes. It records parser-derived identities from completed master runs
for CI, Hourly CI, and Nightly CI, marks failures fixed when their job passes or
their identity disappears, and links monitor repair PRs or escalation issues.
The pinned `Known CI failures on master` issue is a generated view; SQLite is
the source of truth. Create the `known-ci-failures` label before the first
upkeep run so the bot can label the issue it creates.

## Lifecycle

The monitor atomically claims each workflow run in
`$HOME/Projects/bifrost-ci/activity.db` by default. Override the database path
with `BIFROST_CI_DB`. It waits five minutes after a failed attempt
first appears so RunsOn can request a replacement attempt, and serializes
polls with a local lock. Before launch it confirms that the same run is still
red and reads the current master SHA from GitHub.

A new repair uses the CI workspace, podman target, bifrost bundle, and the
`deepseek-flash` model with no sub-agents. Mjolnir creates the branch
ci-repair/<run-id>-<attempt> at that full master SHA and receives the prompt
from a temporary file. Its agent label is `DeepSeek Flash (mj)`. The container's Git and gh
commands use the session's injected GitHub token.

While a turn runs, the monitor polls mj wait and the finished-only transcript
about every five seconds. The bot transport relays each completed agent message
into the Slack thread. The transcript cursor and captured text are saved after
each poll. If the monitor restarts, it looks up the recorded Mjolnir session,
reattaches to an active turn, and resumes from the saved cursor without
reposting completed messages. Relay delivery is acknowledged item by item;
failed Slack posts remain eligible for retry. A post-close transcript revision
with an already-posted stable ID is deduplicated, so its late update is omitted.
When the turn ends, the monitor drains the transcript once more and looks for a
PR created from that session's exact branch. If it finds one, Slack reports the
linked PR number and escalation detection is skipped. If none exists, the
monitor checks the transcript for escalation. GitHub lookup failures retry on
later ticks; after three consecutive failures the invocation gets the distinct
`pr_detection_failed` status and a Slack notice.

The repair budget is one hour. At expiry the monitor interrupts the turn, then
asks that same session for a ten-minute issue handoff. The handoff stops all
repair work and PR publication, lists any commits not yet published, and gives
the session id and branch for a human to continue. At the end of every session
path, the monitor asks Mjolnir to suspend and checkpoint the container without
waiting for the background suspension to finish. Later ticks verify that
requested suspensions reached a stopped state, retry once, and report persistent
failures in the Slack thread.

An invocation with an active session remains attached across monitor restarts.
Older worktree recovery statuses and manifests are retained as finished
history; they do not block current polling. Repair sessions do not use the
local ~/Projects/bifrost-ci Git worktree or its tags; the monitor's existing
SQLite database remains at ~/Projects/bifrost-ci/activity.db.

## Slack delivery

Slack has two transports. With a bot token and channel configured through
--configure-bot, engagement opens a thread and finished agent messages stream
into its replies. An incoming webhook configured through --configure-slack
receives engagement and outcome messages but cannot receive the live feed.
The bot transport is preferred when available. See MORNING-SETUP.md for bot
token setup.

If the monitor cannot launch or supervise a red run because Mjolnir is missing,
too old, unreachable, or fails to start the session, or GitHub blocks launch,
it logs each tick and posts one blocked notification for each distinct
(run, reason).

### Recover an unresolved launch

An invocation may remain in `launching` when the monitor cannot tell whether
`mj new` created its session. Before making it retryable, derive its exact title
from the row and confirm that title is absent from workspace CI:

```sh
run_id=12345
sqlite3 ~/Projects/bifrost-ci/activity.db \
  "SELECT status, attempt_count,
          workflow || ' ' || substr(sha, 1, 8) || ' run ' || workflow_run_id ||
          ' attempt ' || attempt_count || ' CI repair' AS title
   FROM invocations WHERE workflow_run_id = $run_id;"
mj sessions --workspace CI --json
```

Compare the title exactly, including the attempt number. If it is present, leave
the row alone so the monitor can adopt that session. If the workspace listing
succeeds and the exact title is absent, mark only that unresolved row retryable:

```sh
sqlite3 ~/Projects/bifrost-ci/activity.db \
  "UPDATE invocations SET status = 'launch_failed'
   WHERE workflow_run_id = $run_id AND status = 'launching'
     AND codex_session_id IS NULL;"
```

The next monitor tick increments the attempt and launches again. Do not run the
update when Mjolnir cannot list workspace CI or the exact session may still be
starting.

## Agent selection

The CI monitor sessions use `--model deepseek-flash --subagents none` and the
label DeepSeek Flash (mj). Setting `MJ_SUBAGENT_MODEL` (for example to
`gpt-6-luna`) switches to `--subagents single-model --subagent-model <model>`;
the planned setup is `opus` with `gpt-6-luna` sub-agents through Amazon
Bedrock, once the CI account has Bedrock access. Automerge uses `deepseek-flash` too, set separately, with
`--subagents none` and the label DeepSeek Flash (mj). The executables have
absolute defaults: `mj` at `$HOME/.cargo/bin/mj` and `gh` at `/usr/bin/gh`.
Override them with `BIFROST_MJ_BIN` and `BIFROST_GH_BIN`. Both scripts check
that the required executables exist and are executable at startup, then post a
once-per-reason blocked notice if one is missing. `BIFROST_CI_MONITOR_STATE`,
`BIFROST_CI_AUTOMERGE_STATE`, and `BIFROST_CI_CONFIG_DIR` override the state
and secrets directories, which otherwise live under the current user's home.

The installed mj must support transcript --finished-only. The monitor checks
this at startup and reports a blocked reason for a red run when the installed
CLI is too old. Upgrade Mjolnir from ~/Projects/mjolnir before enabling repair
launches.

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
the source queue. When exactly one PR is eligible and GitHub compare reports
`behind_by == 0` against current `master`, the supervisor records a `direct`
attempt and lands that PR without an Mjolnir session or integration branch.
If it is behind, or more than one PR is eligible, the normal batch path runs.

Direct attempts repeat the source state, head, rejection-marker, and workflow
change gates before merging. Async mode lands immediately after those checks.
Sync mode waits for `PR verification` from the exact `.github/workflows/ci.yml`
`pull_request` run at that head; green lands, and red lands only when the
supervisor's same-job test and step comparison proves it is no worse than the
captured master baseline. A worse red result is rejected by the supervisor at
that exact head with `automerge-rejected-head: <full sha>`, evidence, and the
`automerge-rejected` label. A PR that changes `.github/workflows/` or
`.github/actions/` stays pending for human review in either mode. If master
advances before the merge, the direct attempt ends without rejecting the PR;
the next tick sends it through the normal batch path.

For a direct PR held because it changes CI workflow or action files, a
maintainer reviews the diff and CI evidence. An authorized operator can then
post `mergemarshall/verdict: success` on that exact reviewed head with the
supervisor App token and merge it using
`gh pr merge <n> --merge --match-head-commit <sha>`. The job never posts success
automatically for this hold; see the [spec's human review steps](docs/automerge-integration-pr.md#human-review-for-ci-workflow-changes).

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

The agent session is suspended while the supervisor polls `PR verification`
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
After dispatch, the supervisor waits on a persisted
10-minute grace period for the run to appear before retrying. If no baseline
can be established, the batch stays waiting and Slack is notified once. The
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
Once all sync gates pass, it posts the required `mergemarshall/verdict` success
status on the exact CI-tested integration head, then runs
`gh pr merge <n> --merge --match-head-commit <tested-sha>`. A GitHub refusal
caused by master advancing returns the status to pending, merges master into
the batch branch, and requires fresh CI before another success status. The
supervisor posts pending while CI runs and failure when a batch closes without
landing. The bot never pushes master.

If the integration diff changes `.github/workflows/` or `.github/actions/`,
the supervisor leaves the status pending with `needs human review: CI workflow
changes` and holds the batch, because pull-request CI runs workflow definitions
from the PR. A maintainer can review the diff and CI evidence, then have an
authorized operator post success on the exact reviewed head through the
supervisor App status path and merge with `--match-head-commit`. To land the
other batch PRs first, the maintainer can mark the workflow-changing source PR
as a draft; the existing source-state gate will rebuild the integration PR
without it. Afterward the PR can be marked ready for separate human handling.
Its owner can also push a follow-up restoring the workflow/action files to
their master contents, which triggers the normal rebuild and automatic gates.

Sync not-worse mode trusts test output produced by PR code, which could fake
its reported failures. This is accepted while Bifrost PRs are authored by the
team's agents and people. Strict green-only mode does not have this issue.

### Known CI failures ledger

The additive `known_failures` table shares the monitor's SQLite database. Its
key is workflow, job, and either a deterministic test identity parsed from a
failed-job log or a failed step name when the log has no parseable test. The
same parser powers sync not-worse comparisons. Each completed master run is
recorded once; cancelled runs are ignored. A later passing job, or a failure
whose parsed identity no longer appears, closes an open ledger row.

Both cron entry points share a persisted five-minute upkeep guard. They fetch
recent completed runs for CI, Hourly CI, and Nightly CI, and fetch logs only for
failed jobs. The repair and automerge prompts include up to 40 open identities
and a count of additional rows. The monitor omits rows already linked to an
open repair PR or escalation issue. Agents may return `known-failure:` lines
with a one-line diagnosis; a diagnosis is stored only when the corresponding
identity already exists in the ledger. The bot maintains and pins one issue
named `Known CI failures on master`; its rendered table is informational and
is never read back as data.

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
and head, ancestry, and workflow-change checks as sync mode. After they pass, it
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
