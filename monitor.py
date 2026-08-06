#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brokk AI
"""Poll Bifrost CI and launch one Codex repair attempt per failing master SHA."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import getpass
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_NAME = "BrokkAi/bifrost"
WORKFLOW_NAME = "CI"
BRANCH = "master"
WORKTREE_BRANCH = "bifrost-ci"
WORKTREE = Path("/home/jonathan/Projects/bifrost-ci")
DB_PATH = WORKTREE / "activity.db"
STATE_DIR = Path("/home/jonathan/.local/state/bifrost-ci-monitor")
LOCK_PATH = STATE_DIR / "monitor.lock"
CONFIG_DIR = Path("/home/jonathan/.config/bifrost-ci-monitor")
WEBHOOK_PATH = CONFIG_DIR / "slack-webhook-url"
CODEX_BIN = Path("/home/jonathan/.nvm/versions/node/v24.15.0/bin/codex")
GH_BIN = Path("/usr/bin/gh")
GIT_BIN = Path("/usr/bin/git")
CODEX_TIMEOUT_SECONDS = 60 * 60
SLACK_TIMEOUT_SECONDS = 10
RED_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{utc_now()} {message}", file=sys.stderr, flush=True)


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    node_bin = str(CODEX_BIN.parent)
    env.update(
        {
            "HOME": "/home/jonathan",
            "PATH": f"{node_bin}:/usr/local/bin:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "SSH_AUTH_SOCK": "/run/user/1000/openssh_agent",
            "GIT_SSH_COMMAND": "/usr/bin/ssh -o BatchMode=yes",
        }
    )
    return env


class CommandError(RuntimeError):
    pass


class PreflightError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def run_command(
    args: list[str], *, cwd: Path | None = None, timeout: int = 60
) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=child_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError(f"{args[0]} failed to run: {exc}") from exc
    if result.returncode != 0:
        output = result.stdout.strip()
        raise CommandError(
            f"{' '.join(args[:3])} exited {result.returncode}"
            + (f": {output}" if output else "")
        )
    return result.stdout.strip()


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    DB_PATH.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS invocations (
            sha TEXT PRIMARY KEY,
            workflow_run_id INTEGER NOT NULL,
            workflow_run_url TEXT NOT NULL,
            conclusion TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            exit_code INTEGER,
            timed_out INTEGER NOT NULL DEFAULT 0,
            output TEXT NOT NULL DEFAULT '',
            start_notification_attempted INTEGER NOT NULL DEFAULT 0,
            outcome_notification_attempted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS monitor_events (
            sha TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            details TEXT NOT NULL,
            slack_notification_attempted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (sha, kind)
        );
        """
    )
    return conn


