# What the Bifrost CI monitor does

MergeMarshall lands changes in `BrokkAi/bifrost-dev`; triage diagnoses master
CI failures; the issue fixer repairs those failures through the same PR queue.
All three share a failure ledger and report work in Slack. For setup, inspection,
and recovery, use the [operator README](../README.md). Implementation notes
for agents maintaining this repository are in [AGENTS.md](../AGENTS.md).

## PR integration and landing

Eligible source PRs are open, ready, and based on master. A rejection applies
to the exact head that failed, so a corrected branch can re-enter the queue.
The optional approved-review policy can require an approval before selection.

For multiple PRs or a branch behind master, one DeepSeek Flash agent combines
source heads into an integration branch, resolves conflicts, and runs affected
checks using Bifrost's repository guidance and ci-impact. It opens one
`mergemarshall-batch` PR with source heads, test evidence, and conflict/fix notes.
Merge commits preserve the source PRs' history. Integration fixes can address
mechanical updates and interactions; a PR broken on its own is returned to
its author with exact-head evidence. Conflicts alone are not rejections.

The supervisor checks source state, exact heads, ancestry and freshness before
posting the App-specific verdict and merging. A moved source head becomes
draft so its author can finish and ready it again. Interrupted/rebuilt batches
can collect new arrivals at most three times; passing finished work is not
rebuilt simply to collect more PRs.

A master advance requires updating and retesting. A confirmed ancestry
mismatch rebuilds from selected source heads; a head independently landed on
master is valid base ancestry. Unavailable GitHub comparison, CI, or baseline
data stays pending with an operator alert and automatic retries.

Two CI modes are available:

| Mode | Landing evidence | What happens afterward |
| --- | --- | --- |
| Async (default) | Targeted local checks; failures must also reproduce at the captured base or be fixed/ejected | GitHub CI runs; new failures enter triage/repair |
| Sync | Exact integration PR CI, green or proven no worse than the exact captured base | At most four CI rounds can repair/rebuild the candidate |

One eligible PR already up to date can land directly without an integration
agent. Async direct landing performs source/freshness checks without a new
local test run; sync also verifies PR CI. An operator fast-track is an explicit
direct async landing. These paths and their tradeoffs are documented in README.

`mergemarshall:high` gives next-batch priority; `mergemarshall:immediate` can
preempt active work before landing. Both select the eligible high/immediate set.
CI repair PRs have no automatic priority. Current master has no special human
review hold for changes to CI workflow/action files.

GitHub verdict/merge write failures retain the open candidate and retry with
persisted backoff instead of losing completed work. Operators can abort a
batch explicitly. Agent sessions remain live across turns, supervisor polls,
and CI waits, with no elapsed-time work cutoff.

## Failure ledger and triage

CI, Hourly CI, and Nightly CI observations are recorded from completed master
runs. Failures are identified by workflow, job, and parsed test or failed step;
later passing observations retire them. The pinned `Known CI failures on master`
issue is a generated summary, not a repair task or an authoritative data source.

Triage groups new observations into an investigation, reads evidence and
repository history without building/fixing, and proposes one `buildfailure`
issue per cause. It searches existing work, reuses/reopens matching issues,
and distinguishes observations already fixed on master. Report publication
and resolved-observation cleanup survive interrupted polls and lost replies.

## Issue-scoped repair

The fixer works on one open, unclaimed failure issue at a time. It respects
other people's assignments and claims, checks relevant open PRs, and claims
its selected issue with a label/comment and service-account assignment when
available. The dossier includes that issue's comments, failure evidence and
diagnosis; a compact PR inventory helps the agent avoid duplicate work.

It chooses a straightforward fix, mechanical test update, or introducing-change
revert. Difficult or irreconcilable work is documented and escalated to a
person rather than expanded into unrelated repairs. Draft `ci-fix` PRs become
ready after validation and updating against master, then enter MergeMarshall.

A rejected owned repair head gets priority for a new repair session on the
existing branch and PR. Each exact rejected head is handled once; a later
rejection can prompt another correction. Other people's assignments still
prevent automatic takeover.

## Shared operation

Each process keeps durable local state and reattaches to active sessions after
a poller restart. Exact-title session discovery prevents duplicate agents
after an ambiguous launch response. Completed work is checkpointed after its
outcome is verified. Prompt limits preserve target evidence while trimming
general inventory.

Host-side GitHub operations use the MergeMarshall App. Agents have narrower
tokens; the supervisor owns the required verdict. The normal master gate has
no bypass actor. Administrators can grant temporary recovery access or remove
the gate independently of Mjolnir; see [emergency merge access](../README.md#emergency-merge-access).

Slack bot transport supplies engagement threads, completed agent messages, and
outcomes; legacy webhooks supply engagement/outcome notices. Delivery problems
are recorded independently of supervision, and repeated blocked notices are
deduplicated. The operator README lists schedules, resource requirements,
commands, data paths, and restoration steps.
