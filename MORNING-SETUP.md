# Morning setup: switch Slack to threaded live Codex feed

Everything on the code side is done and tested. The monitor already runs the
new code in production but is **still on the incoming webhook** (unchanged
behaviour) because no bot token is configured yet. Configuring the bot token is
the only thing that flips it into threaded-streaming mode — and it's the part
that needs your interactive Slack session, which is why it waited for morning.

## What changes once you finish these steps

- The ":rotating_light: engaged" message is posted via `chat.postMessage` (not
  the webhook), so we get its `ts`.
- Each Codex **assistant message** (not tool calls) is streamed into that
  message's **thread** as it happens, via `--json` event parsing.
- The outcome message lands in the same thread.
- The incoming webhook stays configured as an automatic fallback: if the bot
  token is ever missing or invalid, the monitor reverts to today's behaviour.

## Design note (why nothing was "killed")

This reuses the **same** Slack app (`A0BPC1HK4M6`) — there is no second app.
Adding the `chat:write` scope is additive; reinstalling to grant it should
**not** revoke the existing incoming webhook. So the "old" path is never torn
down by these steps. Decommissioning the webhook later (step 6) is optional and
entirely your call.

## Steps

1. **Add the `chat:write` scope.** `slack/manifest.json` already lists it
   (`oauth_config.scopes.bot` now has `incoming-webhook` + `chat:write`). Apply
   it the reliable way via the app settings UI:

       slack app settings --app A0BPC1HK4M6

   In *OAuth & Permissions → Scopes → Bot Token Scopes*, confirm `chat:write`
   is present (add it if the manifest push didn't take).

   *(CLI alternative — verify the payload shape against your Slack CLI version;
   I'm not fully certain of it:)*

       export SLACK_SERVICE_TOKEN='...'   # from `slack auth token`, see README
       manifest=$(python3 -c 'import json; print(json.dumps(open("slack/manifest.json").read()))')
       slack api apps.manifest.update --token "$SLACK_SERVICE_TOKEN" \
         --json "{\"app_id\":\"A0BPC1HK4M6\",\"manifest\":$manifest}"

2. **Reinstall the app** to grant the new scope and mint a bot token:

       slack app install --team T08PB1S0VL2 --app A0BPC1HK4M6 --token "$SLACK_SERVICE_TOKEN"

   Approve the prompt. Then copy the **Bot User OAuth Token** (`xoxb-...`) from
   *OAuth & Permissions* in the app settings.

   > Sanity check: after reinstalling, the existing webhook should still work.
   > If you want to be sure before switching, the webhook is still the active
   > transport until step 4.

3. **Get the channel ID** for `#github-brokk-desktop`: in Slack, click the
   channel name → *About* → the `C...` ID at the bottom. Then invite the bot so
   it can post there:

       /invite @Bifrost CI Auto-fixer

4. **Configure the monitor** (stores both secrets mode-0600, token entry hidden):

       /home/jonathan/Projects/bifrost-ci-monitor/monitor.py --configure-bot

   Paste the `xoxb-...` token (hidden), then the `C...` channel ID.

5. **Test threading:**

       /home/jonathan/Projects/bifrost-ci-monitor/monitor.py --test-slack

   You should see a top-level message **and** a threaded reply under it in
   `#github-brokk-desktop`. That confirms `chat.postMessage` + `thread_ts` work.
   From here the monitor auto-switches on the next red CI — no cron change.
   To watch a real run, wait for (or observe) the next engagement; the thread
   should fill with Codex's messages live.

6. **Decommission the webhook (done at the monitor level).** The local webhook
   file has been removed, so the monitor no longer has (or uses) a fallback —
   it posts only via the bot transport. Do **not** delete/uninstall app
   `A0BPC1HK4M6`; it's the same app now doing the threaded posting.

   > **Manifest drift, intentional.** `slack/manifest.json` no longer lists the
   > `incoming-webhook` scope, so it now describes the desired bot-only app. The
   > *live* install of `A0BPC1HK4M6` still carries `incoming-webhook` and its
   > (now dormant, uncalled) webhook URL until the next reinstall applies the
   > updated manifest — which may be never. That gap is deliberate: the webhook
   > does nothing, and a reinstall purely to drop it may hit an org-admin
   > restriction (the app is `org_deploy_enabled`). To actually retire the URL
   > in Slack, remove `incoming-webhook` from Bot Token Scopes and reinstall.

## Rollback

The webhook fallback has been retired, so rolling back means either
reconfiguring the webhook or trusting the bot token. If the bot transport
misbehaves, restore the webhook fallback:

    ./monitor.py --configure-slack        # re-add the incoming webhook URL

and/or drop the bot credentials so only the webhook remains:

    rm ~/.config/bifrost-ci-monitor/bot-token ~/.config/bifrost-ci-monitor/channel-id
