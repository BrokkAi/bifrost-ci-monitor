# `master` ruleset for mergecop

The repository ruleset named "Protect `master`" for `BrokkAi/bifrost-dev` must be
active on `refs/heads/master` with these rules:

- Require changes to land through a pull request, with zero required
  approvals.
- Require the `mergecop/verdict` commit status from the GitHub App
  `mergemarshall` (App ID `5203169`).
- Require the pull request branch to be up to date with `master` before it can
  merge.
- Block force-pushes and branch deletion.
- Configure no bypass actors.

This means people cannot push directly to `master` or self-merge their own
changes. Source PRs, including `ci-fix` PRs, land through the automerge queue's
integration PR. The supervisor posts `mergecop/verdict` only after the
mode-specific quality gate and common pre-merge checks pass, on the exact
integration head that was tested. In `sync` mode this includes the verified PR
CI result; in `async` mode it is the agent's local targeted-test pass, and
GitHub CI runs after merge. Session tokens cannot write this status; the
supervisor uses its GitHub App token.
Do not grant that App `checks:write`: a check run named `mergecop/verdict`
created by the App could also satisfy the required status rule. Grant only the
status-writing permission needed by the supervisor.

The existing ruleset named "Protect `master`" (ID `18574277`) already blocks
deletion and force-push. The script below finds the master-targeting ruleset
that contains both protections, keeps its existing name, and updates it in
place. It refuses to guess if the match is ambiguous or if an unrecognized
ruleset already targets master. It creates a new ruleset named "Protect `master`"
only when no ruleset targets master, and only after explicit confirmation. An
administrator must run it manually with their own authenticated `gh` session:

```sh
bash scripts/apply-mergecop-ruleset.sh --dry-run
bash scripts/apply-mergecop-ruleset.sh
```

Both modes make a read-only API request to identify the target ruleset. The
script prints a plan containing the selected action, ruleset name and ID, and
request body. Normal mode asks for confirmation before any create or update.
The `--dry-run` mode performs no writes. The script never runs automatically,
and `automerge.py` does not invoke it.
