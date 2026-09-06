# Bifrost CI Auto-fixer

This monitor polls the CI, Hourly CI, and Nightly CI GitHub Actions workflows
for BrokkAi/bifrost-dev every five minutes. CI is restricted to push runs on
master; Hourly CI and Nightly CI include their scheduled and manually
dispatched runs. When the latest run in any tracked workflow is red, it
launches one Codex repair attempt for that run, regardless of whether the run's
commit is still master HEAD. Codex always works from current master HEAD: if an
intervening commit already fixed the failure it exits without changes,
otherwise it fixes forward, tests, and leaves a clean local commit. The monitor
then merges current origin/master and pushes the verified result.

The monitor:

- claims each CI run atomically in ~/Projects/bifrost-ci/activity.db, keyed on
  the workflow run id; interrupted or orphaned attempts are preserved and may
  be retriaged in the same Slack thread;
- waits five minutes after a workflow attempt first becomes red, allowing
  RunsOn to replace an interrupted runner before launching Codex or filing an
  infrastructure issue;
- serializes runs with a local lock, refuses a dirty repair worktree, and tags
  a clean orphaned commit before restoring origin/master for fresh triage;
- asks Codex to commit but never push, then explicitly pulls with merge policy
  and pushes only after verifying the result on origin/master;
- handles conflict-free master advances itself and resumes the same Codex
  session only when a pull leaves actual content conflicts;
- records combined Codex output, reconciliation state, and verified outcome in
  SQLite;
- after one hour, stops the repair, preserves a verified local recovery package,
  and restores the dedicated worktree to origin/master before resuming that exact
  Codex session for a ten-minute ticket-only handoff with recovery pointers;
- verifies that a design escalation's GitHub issue is still open before
  standing down, so closing a ticket re-arms classification even if CI never
  went green;
- sends Slack engagement and outcome messages without waiting for new CI.

Slack delivery has two transports. If a bot token and channel are configured
(`--configure-bot`), the monitor posts via `chat.postMessage`: the engagement
message opens a thread, and Codex's assistant messages (not tool calls) stream
into that thread live via `codex exec --json`, followed by the outcome. If only
an incoming webhook is configured (`--configure-slack`), it posts the engagement
and outcome as plain channel messages with no live feed. The bot transport is
preferred when present; the webhook is the automatic fallback. See
[MORNING-SETUP.md](MORNING-SETUP.md) for the one-time bot-token migration.

The monitor is intentionally separate from the Bifrost repository. The
bifrost-ci checkout is only the clean repair worktree; this repository owns
the scheduler, database schema, Slack integration, and tests.

## Local layout

    ~/Projects/bifrost-ci-monitor/  this repository
    ~/Projects/bifrost-ci/          clean Bifrost repair worktree
    ~/Projects/bifrost-ci/activity.db
    ~/.local/state/bifrost-ci-monitor/
    ~/.config/bifrost-ci-monitor/

Secrets and runtime state are local-only. Do not commit the Slack webhook,
SQLite database, cron output, or Codex session data.

## Recovering an unfinished repair

Timeout tickets include a monitor-generated recovery block and a continuation
note describing the diagnosis, attempted changes, observed test results, and next
step. Preservation happens **before** the ticket session resumes. A ticket reports
preservation and cleanup separately; a failed cleanup does not imply that a
verified backup is unavailable.

Each attempt keeps its manifest, repair transcript, and recovery instructions in
`~/.local/state/bifrost-ci-monitor/recovery/<run-id>/<attempt>/`. Local Git refs
under `refs/tags/bifrost-ci-recovery/<run-id>/<attempt>/` pin the original HEAD and
any stash object. Tickets include full object IDs, the host and repository path,
and the Codex session ID. These refs and files are **local only**, are not pushed
to GitHub, and have no automatic expiration. Later stashes do not change them.

On the host named in the ticket, use its exact restore command. The general form
is:

```sh
python3 /home/jonathan/Projects/bifrost-ci-monitor/recovery.py \
  /home/jonathan/.local/state/bifrost-ci-monitor/recovery/<run-id>/<attempt>/manifest.json \
  /path/to/a/new/recovery-worktree
```

The destination must not exist. The helper creates a detached worktree at the
saved HEAD, restores staged and unstaged edits and nonignored untracked files,
and leaves the cron repair worktree alone. If the deadline interrupted a merge,
the package also preserves partial resolutions, index stages, and merge metadata;
restoration reconstructs that unfinished merge. Ignored build outputs are excluded.
Review the ticket's continuation note and validate the saved changes before
finishing the repair.

If preservation fails, the monitor leaves the original worktree intact and blocks
new repairs. It retries pending recovery on later ticks using the same attempt's
package. A restart or failed ticket handoff retains existing verified artifacts;
it never substitutes the cleaned worktree for the original WIP. The database and
monitor outcome identify the package even when no ticket could be created.

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

       /home/jonathan/Projects/bifrost-ci-monitor/monitor.py --configure-slack
       /home/jonathan/Projects/bifrost-ci-monitor/monitor.py --test-slack

   The first command hides the URL while reading it and writes a mode-0600
   file under ~/.config/bifrost-ci-monitor/.

## Running and inspecting

       /home/jonathan/Projects/bifrost-ci-monitor/monitor.py --check
       /home/jonathan/Projects/bifrost-ci-monitor/monitor.py --init-db
       sqlite3 ~/Projects/bifrost-ci/activity.db +         'select workflow_run_id,sha,status,exit_code,started_at,finished_at from invocations order by started_at desc;'
       crontab -l

The installed cron entry uses absolute paths and a non-overlapping process
lock. Slack delivery is fail-open after configuration: a Slack outage is
logged, but it does not prevent the Codex repair attempt.

## License

Copyright 2026 Brokk AI. Licensed under the Apache License, Version 2.0.
See LICENSE.
