#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brokk AI
"""Poll Bifrost CI workflows and launch one Codex repair attempt per failed run."""

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


REPO_NAME = "BrokkAi/bifrost-dev"
TRACKED_WORKFLOWS: tuple[tuple[str, str | None], ...] = (
    ("CI", "push"),
    ("Hourly CI", None),
    ("Nightly CI", None),
)
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
CODEX_HOME = Path("/home/jonathan/.codex2")
MBX_BIN = Path("/home/jonathan/.local/share/mbx/bin")
GH_BIN = Path("/usr/bin/gh")
GIT_BIN = Path("/usr/bin/git")
CODEX_TIMEOUT_SECONDS = 60 * 60
CODEX_HANDOFF_TIMEOUT_SECONDS = 10 * 60
# Pin the repair model explicitly rather than inheriting ~/.codex/config.toml's
# default, so the monitor's behavior does not silently change when that file is
# edited for interactive use. These flags are spliced into every `codex exec`.
CODEX_MODEL = "gpt-5.6-sol"
CODEX_REASONING_EFFORT = "medium"
CODEX_MODEL_ARGS = [
    "-m",
    CODEX_MODEL,
    "-c",
    f"model_reasoning_effort={CODEX_REASONING_EFFORT}",
]
SLACK_TIMEOUT_SECONDS = 10
SLACK_CHAT_URL = "https://slack.com/api/chat.postMessage"
SLACK_MESSAGE_LIMIT = 3500
RED_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}
RUN_RETRY_SETTLE_SECONDS = 5 * 60
ISSUE_STATE_RETRY_DELAYS = (1, 2)
PUSH_RETRY_DELAYS = (1, 2)
RETRYABLE_INVOCATION_STATUSES = {
    "interrupted",
    "orphaned_candidate",
    "push_failed",
}

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
            "CODEX_HOME": str(CODEX_HOME),
            "PATH": f"{MBX_BIN}:{node_bin}:/usr/local/bin:/usr/bin:/bin",
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


