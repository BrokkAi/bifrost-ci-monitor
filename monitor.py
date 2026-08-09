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
import re
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
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
BOT_TOKEN_PATH = CONFIG_DIR / "bot-token"
CHANNEL_PATH = CONFIG_DIR / "channel-id"
CODEX_BIN = Path("/home/jonathan/.nvm/versions/node/v24.15.0/bin/codex")
GH_BIN = Path("/usr/bin/gh")
GIT_BIN = Path("/usr/bin/git")
CODEX_TIMEOUT_SECONDS = 60 * 60
SLACK_TIMEOUT_SECONDS = 10
SLACK_CHAT_URL = "https://slack.com/api/chat.postMessage"
SLACK_MESSAGE_LIMIT = 3500
RED_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}

# People Codex pings on Slack when it escalates a design-level CI failure
# instead of fixing it. These are Slack member IDs (e.g. "U08ABCD1234"), not
# handles: embedded as <@ID> in Codex's message, they render as real,
# notifying mentions because the relay forwards that message via
# chat.postMessage. Replace the placeholders below with the real IDs.
ESCALATION_SLACK_MEMBER_IDS = ("U08P3FAEU3G", "U093T782RTN")  # Jonathan, Dave


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


def migrate_invocations(conn: sqlite3.Connection) -> None:
    """Drop the legacy sha-keyed invocations table so it can be recreated run-keyed.

    The monitor now dedups by CI run id rather than by commit SHA. Older
    databases used ``sha`` as the primary key; recreate them only when empty so
    no recorded repair history is silently discarded.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'invocations'"
    ).fetchone() is None:
        return
    primary_key = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(invocations)").fetchall()
        if row["pk"]
    ]
    if primary_key == ["workflow_run_id"]:
        return
    count = conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
    if count:
        raise RuntimeError(
            "invocations table uses the legacy sha-keyed schema and holds "
            f"{count} rows; migrate or remove it before upgrading"
        )
    conn.execute("DROP TABLE invocations")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column if it is missing (additive, non-destructive migration).

    ``table`` and ``column`` are trusted internal literals, never user input.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    DB_PATH.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate_invocations(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS invocations (
            workflow_run_id INTEGER PRIMARY KEY,
            sha TEXT NOT NULL,
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
            outcome_notification_attempted INTEGER NOT NULL DEFAULT 0,
            thread_ts TEXT
        );

        CREATE TABLE IF NOT EXISTS monitor_events (
            sha TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            details TEXT NOT NULL,
            slack_notification_attempted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (sha, kind)
        );

        CREATE TABLE IF NOT EXISTS escalation_gate (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            sha TEXT NOT NULL,
            signature TEXT NOT NULL DEFAULT '',
            issue_url TEXT,
            thread_ts TEXT,
            opened_at TEXT NOT NULL
        );
        """
    )
    # Additive migration for databases created before threaded Slack replies.
    ensure_column(conn, "invocations", "thread_ts", "TEXT")
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


def validate_bot_token(token: str) -> None:
    if not token.startswith("xoxb-") or len(token) < 20 or any(c.isspace() for c in token):
        raise ValueError("expected a Slack bot token beginning with 'xoxb-'")


def validate_channel(channel: str) -> None:
    if (
        len(channel) < 6
        or channel[0] not in "CGD"
        or not channel.isalnum()
        or not channel.isupper()
    ):
        raise ValueError("expected a Slack channel ID like 'C0123ABCD'")


def _store_secret(path: Path, value: str) -> None:
    """Write ``value`` to ``path`` atomically with mode 0600."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def configure_bot() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_DIR.chmod(0o700)
    token = getpass.getpass("Slack bot token xoxb-... (input hidden): ").strip()
    validate_bot_token(token)
    channel = input("Slack channel ID (e.g. C0123ABCD): ").strip()
    validate_channel(channel)
    _store_secret(BOT_TOKEN_PATH, token)
    _store_secret(CHANNEL_PATH, channel)
    print(
        f"Stored Slack bot token and channel securely under {CONFIG_DIR}. "
        "The monitor will now post threaded messages via chat.postMessage; "
        "the incoming webhook remains as a fallback until you remove it."
    )
    return 0


def _read_owned_secret(path: Path) -> str | None:
    """Return the trimmed contents of a mode-0600 file owned by this user, or None."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError(f"{path} must be owned by this user with mode 0600")
    return path.read_text(encoding="utf-8").strip()


