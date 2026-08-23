# Bifrost CI Auto-fixer

This monitor polls the CI, Hourly CI, and Nightly CI GitHub Actions workflows
for BrokkAi/bifrost-dev every five minutes. CI is restricted to push runs on
master; Hourly CI and Nightly CI include their scheduled and manually
dispatched runs. When the latest run in any tracked workflow is red, it
launches one Codex repair attempt for that run, regardless of whether the run's
commit is still master HEAD. Codex always works from current master HEAD: if an
intervening commit already fixed the failure it exits without changes,
otherwise it fixes forward and pushes HEAD:master.

The monitor:

- claims each CI run atomically in ~/Projects/bifrost-ci/activity.db, keyed on
  the workflow run id, so a given run is never invoked twice (a re-run of the
  same commit is a distinct run and is eligible);
- serializes runs with a local lock and refuses a dirty or diverged repair
  worktree;
- fast-forwards ~/Projects/bifrost-ci and asks Codex to push HEAD:master;
- records combined Codex output and exit status in SQLite;
- after one hour, stops the repair and resumes that exact Codex session for a
  ten-minute ticket-only human handoff, then stashes incomplete edits and
  restores the dedicated worktree to origin/master;
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