def run_command(args: list[str], *, cwd: Path | None = None, timeout: int = 60) -> str:
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
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'invocations'"
        ).fetchone()
        is None
    ):
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
    existing = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
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
            thread_ts TEXT,
            codex_session_id TEXT,
            issue_url TEXT,
            timeout_handoff_status TEXT,
            codex_pid INTEGER,
            base_sha TEXT,
            candidate_sha TEXT,
            reconcile_round INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 1
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
            last_reported_run_id INTEGER,
            escalated INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL
        );
        """
    )
    # Additive migrations for databases created before a column existed. The
    # cron runs monitor.py straight from the working tree, so an escalation_gate
    # table can predate the signature column (CREATE TABLE IF NOT EXISTS never
    # adds columns to an existing table); without this, get_escalation would
    # crash every red poll on "no such column: signature".
    ensure_column(conn, "invocations", "thread_ts", "TEXT")
    ensure_column(conn, "invocations", "codex_session_id", "TEXT")
    ensure_column(conn, "invocations", "issue_url", "TEXT")
    ensure_column(conn, "invocations", "timeout_handoff_status", "TEXT")
    ensure_column(conn, "invocations", "codex_pid", "INTEGER")
    ensure_column(conn, "invocations", "base_sha", "TEXT")
    ensure_column(conn, "invocations", "candidate_sha", "TEXT")
    ensure_column(conn, "invocations", "reconcile_round", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "invocations", "attempt_count", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "escalation_gate", "signature", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "escalation_gate", "last_reported_run_id", "INTEGER")
    ensure_column(conn, "escalation_gate", "escalated", "INTEGER NOT NULL DEFAULT 0")
    # Backfill: the episode row now exists for any red streak, and ``escalated``
    # (added defaulting to 0) is what distinguishes a human-owned design failure
    # from a routine one. Any pre-existing row that carries a filed issue is an
    # escalation, so mark it. Idempotent, and non-escalated episodes never carry
    # an issue_url, so this only ever matches genuine escalations.
    with conn:
        conn.execute(
            "UPDATE escalation_gate SET escalated = 1 "
            "WHERE issue_url IS NOT NULL AND escalated = 0"
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


def validate_bot_token(token: str) -> None:
    if (
        not token.startswith("xoxb-")
        or len(token) < 20
        or any(c.isspace() for c in token)
    ):
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
    workflow: str
    sha: str
    run_id: int
    url: str
    status: str
    conclusion: str
    created_at: str
    attempt: int
    updated_at: str


@dataclass(frozen=True)
class PollResult:
    state: str
    head_sha: str
    run: CiRun | None


@dataclass(frozen=True)
class PreflightResult:
    base_sha: str
    recovered_tag: str | None = None
    recovered_sha: str | None = None


def poll_ci(
    excluded_run_ids: set[int] | None = None,
    *,
    now: dt.datetime | None = None,
) -> PollResult:
    """Poll the latest run in every tracked workflow and select work to handle.

    A red latest run takes precedence over green or in-progress runs in the
    other workflows after a short settling window. GitHub briefly exposes a
    failed attempt as terminal before RunsOn requests the replacement attempt,
    and both attempts share one workflow run id. Waiting prevents that expected
    recovery gap from launching Codex and filing an infrastructure issue.

    When more than one workflow is red, prefer the newest run that has not
    already been handled; this prevents a persistent failure in one workflow
    from hiding a new failure in another. If all red runs have already been
    handled, still return red so a green workflow cannot incorrectly clear the
    current failure episode.
    """
    head_sha = run_command(
        [str(GH_BIN), "api", f"repos/{REPO_NAME}/commits/{BRANCH}", "--jq", ".sha"],
        timeout=30,
    )
    runs: list[CiRun] = []
    for workflow, event in TRACKED_WORKFLOWS:
        command = [
            str(GH_BIN),
            "run",
            "list",
            "--repo",
            REPO_NAME,
            "--workflow",
            workflow,
            "--branch",
            BRANCH,
            "--limit",
            "1",
            "--json",
            "databaseId,headSha,status,conclusion,url,createdAt,attempt,updatedAt",
        ]
        if event is not None:
            limit_index = command.index("--limit")
            command[limit_index:limit_index] = ["--event", event]
        raw = run_command(command, timeout=30)
        items: list[dict[str, Any]] = json.loads(raw)
        if not items:
            continue
        item = items[0]
        runs.append(
            CiRun(
                workflow=workflow,
                sha=str(item["headSha"]),
                run_id=int(item["databaseId"]),
                url=str(item["url"]),
                status=str(item["status"]),
                conclusion=str(item.get("conclusion") or ""),
                created_at=str(item["createdAt"]),
                attempt=int(item["attempt"]),
                updated_at=str(item["updatedAt"]),
            )
        )
    if not runs:
        return PollResult("no_ci_run", head_sha, None)

    red_runs = [
        run
        for run in runs
        if run.status == "completed" and run.conclusion in RED_CONCLUSIONS
    ]
    poll_time = now or dt.datetime.now(dt.timezone.utc)
    settle_cutoff = poll_time - dt.timedelta(seconds=RUN_RETRY_SETTLE_SECONDS)
    settled_red_runs: list[CiRun] = []
    settling_red_runs: list[CiRun] = []
    for run in red_runs:
        try:
            updated_at = dt.datetime.fromisoformat(
                run.updated_at.replace("Z", "+00:00")
            )
        except ValueError:
            # An unreadable timestamp must not suppress a genuine failure.
            settled_red_runs.append(run)
            continue
        if updated_at <= settle_cutoff:
            settled_red_runs.append(run)
        else:
            settling_red_runs.append(run)

    if settled_red_runs:
        excluded = excluded_run_ids or set()
        unhandled = [run for run in settled_red_runs if run.run_id not in excluded]
        selected = max(unhandled or settled_red_runs, key=lambda run: run.created_at)
        return PollResult("red", head_sha, selected)
    if settling_red_runs:
        selected = max(settling_red_runs, key=lambda run: run.created_at)
        return PollResult("settling", head_sha, selected)

    # Do not clear an episode while any tracked run is still settling. Once all
    # three latest runs are terminal and none is red, a successful push CI run
    # re-arms the monitor. Cancelled scheduled runs are neutral rather than
    # holding an episode open until the next hourly or nightly tick.
    active_runs = [run for run in runs if run.status != "completed"]
    if active_runs:
        selected = max(active_runs, key=lambda run: run.created_at)
        return PollResult(selected.status, head_sha, selected)
    if len(runs) != len(TRACKED_WORKFLOWS):
        return PollResult(
            "incomplete", head_sha, max(runs, key=lambda run: run.created_at)
        )
    primary = next((run for run in runs if run.workflow == "CI"), None)
    if primary is not None and primary.conclusion == "success":
        return PollResult("completed:success", head_sha, primary)
    selected = max(runs, key=lambda run: run.created_at)
    return PollResult(f"completed:{selected.conclusion}", head_sha, selected)


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
            [
                str(GH_BIN),
                "run",
                "view",
                str(run.run_id),
                "--repo",
                REPO_NAME,
                "--json",
                "jobs",
            ],
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
    placeholders = ", ".join("?" for _ in RETRYABLE_INVOCATION_STATUSES)
    return (
        conn.execute(
            f"SELECT 1 FROM invocations WHERE workflow_run_id = ? "
            f"AND status NOT IN ({placeholders})",
            (run_id, *sorted(RETRYABLE_INVOCATION_STATUSES)),
        ).fetchone()
        is not None
    )


def handled_run_ids(conn: sqlite3.Connection) -> set[int]:
    """Return runs already invoked or reported as human-owned repeats."""
    placeholders = ", ".join("?" for _ in RETRYABLE_INVOCATION_STATUSES)
    run_ids = {
        int(row[0])
        for row in conn.execute(
            f"SELECT workflow_run_id FROM invocations "
            f"WHERE status NOT IN ({placeholders})",
            tuple(sorted(RETRYABLE_INVOCATION_STATUSES)),
        ).fetchall()
    }
    episode = get_escalation(conn)
    if episode is not None and episode["last_reported_run_id"] is not None:
        run_ids.add(int(episode["last_reported_run_id"]))
    return run_ids


def get_escalation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The current red episode, or None when CI is not in a tracked red streak.

    The row exists for any red streak (not only escalations). It carries the
    failing-surface ``signature`` baseline, the Slack ``thread_ts`` we are
    reporting the episode in, the newest CI run already announced
    (``last_reported_run_id``), and whether a human owns it (``escalated``).

    A poll whose failing surface is contained in the baseline is a repeat: if
    ``escalated`` the monitor stands down with a threaded note; otherwise it
    re-engages Codex in the same thread. A surface outside the baseline resets
    the episode into a fresh top-level thread.

    Fails open: if the row cannot be read (e.g. a schema drift), it logs and
    returns None so the monitor degrades to normal engagement rather than
    crashing out of every poll and going silent.
    """
    try:
        return conn.execute(
            "SELECT sha, signature, issue_url, thread_ts, last_reported_run_id, "
            "escalated, opened_at FROM escalation_gate WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        log(f"escalation latch unreadable ({exc}); treating as un-latched")
        return None


def github_issue_state(issue_url: str) -> str:
    """Return OPEN or CLOSED for an escalation issue, retrying transient errors.

    Three total attempts (the initial request plus the two delays above) keep a
    momentary GitHub failure from either releasing human-owned work or parking
    the monitor indefinitely. An unexpected response is retried just like a
    command failure because it cannot safely establish ownership.
    """
    attempts = len(ISSUE_STATE_RETRY_DELAYS) + 1
    last_error: CommandError | None = None
    for attempt in range(attempts):
        try:
            state = (
                run_command(
                    [
                        str(GH_BIN),
                        "issue",
                        "view",
                        issue_url,
                        "--json",
                        "state",
                        "--jq",
                        ".state",
                    ],
                    timeout=30,
                )
                .strip()
                .upper()
            )
            if state not in {"OPEN", "CLOSED"}:
                raise CommandError(
                    f"GitHub returned unexpected issue state {state!r} for {issue_url}"
                )
            return state
        except CommandError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            delay = ISSUE_STATE_RETRY_DELAYS[attempt]
            log(
                f"issue-state lookup failed for {issue_url} "
                f"(attempt {attempt + 1}/{attempts}): {exc}; retrying in {delay}s"
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def refresh_escalation_ownership(
    conn: sqlite3.Connection, episode: sqlite3.Row | None
) -> sqlite3.Row | None:
    """Keep an escalated episode only while its linked issue is still open.

    A closed issue no longer represents human ownership, even when CI never
    went green and the next failure occurs in the same broad job and step. A
    legacy or malformed escalated row without a ticket cannot establish live
    ownership either, so it is retired and the current red run is classified
    as a fresh episode.

    Issue lookup failures propagate without changing the database. The caller
    leaves the run unclaimed so the next cron tick can retry safely.
    """
    if episode is None or not episode["escalated"]:
        return episode
    issue_url = episode["issue_url"]
    if not issue_url:
        log("escalated episode has no issue URL; retiring stale ownership")
        clear_escalation(conn)
        return None
    state = github_issue_state(str(issue_url))
    if state == "OPEN":
        return episode
    log(f"escalation issue {issue_url} is closed; retiring stale ownership")
    clear_escalation(conn)
    return None


def open_escalation(
    conn: sqlite3.Connection,
    sha: str,
    signature: str,
    issue_url: str | None,
    thread_ts: str | None,
    last_reported_run_id: int | None = None,
    escalated: bool = False,
) -> None:
    """Open or re-point the current red episode.

    The row records the failing ``signature`` baseline that a repeat is tested
    against, the filed issue (escalations only), and the Slack thread. When
    ``escalated`` a human owns it and repeats stand down; otherwise repeats
    re-engage Codex. It is grown as same-episode passes absorb new surfaces.

    ``thread_ts`` is the current reporting thread: every engaging run re-points
    it at that run's own top-level message so later notes and the green re-arm
    land in the newest thread and never an abandoned one.
    ``last_reported_run_id`` is the newest CI run already announced, so a cron
    tick that re-fires while that same run is still red stays quiet.
    ``clear_escalation`` removes the row once CI next goes green.
    """
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO escalation_gate "
            "(id, sha, signature, issue_url, thread_ts, last_reported_run_id, "
            "escalated, opened_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                sha,
                signature,
                issue_url,
                thread_ts,
                last_reported_run_id,
                int(escalated),
                utc_now(),
            ),
        )


def mark_reported(conn: sqlite3.Connection, run_id: int) -> None:
    """Record the newest CI run the monitor has already announced on the open
    latch, so a later tick that still sees that same red run most-recent stays
    quiet instead of re-posting the same failed build."""
    with conn:
        conn.execute(
            "UPDATE escalation_gate SET last_reported_run_id = ? WHERE id = 1",
            (run_id,),
        )


