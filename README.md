# Operating the Bifrost CI monitor and MergeMarshall

This repository runs the merge queue, failure triage, and issue fixer for
[`BrokkAi/bifrost-dev`](https://github.com/BrokkAi/bifrost-dev). This README is
the operator runbook. See [docs/ABOUT.md](docs/ABOUT.md) for the feature overview
and [AGENTS.md](AGENTS.md) for agent-facing implementation contracts.

If MergeMarshall is completely stuck, start with
[emergency merge access](#emergency-merge-access). Recovery can be performed
from GitHub without a working CI host or Mjolnir daemon.

## Where it runs

The CI host runs `~/Projects/bifrost-ci-monitor` as its normal login user.
Cron executes the Python files directly from that checkout:

| Entry point | Schedule | Purpose | Journal tag |
| --- | --- | --- | --- |
| `automerge.py` | Every minute | Supervise and land PR batches | `bifrost-ci-automerge` |
| `triage.py` | Every minute | Diagnose new master CI failures | `bifrost-ci-triage` |
| `monitor.py` | Every five minutes | Repair one unclaimed failure issue | `bifrost-ci-monitor` |

Each job has its own process lock. Ticks observe existing work rather than
launching another copy. Agent sessions stay live across ticks and have no
elapsed-time deadline. A long-running session is not, by itself, stuck.
Run the host commands below as the cron user, from the checkout:

```sh
cd ~/Projects/bifrost-ci-monitor
```

This repository's `master` accepts direct commits and pushes. The protected
`master` discussed below belongs to **bifrost-dev**, where agent changes land
through pull requests.

## Initial setup

Install Python 3.11 or later, Node.js 18 or later, Git, GitHub CLI (`gh`), Mjolnir (`mj`), Podman,
and SQLite's CLI for inspection/backups. The Python programs use the standard
library; the merger uses host Node.js to run Bifrost's CI impact classifier.
Configure Mjolnir's `CI` workspace, `bifrost` bundle, and the `podman` and
`bedrock-podman` targets before enabling cron. Merge sessions use the configured
Opus 5.5 model (`opus`) with Luna 6 subagents (`global.openai.gpt-6-luna`, high
effort), with up to 16 concurrent subagents per session. Set
`max_concurrent = 16` under `[subagents]` and make the Luna profile eligible
under `[subagents.eligible_profiles]`.
Fixer and triage sessions use the `deepseek-flash` model.

Merger/fixer sessions each use 32 CPUs and 28 GiB RAM; triage uses 2 CPUs and
4 GiB RAM. Provision capacity for overlapping work. The published Mjolnir
agent-dev image contains the Rust/Node tooling, uv, and Python development
dependencies. Refresh the host's image cache after an image update:

```sh
podman pull ghcr.io/brokkai/mjolnir/agent-dev:latest
```

Default executable paths are `~/.cargo/bin/mj` and `/usr/bin/gh`. Override
them in cron and the skill service when installed elsewhere, for example:

```sh
export BIFROST_MJ_BIN="$HOME/.cargo/bin/mj"
export BIFROST_GH_BIN="$HOME/.local/bin/gh"
```

Jobs obtain the MergeMarshall installation token from
`mj github-token --owner BrokkAi`. Configure that App access in Mjolnir;
an operator's ambient `gh` login is not the production credential.
Installed Mjolnir must support `transcript --finished-only`.

Create these repository labels: `ci-fix`, `buildfailure`, `agent-in-progress`,
`Escalated`, `mergemarshall:high`,
`mergemarshall:immediate`, and `known-ci-failures`.

MergeMarshall creates and manages `mergemarshall:in-progress` automatically.
It marks source PRs selected for a batch (including expansions and direct
landing), then removes the label when they leave the batch or the batch ends.
Updates are asynchronous and retry after GitHub outages; the label is
informational and never gates a merge. The labels listed above require initial setup.

The previous names `automerge-rejected` and `mergemarshall-batch` remain readable
for existing PRs; new writes use `mergemarshall:rejected` and `mergemarshall:batch`.
MergeMarshall creates these repository labels when needed.

Install the batch skills and start their user service on the host's private IP:

```sh
python3 scripts/install-mm-skills.py --service-listen <host-private-IP>
systemctl --user status mm-skills.service
```

The service listens on port 8769. Container agents reach it through
`host.containers.internal`; verify that route and restrict access to the
private host/container network. Pass `--github-cli <absolute-gh-path>` to the
installer if the service needs a different GitHub CLI path.

### Slack

Create and install the app described by [slack/manifest.json](slack/manifest.json),
authorize its bot to post, and invite it to the chosen channel. Configure its
bot token and Slack channel ID, not the channel's display name:

```sh
python3 monitor.py --configure-bot
python3 monitor.py --test-slack
```

Prompts hide the token and store it in mode-0600 files. The bot transport
provides engagement threads and relays completed agent messages. A legacy
incoming webhook can instead be configured with `monitor.py --configure-slack`;
it provides engagement/outcome notices without the threaded transcript.
After configuration, delivery failures are logged without stopping supervision.
`--test-slack` sends a real test message.

### State and scheduling

| Item | Default path | Override |
| --- | --- | --- |
| Shared SQLite database | `~/Projects/bifrost-ci/activity.db` | `BIFROST_CI_DB` |
| Fixer state and lock | `~/.local/state/bifrost-ci-monitor` | `BIFROST_CI_MONITOR_STATE` |
| Merger state and lock | `~/.local/state/bifrost-ci-automerge` | `BIFROST_CI_AUTOMERGE_STATE` |
| Slack configuration | `~/.config/bifrost-ci-monitor` | `BIFROST_CI_CONFIG_DIR` |

Triage's lock lives in `bifrost-ci-triage` beside the fixer's state directory.
Keep databases, tokens, logs, and session data out of Git.

Initialize and inspect the jobs, then install the schedules with `crontab -e`.
Use absolute executable/checkout paths and the journal tags above. Preserve
the required environment in each entry. An entry point without an inspection
flag performs real work.

```sh
python3 monitor.py --init-db
python3 monitor.py --check
python3 triage.py --check
python3 automerge.py --check
crontab -l
```

`automerge.py --check` (alias `--once`) reports the active batch or next
selection without changing SQLite, GitHub, or sessions. `triage.py --check`
shows local jobs without polling GitHub/Mjolnir; it can initialize/migrate
local schema. `monitor.py --check` checks current CI and prerequisites without
starting a repair, but can initialize state and send a blocked notice.

## Monitor and intervene

Inspect the batch's Slack thread and integration PR for source PRs, the tested
head, test evidence, and outcome. The pinned `Known CI failures on master`
issue is the generated view of unresolved failures; individual `buildfailure`
issues are repair targets.

Dependent PRs may target the prerequisite branch and become ready before it
lands. MergeMarshall orders prerequisites before dependents and can land them
in one integration batch. A priority PR brings its eligible prerequisites into
the priority batch. Draft, rejected, changed, or closed-unmerged prerequisites
block descendants; unrelated work continues. Rejection of a prerequisite does
not reject its descendants.

MergeMarshall retargets dependents to master after their prerequisites land,
using commit ancestry and durable GitHub retries. Authors update changed
prerequisites with merges on draft branches, validate, and ready their PRs again.
They do not need to poll or promote submissions. `--land-now` also enforces
dependencies and refuses a PR that cannot land alone.

When a PR waits on a dependency, inspect its Slack notice or the next idle
`automerge.py --check` selection's `dependency_blocks`. Confirm the prerequisite
PR's readiness and that the dependent contains its current submitted head.
Ambiguous branch ownership, cycles, and an unresolved base require correcting
the PR relationship. Promotion writes appear as `promote_dependency` in the
outbox and retry automatically; preserve the database across restarts so
retargeting cannot erase captured dependencies.

Infrastructure incidents such as runner acquisition/loss, provider quota, or
external outages appear as short top-level Slack notices with diagnosis,
evidence, uncertainty, and run links in the thread, rather than Bifrost product
tickets. Flaky product tests still create repair tickets. Failed Slack/GitHub
publication retries the prepared result from SQLite on the next poll without
another investigation; unrelated triage and repair work can continue.
An interrupted job preserves earlier product failures until completed results
show recovery. Merge agents see classified infrastructure as diagnostic context,
so expected Spot preemption does not become a code repair task.

For RunsOn incidents, inspect `/aws/ecs/runs-on/runs-on-worker` in the CI AWS
account's `us-east-1` CloudWatch logs, filtering by job ID and failure time.
An EC2 launch quota error warrants a quota review; a successfully launched
runner later reclaimed as Spot is an interruption, which a quota increase
does not prevent. Preserve the intentional `spot=true/retry=false` policy.

```sh
journalctl -t bifrost-ci-automerge -t bifrost-ci-monitor -t bifrost-ci-triage --since '1 hour ago'
systemctl --user status mm-skills.service
journalctl --user -u mm-skills.service --since '1 hour ago'
sqlite3 -readonly "${BIFROST_CI_DB:-$HOME/Projects/bifrost-ci/activity.db}" \
  'SELECT batch_id,status,phase,ci_mode,integration_pr_number,session_id,
          github_write_retry_attempts,github_write_retry_after
   FROM automerge_batches ORDER BY created_at DESC LIMIT 10;'
sqlite3 -readonly "${BIFROST_CI_DB:-$HOME/Projects/bifrost-ci/activity.db}" \
  'SELECT issue_number,status,session_id,retry_pr_number,repair_pr_number,last_error
   FROM issue_repairs ORDER BY created_at DESC LIMIT 10;'
```

The default mode is **async**: integration agents run targeted local tests;
GitHub CI need not finish before landing. One eligible PR already current with
master can land directly without an agent or a new local test run. **Sync**
waits for verified PR CI and permits failures proven no worse than the captured
master baseline. `CI_MODE` in `automerge.py` controls new batches; existing
batches retain their recorded mode.

Integration agents hand off through the shared database: successful `mm-autopr`
publication records readiness atomically, and speculative successors use
`mm-db ready`. The supervisor advances from that receipt even if `mj wait` times
out or the session still appears active. It applies the normal source, master,
tested-head and CI gates, then suspends the session after confirmed landing.
An assessment alone or an externally tagged PR is not a completion signal.
Supervisor follow-ups use Mjolnir's message delivery: busy agents receive them at
tool boundaries and idle agents wake to handle them. Delivery retries reuse the
same request ID. Typed context clears and their ordered restart prompts keep the
recovery protocol.

Local checks run through `mm-db run` retain their command, tested commit/tree,
settings/environment, timing, exit status, and output paths in
`automerge_executions`. `mm-compare` records both sides in configured checkouts.
Read a batch's history with `mm-db executions`; linked records also appear in
the integration PR and final report. Output files and retryable receipts remain
in the agent environment. If delivery fails, use `mm-db execution --receipt FILE`
to upload the saved result without rerunning the check. Recording does not change
test selection or merge policy, and older assessments remain accepted.

The scheduler permits one speculative successor while the foreground batch
validates. It starts from the foreground's pushed candidate and owns only newly
selected PRs. `python3 automerge.py --check` shows the candidate, successor and
recovery generation. A successor publishes and lands only after promotion,
incorporating the actual landed base and recording a fresh assessment. Both CI
modes use this lifecycle.

When the foreground lands, its successor becomes primary immediately and keeps
any running checks. Its checkpoint can start a new speculative successor before
validation finishes. After a passing handoff it incorporates the landed merge
commit and records a fresh assessment; unchanged trees can reuse applicable
evidence and preserve the next successor's work.

If the predecessor changes, the supervisor stops the successor's work, clears
its native conversation, and sends a generated restart brief in the same
session/container. Its helper saves pending source work and the old tip in the
Git directory; caches, logs and reviewed rerere resolutions remain available.
No agent-written handoff is required. Set `BIFROST_CI_SPECULATIVE_LOOKAHEAD=0`
in the cron environment to disable new successors; existing ones still finish
or recover. Lookahead requires mj's prompt command IDs and `clear-queue` API;
missing controls leave selection pending rather than starting an unrecoverable
successor. Existing sessions acquire lookahead only when they checkpoint a
candidate.

### Prioritize, fast-track, or abort

Apply `mergemarshall:high` to enter the next priority batch after current work
finishes. `mergemarshall:immediate` aborts active work before landing, then
selects all eligible high/immediate PRs. A batch already merging or carrying
a success verdict finishes first. `mergemarshall-priority:high` and
`mergemarshall-priority:immediate` are aliases; the old
`mergemarshall-priority` label counts as high. `ci-fix` alone is not priority.
Draft and rejected heads remain ineligible.

An operator who has reviewed a source PR can fast-track it through the App:

```sh
python3 automerge.py --land-now <PR-number>
```

The PR must be open, non-draft, unrejected, based on master and current with
master. This command does not wait for CI or run tests. It waits up to two
minutes for the cron lock and does not abort an existing batch. It still needs
the MergeMarshall credential and GitHub merge permission. Current master has
no special workflow/action-change hold; `--allow-workflow-changes` was removed.

To stop a batch and release its source PRs back to the queue:

```sh
python3 automerge.py --abort-batch <batch-id> --reason 'Operator intervention: ...'
```

Abort interrupts/suspends the agent, closes the integration PR, records a
failure verdict, and leaves the branch for inspection. It rejects no source
PRs. Established exact-head rejections remain recorded and their delivery is
retried; other source heads become eligible again. Aborting a predecessor also
aborts its speculative successor. For a direct attempt, the source PR stays open.

### Common stalls

| Symptom | Operator action |
| --- | --- |
| Verdict or merge write refused | Read the top-level Slack alert and GitHub rules. Retries wait 1, 2, 4, 8, then 10 minutes, continuing every 10 minutes. Repair credentials/rules or use emergency access below. |
| Missing executable or App token | Check the cron user's paths and Mjolnir App configuration. Keep production jobs on App authentication. |
| Skill service unreachable | Check its journal, private IP and container route; after correcting the cause, run `systemctl --user restart mm-skills.service`. |
| Source PR changed during a batch | The changed head is made draft. Finish its update and mark it ready again. |
| Exact head rejected | The bot comments and labels that head. Read its evidence and fix the existing branch; a new head can re-enter without a draft-state change. Removing the label does not establish a fix. |
| GitHub comment, label, or draft write pending | The supervisor stores the request in `automerge_github_outbox` and retries with backoff, including after the batch ends. Check `last_error` and `next_attempt_at`; repeated failures send a top-level Slack alert. Agents should keep working and never retry these writes with `gh`. |
| Fixer issue not selected | Check assignment, `agent-in-progress`, and `Escalated`. Respect other people's work; escalations go to `DavidBakerEffendi`. |
| CI run, baseline, or ancestry lookup unavailable | Read the top-level Slack alert. The batch remains pending and retries automatically; inspect the exact SHA and GitHub availability. |
| Master advanced or ancestry mismatch confirmed | The agent updates/rebuilds the integration branch and retests before landing. |
| `launching` job without a session ID | Run `mj sessions --workspace CI --json` and match the persisted title before retrying. A lost response can still mean a session exists. |

After proving no matching session exists, triage offers
`python3 triage.py --retry-launch <job-id>`. Fixer has no reset command; state
repair requires investigating its persisted job and taking the fixer lock.
Never blindly delete/reset a launching job: that can create duplicate agents.
Explicit fixer prompt-size rejections already return to a retryable state.

## Emergency merge access

Use this if MergeMarshall or Mjolnir cannot recover and PRs must land. These
steps change the gate on **BrokkAi/bifrost-dev**, not this repository.

### Pause automation and inspect the gate

On the CI host, save `crontab -l > ~/bifrost-ci-crontab-before-recovery.txt`,
then use `crontab -e` to comment out only the `automerge.py` entry. Let the
active poll finish. With the default lock path, wait for it using:

```sh
flock -w 120 "${BIFROST_CI_AUTOMERGE_STATE:-$HOME/.local/state/bifrost-ci-automerge}/automerge.lock" true
```

Pausing cron does not stop the live agent or remove GitHub's gate. Use
`--abort-batch` if the supervisor is healthy and its agent should stop. A
broken host does not prevent recovery on GitHub; review the PR head again
immediately before merging.

Use your own repository-administrator login in GitHub or `gh`. The supervisor
App cannot administer this recovery. Clear token overrides in the operator
shell with `unset GH_TOKEN GITHUB_TOKEN` before using your stored personal
`gh` login. Inspect the rules:

```sh
gh api repos/BrokkAi/bifrost-dev/rulesets --paginate \
  --jq '.[] | {id,name,source,enforcement}'
gh api repos/BrokkAi/bifrost-dev/rulesets/18574277
```

At writing, **Protect `master`**, ID **18574277**, targets the default branch.
It requires PRs with zero approvals and the up-to-date `mergemarshall/verdict`
status from App **5203169**, blocks deletion/force-push, and has no bypass
actors. Verify its ID and scope before changing it. Read-only credentials may
omit `bypass_actors`; omission does not prove the list is empty. Organization
rulesets or classic branch protection can add requirements.

### Disable the merge gate

An administrator can open
[bifrost-dev's ruleset settings](https://github.com/BrokkAi/bifrost-dev/settings/rules),
select **Protect `master`**, remove only `mergemarshall/verdict` from required
status checks, and save. Keep its PR and deletion/force-push rules. Removing
the last required check also removes that check rule's up-to-date requirement;
update the reviewed branch against current master yourself.

For a complete temporary disable, set that ruleset's enforcement to
**Disabled** and save. This disables all its rules, including PR and
deletion/force-push protections. Export or record the original configuration
first. See GitHub's [ruleset management instructions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/managing-rulesets-for-a-repository).
Pausing cron or using `--land-now` alone does not disable GitHub's gate.

### Designate someone else to merge

Keep the ruleset active and add a recovery actor to its **Bypass list**. To
designate one person, grant them repository write access, place them in a
non-secret team, and add that team. A suitable repository role or installed
GitHub App can also be selected. Choose **For pull requests only** to keep the
PR trail while permitting bypass of the missing verdict. An assignee/reviewer
alone has no bypass. See GitHub's [bypass configuration instructions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository#granting-bypass-permissions-for-your-branch-or-tag-ruleset).

That actor can use the PR interface's bypass option or authenticate the CLI/API
as themselves. `gh pr merge --admin` requests bypass; it grants no new rights.
See the [GitHub CLI merge options](https://cli.github.com/manual/gh_pr_merge).

### Create an emergency token or App key

For a person, open GitHub **Settings → Developer settings → Personal access
tokens → Fine-grained tokens**. Choose resource owner **BrokkAi**, only
**bifrost-dev**, an expiry, and **Contents: Read and write** plus **Pull
requests: Read and write** for `gh` PR commands. Obtain required organization
approval. Add **Administration: Read and write** only if editing rulesets and
the account has that authority. See GitHub's [merge-token permissions](https://docs.github.com/en/rest/pulls/pulls#fine-grained-access-tokens-for-merge-a-pull-request)
and [ruleset-update permissions](https://docs.github.com/en/rest/repos/rules#fine-grained-access-tokens-for-update-a-repository-ruleset).

**The token still obeys the ruleset.** Its user needs bypass, or an administrator
must remove/disable the gate first. A personal status named
`mergemarshall/verdict` cannot satisfy the App-specific requirement. A writable
SSH/deploy key authenticates Git, not the PR merge API.

Read the token without echoing it or saving it in shell history. Replace the
placeholders with the PR number and the full SHA you have reviewed:

```sh
set +x
read -r -s -p 'Emergency GitHub token: ' recovery_token
printf '\n'
export GH_TOKEN="$recovery_token"
unset recovery_token
pr_number='<PR-number>'
gh pr view "$pr_number" --repo BrokkAi/bifrost-dev \
  --json state,isDraft,baseRefName,headRefOid,url
reviewed_head='<full-reviewed-head-SHA>'
gh pr merge "$pr_number" --repo BrokkAi/bifrost-dev \
  --merge --match-head-commit "$reviewed_head"
unset GH_TOKEN
```

Add `--admin` when exercising configured bypass. For an integration PR, use a
**merge commit** so source commits remain reachable and their PRs can be marked
merged. Verify with `gh pr view <PR-number> --repo BrokkAi/bifrost-dev
--json state,mergeCommit`.

For a separate automation identity, create/install a recovery GitHub App on
bifrost-dev with Contents and Pull requests write permissions, add it to the
PR-only bypass list, and generate its private key. The key must mint an
installation access token; it is not itself a `gh` token. Follow
[GitHub's installation-token procedure](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).
The replacement App also needs bypass or a changed gate: its App ID differs
from MergeMarshall's. Keep credentials local and revoke emergency access
after recovery.

### Restore normal operation

Confirm the manual merge and master CI, then restore the original enforcement,
App-specific required check, and bypass list. Remove temporary bypass entries
and revoke the emergency token/key. The helper restores normal policy with
**no bypass actors**:

```sh
bash scripts/apply-mergemarshall-ruleset.sh --dry-run
bash scripts/apply-mergemarshall-ruleset.sh
```

Use administrator authentication. It prints a plan and asks before writing;
cron never calls it. It replaces the selected ruleset's rules/bypass list, so
inspect the plan if other policy has been added. Restore the merger cron entry
with `crontab -e`, run `automerge.py --check`, and watch the next tick reconcile
PRs that were merged manually.

## Update or recover the host

Use `git pull --ff-only` in the cron checkout. The next tick uses that code;
there is no monitor daemon to restart. Restart `mm-skills.service` when its
implementation changes. Preserve credentials, SQLite, and Mjolnir session data
when replacing a host. Before upgrading or repairing state, take a consistent
backup:

```sh
sqlite3 "${BIFROST_CI_DB:-$HOME/Projects/bifrost-ci/activity.db}" \
  ".backup '$HOME/bifrost-ci-activity-backup.db'"
```

Restore only with all three cron entries paused and their active polls
finished. Reconcile preserved sessions and GitHub PRs; deleting the database
does not stop agents or undo GitHub writes.

## License

Copyright 2026 Brokk AI. Licensed under the Apache License, Version 2.0.
See [LICENSE](LICENSE).
