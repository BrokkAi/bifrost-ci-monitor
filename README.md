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
and SQLite's CLI for inspection/backups. The cron programs and the skill
service use only the standard library; the merger uses host Node.js to run
Bifrost's CI impact classifier. The web dashboard (below) also needs `uv` and
`npm` to install its dependencies and build its UI.
Configure Mjolnir's `CI` workspace, `bifrost` bundle, and the `podman` and
`bedrock-podman` targets before enabling cron. Every new integration and fixer
session uses Opus (`opus`) at high effort on `bedrock-podman`, with straightforward
DeepSeek Flash (`deepseek-flash`) subagents at high effort and no subtle
subagents, including campaign batches and speculative successors. Each batch
retains its configuration through membership changes and recovery. Existing live
sessions keep their model and delegation settings. Opus merger sessions still move
automatically to DeepSeek Flash after two consecutive Bedrock 503 provider
errors, retaining the session, container, work, validation evidence and
delegation. Successful agent or tool progress resets the error count. A threaded
notice confirms the switch. Native Move failures or uncertain acceptance produce
an actionable alert; the original session and recovery state remain available.
On mj 2.46, configure `[subagents.straightforward]` with
`model = "deepseek-flash"` and `effort = "high"`, remove `[subagents.subtle]`,
and enable `deepseek = true` under `[subagents.eligible_profiles]`.
Keep `max_concurrent = 16` under `[subagents]`. Launches explicitly set the
straightforward choice; an omitted subtle override inherits the global setting,
so removing that global choice is required. Triage uses `deepseek-flash` with
no subagents. Apply model preferences to future launches; do not Move active
sessions to apply them.

For Opus sessions using one-hour prompt caching, set
`ENABLE_PROMPT_CACHING_1H = "1"` under
`[profiles.bedrock-claude.environment]` in the CI host's Mjolnir `config.toml`.
New workers pick up the setting automatically.

Merger/fixer sessions each use 32 CPUs and 28 GiB RAM; triage uses 2 CPUs and
4 GiB RAM. Provision capacity for overlapping work. The published Mjolnir
agent-dev image contains the Rust/Node tooling, uv, and Python development
dependencies. Refresh the host's image cache after an image update:

```sh
podman pull ghcr.io/brokkai/mjolnir/agent-dev:latest
```

Bifrost validation also requires Go and ShellCheck for the workflow script
checks. The upstream image and a separately pinned Bedrock image may omit
them; installing them in one session does not update future
sessions. Build the CI prerequisite layer for each target's actual image:

```sh
podman image inspect BASE_IMAGE --format '{{.Id}} {{.Config.User}}'
podman build --pull=never -f containers/Containerfile.ci-tools \
  --build-arg BASE_IMAGE=INSPECTED_IMAGE_ID --build-arg BASE_USER=INSPECTED_USER \
  -t localhost/mergemarshall-ci:DESCRIPTIVE_TAG .
podman run --rm --network=none localhost/mergemarshall-ci:DESCRIPTIVE_TAG \
  bash -lc 'go version && go env GOROOT && command -v gofmt && shellcheck --version'
```