def clear_escalation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Close the current red episode on green, returning the row it cleared (so
    the caller can announce the recovery) or None if no episode was open."""
    row = get_escalation(conn)
    if row is None:
        return None
    with conn:
        conn.execute("DELETE FROM escalation_gate WHERE id = 1")
    return row


def preserve_commit(tag: str, sha: str) -> None:
    """Create an idempotent local preservation tag for an unpushed commit."""
    try:
        existing = run_command(
            [str(GIT_BIN), "rev-parse", f"refs/tags/{tag}"], cwd=WORKTREE
        )
    except CommandError:
        run_command([str(GIT_BIN), "tag", tag, sha], cwd=WORKTREE)
        return
    if existing != sha:
        raise CommandError(f"local preservation tag {tag!r} points elsewhere")


def preflight_worktree() -> PreflightResult:
    """Synchronize the repair worktree to current origin/master.

    The repair is no longer pinned to the failing run's commit: Codex works from
    whatever master is now. A clean orphaned commit is tagged and retired so a
    fresh triage can decide whether its change is still needed; dirty state is
    never moved automatically.
    """
    if not WORKTREE.is_dir():
        raise PreflightError("worktree_missing", f"{WORKTREE} does not exist")
    branch = run_command([str(GIT_BIN), "branch", "--show-current"], cwd=WORKTREE)
    if branch != WORKTREE_BRANCH:
        raise PreflightError(
            "wrong_branch", f"expected branch {WORKTREE_BRANCH!r}, found {branch!r}"
        )
    dirty = run_command(
        [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"],
        cwd=WORKTREE,
    )
    if dirty:
        raise PreflightError("dirty_worktree", "dedicated worktree is not clean")
    try:
        run_command(
            [str(GIT_BIN), "fetch", "origin", BRANCH], cwd=WORKTREE, timeout=180
        )
        remote_sha = run_command(
            [str(GIT_BIN), "rev-parse", f"origin/{BRANCH}"], cwd=WORKTREE
        )
    except CommandError as exc:
        raise PreflightError("sync_failed", str(exc)) from exc
    local_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
    recovered_tag: str | None = None
    recovered_sha: str | None = None
    try:
        if local_sha != remote_sha:
            if git_is_ancestor(local_sha, f"origin/{BRANCH}"):
                run_command(
                    [str(GIT_BIN), "merge", "--ff-only", f"origin/{BRANCH}"],
                    cwd=WORKTREE,
                    timeout=60,
                )
            else:
                recovered_sha = local_sha
                recovered_tag = f"bifrost-ci-recovery/preflight-{local_sha[:12]}"
                preserve_commit(recovered_tag, local_sha)
                run_command(
                    [str(GIT_BIN), "reset", "--hard", f"origin/{BRANCH}"],
                    cwd=WORKTREE,
                )
    except CommandError as exc:
        raise PreflightError("sync_failed", str(exc)) from exc
    local_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
    if local_sha != remote_sha:
        raise PreflightError(
            "diverged_worktree",
            f"local HEAD is {local_sha}, expected origin/{BRANCH} {remote_sha}",
        )
    dirty = run_command(
        [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"],
        cwd=WORKTREE,
    )
    if dirty:
        raise PreflightError(
            "dirty_after_sync", "worktree became dirty while synchronizing"
        )
    return PreflightResult(local_sha, recovered_tag, recovered_sha)


def record_preflight_event(
    conn: sqlite3.Connection,
    transport: SlackTransport,
    sha: str,
    kind: str,
    details: str,
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


def record_worktree_recovery(
    conn: sqlite3.Connection,
    transport: SlackTransport,
    sha: str,
    recovered_sha: str,
    tag: str,
) -> None:
    kind = f"worktree_recovered:{recovered_sha}"
    details = f"preserved {recovered_sha} as {tag} and reset to origin/{BRANCH}"
    with conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO monitor_events (sha, kind, created_at, details)
            VALUES (?, ?, ?, ?)
            """,
            (sha, kind, utc_now(), details),
        )
        conn.execute(
            """
            UPDATE invocations
            SET status = 'orphaned_candidate', finished_at = ?,
                output = output || ?
            WHERE candidate_sha = ? AND status IN ('completed', 'reconciling', 'running')
            """,
            (utc_now(), f"\nWorktree recovery: {details}.\n", recovered_sha),
        )
    if cursor.rowcount != 1:
        return
    slack_send(
        transport,
        f":warning: Bifrost CI preserved orphaned repair `{recovered_sha[:8]}` "
        f"as `{tag}`, restored `origin/{BRANCH}`, and is retriaging the current "
        f"red run on `{socket.gethostname()}`.",
    )
    with conn:
        conn.execute(
            "UPDATE monitor_events SET slack_notification_attempted = 1 "
            "WHERE sha = ? AND kind = ?",
            (sha, kind),
        )