def load_bot_credentials() -> tuple[str, str] | None:
    """Return (token, channel) if both are configured and valid, else None."""
    token = _read_owned_secret(BOT_TOKEN_PATH)
    channel = _read_owned_secret(CHANNEL_PATH)
    if not token or not channel:
        return None
    validate_bot_token(token)
    validate_channel(channel)
    return token, channel


@dataclass(frozen=True)
class SlackTransport:
    kind: str  # "chat" (bot token, supports threading) or "webhook" (legacy)
    webhook: str | None = None
    token: str | None = None
    channel: str | None = None


def load_slack_transport() -> SlackTransport:
    """Prefer the threaded chat.postMessage transport; fall back to the webhook.

    Raises RuntimeError only when neither Slack integration is configured, so the
    monitor behaves exactly as before until a bot token is added.
    """
    credentials = load_bot_credentials()
    if credentials is not None:
        return SlackTransport("chat", token=credentials[0], channel=credentials[1])
    return SlackTransport("webhook", webhook=load_webhook())


def slack_chat_post(
    token: str, channel: str, text: str, thread_ts: str | None = None
) -> tuple[bool, str | None]:
    """Post via chat.postMessage. Returns (ok, message_ts). Fails open like post_slack."""
    body: dict[str, Any] = {"channel": channel, "text": text[:SLACK_MESSAGE_LIMIT]}
    if thread_ts:
        body["thread_ts"] = thread_ts
    request = urllib.request.Request(
        SLACK_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
            "User-Agent": "bifrost-ci-monitor/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=SLACK_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        log(f"Slack chat.postMessage failed: {exc}")
        return False, None
    if not data.get("ok"):
        log(f"Slack chat.postMessage error: {data.get('error')!r}")
        return False, None
    return True, data.get("ts")


def slack_send(
    transport: SlackTransport, text: str, thread_ts: str | None = None
) -> tuple[bool, str | None]:
    """Send one message over the active transport. Returns (ok, ts) — ts is None for webhooks."""
    if transport.kind == "chat":
        return slack_chat_post(transport.token, transport.channel, text, thread_ts)
    return post_slack(transport.webhook, text), None


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
    if run.status != "completed":
        return PollResult(run.status, head_sha, run)
    if run.conclusion in RED_CONCLUSIONS:
        return PollResult("red", head_sha, run)
    return PollResult(f"completed:{run.conclusion}", head_sha, run)


def failing_signature(run: CiRun) -> str:
    """A static fingerprint of what is failing in a CI run.

    Returns the newline-joined, sorted set of ``job ▸ step`` names whose step
    failed (falling back to the job name for a job that failed without a failed
    step). This is a stable identity for a failure: it survives new commits as
    long as the same thing breaks, and it changes when a new job/step starts
    failing — which is exactly the signal the monitor uses to decide whether an
    already-escalated failure is unchanged or something new has appeared.

    Returns '' if the jobs cannot be read; callers treat an empty signature as
    "cannot confirm unchanged" rather than risk a false match.
    """
    try:
        raw = run_command(
            [str(GH_BIN), "run", "view", str(run.run_id), "--repo", REPO_NAME,
             "--json", "jobs"],
            timeout=30,
        )
        jobs = json.loads(raw).get("jobs", [])
    except (CommandError, ValueError, json.JSONDecodeError):
        return ""
    failed: set[str] = set()
    for job in jobs:
        job_name = str(job.get("name", "?"))
        step_failed = False
        for step in job.get("steps") or []:
            if str(step.get("conclusion") or "") in RED_CONCLUSIONS:
                failed.add(f"{job_name} ▸ {step.get('name', '?')}")
                step_failed = True
        if not step_failed and str(job.get("conclusion") or "") in RED_CONCLUSIONS:
            failed.add(job_name)
    return "\n".join(sorted(failed))


def signature_members(signature: str) -> set[str]:
    """The set of failing ``job ▸ step`` entries encoded in a signature string."""
    return {line for line in (signature or "").split("\n") if line}


def invocation_exists(conn: sqlite3.Connection, run_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM invocations WHERE workflow_run_id = ?", (run_id,)
        ).fetchone()
        is not None
    )