Set the corresponding target's `image` in Mjolnir's `config.toml` to the verified
tag, retaining a private backup of the original configuration. Repeat for both
`podman` and `bedrock-podman`; refreshing only the upstream image does not update
the separate Bedrock image. New sessions use the new image. Preserve active
sessions and coordinate repairs with their owners: the same checksum-verified
`scripts/install-ci-go.sh` and `scripts/install-ci-shellcheck.sh` can run as
container root at a validation boundary. Both refuse to replace another
installation and are safe to repeat offline once installed.
Recheck affected tests afterward; earlier missing-tool results remain evidence
of the earlier environment.
Keep AWS configuration and credentials out of the common image; the tools layer
does not copy either. Bedrock-specific authentication stays separate.

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
It marks source PRs selected for a batch (including membership changes and direct
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

### Web dashboard

`mm-web.service` serves a read-only dashboard and JSON API over the shared
database, plus the stable release branch form. It runs from this checkout in a
uv-managed virtual environment (`pyproject.toml`, `uv.lock`); the React UI in
`web/` is built at install time. It listens on port 8770.

GitHub sign-in uses the `mergemarshall` App. An App administrator adds the
callback URL `http://localhost:8770/auth/callback` under the App's "Identifying
and authorizing users" settings and generates a client secret. Save both on
the host, readable only by `ubuntu`:

```sh
install -m 600 /dev/null ~/.config/bifrost-ci-monitor/mm-web-oauth.json
# then edit it to contain {"client_id": "...", "client_secret": "..."}
```

Install or update, then check the service:

```sh
# uv and npm live in ~/.local/bin, which a non-interactive SSH shell omits.
PATH=$HOME/.local/bin:$PATH python3 scripts/install-mm-web.py --listen <host-private-IP> \
  --github-cli /home/ubuntu/.local/bin/gh
systemctl --user status mm-web.service
journalctl --user -u mm-web.service
```

Add `--dry-run` to print the unit and commands without running them. Reach the
dashboard through an SSH tunnel and open `http://localhost:8770`:

```sh
ssh -N -L 8770:<host-private-IP>:8770 ubuntu@CURRENT_PUBLIC_IP
```

Access follows the user's permission on `BrokkAi/bifrost-dev`: write can view,
maintain or admin can also start or abort a stable release branch. The
permission is rechecked every ten minutes.

Agents use the same API with `Authorization: Bearer <token>`. A GitHub user
token (for example `gh auth token`) receives that user's permission. For agents
without a GitHub user token, mint a view-only service token on the host:

```sh
.venv/bin/python -m mm_web.tokens mint NAME
```

Service tokens cannot be revoked one at a time. To revoke all of them, delete
`~/.local/state/bifrost-ci-monitor/mm-web-service.key` and mint new ones. The
API schema is at `/api/v1/openapi.json` and interactive documentation at
`/api/v1/docs`. `POST /api/v1/sql` runs one read-only query over reviewed
columns; prompt, report and recovery text is never exposed.

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

Triage's lock lives in `bifrost-ci-triage` beside the fixer's state directory;
the stabilize engine's lock lives in `bifrost-ci-stabilize` beside it.
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
python3 stabilize.py check
crontab -l
```

`automerge.py --check` (alias `--once`) reports the active batch or next
selection without changing SQLite, GitHub, or sessions. `triage.py --check`
shows local jobs without polling GitHub/Mjolnir; it can initialize/migrate
local schema. `monitor.py --check` checks current CI and prerequisites without
starting a repair, but can initialize state and send a blocked notice.

For an active merger, `--check` includes pending supervisor actions, retry times,
and their last errors. The supervisor advances ready primary work before selecting
or observing a successor. It delivers promotion instructions in the same poll;
agent idleness is not a completion gate. Queue reads yield within the poll budget
and resume from saved ancestry comparisons on later polls.

Cold ancestry fetches run separately from the supervisor, under the merger state
directory in `ancestry-*/objects.git`; `fetch.json` identifies the current request
and its matching JSON receipt reports success. This cache contains full commit
history without a working tree and never changes a batch checkout. A pending
fetch is normal. Updates fetch branch and PR refs together; detached historical
tips are fetched in batches only if absent. The supervisor and skill service share
cached ancestry, tree identities, behind counts, changed paths and base-pinned
classification. Only needed classifier blobs are downloaded. Current master and
published batch heads are read freshly over Git at their validation gates.
Authentication/network failures defer; confirmed unavailable historical commits
can fall back to GitHub. `--check` can read existing facts but never warms or
writes the cache. Do not substitute a shallow clone. When no batch or eligible work remains,
the last primary Slack thread receives an open-PR summary grouped by draft,
rejection, and dependency state. Unchanged summaries are quiet; webhook transport
does not provide these threaded summaries.

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

Fixer concurrency defaults to one. Set the live limit on the CI host with:

```sh
printf '5\n' > "${BIFROST_CI_CONFIG_DIR:-$HOME/.config/bifrost-ci-monitor}/fixer-concurrency"
```

`BIFROST_FIXER_CONCURRENCY` in the monitor's environment overrides this file.
Both accept a positive integer; cron reads the setting every tick without a
restart. Lowering it lets existing repairs finish before admitting more work.
Each session still owns one issue. Ambiguous launches and unfinished suspension
reserve capacity; a failed repair does not stop supervision of the other repairs.

The default mode is **async**: integration agents run targeted local tests;
GitHub CI need not finish before landing, except the integration PR's
`quick policy` job (open-core classification and projection dry run), which
must pass. A red one goes back to the batch agent. One eligible PR already current with
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
validates, once the foreground has checkpointed a candidate. The successor
starts from a fresh read of master, not from the foreground's candidate, and
owns only newly selected PRs; a PR whose prerequisite is in a live batch waits
for it to land. `python3 automerge.py --check` shows every slot. A successor
records readiness and does not publish while its predecessor is live. Nothing
the predecessor does (ejection, rebuild, candidate withdrawal, failed tests or
abort) restarts or aborts the successor.

When the predecessor ends, landed or not, the successor becomes primary
immediately and keeps any running checks. Promotion opens a new top-level Slack
thread for the primary batch; subsequent progress and its outcome appear there.
The speculative thread retains the earlier history. If master moved, the
supervisor sends the agent an `mm-merge --promote` instruction in the same
session, without interrupting it and without waiting for its validation to
finish. The agent merges current master, chooses reruns from the diff, and
records a fresh assessment. If master did not move, there is no extra step. The
promoted batch's checkpoint can start the next successor.

Set `BIFROST_CI_SPECULATIVE_LOOKAHEAD=0` in the cron environment to disable new
successors; existing ones still finish. Lookahead requires mj's prompt command
IDs and `clear-queue` API; missing controls leave selection pending rather than
starting an unrecoverable successor. A successor deployed under the older
candidate-based design is reset once onto master in its existing session.

### Prioritize, fast-track, or abort

Apply `mergemarshall:high` to enter the next ordinary batch after current work
finishes; while any high PR is ready, that batch admits only high PRs and their
prerequisites. `mergemarshall:immediate` runs in its own lane alongside the
ordinary batches: one immediate batch at a time admits all eligible immediate
PRs and their prerequisites, never has a successor, and never aborts other work.
Ordinary batches merge the landed result when they reach it. A PR already in a
live batch stays there when relabeled. `mergemarshall-priority:high` and
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
retried; other source heads become eligible again. Aborting a predecessor does
not abort its speculative successor, which becomes primary. For a direct
attempt, the source PR stays open.

If suspension fails, the terminal batch retains its cleanup request and the
original session. The supervisor continues checking for completion; retries of
the suspension itself back off from one minute to at most fifteen minutes and
survive restarts. Mjolnir currently refuses to checkpoint a clone with unresolved
Git conflicts or an unfinished Git operation. Inspect the error's reference in
`~/.local/share/mjolnir/logs/mj-daemon-*.log` on the CI host. Preserve the conflicted
index, pending merge, files and environment; do not reset, stage conflict markers
as a resolution, or destroy the session just to make cleanup succeed. This
cleanup failure does not reserve the aborted batch's source PRs.

### Stabilize a branch

A stabilize campaign cuts `stabilize/<id>` from one master commit, runs push CI,
then hourly, then nightly on it, and lets triage, the fixer and the merger fix it
until every stage is green at one tip. Each passed stage opens a
`mergemarshall:high` PR from a snapshot of that tip to master. Progress goes to
one Slack thread. Only one campaign can be live.

```sh
python3 stabilize.py start --ref master --requested-by <github-login>
python3 stabilize.py status --json
python3 stabilize.py check
python3 stabilize.py abort <campaign-id> [--no-salvage]
python3 stabilize.py resume <campaign-id> --requested-by <github-login>
```

`start` without `--ref` uses the newest master commit with a completed push CI
run. `status` and `check` only read SQLite. `abort` keeps the branches and PRs
for inspection and, unless `--no-salvage` is given, opens one final salvage gate
PR from the campaign branch tip to master so fixes made so far still land; the
merge queue tests it like any PR. Use `--no-salvage` when the branch itself went
bad. A paused campaign names its reason in `status` and in its
thread; common reasons are escalated issues, a dispatched run that could not be
found, repeated infrastructure failures and the round limit.

Dispatching the suites needs `actions: write` on the MergeMarshall App
installation and in the host's `mj` `[github.app.token_permissions]`, which
narrows every host token. A refused dispatch pauses the campaign with GitHub's
status and message, for example "hourly workflow_dispatch refused (attempt 1):
HTTP 403: Resource not accessible by integration". After the permission is
fixed, run `resume`; the next tick retries the refused dispatch. `resume` never replays a
dispatch whose result was ambiguous, and it keeps the round and
infrastructure limits.

The engine needs its own cron entry. It relies on the crontab's existing
`PATH`, `HOME`, `XDG_RUNTIME_DIR` and `BIFROST_GH_BIN` lines:

```
*/5 * * * * /usr/bin/python3 /home/ubuntu/Projects/bifrost-ci-monitor/stabilize.py once 2>&1 | /usr/bin/logger -t bifrost-ci-stabilize
```

### Common stalls

| Symptom | Operator action |
| --- | --- |
| Verdict or merge write refused | Read the top-level Slack alert and GitHub rules. Retries wait 1, 2, 4, 8, then 10 minutes, continuing every 10 minutes. Repair credentials/rules or use emergency access below. |
| Missing executable or App token | Check the cron user's paths and Mjolnir App configuration. Keep production jobs on App authentication. |
| Skill service unreachable | Check its journal, private IP and container route; after correcting the cause, run `systemctl --user restart mm-skills.service`. |
| Session history reports an index version mismatch | Install the SessionWiki CLI version linked by the running Mjolnir release. Preserve a consistent SQLite backup of the index and verify its durable summaries, tags, notes and archive before migration. Validate `sessionwiki stats` on a private copy first, then use it to run the supported upgrade on the live index. The daemon rebuilds derived search rows in the background. Never delete the index or change its schema-version pragma by hand. |
| Source PR changed during a batch | The changed head is made draft. Finish its update and mark it ready again. |
| Exact head rejected | The bot comments and labels that head. Rejections favor unblocking the batch, so attribution may be by inspection rather than a standalone reproduction; the evidence says which. Fix the existing branch, or if the rejection is wrong push an empty commit (`git commit --allow-empty`). A new head re-enters without a draft-state change and is tested again in a batch; merging master instead can let a lone up-to-date PR take the direct path without a new test run. Removing the label alone does not clear the rejection. |
| PR has `mergemarshall:dependency-blocked` | Read the bot's comment on that PR for the prerequisite link and needed action. The label clears automatically when the dependency is eligible or the PR changes readiness/head. Dependency blocks do not create Slack threads. |
| GitHub comment, label, or draft write pending | The supervisor stores the request in `automerge_github_outbox` and retries with backoff, including after the batch ends. Check `last_error` and `next_attempt_at`; repeated failures send a top-level Slack alert except for informational membership/dependency notices. Agents should keep working and never retry these writes with `gh`. |
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
implementation changes. Rerun `scripts/install-mm-web.py` when `mm_web/`,
`web/`, `pyproject.toml` or `uv.lock` change; it rebuilds the UI and restarts
`mm-web.service`. Preserve credentials, SQLite, and Mjolnir session data
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

## Attest shadow test evidence

Attest is a separate Go project at `../attest`. With `attest.toml` available,
ordinary `mm-db run` commands and configured `mm-compare` checks invoke
`attest exec -- COMMAND`, which automatically captures supported native test
calls, including calls inside scripts. Agents do not need to choose a suite.
Runner interception and parsing belong to standalone Attest; Merge Marshall
only links compact results to its execution receipts and supplies storage.
Nextest is the first adapter; other test systems use the same interface.
The explicit `mm-db attest SUITE --filter EXPR` command remains available.
This currently executes all selected tests in shadow
mode; normal assessment and landing policies still apply. Command receipts
retain the compact summary and a pointer to the detailed result.

The skill service proxies storage through the installed `attest store` CLI.
Its default configuration is `~/.config/attest/storage.toml`, overridable with
`ATTEST_STORAGE_CONFIG`; `ATTEST_BIN` overrides `~/.local/bin/attest`. Cloud
credentials stay on the host. CI uses a repository-scoped read-only OIDC role with separate `ATTEST_AWS_*` credentials, preserving existing build-cache credentials. CI readers have List/Get access; the host alone
can publish under `attest/results/v1/` in the CI cache bucket. Results expire
after seven days; the separate versioned binary lives under `agents/attest/`.

Completed master CI and exact-head integration CI upload narrow result
artifacts. The fixer entry point queues trusted completed attempts durably,
then imports one per tick after supervising repairs. Discovery refreshes
today's results and walks older pages across the seven-day evidence retention
window, so a delayed poll does not lose results beyond the newest page.
`attest_ci_imports` shows `pending` until publication succeeds; failed attempts
retry independently. Intake never authorizes or blocks a merge.
Look for `Attest CI evidence import deferred` in monitor logs and inspect
`automerge_executions` summaries for potential reuse, contradictions, and
contract mismatch reasons. Investigate low reuse before changing policy: match
Git trees, suite/build settings, toolchain, environment and publication status,
then separate build/discovery overhead from test execution.

Rollback: use native nextest commands or disable the Attest wrapper. CI's
shadow wrapper falls back to the original command when the tool/configuration
is unavailable or drifts from native coverage, and preserves native exit status
if result observation fails. No ruleset or merge-mode change is part of this
rollout. Material, safe savings authorize a subsequent synchronous merge-mode
integration; do not enable skipping or change landing policy on a small fixture
benchmark alone.