def claim_invocation(conn: sqlite3.Connection, run: CiRun, base_sha: str) -> bool:
    now = utc_now()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO invocations (
                    workflow_run_id, sha, workflow_run_url, conclusion,
                    observed_at, started_at, status, base_sha
                ) VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)
                """,
                (run.run_id, run.sha, run.url, run.conclusion, now, now, base_sha),
            )
    except sqlite3.IntegrityError:
        placeholders = ", ".join("?" for _ in RETRYABLE_INVOCATION_STATUSES)
        with conn:
            cursor = conn.execute(
                f"""
                UPDATE invocations
                SET sha = ?, workflow_run_url = ?, conclusion = ?, started_at = ?,
                    finished_at = NULL, status = 'claimed', exit_code = NULL,
                    timed_out = 0, output = output || ?,
                    start_notification_attempted = 0,
                    outcome_notification_attempted = 0, codex_session_id = NULL,
                    issue_url = NULL, timeout_handoff_status = NULL, codex_pid = NULL,
                    base_sha = ?, candidate_sha = NULL, reconcile_round = 0,
                    attempt_count = attempt_count + 1
                WHERE workflow_run_id = ? AND status IN ({placeholders})
                """,
                (
                    run.sha,
                    run.url,
                    run.conclusion,
                    now,
                    f"\n--- retry {now} ---\n",
                    base_sha,
                    run.run_id,
                    *sorted(RETRYABLE_INVOCATION_STATUSES),
                ),
            )
        return cursor.rowcount == 1
    return True


def terminate_recorded_codex(pid: int | None) -> bool:
    """Terminate a recorded Codex process group after a monitor restart."""
    if not pid or pid <= 1:
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except FileNotFoundError:
        return True
    except OSError as exc:
        log(f"could not inspect interrupted Codex pid {pid}: {exc}")
        return False
    if b"codex" not in cmdline:
        log(f"refusing to signal reused non-Codex pid {pid}")
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def recover_interrupted(conn: sqlite3.Connection, transport: SlackTransport) -> None:
    rows = conn.execute(
        "SELECT workflow_run_id, sha, workflow_run_url, thread_ts, status, "
        "codex_session_id, codex_pid, candidate_sha FROM invocations "
        "WHERE status IN ('claimed', 'running', 'reconciling', 'handoff_running')"
    ).fetchall()
    for row in rows:
        run_id = int(row["workflow_run_id"])
        sha = str(row["sha"])
        if row["status"] == "handoff_running":
            stopped = terminate_recorded_codex(row["codex_pid"])
            if stopped:
                cleanup_ok, cleanup_detail = recover_timeout_worktree(
                    run_id, sha, row["codex_session_id"]
                )
            else:
                cleanup_ok, cleanup_detail = (
                    False,
                    "could not safely stop recorded Codex process",
                )
            with conn:
                conn.execute(
                    """
                    UPDATE invocations
                    SET status = 'timed_out', timeout_handoff_status = 'failed',
                        codex_pid = NULL, finished_at = ?, output = output || ?
                    WHERE workflow_run_id = ?
                    """,
                    (
                        utc_now(),
                        f"\nMonitor restarted during timeout handoff. {cleanup_detail}.\n",
                        run_id,
                    ),
                )
            mentions = " ".join(
                f"<@{member_id}>" for member_id in ESCALATION_SLACK_MEMBER_IDS
            )
            slack_send(
                transport,
                f"{mentions} Bifrost CI timeout handoff for "
                f"<https://github.com/{REPO_NAME}/commit/{sha}|`{sha[:8]}`> was "
                f"interrupted. Worktree recovery "
                f"{'succeeded' if cleanup_ok else 'failed'}: {cleanup_detail}. "
                f"<{row['workflow_run_url']}|CI run>",
                thread_ts=row["thread_ts"],
            )
            with conn:
                conn.execute(
                    "UPDATE invocations SET outcome_notification_attempted = 1 "
                    "WHERE workflow_run_id = ?",
                    (run_id,),
                )
            continue
        stopped = terminate_recorded_codex(row["codex_pid"])
        candidate_sha = row["candidate_sha"]
        if stopped and row["status"] == "reconciling" and candidate_sha:
            try:
                remote_sha = fetch_remote_sha()
                clean = not worktree_status()
            except CommandError:
                remote_sha = ""
                clean = False
            if clean and git_is_ancestor(str(candidate_sha), f"origin/{BRANCH}"):
                detail = (
                    f"Monitor restarted after push; verified candidate "
                    f"{candidate_sha} on origin/{BRANCH} at {remote_sha}."
                )
                with conn:
                    conn.execute(
                        """
                        UPDATE invocations
                        SET status = 'completed', codex_pid = NULL, finished_at = ?,
                            output = output || ?, outcome_notification_attempted = 1
                        WHERE workflow_run_id = ?
                        """,
                        (utc_now(), f"\n{detail}\n", run_id),
                    )
                slack_send(
                    transport,
                    f":white_check_mark: Bifrost CI auto-fixer for "
                    f"<https://github.com/{REPO_NAME}/commit/{sha}|`{sha[:8]}`> "
                    f"was interrupted after pushing; verified "
                    f"{format_commit(str(candidate_sha))} on master. "
                    f"<{row['workflow_run_url']}|CI run>",
                    thread_ts=row["thread_ts"],
                )
                continue
        if stopped:
            cleanup_ok, cleanup_detail = recover_timeout_worktree(
                run_id, sha, row["codex_session_id"]
            )
        else:
            cleanup_ok, cleanup_detail = (
                False,
                "could not safely stop recorded Codex process",
            )
        retry_status = "orphaned_candidate" if cleanup_ok else "interrupted"
        with conn:
            conn.execute(
                """
                UPDATE invocations
                SET status = ?, codex_pid = NULL, finished_at = ?, output = output || ?
                WHERE workflow_run_id = ?
                """,
                (
                    retry_status,
                    utc_now(),
                    f"\nMonitor restarted before repair reconciliation completed. "
                    f"Worktree recovery: {cleanup_detail}.\n",
                    run_id,
                ),
            )
        slack_send(
            transport,
            f":warning: Bifrost CI auto-fixer for "
            f"<https://github.com/{REPO_NAME}/commit/{sha}|`{sha[:8]}`> "
            f"was interrupted before completion. Worktree recovery "
            f"{'succeeded; the run will be retriaged' if cleanup_ok else 'failed'}: "
            f"{cleanup_detail}. <{row['workflow_run_url']}|CI run>",
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
- A NEW fixable failure layered on top of it (the FIX IT YOURSELF or RESOLVE FROM REPO EVIDENCE categories defined below): fix and commit just that. Do not attempt to resolve {open_issue_url} itself.
- A NEW design-level failure distinct from {open_issue_url}: file a SEPARATE issue and ping, following the ESCALATE path.
"""
    return f"""You are triaging a red CI run for {REPO_NAME}. The monitor observed the failing workflow run {run.url} for master commit {run.sha}.

First, orient. Use `gh` outside your sandbox to read the failing run, the commits after {run.sha}, and the latest CI/check results. The original SHA may no longer be current; do not stop merely because newer commits landed. If a subsequent commit clearly addresses this same failure, make no changes and exit successfully. The monitor synchronized this worktree immediately before launching you and will merge any later master advances after you finish. Stay on the existing `{WORKTREE_BRANCH}` branch: never create or switch branches, never pull or merge remote changes yourself, and never open a pull request.
{open_issue_context}
Your job is NOT to fix every failure, but escalation is the last resort, not the default. Classify EACH failing test independently (a red run often bundles unrelated regressions), then pick ONE overall action for this invocation:
- If ANY failure is fixable under the first two paths below, fix ALL the fixable ones, commit once, and exit successfully — do NOT push, do NOT escalate anything in the same invocation, and do NOT emit the Slack mention tokens. The monitor will merge current master and push your clean commit after you exit; that push triggers a fresh CI run. If the remainder keeps it red, the monitor re-engages you and that later pass escalates with nothing left to fix. If you already diagnosed a remaining design-level failure, summarize the diagnosis in your closing message (plain text, no mentions) so the later pass and the humans can pick it up from the thread.
- Only when NOTHING is fixable, follow the ESCALATE path, covering all the design-level failures in one issue.

Before classifying anything beyond lint/format noise, pin the INTRODUCING commit. The failing run's commit ({run.sha}) is only where CI first observed the failure — the cause usually landed earlier. Run the failing test locally at suspect commits, use `git log -S`/`-p` on the code the test exercises, or bisect with the single failing test (it is cheap: build once per step, run one test). Read the introducing commit's message, diff, and the tests it added or changed — that commit's own intent is the evidence most classifications turn on. Treat "recorded baseline failure" notes in `.agents/plans/` or commit messages as symptoms of an unhandled regression, never as permission to ignore one.

FIX IT YOURSELF — commit for the monitor to push — when the failure is mechanical and the correct fix is unambiguous:
- lint or formatting violations (spotless, checkstyle, import order, whitespace, and the like);
- a test the author plainly forgot to update after an intentional, correct code change — an assertion trailing a renamed symbol, an updated golden value, a signature the production code deliberately changed — where the production code is right and only the test lagged;
- equally trivial build breakage of that kind (an unused import, a rename applied in one place but not another).

RESOLVE FROM REPO EVIDENCE — also commit for the monitor to push — when the failure is a contract regression whose resolution the repository already records. Most "behavior-sensitive" failures are this, not design calls. Two patterns cover nearly all of them:
- The introducing commit DELIBERATELY changed the contract: it re-specified some assertions to the new behavior but missed a sibling test (often the same shape in another language or suite). The decision was already made; bring the lagging test to the same contract the commit's own updated tests express. This is the cross-commit form of the forgotten-test rule above.
- The introducing commit did NOT intend the regression: its message and its own tests are about a narrower case, and nothing it added conflicts with the failing test. The implementation was over-broad or its blast radius unaudited. Repair the production code at the root cause so the failing test AND the introducing commit's tests all pass together — narrow the over-broad condition, split the conflated concern — following the repo's design philosophy in CLAUDE.md (fix root cause, structured solutions, no fallbacks that hide failures).
The acceptance bar for this path: the failing test and every test the introducing commit added or touched pass together, the relevant suites pass, and you weakened no assertion — never delete a check, broaden a tolerance, or loosen an expected value to make a test pass. Meeting that bar IS the evidence the two contracts were compatible and no human decision was needed. If you cannot meet it, the conflict is real: escalate.

Test your change locally, stage only the files you changed, and create a detailed commit on the existing branch. Leave the worktree clean and exit successfully. Do not push: the monitor owns the pull-and-push reconciliation after you exit.

ESCALATE — do not touch code, do not push — only when nothing is fixable and your completed root-cause investigation shows a decision that is not yours to make: the introducing commit's contract and the failing test's contract genuinely cannot both hold; the fix requires choosing semantics with no evidence in the repository (for example how an analysis reports uncertainty, or a public result's meaning); or the fix crosses a versioned schema or architectural boundary (policy evidence schema, RQL schema version, public API semantics). Flaky and infrastructure failures escalate too. Doubt alone is not a reason: doubt before the introducing commit is pinned means investigate more, and only doubt that survives a finished investigation escalates. To escalate:
1. Leave master untouched — make no commits and no pushes.
2. File a GitHub issue on {REPO_NAME} with `gh issue create`. Give it a clear title and a body that includes: the failing run link ({run.url}), the failing job/test, the introducing commit pinned by your bisect with pass/fail confirmed on both sides, the mechanism (what changed in the code, with files and lines), the specific decision a human must make, and the concrete repair options you weighed with their consequences. An escalation must deliver a finished diagnosis, not a symptom report. Note the issue URL that `gh` prints.
3. As your final assistant message — on its own, nothing after it — post to Slack by writing exactly:
   {mentions} Design-level CI failure needs a human call — filed <ISSUE_URL>. <one-sentence summary of the problem>
   Replace <ISSUE_URL> with the URL from step 2 and keep the `{mentions}` tokens verbatim so they render as real mentions. Everything you say streams into the Slack thread, so this message is the ping; do not attempt to call Slack yourself.
Then exit successfully.
"""