def get_escalation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The open design-level escalation, or None if the auto-fixer is un-latched.

    While a row exists a human owns an open failure, so the monitor no longer
    blindly re-runs Codex on every red poll. Instead it checks whether the
    current failing surface is contained in this row's ``signature`` baseline:
    contained means stand down; a new job ▸ step means run one classification
    pass that knows the open issue.
    """
    return conn.execute(
        "SELECT sha, signature, issue_url, thread_ts, opened_at "
        "FROM escalation_gate WHERE id = 1"
    ).fetchone()


def open_escalation(
    conn: sqlite3.Connection,
    sha: str,
    signature: str,
    issue_url: str | None,
    thread_ts: str | None,
) -> None:
    """Latch (or re-point) the auto-fixer after a design-level escalation.

    The row records which failure a human owns — its commit and its "job ▸ step"
    ``signature`` baseline — plus the filed issue and the Slack thread. The
    baseline is what the superset gate tests against: it is set when a design
    issue is filed and grown as same-issue passes absorb new surfaces, so
    re-engagement is suppressed until a genuinely new job ▸ step fails.
    ``clear_escalation`` removes it once CI next goes green.
    """
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO escalation_gate "
            "(id, sha, signature, issue_url, thread_ts, opened_at) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (sha, signature, issue_url, thread_ts, utc_now()),
        )


def clear_escalation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Release the escalation latch, returning the row it cleared, else None."""
    row = get_escalation(conn)
    if row is None:
        return None
    with conn:
        conn.execute("DELETE FROM escalation_gate WHERE id = 1")
    return row


