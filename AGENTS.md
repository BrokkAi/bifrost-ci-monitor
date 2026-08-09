# Bifrost CI monitor — agent guide

This repository is the scheduler, SQLite database, Slack integration, and tests
for the Bifrost CI auto-fixer. See [README.md](README.md) for how it runs and
[MORNING-SETUP.md](MORNING-SETUP.md) for the Slack bot-token setup.

# Git / version control

We commit and push to `origin/master` directly. This repository has no
pull-request flow: commit on `master` and `git push` as part of finishing a
change. This rule also applies when the current branch is `master` (it always
is).

Do not create a branch, change branches, rebase, or open a pull request unless
the user gives an explicit instruction. Do not run `git checkout -b`. The
instruction "commit" means commit on the current branch; it does not mean create
a branch first. This rule overrides other default branch procedures.

Stage and commit only the files you changed. Do not run `git add -A`. Do not
include unrelated working-tree changes in the commit.

# Operational caveat: cron runs this working tree

Cron executes `monitor.py` straight from this working tree every five minutes
(see `crontab -l`). Two consequences follow:

- Keep `monitor.py` runnable at every save. A change you push is live on the
  next tick, and even an uncommitted mid-edit state can be executed by a tick
  that lands while you are editing.
- Any new column on a table the running monitor may already have created needs
  an additive `ensure_column` migration in `connect_db`. `CREATE TABLE IF NOT
  EXISTS` never adds a column to an existing table, so a bare schema change will
  crash the running monitor on the next poll instead of upgrading it.