def build_merge_conflict_prompt(run: CiRun) -> str:
    return f"""The monitor tried to merge current origin/{BRANCH} into your committed CI repair for {run.url}, and Git left real content conflicts in the existing `{WORKTREE_BRANCH}` worktree.

Resume the repair using the diagnosis and intent already established in this session. Inspect the incoming commits and every unmerged path, resolve the current merge so both the upstream changes and the intended CI fix are preserved, run the relevant tests for the resolution, and commit the merge on the existing branch. Do not abort merely because master advanced. Do not rebase, cherry-pick, switch branches, open a pull request, or push. Leave the worktree clean with no unmerged paths and exit successfully; the monitor will pull again and push.
"""


def build_timeout_handoff_prompt(run: CiRun) -> str:
    mentions = " ".join(f"<@{member_id}>" for member_id in ESCALATION_SLACK_MEMBER_IDS)
    return f"""Since this has taken over one hour, it is time to create a ticket and turn it over to a human for resolution; definitionally this was not as simple as it looked.

Stop the repair now. Do not investigate further, run more tests, edit files, commit, or push. Using only the diagnosis and evidence already present in this session, file a GitHub issue on {REPO_NAME} with `gh issue create`. The issue must include the failing run ({run.url}), the failing jobs and tests, the findings so far, the approach attempted, the unresolved blocker or decision, and a note that the automated attempt timed out with incomplete worktree changes that the monitor will stash. Note the issue URL printed by `gh`.

As your final assistant message, on its own with nothing after it, write exactly:
{mentions} CI repair exceeded the one-hour automation budget — filed <ISSUE_URL>. <one-sentence summary of the unresolved problem>
Replace <ISSUE_URL> with the issue URL. Keep the mention tokens verbatim and do not attempt to call Slack yourself. Then exit.
"""


@dataclass(frozen=True)
class CodexResult:
    status: str
    exit_code: int | None
    timed_out: bool
    output: str
    session_id: str | None


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


def extract_session_id(obj: Any) -> str | None:
    """Return the saved Codex session id from a JSONL event, if present."""
    if not isinstance(obj, dict) or obj.get("type") != "thread.started":
        return None
    session_id = obj.get("thread_id")
    return session_id if isinstance(session_id, str) and session_id.strip() else None