def preflight_worktree() -> str:
    """Fast-forward the repair worktree to current origin/master and return its SHA.

    The repair is no longer pinned to the failing run's commit: Codex works from
    whatever master is now, so this syncs to the live HEAD instead of refusing
    when master has advanced. Returns the synced HEAD SHA.
    """
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
        run_command(
            [str(GIT_BIN), "merge", "--ff-only", f"origin/{BRANCH}"], cwd=WORKTREE, timeout=60
        )
    except CommandError as exc:
        raise PreflightError("sync_failed", str(exc)) from exc
    local_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
    if local_sha != remote_sha:
        raise PreflightError(
            "diverged_worktree", f"local HEAD is {local_sha}, expected origin/{BRANCH} {remote_sha}"
        )
    dirty = run_command(
        [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"], cwd=WORKTREE
    )
    if dirty:
        raise PreflightError("dirty_after_sync", "worktree became dirty while synchronizing")
    return local_sha


def record_preflight_event(
    conn: sqlite3.Connection, transport: SlackTransport, sha: str, kind: str, details: str
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
    slack_send(transport, text)
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
                    workflow_run_id, sha, workflow_run_url, conclusion,
                    observed_at, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'claimed')
                """,
                (run.run_id, run.sha, run.url, run.conclusion, now, now),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def recover_interrupted(conn: sqlite3.Connection, transport: SlackTransport) -> None:
    rows = conn.execute(
        "SELECT workflow_run_id, sha, workflow_run_url, thread_ts FROM invocations "
        "WHERE status IN ('claimed', 'running')"
    ).fetchall()
    for row in rows:
        run_id = int(row["workflow_run_id"])
        sha = str(row["sha"])
        with conn:
            conn.execute(
                """
                UPDATE invocations
                SET status = 'interrupted', finished_at = ?, output = output || ?
                WHERE workflow_run_id = ?
                """,
                (utc_now(), "\nMonitor restarted before Codex completed.\n", run_id),
            )
        slack_send(
            transport,
            f":warning: Bifrost CI auto-fixer for "
            f"<https://github.com/{REPO_NAME}/commit/{sha}|`{sha[:8]}`> "
            f"was interrupted before completion. <{row['workflow_run_url']}|CI run>",
            thread_ts=row["thread_ts"],
        )
        with conn:
            conn.execute(
                "UPDATE invocations SET outcome_notification_attempted = 1 "
                "WHERE workflow_run_id = ?",
                (run_id,),
            )


def build_prompt(run: CiRun, open_issue_url: str | None = None) -> str:
    mentions = " ".join(f"<@{member_id}>" for member_id in ESCALATION_SLACK_MEMBER_IDS)
    open_issue_context = ""
    if open_issue_url:
        open_issue_context = f"""
A design-level escalation is ALREADY OPEN for this CI: {open_issue_url}, and a human is handling it. CI has changed since it was filed, so before doing anything, decide which of these the current red state is:
- The SAME problem already covered by {open_issue_url} (even on a newer commit): make no changes, do not file anything, and do not ping anyone — just exit successfully. Do not re-file or re-notify for a failure a human already owns.
- A NEW mechanical failure layered on top of it (lint/formatting/forgotten-test as defined below): fix and push just that, following the FIX IT YOURSELF path. Do not attempt to resolve {open_issue_url} itself.
- A NEW design-level failure distinct from {open_issue_url}: file a SEPARATE issue and ping, following the ESCALATE path.
"""
    return f"""You are triaging a red CI run for {REPO_NAME}. The monitor observed the failing workflow run {run.url} for master commit {run.sha}.

First, orient. Use `gh` outside your sandbox to read the failing run, the commits after {run.sha}, and the latest CI/check results. The original SHA may no longer be current; do not stop merely because newer commits landed. If a subsequent commit clearly addresses this same failure, make no changes and exit successfully. If the remote master advanced, update the existing worktree to that HEAD before doing anything else. Stay on the existing `{WORKTREE_BRANCH}` branch: never create or switch branches, and never open a pull request.
{open_issue_context}
Your job is NOT to fix every failure. Classify the failure first, then take exactly one of the two paths below.

FIX IT YOURSELF — push directly to master — only when the failure is mechanical and the correct fix is unambiguous:
- lint or formatting violations (spotless, checkstyle, import order, whitespace, and the like);
- a test the author plainly forgot to update after an intentional, correct code change — an assertion trailing a renamed symbol, an updated golden value, a signature the production code deliberately changed — where the production code is right and only the test lagged;
- equally trivial build breakage of that kind (an unused import, a rename applied in one place but not another).
Test your change locally, then `git push origin HEAD:master`. Do not wait for CI to run against your push, then exit successfully.

ESCALATE — do not touch code, do not push — when the failure is a design-level or judgement call: the correct behavior is unclear, the failing test may be asserting intended behavior, or a fix would change product semantics, cross an architectural boundary, or otherwise require a decision that is not yours to make unilaterally. Flaky, infrastructure, or genuinely ambiguous failures escalate too. When in doubt, escalate rather than guess. To escalate:
1. Leave master untouched — make no commits and no pushes.
2. File a GitHub issue on {REPO_NAME} with `gh issue create`. Give it a clear title and a body that includes: the failing run link ({run.url}), the failing job/test, what you found, why this is a design-level call rather than a mechanical fix, and any options you weighed. Note the issue URL that `gh` prints.
3. As your final assistant message — on its own, nothing after it — post to Slack by writing exactly:
   {mentions} Design-level CI failure needs a human call — filed <ISSUE_URL>. <one-sentence summary of the problem>
   Replace <ISSUE_URL> with the URL from step 2 and keep the `{mentions}` tokens verbatim so they render as real mentions. Everything you say streams into the Slack thread, so this message is the ping; do not attempt to call Slack yourself.
Then exit successfully.
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


def extract_agent_text(obj: Any) -> str | None:
    """Return the assistant message text from one codex JSONL event, else None.

    Only assistant messages are surfaced; tool calls, reasoning, and command
    output carry other item types and are deliberately ignored. Tolerant of the
    current ``item.completed`` thread-event schema and older envelopes so a Codex
    upgrade does not silently drop the feed.
    """
    if not isinstance(obj, dict):
        return None
    candidates = []
    if obj.get("type") == "item.completed" and isinstance(obj.get("item"), dict):
        item = obj["item"]
        if item.get("type") == "agent_message":
            candidates.append(item.get("text") or item.get("message"))
    for envelope in (obj.get("msg"), obj.get("payload")):
        if isinstance(envelope, dict) and envelope.get("type") == "agent_message":
            candidates.append(envelope.get("message") or envelope.get("text"))
    for text in candidates:
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def invoke_codex_stream(
    prompt: str, on_message
) -> tuple[str, int | None, bool, str]:
    """Run Codex with --json, invoking ``on_message(text)`` per assistant message.

    Reads stdout as JSONL as it arrives so the Slack thread updates live, while
    still capturing the full transcript for the database and enforcing the same
    one-hour timeout and SIGTERM/SIGKILL escalation as the batch path.
    """
    args = [
        str(CODEX_BIN),
        "exec",
        "-C",
        str(WORKTREE),
        "--json",
        "--sandbox",
        "workspace-write",
        "--color",
        "never",
        "-c",
        "shell_environment_policy.inherit=all",
        "-",
    ]
    stderr_file = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            args,
            cwd=WORKTREE,
            env=child_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            start_new_session=True,
        )
    except OSError as exc:
        stderr_file.close()
        return "spawn_failed", None, False, f"Could not start Codex: {exc}\n"

    def dispatch(raw_line: bytes) -> None:
        line = raw_line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        text = extract_agent_text(obj)
        if text:
            try:
                on_message(text)
            except Exception as exc:  # never let a Slack hiccup break the read loop
                log(f"streamed Slack post failed: {exc}")

    try:
        process.stdin.write(prompt.encode("utf-8"))
        process.stdin.close()
    except OSError:
        pass

    stdout_fd = process.stdout.fileno()
    deadline = time.monotonic() + CODEX_TIMEOUT_SECONDS
    chunks: list[bytes] = []
    buffer = b""
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([stdout_fd], [], [], min(1.0, remaining))
        if not ready:
            continue
        data = os.read(stdout_fd, 65536)
        if not data:
            break  # EOF: Codex closed stdout
        chunks.append(data)
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            dispatch(line)
    if buffer.strip():
        dispatch(buffer)

    if timed_out:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    else:
        process.wait()

    process.stdout.close()
    stderr_file.seek(0)
    stderr_text = stderr_file.read().decode("utf-8", errors="replace")
    stderr_file.close()
    output = b"".join(chunks).decode("utf-8", errors="replace") + stderr_text
    if timed_out:
        output += "\nCodex exceeded the one-hour monitor timeout.\n"
        return "timed_out", process.returncode, True, output
    status = "completed" if process.returncode == 0 else "failed"
    return status, process.returncode, False, output