def configure_slack() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_DIR.chmod(0o700)
    webhook = getpass.getpass("Slack incoming webhook URL (input hidden): ").strip()
    validate_webhook_url(webhook)
    temporary = WEBHOOK_PATH.with_name(f".{WEBHOOK_PATH.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(webhook)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, WEBHOOK_PATH)
        WEBHOOK_PATH.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    print(f"Stored Slack webhook securely at {WEBHOOK_PATH}")
    return 0


def validate_webhook_url(webhook: str) -> None:
    parsed = urllib.parse.urlsplit(webhook)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "hooks.slack.com"
        or not parsed.path.startswith("/services/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("expected an https://hooks.slack.com/services/... URL")


def load_webhook() -> str:
    try:
        stat = WEBHOOK_PATH.stat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Slack is not configured; run {Path(__file__)} --configure-slack"
        ) from exc
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError(f"{WEBHOOK_PATH} must be owned by this user with mode 0600")
    webhook = WEBHOOK_PATH.read_text(encoding="utf-8").strip()
    validate_webhook_url(webhook)
    return webhook


def post_slack(webhook: str, text: str) -> bool:
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "bifrost-ci-monitor/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=SLACK_TIMEOUT_SECONDS) as response:
            body = response.read(256).decode("utf-8", errors="replace").strip()
            if response.status != 200 or body != "ok":
                log(f"Slack returned HTTP {response.status}: {body!r}")
                return False
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        log(f"Slack notification failed: {exc}")
        return False
    return True


@dataclass(frozen=True)
class CiRun:
    sha: str
    run_id: int
    url: str
    status: str
    conclusion: str


@dataclass(frozen=True)
class PollResult:
    state: str
    head_sha: str
    run: CiRun | None


def poll_ci() -> PollResult:
    head_sha = run_command(
        [str(GH_BIN), "api", f"repos/{REPO_NAME}/commits/{BRANCH}", "--jq", ".sha"],
        timeout=30,
    )
    raw = run_command(
        [
            str(GH_BIN),
            "run",
            "list",
            "--repo",
            REPO_NAME,
            "--workflow",
            WORKFLOW_NAME,
            "--branch",
            BRANCH,
            "--event",
            "push",
            "--limit",
            "1",
            "--json",
            "databaseId,headSha,status,conclusion,url",
        ],
        timeout=30,
    )
    runs: list[dict[str, Any]] = json.loads(raw)
    if not runs:
        return PollResult("no_ci_run", head_sha, None)
    item = runs[0]
    run = CiRun(
        sha=str(item["headSha"]),
        run_id=int(item["databaseId"]),
        url=str(item["url"]),
        status=str(item["status"]),
        conclusion=str(item.get("conclusion") or ""),
    )
    if run.sha != head_sha:
        return PollResult("no_ci_run_for_current_head", head_sha, run)
    if run.status != "completed":
        return PollResult(run.status, head_sha, run)
    if run.conclusion in RED_CONCLUSIONS:
        return PollResult("red", head_sha, run)
    return PollResult(f"completed:{run.conclusion}", head_sha, run)


def invocation_exists(conn: sqlite3.Connection, sha: str) -> bool:
    return conn.execute("SELECT 1 FROM invocations WHERE sha = ?", (sha,)).fetchone() is not None


def preflight_worktree(expected_sha: str) -> None:
    if not WORKTREE.is_dir():
        raise PreflightError("worktree_missing", f"{WORKTREE} does not exist")
    branch = run_command([str(GIT_BIN), "branch", "--show-current"], cwd=WORKTREE)
    if branch != WORKTREE_BRANCH:
        raise PreflightError("wrong_branch", f"expected branch {WORKTREE_BRANCH!r}, found {branch!r}")
    dirty = run_command(
        [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"], cwd=WORKTREE
    )
    if dirty:
        raise PreflightError("dirty_worktree", "dedicated worktree is not clean")
    try:
        run_command([str(GIT_BIN), "fetch", "origin", BRANCH], cwd=WORKTREE, timeout=180)
        remote_sha = run_command([str(GIT_BIN), "rev-parse", f"origin/{BRANCH}"], cwd=WORKTREE)
        if remote_sha != expected_sha:
            raise PreflightError(
                "head_changed", f"origin/{BRANCH} moved from {expected_sha} to {remote_sha}"
            )
        run_command(
            [str(GIT_BIN), "merge", "--ff-only", f"origin/{BRANCH}"], cwd=WORKTREE, timeout=60
        )
    except PreflightError:
        raise
    except CommandError as exc:
        raise PreflightError("sync_failed", str(exc)) from exc
    local_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
    if local_sha != expected_sha:
        raise PreflightError("diverged_worktree", f"local HEAD is {local_sha}, expected {expected_sha}")
    dirty = run_command(
        [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"], cwd=WORKTREE
    )
    if dirty:
        raise PreflightError("dirty_after_sync", "worktree became dirty while synchronizing")


def record_preflight_event(
    conn: sqlite3.Connection, webhook: str, sha: str, kind: str, details: str
) -> None:
    with conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO monitor_events (sha, kind, created_at, details)
            VALUES (?, ?, ?, ?)
            """,
            (sha, kind, utc_now(), details),
        )
    if cursor.rowcount != 1:
        return
    text = (
        f":warning: Bifrost CI auto-fixer could not engage for "
        f"<https://github.com/{REPO_NAME}/commit/{sha}|`{sha[:8]}`> "
        f"on `{socket.gethostname()}`: {details}"
    )
    post_slack(webhook, text)
    with conn:
        conn.execute(
            "UPDATE monitor_events SET slack_notification_attempted = 1 WHERE sha = ? AND kind = ?",
            (sha, kind),
        )


def claim_invocation(conn: sqlite3.Connection, run: CiRun) -> bool:
    now = utc_now()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO invocations (
                    sha, workflow_run_id, workflow_run_url, conclusion,
                    observed_at, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'claimed')
                """,
                (run.sha, run.run_id, run.url, run.conclusion, now, now),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def recover_interrupted(conn: sqlite3.Connection, webhook: str) -> None:
    rows = conn.execute(
        "SELECT sha, workflow_run_url FROM invocations WHERE status IN ('claimed', 'running')"
    ).fetchall()
    for row in rows:
        sha = str(row["sha"])
        with conn:
            conn.execute(
                """
                UPDATE invocations
                SET status = 'interrupted', finished_at = ?, output = output || ?
                WHERE sha = ?
                """,
                (utc_now(), "\nMonitor restarted before Codex completed.\n", sha),
            )
        post_slack(
            webhook,
            f":warning: Bifrost CI auto-fixer for "
            f"<https://github.com/{REPO_NAME}/commit/{sha}|`{sha[:8]}`> "
            f"was interrupted before completion. <{row['workflow_run_url']}|CI run>",
        )
        with conn:
            conn.execute(
                "UPDATE invocations SET outcome_notification_attempted = 1 WHERE sha = ?", (sha,)
            )


def build_prompt(run: CiRun) -> str:
    return f"""You are repairing the CI workflow for {REPO_NAME}. The monitor observed the failing workflow run {run.url} for master commit {run.sha}.

If CI is still red, fix it. Use `gh` outside your sandbox to see it. Test your changes locally, then push, then you're done; do not wait for CI to run against your push.

Before editing, inspect the failing run, the commits after {run.sha}, and the latest CI/check results. The original SHA may no longer be current; do not stop merely because newer commits landed. If a subsequent commit clearly addresses this same failure, make no changes and exit successfully. Otherwise, if the failure remains red or reproducible, continue from the current master HEAD and fix it. If the remote master advanced, update the existing worktree to that HEAD before editing. Stay on the existing `{WORKTREE_BRANCH}` branch: do not create or switch branches and do not open a pull request. When a fix is ready, push the current HEAD directly to master with `git push origin HEAD:master`.
"""


def invoke_codex(prompt: str) -> tuple[str, int | None, bool, str]:
    args = [
        str(CODEX_BIN),
        "exec",
        "-C",
        str(WORKTREE),
        "--sandbox",
        "workspace-write",
        "--color",
        "never",
        "-c",
        "shell_environment_policy.inherit=all",
        "-",
    ]
    try:
        process = subprocess.Popen(
            args,
            cwd=WORKTREE,
            env=child_environment(),
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return "spawn_failed", None, False, f"Could not start Codex: {exc}\n"
    try:
        output, _ = process.communicate(input=prompt, timeout=CODEX_TIMEOUT_SECONDS)
        status = "completed" if process.returncode == 0 else "failed"
        return status, process.returncode, False, output or ""
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
        return (
            "timed_out",
            process.returncode,
            True,
            (output or "") + "\nCodex exceeded the one-hour monitor timeout.\n",
        )


def remote_master_sha() -> str:
    try:
        return run_command(
            [str(GH_BIN), "api", f"repos/{REPO_NAME}/commits/{BRANCH}", "--jq", ".sha"],
            timeout=30,
        )
    except CommandError:
        return "unknown"


def run_monitor() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.chmod(0o700)
    lock_handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    try:
        webhook = load_webhook()
    except (OSError, RuntimeError, ValueError) as exc:
        log(str(exc))
        return 2

    conn = connect_db()
    try:
        recover_interrupted(conn, webhook)
        try:
            first = poll_ci()
        except (CommandError, ValueError, json.JSONDecodeError) as exc:
            log(f"CI poll failed: {exc}")
            return 3
        if first.state != "red" or first.run is None:
            return 0
        run = first.run
        if invocation_exists(conn, run.sha):
            return 0
        try:
            preflight_worktree(run.sha)
        except (CommandError, PreflightError) as exc:
            kind = exc.kind if isinstance(exc, PreflightError) else "preflight_failed"
            log(f"preflight failed for {run.sha[:8]}: {exc}")
            record_preflight_event(conn, webhook, run.sha, kind, str(exc))
            return 4
        try:
            second = poll_ci()
        except (CommandError, ValueError, json.JSONDecodeError) as exc:
            log(f"final CI poll failed: {exc}")
            return 3
        if (
            second.state != "red"
            or second.run is None
            or second.run.sha != run.sha
            or second.run.run_id != run.run_id
        ):
            return 0
        local_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
        if local_sha != run.sha:
            log(f"worktree HEAD moved before claim: {local_sha}")
            return 4
        if not claim_invocation(conn, run):
            return 0

        post_slack(
            webhook,
            f":rotating_light: Bifrost CI is red at "
            f"<https://github.com/{REPO_NAME}/commit/{run.sha}|`{run.sha[:8]}`>. "
            f"Codex auto-fixer engaged on `{socket.gethostname()}`. <{run.url}|Open CI run>",
        )
        with conn:
            conn.execute(
                "UPDATE invocations SET status = 'running', start_notification_attempted = 1 WHERE sha = ?",
                (run.sha,),
            )

        log(f"launching Codex for red CI at {run.sha[:8]}")
        status, exit_code, timed_out, output = invoke_codex(build_prompt(run))
        with conn:
            conn.execute(
                """
                UPDATE invocations
                SET status = ?, exit_code = ?, timed_out = ?, output = ?, finished_at = ?
                WHERE sha = ?
                """,
                (status, exit_code, int(timed_out), output, utc_now(), run.sha),
            )

        new_sha = remote_master_sha()
        if status == "completed":
            emoji, outcome = ":white_check_mark:", "finished"
        elif status == "timed_out":
            emoji, outcome = ":hourglass_flowing_sand:", "timed out after one hour"
        elif status == "spawn_failed":
            emoji, outcome = ":x:", "could not start"
        else:
            emoji, outcome = ":x:", f"exited with status {exit_code}"
        master_text = (
            f"<https://github.com/{REPO_NAME}/commit/{new_sha}|`{new_sha[:8]}`>"
            if new_sha != "unknown"
            else "`unknown`"
        )
        post_slack(
            webhook,
            f"{emoji} Bifrost CI auto-fixer for "
            f"<https://github.com/{REPO_NAME}/commit/{run.sha}|`{run.sha[:8]}`> "
            f"{outcome}; remote master is now {master_text}. It did not wait for new CI. "
            f"<{run.url}|Original CI run>",
        )
        with conn:
            conn.execute(
                "UPDATE invocations SET outcome_notification_attempted = 1 WHERE sha = ?",
                (run.sha,),
            )
        log(f"Codex {outcome} for {run.sha[:8]}")
        return 0 if status == "completed" else 5
    finally:
        conn.close()


def check_only() -> int:
    result = poll_ci()
    print(
        json.dumps(
            {
                "state": result.state,
                "head_sha": result.head_sha,
                "run": (
                    {
                        "sha": result.run.sha,
                        "id": result.run.run_id,
                        "url": result.run.url,
                        "status": result.run.status,
                        "conclusion": result.run.conclusion,
                    }
                    if result.run
                    else None
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--configure-slack", action="store_true")
    actions.add_argument("--test-slack", action="store_true")
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--init-db", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.configure_slack:
            return configure_slack()
        if args.test_slack:
            return 0 if post_slack(
                load_webhook(), ":white_check_mark: Bifrost CI auto-fixer Slack integration test."
            ) else 1
        if args.check:
            return check_only()
        if args.init_db:
            conn = connect_db()
            conn.close()
            print(f"Initialized {DB_PATH}")
            return 0
        return run_monitor()
    except (CommandError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        log(f"fatal: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