def invoke_codex_stream(
    prompt: str,
    on_message,
    *,
    timeout_seconds: int = CODEX_TIMEOUT_SECONDS,
    resume_session_id: str | None = None,
    on_session=None,
    on_process=None,
) -> CodexResult:
    """Run Codex with --json, invoking ``on_message(text)`` per assistant message.

    Reads stdout as JSONL as it arrives so the Slack thread updates live, while
    still capturing the full transcript for the database and enforcing the
    caller's timeout with SIGTERM/SIGKILL escalation.
    """
    if resume_session_id:
        # exec-resume has its own option parser. Put workspace and sandbox
        # overrides at the CLI root, and pass the follow-up prompt on stdin.
        args = [
            str(CODEX_BIN),
            "-C",
            str(WORKTREE),
            "--sandbox",
            "workspace-write",
            "exec",
            "resume",
            *CODEX_MODEL_ARGS,
            "--json",
            "-c",
            "shell_environment_policy.inherit=all",
            resume_session_id,
            "-",
        ]
    else:
        args = [
            str(CODEX_BIN),
            "exec",
            "-C",
            str(WORKTREE),
            *CODEX_MODEL_ARGS,
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
        return CodexResult(
            "spawn_failed",
            None,
            False,
            f"Could not start Codex: {exc}\n",
            resume_session_id,
        )

    if on_process is not None:
        on_process(process.pid)

    session_id = resume_session_id

    def dispatch(raw_line: bytes) -> None:
        line = raw_line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        nonlocal session_id
        found_session_id = extract_session_id(obj)
        if found_session_id:
            session_id = found_session_id
            if on_session is not None:
                on_session(found_session_id)
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
    deadline = time.monotonic() + timeout_seconds
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
        output += f"\nCodex exceeded the {timeout_seconds}-second monitor timeout.\n"
        return CodexResult("timed_out", process.returncode, True, output, session_id)
    status = "completed" if process.returncode == 0 else "failed"
    return CodexResult(status, process.returncode, False, output, session_id)


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
    escalated = any(
        f"<@{member_id}>" in text for member_id in ESCALATION_SLACK_MEMBER_IDS
    )
    if not escalated:
        return False, None
    return True, find_issue_url(text, exclude_url=exclude_url)


def find_issue_url(output: str, exclude_url: str | None = None) -> str | None:
    """Return the last repository issue URL in output, preferring a new one."""
    urls = re.findall(
        rf"https://github\.com/{re.escape(REPO_NAME)}/issues/\d+", output or ""
    )
    if exclude_url is not None:
        urls = [url for url in urls if url != exclude_url]
    return urls[-1] if urls else None


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


def recover_timeout_worktree(
    run_id: int, sha: str, session_id: str | None
) -> tuple[bool, str]:
    """Preserve an unfinished attempt and restore the branch to remote master.

    Refuse the destructive reset unless every piece of unfinished work has a
    durable Git reference. An active conflicted merge is aborted only after its
    candidate HEAD is known; ordinary changes go into refs/stash, while an
    unpushed commit receives a local recovery tag.
    """
    try:
        branch = run_command([str(GIT_BIN), "branch", "--show-current"], cwd=WORKTREE)
        if branch != WORKTREE_BRANCH:
            return False, f"expected branch {WORKTREE_BRANCH!r}, found {branch!r}"

        merge_aborted = False
        try:
            run_command(
                [str(GIT_BIN), "rev-parse", "--verify", "MERGE_HEAD"], cwd=WORKTREE
            )
        except CommandError:
            pass
        else:
            run_command([str(GIT_BIN), "merge", "--abort"], cwd=WORKTREE)
            merge_aborted = True

        dirty = run_command(
            [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"],
            cwd=WORKTREE,
        )
        stash_label = (
            f"bifrost-ci timeout run {run_id} sha {sha[:8]} "
            f"session {session_id or 'unknown'}"
        )
        stashed = False
        if dirty:
            run_command(
                [
                    str(GIT_BIN),
                    "stash",
                    "push",
                    "--include-untracked",
                    "--message",
                    stash_label,
                ],
                cwd=WORKTREE,
                timeout=180,
            )
            stashed = True
            remaining = run_command(
                [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"],
                cwd=WORKTREE,
            )
            if remaining:
                return False, "git stash left tracked or untracked changes behind"

        run_command(
            [str(GIT_BIN), "fetch", "origin", BRANCH], cwd=WORKTREE, timeout=180
        )
        local_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
        remote_ref = f"origin/{BRANCH}"
        remote_sha = run_command([str(GIT_BIN), "rev-parse", remote_ref], cwd=WORKTREE)
        tagged = False
        if local_sha != remote_sha and not git_is_ancestor(local_sha, remote_ref):
            tag = f"bifrost-ci-recovery/{run_id}-{local_sha[:12]}"
            preserve_commit(tag, local_sha)
            tagged = True

        run_command([str(GIT_BIN), "reset", "--hard", remote_ref], cwd=WORKTREE)
        final_status = run_command(
            [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"],
            cwd=WORKTREE,
        )
        final_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
        if final_status or final_sha != remote_sha:
            return False, "worktree was not clean and synchronized after recovery"
        saved = []
        if merge_aborted:
            saved.append("aborted conflicted merge")
        if stashed:
            saved.append("stashed incomplete edits")
        if tagged:
            saved.append(f"tagged unpushed commit {tag}")
        return True, "; ".join(saved) if saved else "no incomplete edits to stash"
    except CommandError as exc:
        return False, str(exc)


def fetch_remote_sha() -> str:
    """Fetch and return current origin/master."""
    run_command([str(GIT_BIN), "fetch", "origin", BRANCH], cwd=WORKTREE, timeout=180)
    return run_command([str(GIT_BIN), "rev-parse", f"origin/{BRANCH}"], cwd=WORKTREE)


def worktree_status() -> str:
    return run_command(
        [str(GIT_BIN), "status", "--porcelain", "--untracked-files=normal"],
        cwd=WORKTREE,
    )


def unmerged_paths() -> str:
    return run_command(
        [str(GIT_BIN), "diff", "--name-only", "--diff-filter=U"], cwd=WORKTREE
    )


@dataclass(frozen=True)
class RepairResolution:
    result: CodexResult
    new_sha: str
    pushed_sha: str | None
    detail: str = ""
    failure_kind: str | None = None


def reconcile_repair(
    run: CiRun,
    base_sha: str,
    initial: CodexResult,
    relay,
    *,
    deadline: float,
    on_session=None,
    on_process=None,
    on_candidate=None,
) -> RepairResolution:
    """Merge current master into a successful Codex commit and push it.

    Conflict-free pulls and push races are handled entirely by the monitor.
    Only a pull that leaves unmerged paths resumes the exact Codex session.
    """
    if initial.status != "completed" or initial.timed_out:
        return RepairResolution(initial, "unknown", None)

    output = initial.output
    result = initial
    reconcile_round = 0

    while True:
        try:
            branch = run_command(
                [str(GIT_BIN), "branch", "--show-current"], cwd=WORKTREE
            )
            if branch != WORKTREE_BRANCH:
                raise CommandError(
                    f"expected branch {WORKTREE_BRANCH!r}, found {branch!r}"
                )
            dirty = worktree_status()
            if dirty:
                raise CommandError(
                    "Codex reported success but left a dirty or conflicted worktree"
                )
            local_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
        except CommandError as exc:
            failed = CodexResult(
                "failed", result.exit_code, False, output, result.session_id
            )
            return RepairResolution(failed, "unknown", None, str(exc), "repair_failed")

        if local_sha == base_sha:
            try:
                remote_sha = fetch_remote_sha()
            except CommandError as exc:
                failed = CodexResult(
                    "failed", result.exit_code, False, output, result.session_id
                )
                return RepairResolution(
                    failed, "unknown", None, str(exc), "push_failed"
                )
            return RepairResolution(result, remote_sha, None)

        if time.monotonic() >= deadline:
            timed_output = (
                output + "\nRepair deadline expired before reconciliation completed.\n"
            )
            timed = CodexResult(
                "timed_out", result.exit_code, True, timed_output, result.session_id
            )
            return RepairResolution(
                timed,
                "unknown",
                None,
                "repair deadline expired before reconciliation completed",
            )

        if not git_is_ancestor(base_sha, "HEAD"):
            detail = (
                f"local HEAD {local_sha} does not descend from repair base {base_sha}"
            )
            failed = CodexResult(
                "failed", result.exit_code, False, output, result.session_id
            )
            return RepairResolution(failed, "unknown", None, detail, "repair_failed")

        if on_candidate is not None:
            on_candidate(local_sha, reconcile_round)

        try:
            run_command(
                [
                    str(GIT_BIN),
                    "pull",
                    "--no-rebase",
                    "--no-edit",
                    "origin",
                    BRANCH,
                ],
                cwd=WORKTREE,
                timeout=180,
            )
        except CommandError as pull_error:
            try:
                conflicts = unmerged_paths()
            except CommandError:
                conflicts = ""
            if not conflicts:
                failed = CodexResult(
                    "failed", result.exit_code, False, output, result.session_id
                )
                return RepairResolution(
                    failed, "unknown", None, str(pull_error), "push_failed"
                )
            if not result.session_id:
                detail = "merge conflicts require Codex, but no session id was emitted"
                failed = CodexResult(
                    "failed", result.exit_code, False, output, result.session_id
                )
                return RepairResolution(
                    failed, "unknown", None, detail, "repair_failed"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_output = (
                    output + "\nRepair deadline expired during merge reconciliation.\n"
                )
                timed = CodexResult(
                    "timed_out", result.exit_code, True, timed_output, result.session_id
                )
                return RepairResolution(
                    timed,
                    "unknown",
                    None,
                    "repair deadline expired during merge reconciliation",
                )
            reconcile_round += 1
            if on_candidate is not None:
                on_candidate(local_sha, reconcile_round)
            log(
                f"merge round {reconcile_round} left conflicts; resuming Codex session "
                f"{result.session_id}"
            )
            resumed = invoke_codex_stream(
                build_merge_conflict_prompt(run),
                relay,
                timeout_seconds=max(1, int(remaining)),
                resume_session_id=result.session_id,
                on_session=on_session,
                on_process=on_process,
            )
            output += (
                f"\n--- merge conflict round {reconcile_round} ---\n" + resumed.output
            )
            result = CodexResult(
                resumed.status,
                resumed.exit_code,
                resumed.timed_out,
                output,
                resumed.session_id or result.session_id,
            )
            if result.status != "completed" or result.timed_out:
                return RepairResolution(result, "unknown", None)
            continue

        try:
            if worktree_status():
                raise CommandError("git pull succeeded but left a dirty worktree")
            local_sha = run_command([str(GIT_BIN), "rev-parse", "HEAD"], cwd=WORKTREE)
        except CommandError as exc:
            failed = CodexResult(
                "failed", result.exit_code, False, output, result.session_id
            )
            return RepairResolution(failed, "unknown", None, str(exc), "repair_failed")
        if on_candidate is not None:
            on_candidate(local_sha, reconcile_round)

        raced = False
        last_push_error: CommandError | None = None
        for attempt in range(len(PUSH_RETRY_DELAYS) + 1):
            try:
                run_command(
                    [str(GIT_BIN), "push", "origin", "HEAD:master"],
                    cwd=WORKTREE,
                    timeout=180,
                )
            except CommandError as exc:
                last_push_error = exc
            try:
                remote_sha = fetch_remote_sha()
            except CommandError as exc:
                last_push_error = exc
                if attempt < len(PUSH_RETRY_DELAYS):
                    time.sleep(PUSH_RETRY_DELAYS[attempt])
                    continue
                failed = CodexResult(
                    "failed", result.exit_code, False, output, result.session_id
                )
                return RepairResolution(
                    failed, "unknown", None, str(last_push_error), "push_failed"
                )
            if git_is_ancestor(local_sha, f"origin/{BRANCH}"):
                return RepairResolution(result, remote_sha, local_sha)
            if not git_is_ancestor(remote_sha, "HEAD"):
                raced = True
                break
            if attempt < len(PUSH_RETRY_DELAYS):
                time.sleep(PUSH_RETRY_DELAYS[attempt])
                continue
            failed = CodexResult(
                "failed", result.exit_code, False, output, result.session_id
            )
            return RepairResolution(
                failed,
                remote_sha,
                None,
                str(last_push_error or "push was not reflected on origin/master"),
                "push_failed",
            )
        if raced:
            log("origin/master advanced during push; pulling again")
            continue


def resolve_master_outcome(base_sha: str, status: str) -> tuple[str, str | None]:
    """Compatibility helper for non-reconciled and failed attempts."""
    try:
        new_sha = fetch_remote_sha()
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
        excluded_run_ids = handled_run_ids(conn)
        try:
            first = poll_ci(excluded_run_ids)
        except (CommandError, ValueError, json.JSONDecodeError) as exc:
            log(f"CI poll failed: {exc}")
            return 3
        if first.state == "completed:success":
            cleared = clear_escalation(conn)
            if cleared is not None:
                # Green resets the episode: a fresh top-level message (never back
                # in the closing thread), so the next failure opens its own thread.
                log("CI is green again; re-arming the auto-fixer")
                resolved = (
                    " The open escalation is resolved." if cleared["escalated"] else ""
                )
                slack_send(
                    transport,
                    f":white_check_mark: Bifrost CI is green again.{resolved} "
                    "The auto-fixer is re-armed.",
                )
            return 0
        if first.state != "red" or first.run is None:
            return 0
        run = first.run

        # Classify this red poll against the current episode (if any). A poll
        # whose failing surface is contained in the episode baseline is a repeat;
        # a surface outside it, or no episode at all, is a reset that opens a
        # fresh top-level thread. Repeats stay in the episode's thread: an
        # escalated (human-owned) repeat stands down with a note and no Codex; a
        # non-escalated repeat re-engages Codex in that same thread.
        episode = get_escalation(conn)
        try:
            # Human ownership is live only while the linked issue is open. Do
            # this before same-run reporting deduplication so closing a ticket
            # re-engages an already-reported red run on the very next poll.
            episode = refresh_escalation_ownership(conn, episode)
        except CommandError as exc:
            log(f"escalation issue-state lookup failed: {exc}; retrying next tick")
            return 3
        signature = failing_signature(run)
        open_issue_url: str | None = None
        reply_ts: str | None = None  # set => engage in this existing thread
        if episode is not None:
            baseline = signature_members(episode["signature"])
            if signature:
                new_surface = signature_members(signature) - baseline
            elif run.sha == episode["sha"]:
                # Could not read the failing jobs, but it is the same commit the
                # episode already covers: treat as the same surface (a repeat).
                new_surface = set()
            else:
                # Unreadable surface on a new commit: treat as new so we reset and
                # re-classify rather than silently fold it into the episode.
                new_surface = {"<unreadable surface>"}
            if not new_surface:
                # Repeat: same failing set as the current episode.
                if episode["last_reported_run_id"] == run.run_id:
                    # The same red run is still most recent; cron just fired again
                    # over an unchanged failure. Nothing new to report.
                    return 0
                if episode["escalated"]:
                    # A human owns this design failure. Stand down, but report the
                    # new build as a threaded note under the episode's thread.
                    log(
                        "escalation open; new failed build within the owned surface; threading a note"
                    )
                    issue = episode["issue_url"]
                    ticket = f" (<{issue}|open ticket>)" if issue else ""
                    slack_send(
                        transport,
                        f":red_circle: New failed build "
                        f"<https://github.com/{REPO_NAME}/commit/{run.sha}|`{run.sha[:8]}`> — "
                        f"still the failing set a human already owns{ticket}; standing down. "
                        f"<{run.url}|{run.workflow} run>",
                        thread_ts=episode["thread_ts"],
                    )
                    mark_reported(conn, run.run_id)
                    return 0
                # Non-escalated repeat: re-engage Codex, threaded under the episode.
                reply_ts = episode["thread_ts"]
                log(
                    "new failed build within the current set; re-engaging Codex in-thread"
                )
            else:
                # Reset: a surface outside the episode baseline. If the episode is
                # escalated this classification pass knows the open issue.
                open_issue_url = episode["issue_url"] if episode["escalated"] else None
                log(
                    f"new failing surface ({sorted(new_surface)}); resetting the thread and classifying"
                )

        retry_row = conn.execute(
            "SELECT thread_ts, status FROM invocations WHERE workflow_run_id = ?",
            (run.run_id,),
        ).fetchone()
        if invocation_exists(conn, run.run_id):
            return 0
        try:
            preflight = preflight_worktree()
            base_sha = preflight.base_sha
        except (CommandError, PreflightError) as exc:
            kind = exc.kind if isinstance(exc, PreflightError) else "preflight_failed"
            log(f"preflight failed for run {run.run_id}: {exc}")
            record_preflight_event(conn, transport, run.sha, kind, str(exc))
            return 4
        if preflight.recovered_tag and preflight.recovered_sha:
            record_worktree_recovery(
                conn,
                transport,
                run.sha,
                preflight.recovered_sha,
                preflight.recovered_tag,
            )
        try:
            second = poll_ci(excluded_run_ids)
        except (CommandError, ValueError, json.JSONDecodeError) as exc:
            log(f"final CI poll failed: {exc}")
            return 3
        if (
            second.state != "red"
            or second.run is None
            or second.run.run_id != run.run_id
        ):
            return 0
        if not claim_invocation(conn, run, base_sha):
            return 0

        if (
            reply_ts is None
            and retry_row is not None
            and retry_row["status"] in RETRYABLE_INVOCATION_STATUSES
            and retry_row["thread_ts"]
        ):
            reply_ts = str(retry_row["thread_ts"])

        # A reset (new episode or a surface outside the baseline) opens a fresh
        # top-level thread; a non-escalated repeat re-engages in the episode's
        # existing thread (reply_ts). Either way we never post into an abandoned
        # thread, and every same-run re-fire was already dropped above.
        commit_link = (
            f"<https://github.com/{REPO_NAME}/commit/{run.sha}|`{run.sha[:8]}`>"
        )
        if reply_ts:
            thread_ts = reply_ts
            slack_send(
                transport,
                f":rotating_light: New failed build {commit_link} still red on the "
                f"same set; Codex re-engaged. <{run.url}|Open {run.workflow} run>",
                thread_ts=thread_ts,
            )
        else:
            _, thread_ts = slack_send(
                transport,
                f":rotating_light: Bifrost {run.workflow} is red at {commit_link}. "
                f"Codex auto-fixer engaged. <{run.url}|Open {run.workflow} run>",
            )
        with conn:
            conn.execute(
                "UPDATE invocations SET status = 'running', start_notification_attempted = 1, "
                "thread_ts = ? WHERE workflow_run_id = ?",
                (thread_ts, run.run_id),
            )

        def relay(text: str, _thread_ts: str | None = thread_ts) -> None:
            if transport.kind == "chat" and _thread_ts:
                slack_send(transport, text, thread_ts=_thread_ts)

        def record_session(session_id: str) -> None:
            with conn:
                conn.execute(
                    "UPDATE invocations SET codex_session_id = ? WHERE workflow_run_id = ?",
                    (session_id, run.run_id),
                )

        def record_process(pid: int) -> None:
            with conn:
                conn.execute(
                    "UPDATE invocations SET codex_pid = ? WHERE workflow_run_id = ?",
                    (pid, run.run_id),
                )

        def record_candidate(candidate_sha: str, reconcile_round: int) -> None:
            with conn:
                conn.execute(
                    """
                    UPDATE invocations
                    SET status = 'reconciling', candidate_sha = ?, reconcile_round = ?,
                        codex_pid = NULL
                    WHERE workflow_run_id = ?
                    """,
                    (candidate_sha, reconcile_round, run.run_id),
                )

        log(f"launching Codex for red {run.workflow} at {run.sha[:8]}")
        repair_deadline = time.monotonic() + CODEX_TIMEOUT_SECONDS
        result = invoke_codex_stream(
            build_prompt(run, open_issue_url),
            relay,
            timeout_seconds=CODEX_TIMEOUT_SECONDS,
            on_session=record_session,
            on_process=record_process,
        )
        resolution_detail = ""
        failure_kind: str | None = None
        if result.status == "completed" and not result.timed_out:
            resolution = reconcile_repair(
                run,
                base_sha,
                result,
                relay,
                deadline=repair_deadline,
                on_session=record_session,
                on_process=record_process,
                on_candidate=record_candidate,
            )
            result = resolution.result
            new_sha = resolution.new_sha
            pushed_sha = resolution.pushed_sha
            resolution_detail = resolution.detail
            failure_kind = resolution.failure_kind
        else:
            new_sha, pushed_sha = resolve_master_outcome(base_sha, result.status)
        status = result.status
        exit_code = result.exit_code
        output = result.output
        issue_url: str | None = None
        escalated = False
        cleanup_ok = True
        cleanup_detail = ""
        if resolution_detail:
            output += f"\n--- reconciliation ---\n{resolution_detail}\n"

        if result.timed_out:
            # The repair budget is exhausted. Preserve its complete context by
            # resuming the exact saved session for one short, ticket-only turn.
            with conn:
                conn.execute(
                    """
                    UPDATE invocations
                    SET status = 'handoff_running', exit_code = ?, timed_out = 1,
                        output = ?, codex_session_id = ?, timeout_handoff_status = 'running',
                        codex_pid = NULL
                    WHERE workflow_run_id = ?
                    """,
                    (exit_code, output, result.session_id, run.run_id),
                )
            handoff_output = ""
            if result.session_id:
                log(
                    f"repair timed out; resuming Codex session {result.session_id} for handoff"
                )
                handoff = invoke_codex_stream(
                    build_timeout_handoff_prompt(run),
                    relay,
                    timeout_seconds=CODEX_HANDOFF_TIMEOUT_SECONDS,
                    resume_session_id=result.session_id,
                    on_process=record_process,
                )
                handoff_output = handoff.output
                issue_url = find_issue_url(handoff_output, exclude_url=open_issue_url)
            else:
                handoff_output = (
                    "Could not resume timed-out Codex: no session id was emitted.\n"
                )

            cleanup_ok, cleanup_detail = recover_timeout_worktree(
                run.run_id, run.sha, result.session_id
            )
            output = (
                output
                + "\n--- timeout handoff ---\n"
                + handoff_output
                + f"\n--- worktree recovery ---\n{cleanup_detail}\n"
            )
            escalated = issue_url is not None
            handoff_status = "completed" if escalated else "failed"
            with conn:
                conn.execute(
                    """
                    UPDATE invocations
                    SET status = 'timed_out', output = ?, finished_at = ?, issue_url = ?,
                        timeout_handoff_status = ?, codex_pid = NULL
                    WHERE workflow_run_id = ?
                    """,
                    (output, utc_now(), issue_url, handoff_status, run.run_id),
                )
            if escalated:
                mention_text = " ".join(
                    f"<@{member_id}>" for member_id in ESCALATION_SLACK_MEMBER_IDS
                )
                if transport.kind != "chat" or not any(
                    f"<@{member_id}>" in handoff_output
                    for member_id in ESCALATION_SLACK_MEMBER_IDS
                ):
                    slack_send(
                        transport,
                        f"{mention_text} CI repair exceeded the one-hour automation budget — "
                        f"filed <{issue_url}|a ticket> for human resolution.",
                        thread_ts=thread_ts,
                    )
            else:
                mention_text = " ".join(
                    f"<@{member_id}>" for member_id in ESCALATION_SLACK_MEMBER_IDS
                )
                slack_send(
                    transport,
                    f"{mention_text} Bifrost CI repair exceeded one hour, and the "
                    f"ticket handoff failed. Worktree recovery: {cleanup_detail}.",
                    thread_ts=thread_ts,
                )
        else:
            persisted_status = failure_kind or status
            if status != "completed":
                cleanup_ok, cleanup_detail = recover_timeout_worktree(
                    run.run_id, run.sha, result.session_id
                )
                output += f"\n--- worktree recovery ---\n{cleanup_detail}\n"
            with conn:
                conn.execute(
                    """
                    UPDATE invocations
                    SET status = ?, exit_code = ?, timed_out = 0, output = ?,
                        codex_session_id = ?, codex_pid = NULL, finished_at = ?
                    WHERE workflow_run_id = ?
                    """,
                    (
                        persisted_status,
                        exit_code,
                        output,
                        result.session_id,
                        utc_now(),
                        run.run_id,
                    ),
                )

        if not result.timed_out and status == "completed" and pushed_sha is None:
            escalated, issue_url = detect_escalation(output, exclude_url=open_issue_url)
            if escalated and issue_url:
                with conn:
                    conn.execute(
                        "UPDATE invocations SET issue_url = ? WHERE workflow_run_id = ?",
                        (issue_url, run.run_id),
                    )

        if escalated and result.timed_out:
            outcome = "timed out and escalated"
            recovery = (
                f" Worktree recovered: {cleanup_detail}."
                if cleanup_ok
                else f" Worktree recovery failed: {cleanup_detail}."
            )
            outcome_line = (
                f":memo: Bifrost CI auto-fixer for {commit_link} exceeded its one-hour "
                f"budget and filed <{issue_url}|a ticket> for human resolution.{recovery} "
                f"<{run.url}|Original CI run>"
            )
        elif escalated:
            emoji, outcome = ":memo:", "escalated"
            filed = f"filed <{issue_url}|a ticket>" if issue_url else "filed a ticket"
            distinct = (
                " (a new problem, distinct from the one already open)"
                if episode is not None and episode["escalated"]
                else ""
            )
            outcome_line = (
                f"{emoji} Bifrost CI auto-fixer for {commit_link} judged this a "
                f"design-level call and escalated it{distinct}. No fix pushed; {filed} "
                f"with its findings and pinged the team above. <{run.url}|Original CI run>"
            )
        elif failure_kind:
            emoji, outcome = ":x:", "could not publish its repair"
            recovery = (
                f" Worktree recovery: {cleanup_detail}." if cleanup_detail else ""
            )
            outcome_line = (
                f"{emoji} Bifrost CI auto-fixer for {commit_link} {outcome}: "
                f"{resolution_detail or failure_kind}.{recovery} "
                f"<{run.url}|Original CI run>"
            )
        elif episode is not None and episode["escalated"] and status == "completed":
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
                detail = (
                    "No new actionable problem; the open design ticket still stands."
                )
            outcome_line = (
                f"{emoji} Bifrost CI auto-fixer for {commit_link}: {detail} "
                f"<{run.url}|CI run>"
            )
        else:
            if status == "completed":
                emoji, outcome = ":white_check_mark:", "finished"
            elif status == "timed_out":
                emoji, outcome = (
                    ":hourglass_flowing_sand:",
                    "timed out; ticket handoff failed",
                )
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

        # Episode bookkeeping. Every completed pass records an episode so the next
        # poll can tell a repeat (thread) from a new surface (reset); all of them
        # advance the reporting thread to this run's own message and mark it the
        # newest announced. A transient Codex failure (not completed) leaves the
        # episode untouched so the run is retried, not frozen or lost.
        if escalated:
            # Human now owns it: the current failing set is the owned baseline and
            # the ticket pointer moves to the freshly filed issue.
            open_escalation(
                conn,
                run.sha,
                signature,
                issue_url,
                thread_ts,
                run.run_id,
                escalated=True,
            )
        elif status == "completed" and episode is not None and episode["escalated"]:
            # A pass against an already-open escalation that did not re-escalate.
            if pushed_sha:
                # Fixed new mechanical breakage: keep the design baseline and
                # ticket untouched so the fixed surface drops out on its own — and
                # if it recurs, it reads as new again and gets fixed again.
                open_escalation(
                    conn,
                    episode["sha"],
                    episode["signature"],
                    episode["issue_url"],
                    thread_ts,
                    run.run_id,
                    escalated=True,
                )
            else:
                # Stood down on the same design issue: absorb the new surface into
                # the baseline so this now-classified state won't re-trigger.
                merged = "\n".join(
                    sorted(
                        signature_members(episode["signature"])
                        | signature_members(signature)
                    )
                )
                open_escalation(
                    conn,
                    run.sha,
                    merged,
                    episode["issue_url"],
                    thread_ts,
                    run.run_id,
                    escalated=True,
                )
        elif status == "completed":
            # Routine (non-escalated) episode, new or continuing: record the
            # current failing set as the baseline so a repeat threads and a new
            # surface resets. No ticket; a green next poll clears it.
            open_escalation(
                conn, run.sha, signature, None, thread_ts, run.run_id, escalated=False
            )
        with conn:
            conn.execute(
                "UPDATE invocations SET outcome_notification_attempted = 1 "
                "WHERE workflow_run_id = ?",
                (run.run_id,),
            )
        log(f"Codex {outcome} for {run.sha[:8]}")
        return 0 if (status == "completed" or escalated) and cleanup_ok else 5
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
                        "workflow": result.run.workflow,
                        "sha": result.run.sha,
                        "id": result.run.run_id,
                        "url": result.run.url,
                        "status": result.run.status,
                        "conclusion": result.run.conclusion,
                        "attempt": result.run.attempt,
                        "updated_at": result.run.updated_at,
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