def format_commit(sha: str) -> str:
    if sha and sha != "unknown":
        return f"<https://github.com/{REPO_NAME}/commit/{sha}|`{sha[:8]}`>"
    return "`unknown`"


def outcome_detail(
    status: str, pushed_sha: str | None, base_sha: str, new_sha: str
) -> str:
    """One sentence describing what happened to master after the repair attempt."""
    if status == "completed" and pushed_sha:
        return f"Pushed {format_commit(pushed_sha)} to fix the problem."
    if status == "completed":
        if new_sha == "unknown":
            return "Codex made no changes; could not read master state."
        if new_sha == base_sha:
            return f"Codex made no changes; master is unchanged at {format_commit(new_sha)}."
        return f"Looks like {format_commit(new_sha)} fixes the problem."
    return f"Remote master is now {format_commit(new_sha)}."


def detect_escalation(
    output: str, exclude_url: str | None = None
) -> tuple[bool, str | None]:
    """Recognize a design-level escalation from Codex's captured output.

    Codex escalates by filing a GitHub issue and pinging the humans, so its
    streamed output carries the ``<@member-id>`` mention tokens — which appear
    on no other path — and, when issue creation succeeded, the filed issue URL.
    Returns ``(escalated, issue_url)``; ``issue_url`` is ``None`` if the ping
    is present but no issue link was found. Callers must gate this on "no push
    happened" so a mechanical fix that merely references an issue is not
    misread as an escalation.

    ``exclude_url`` is the already-open issue a classification pass was told
    about: it may be echoed in the output, so it is discarded when choosing the
    newly filed URL. The last remaining match wins, since Codex prints the URL
    it just created after any it merely referenced.
    """
    text = output or ""
    escalated = any(f"<@{member_id}>" in text for member_id in ESCALATION_SLACK_MEMBER_IDS)
    if not escalated:
        return False, None
    urls = re.findall(
        rf"https://github\.com/{re.escape(REPO_NAME)}/issues/\d+", text
    )
    fresh = [url for url in urls if url != exclude_url]
    candidates = fresh or urls
    return True, (candidates[-1] if candidates else None)


def git_is_ancestor(ancestor: str, ref: str) -> bool:
    """True if ``ancestor`` is an ancestor of (or equal to) ``ref`` in the worktree."""
    result = subprocess.run(
        [str(GIT_BIN), "merge-base", "--is-ancestor", ancestor, ref],
        cwd=WORKTREE,
        env=child_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def resolve_master_outcome(base_sha: str, status: str) -> tuple[str, str | None]:
    """Return (current master SHA or 'unknown', the SHA we pushed or None).

    A push is only claimed when Codex completed, the worktree HEAD advanced past
    the synced base, and that HEAD actually landed on origin/master — so a
    rejected push or a no-op exit is never reported as a fix we pushed.
    """
    try:
        run_command([str(GIT_BIN), "fetch", "origin", BRANCH], cwd=WORKTREE, timeout=180)
        new_sha = run_command([str(GIT_BIN), "rev-parse", f"origin/{BRANCH}"], cwd=WORKTREE)
    except CommandError:
        return "unknown", None
    pushed: str | None = None
    if status == "completed":
        try:
            local = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
        except CommandError:
            local = None
        if local and local != base_sha and git_is_ancestor(local, f"origin/{BRANCH}"):
            pushed = local
    return new_sha, pushed


def run_monitor() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.chmod(0o700)
    lock_handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    try:
        transport = load_slack_transport()
    except (OSError, RuntimeError, ValueError) as exc:
        log(str(exc))
        return 2

    conn = connect_db()
    try:
        recover_interrupted(conn, transport)
        try:
            first = poll_ci()
        except (CommandError, ValueError, json.JSONDecodeError) as exc:
            log(f"CI poll failed: {exc}")
            return 3
        if first.state == "completed:success":
            cleared = clear_escalation(conn)
            if cleared is not None:
                log("CI is green again; re-arming the auto-fixer")
                slack_send(
                    transport,
                    ":white_check_mark: Bifrost CI is green again; the auto-fixer is "
                    "re-armed after the escalation.",
                    thread_ts=cleared["thread_ts"],
                )
            return 0
        if first.state != "red" or first.run is None:
            return 0
        run = first.run

        # If a design-level escalation is open, re-engage Codex only when a NEW
        # failing surface appears — a job ▸ step that is not already part of the
        # escalated baseline. While the current failures stay within that
        # baseline (the same design failure, even across many new commits), the
        # human still owns it and we stand down. A new surface triggers one
        # classification pass that knows the open issue and decides whether this
        # is that same problem, a new mechanical failure, or a new design one.
        latch = get_escalation(conn)
        signature = failing_signature(run)
        open_issue_url: str | None = None
        escalation_thread: str | None = None
        if latch is not None:
            baseline = signature_members(latch["signature"])
            if signature:
                new_surface = signature_members(signature) - baseline
                stand_down = not new_surface
            else:
                # Could not read the failing jobs; fall back to sha so we neither
                # churn on the same commit nor freeze on a genuinely new one.
                new_surface = set()
                stand_down = run.sha == latch["sha"]
            if stand_down:
                log("escalation open and no new failing surface; standing down until CI is green")
                return 0
            open_issue_url = latch["issue_url"]
            escalation_thread = latch["thread_ts"]
            log(f"escalation open but new failing surface appeared ({sorted(new_surface)}); classifying")

        if invocation_exists(conn, run.run_id):
            return 0
        try:
            base_sha = preflight_worktree()
        except (CommandError, PreflightError) as exc:
            kind = exc.kind if isinstance(exc, PreflightError) else "preflight_failed"
            log(f"preflight failed for run {run.run_id}: {exc}")
            record_preflight_event(conn, transport, run.sha, kind, str(exc))
            return 4
        try:
            second = poll_ci()
        except (CommandError, ValueError, json.JSONDecodeError) as exc:
            log(f"final CI poll failed: {exc}")
            return 3
        if (
            second.state != "red"
            or second.run is None
            or second.run.run_id != run.run_id
        ):
            return 0
        if not claim_invocation(conn, run):
            return 0

        if latch is not None:
            # Quiet classification pass: fold it into the existing escalation
            # thread instead of firing a fresh channel-wide red alert.
            thread_ts = escalation_thread
            issue_ref = (
                f"<{open_issue_url}|the open ticket>" if open_issue_url else "the open ticket"
            )
            slack_send(
                transport,
                f":repeat: Bifrost CI changed while {issue_ref} is open "
                f"(now red at "
                f"<https://github.com/{REPO_NAME}/commit/{run.sha}|`{run.sha[:8]}`>); "
                f"re-checking whether this is something new. <{run.url}|CI run>",
                thread_ts=thread_ts,
            )
        else:
            _, thread_ts = slack_send(
                transport,
                f":rotating_light: Bifrost CI is red at "
                f"<https://github.com/{REPO_NAME}/commit/{run.sha}|`{run.sha[:8]}`>. "
                f"Codex auto-fixer engaged. <{run.url}|Open CI run>",
            )
        with conn:
            conn.execute(
                "UPDATE invocations SET status = 'running', start_notification_attempted = 1, "
                "thread_ts = ? WHERE workflow_run_id = ?",
                (thread_ts, run.run_id),
            )

        log(f"launching Codex for red CI at {run.sha[:8]}")
        if transport.kind == "chat" and thread_ts:
            def relay(text: str, _thread_ts: str = thread_ts) -> None:
                slack_send(transport, text, thread_ts=_thread_ts)

            status, exit_code, timed_out, output = invoke_codex_stream(
                build_prompt(run, open_issue_url), relay
            )
        else:
            status, exit_code, timed_out, output = invoke_codex(
                build_prompt(run, open_issue_url)
            )
        with conn:
            conn.execute(
                """
                UPDATE invocations
                SET status = ?, exit_code = ?, timed_out = ?, output = ?, finished_at = ?
                WHERE workflow_run_id = ?
                """,
                (status, exit_code, int(timed_out), output, utc_now(), run.run_id),
            )

        new_sha, pushed_sha = resolve_master_outcome(base_sha, status)
        escalated, issue_url = (False, None)
        if status == "completed" and pushed_sha is None:
            escalated, issue_url = detect_escalation(output, exclude_url=open_issue_url)

        commit_link = (
            f"<https://github.com/{REPO_NAME}/commit/{run.sha}|`{run.sha[:8]}`>"
        )
        if escalated:
            emoji, outcome = ":memo:", "escalated"
            filed = (
                f"filed <{issue_url}|a ticket>" if issue_url else "filed a ticket"
            )
            distinct = (
                " (a new problem, distinct from the one already open)"
                if latch is not None
                else ""
            )
            outcome_line = (
                f"{emoji} Bifrost CI auto-fixer for {commit_link} judged this a "
                f"design-level call and escalated it{distinct}. No fix pushed; {filed} "
                f"with its findings and pinged the team above. <{run.url}|Original CI run>"
            )
        elif latch is not None and status == "completed":
            # A classification pass against an already-open escalation that did
            # not itself escalate: Codex either fixed new mechanical breakage or
            # found nothing new. The design ticket stays open either way.
            if pushed_sha:
                emoji, outcome = ":wrench:", "fixed a new failure"
                detail = (
                    f"Fixed a new mechanical failure and pushed "
                    f"{format_commit(pushed_sha)}; the open design ticket still stands."
                )
            else:
                emoji, outcome = ":repeat:", "re-checked"
                detail = "No new actionable problem; the open design ticket still stands."
            outcome_line = (
                f"{emoji} Bifrost CI auto-fixer for {commit_link}: {detail} "
                f"<{run.url}|CI run>"
            )
        else:
            if status == "completed":
                emoji, outcome = ":white_check_mark:", "finished"
            elif status == "timed_out":
                emoji, outcome = ":hourglass_flowing_sand:", "timed out after one hour"
            elif status == "spawn_failed":
                emoji, outcome = ":x:", "could not start"
            else:
                emoji, outcome = ":x:", f"exited with status {exit_code}"
            outcome_line = (
                f"{emoji} Bifrost CI auto-fixer for {commit_link} {outcome}. "
                f"{outcome_detail(status, pushed_sha, base_sha, new_sha)} "
                f"<{run.url}|Original CI run>"
            )
        slack_send(transport, outcome_line, thread_ts=thread_ts)

        # Latch bookkeeping, so the next poll's superset gate behaves correctly.
        # Only touch the latch on a completed pass; a transient Codex failure
        # must leave the baseline alone so the new surface is retried, not frozen.
        if escalated:
            # New/updated design issue: the current failing set is now the owned
            # baseline, and the ticket pointer moves to the freshly filed issue.
            open_escalation(conn, run.sha, signature, issue_url, thread_ts)
        elif latch is not None and status == "completed":
            if pushed_sha:
                # Fixed new mechanical breakage: leave the design baseline as-is
                # so the fixed surface drops out on its own — and if it recurs, it
                # reads as new again and gets fixed again.
                pass
            else:
                # Stood down on the same design issue: absorb the new surface into
                # the baseline so this now-classified state won't re-trigger.
                merged = "\n".join(
                    sorted(signature_members(latch["signature"]) | signature_members(signature))
                )
                open_escalation(conn, run.sha, merged, latch["issue_url"], thread_ts)
        with conn:
            conn.execute(
                "UPDATE invocations SET outcome_notification_attempted = 1 "
                "WHERE workflow_run_id = ?",
                (run.run_id,),
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
    actions.add_argument("--configure-bot", action="store_true")
    actions.add_argument("--test-slack", action="store_true")
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--init-db", action="store_true")
    return parser.parse_args()


def test_slack() -> int:
    transport = load_slack_transport()
    ok, ts = slack_send(
        transport, ":white_check_mark: Bifrost CI auto-fixer Slack integration test."
    )
    if not ok:
        return 1
    if transport.kind == "chat" and ts:
        slack_send(
            transport,
            "Threaded reply test — the live Codex feed will appear in replies like this.",
            thread_ts=ts,
        )
        print("Sent a threaded test message via chat.postMessage.")
    else:
        print("Sent a test message via the incoming webhook.")
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.configure_slack:
            return configure_slack()
        if args.configure_bot:
            return configure_bot()
        if args.test_slack:
            return test_slack()
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
