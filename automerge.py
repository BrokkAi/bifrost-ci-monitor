#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brokk AI
"""Batch open Bifrost pull requests through one supervised Mjolnir session."""

from __future__ import annotations

import fcntl
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import monitor


REPO_NAME = monitor.REPO_NAME
BASE_BRANCH = monitor.BRANCH
DB_PATH = monitor.DB_PATH
GH_BIN = monitor.GH_BIN
MJ_BIN = monitor.MJ_BIN
MJ_WORKSPACE = monitor.MJ_WORKSPACE
MJ_TARGET = monitor.MJ_TARGET
MJ_BUNDLE = monitor.MJ_BUNDLE
MJ_MODEL = monitor.MJ_MODEL
MJ_WAIT_POLL_SECONDS = monitor.MJ_WAIT_POLL_SECONDS
SLACK_MESSAGE_LIMIT = monitor.SLACK_MESSAGE_LIMIT

STATE_DIR = Path("/home/jonathan/.local/state/bifrost-ci-automerge")
LOCK_PATH = STATE_DIR / "automerge.lock"
REJECTED_LABEL = "automerge-rejected"
REJECTION_MARKER = re.compile(
    r"(?m)^automerge-rejected-head:\s*([0-9a-f]{40})\s*$", re.IGNORECASE
)
BASELINE_BLOCKED_MARKER = "BLOCKED: BASELINE_UNRESOLVED"
BATCH_TIMEOUT_SECONDS = 2 * 60 * 60
INTERRUPTION_GRACE_SECONDS = 60
AMBIGUOUS_LAUNCH_GRACE_SECONDS = 10 * 60
GH_LOGIN_CACHE: str | None = None


class AutomergeError(RuntimeError):
    """A command or state error that should be reported against a batch."""

    def __init__(self, message: str, *, reason: str = "automerge_failed") -> None:
        super().__init__(message)
        self.reason = reason


class LaunchAttemptError(AutomergeError):
    """A launch failure with explicit evidence about session-list completeness."""

    def __init__(
        self,
        message: str,
        *,
        ambiguous: bool,
        reason: str,
        absence_proven: bool = False,
    ) -> None:
        super().__init__(message, reason=reason)
        self.ambiguous = ambiguous
        self.absence_proven = absence_proven


@dataclass(frozen=True)
class RejectionMarker:
    head_sha: str
    evidence: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    head_sha: str
    url: str

    def as_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "head_sha": self.head_sha,
            "url": self.url,
        }


@dataclass(frozen=True)
class PullRequestOutcome:
    pull: PullRequest
    state: str
    rejected: bool = False
    rejection_evidence: str = ""


@dataclass(frozen=True)
class BatchOutcome:
    merged: tuple[PullRequestOutcome, ...]
    rejected: tuple[PullRequestOutcome, ...]
    pending: tuple[PullRequestOutcome, ...]


def log(message: str) -> None:
    monitor.log(f"automerge: {message}")


def run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Run gh through the monitor's subprocess helper; patched by tests."""
    return monitor.run_command([str(GH_BIN), *args], timeout=timeout)


def gh_json(args: list[str], *, timeout: int = 60) -> Any:
    raw = run_gh(args, timeout=timeout)
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise AutomergeError(f"gh returned invalid JSON for {' '.join(args[:3])}: {exc}",
                             reason="github_invalid_response") from exc


def _paginated_objects(payload: Any, *, context: str) -> list[dict[str, Any]]:
    """Flatten gh api --paginate --slurp pages into API objects."""
    if not isinstance(payload, list):
        raise AutomergeError(f"{context} is not a paginated JSON array",
                             reason="github_invalid_response")
    objects: list[dict[str, Any]] = []
    for page in payload:
        if isinstance(page, list):
            entries = page
        elif isinstance(page, dict):
            # gh may return a single page directly when pagination has one result.
            entries = [page]
        else:
            raise AutomergeError(f"{context} contains an invalid page",
                                 reason="github_invalid_response")
        if any(not isinstance(item, dict) for item in entries):
            raise AutomergeError(f"{context} contains a non-object entry",
                                 reason="github_invalid_response")
        objects.extend(entries)
    return objects


def _labels(pull: dict[str, Any]) -> set[str]:
    labels = pull.get("labels", [])
    if not isinstance(labels, list):
        return set()
    return {
        str(item.get("name", ""))
        for item in labels
        if isinstance(item, dict) and item.get("name")
    }


def github_login() -> str:
    """Return the authenticated GitHub login, cached for this cron process."""
    global GH_LOGIN_CACHE
    if GH_LOGIN_CACHE is None:
        payload = gh_json(["api", "user"], timeout=30)
        login = payload.get("login") if isinstance(payload, dict) else None
        if not isinstance(login, str) or not login:
            raise AutomergeError(
                "gh api user returned no authenticated login",
                reason="github_invalid_response",
            )
        GH_LOGIN_CACHE = login
    return GH_LOGIN_CACHE


def newest_trusted_rejection(
    comments: list[dict[str, Any]], *, login: str | None = None
) -> RejectionMarker | None:
    """Return the newest machine-readable rejection written by the bot identity."""
    trusted_login = login or github_login()
    newest: tuple[tuple[str, int, int], RejectionMarker] | None = None
    for index, comment in enumerate(comments):
        author = comment.get("user")
        author_login = author.get("login") if isinstance(author, dict) else None
        if not isinstance(author_login, str) or author_login.casefold() != trusted_login.casefold():
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        created_at = str(comment.get("created_at") or "")
        comment_id = comment.get("id", index)
        try:
            comment_order = int(comment_id)
        except (TypeError, ValueError):
            comment_order = index
        for match in REJECTION_MARKER.finditer(body):
            key = (created_at, comment_order, match.start())
            marker = RejectionMarker(match.group(1).lower(), body.strip())
            if newest is None or key > newest[0]:
                newest = (key, marker)
    return newest[1] if newest is not None else None


def list_open_pull_requests() -> list[dict[str, Any]]:
    endpoint = (
        f"repos/{REPO_NAME}/pulls?state=open&base={BASE_BRANCH}&per_page=100"
    )
    payload = gh_json(["api", "--paginate", "--slurp", endpoint], timeout=90)
    return _paginated_objects(payload, context="open pull request listing")


def list_pull_comments(number: int) -> list[dict[str, Any]]:
    endpoint = f"repos/{REPO_NAME}/issues/{number}/comments?per_page=100"
    payload = gh_json(["api", "--paginate", "--slurp", endpoint], timeout=90)
    return _paginated_objects(payload, context=f"comments for PR #{number}")


def remove_rejection_label(number: int) -> None:
    run_gh(
        [
            "pr", "edit", str(number), "--repo", REPO_NAME,
            "--remove-label", REJECTED_LABEL,
        ],
        timeout=30,
    )


def select_eligible_pull_requests() -> list[PullRequest]:
    """Return all open, non-draft master PRs not rejected at their current head."""
    eligible: list[PullRequest] = []
    for item in list_open_pull_requests():
        base = item.get("base")
        head = item.get("head")
        base_name = base.get("ref") if isinstance(base, dict) else item.get("baseRefName")
        head_sha = head.get("sha") if isinstance(head, dict) else item.get("headRefOid")
        if (
            str(item.get("state", "open")).lower() != "open"
            or item.get("draft", item.get("isDraft", False))
            or base_name != BASE_BRANCH
        ):
            continue
        try:
            number = int(item["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AutomergeError("GitHub returned a PR without a valid number",
                                 reason="github_invalid_response") from exc
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise AutomergeError(f"GitHub returned an invalid head SHA for PR #{number}",
                                 reason="github_invalid_response")

        labels = _labels(item)
        if REJECTED_LABEL in labels:
            comments = list_pull_comments(number)
            rejected = newest_trusted_rejection(comments)
            current_sha = head_sha.lower()
            if rejected is not None and current_sha == rejected.head_sha:
                continue
            # A newer head clears the recorded rejection. A malformed label
            # without a matching machine-readable comment is not a rejection.
            remove_rejection_label(number)

        eligible.append(
            PullRequest(
                number=number,
                title=str(item.get("title") or f"PR #{number}"),
                head_sha=head_sha.lower(),
                url=str(item.get("html_url") or f"https://github.com/{REPO_NAME}/pull/{number}"),
            )
        )
    return sorted(eligible, key=lambda pull: pull.number)


def current_master_sha() -> str:
    payload = gh_json(["api", f"repos/{REPO_NAME}/commits/{BASE_BRANCH}"], timeout=30)
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise AutomergeError("GitHub returned an invalid master SHA",
                             reason="github_invalid_response")
    return sha.lower()


def ensure_column(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    existing = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def connect_db() -> sqlite3.Connection:
    """Create the automerge-owned tables additively in the monitor's DB."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        DB_PATH.chmod(0o600)
    except OSError:
        conn.close()
        raise
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS automerge_batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            pull_requests_json TEXT NOT NULL,
            title TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            launch_attempted INTEGER NOT NULL DEFAULT 0,
            launch_attempted_at TEXT,
            session_id TEXT,
            thread_ts TEXT,
            start_notification_sent INTEGER NOT NULL DEFAULT 0,
            transcript_after_seq INTEGER NOT NULL DEFAULT 0,
            terminal_status TEXT,
            agent_transcript TEXT NOT NULL DEFAULT '',
            agent_final_message TEXT NOT NULL DEFAULT '',
            suspend_pending INTEGER NOT NULL DEFAULT 0,
            suspend_verify_failures INTEGER NOT NULL DEFAULT 0,
            outcome_posted INTEGER NOT NULL DEFAULT 0,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS automerge_relayed_messages (
            batch_id TEXT NOT NULL,
            stable_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            PRIMARY KEY (batch_id, stable_id)
        );

        CREATE TABLE IF NOT EXISTS automerge_blocked_notifications (
            batch_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            details TEXT NOT NULL,
            slack_notification_attempted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (batch_id, reason)
        );
        """
    )
    ensure_column(conn, "automerge_batches", "launch_attempted_at", "TEXT")
    ensure_column(conn, "automerge_batches", "agent_final_message", "TEXT NOT NULL DEFAULT ''")
    return conn


def utc_now() -> str:
    return monitor.utc_now()


def _new_batch_id() -> str:
    return uuid.uuid4().hex


def create_batch(
    conn: sqlite3.Connection,
    pulls: list[PullRequest],
    base_sha: str,
    *,
    batch_id: str | None = None,
) -> str:
    if not pulls:
        raise ValueError("cannot create an empty automerge batch")
    identifier = batch_id or _new_batch_id()
    title = f"Bifrost automerge batch {identifier}"
    branch = f"automerge/{identifier}"
    with conn:
        conn.execute(
            """
            INSERT INTO automerge_batches
                (batch_id, status, base_sha, pull_requests_json, title, branch, created_at)
            VALUES (?, 'launching', ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                base_sha,
                json.dumps([pull.as_json() for pull in pulls]),
                title,
                branch,
                utc_now(),
            ),
        )
    return identifier


def row_pulls(row: sqlite3.Row | dict[str, Any]) -> list[PullRequest]:
    try:
        data = json.loads(str(row["pull_requests_json"]))
        if not isinstance(data, list):
            raise TypeError("pull_requests_json is not a list")
        pulls = [
            PullRequest(
                number=int(item["number"]),
                title=str(item["title"]),
                head_sha=str(item["head_sha"]),
                url=str(item["url"]),
            )
            for item in data
            if isinstance(item, dict)
        ]
        if len(pulls) != len(data):
            raise TypeError("pull_requests_json contains a non-object")
        return pulls
    except (ValueError, TypeError, KeyError) as exc:
        raise AutomergeError(f"stored PR list is invalid: {exc}",
                             reason="database_state_invalid") from exc


def build_prompt(batch_id: str, pulls: list[PullRequest], base_sha: str) -> str:
    pr_list = "\n".join(
        f"{index}. PR #{pull.number}: {pull.title}\n"
        f"   Expected full head SHA: {pull.head_sha}\n"
        f"   URL: {pull.url}"
        for index, pull in enumerate(pulls, start=1)
    )
    return f"""You are landing one batch of pull requests for {REPO_NAME}. This batch is {batch_id}; your branch is automerge/{batch_id}, based at the exact master commit {base_sha}. Do not create or switch branches, and do not push any branch except by the publication command below.

Process these PRs in the order listed:
{pr_list}

For each PR, fetch its head with `git fetch origin pull/<N>/head`. Verify the fetched commit is the listed full head SHA before merging. If the fetched SHA differs, do not merge or reject that PR: remove it from this batch, rebuild from the original {base_sha} using only the remaining listed heads, rerun the full suite, and recheck the remaining PRs. The changed PR stays eligible for a later batch; never substitute a newer or older head into this tested tree. If no PRs remain, do not push. Merge each expected head into this batch branch with a merge commit (no squash and no rebase), so GitHub can recognize the PR as merged when its commits reach master. Resolve every conflict yourself. Never reject or send a PR back because it conflicts: read the PR description and commits, preserve both sides' intent, and finish the merge. Every commit you create, including each merge commit and any conflict-resolution commit, must carry the trailer `Automerge-Batch: {batch_id}`.

After all listed PRs are merged into the batch branch, run the full Bifrost test suite once. To find the exact build and test commands, read this repository's root CLAUDE.md and AGENTS.md and inspect the CI workflow files under `.github/workflows`; follow the commands the Bifrost CI uses.

The test baseline is this batch's exact base SHA ({base_sha}), never the latest master or its CI result. When tests fail on the batch, run those same failing tests at {base_sha} in a separate worktree or checkout to classify baseline failures. A failure reproduced at {base_sha} is not evidence against a PR. If the base cannot build or the tests cannot be run there, the baseline is unknown: if the batch's full suite passes, continue toward publication; if the batch's full suite fails, reject nothing and publish nothing. End with a clear report beginning with the standalone marker `{BASELINE_BLOCKED_MARKER}`, naming the failing tests and why the baseline could not be established. The scheduler will post that report to Slack as a blocked batch.

If the full suite fails and the baseline is established, identify the responsible PR or PRs using the failing tests and commits. Split the batch as needed: rebuild from its original base without the suspect PRs, then rerun the full suite on the remaining batch. Reject only the exact listed head SHA that was tested and shown to cause a failure. For every rejected PR, record the rejection on GitHub by adding label `{REJECTED_LABEL}` and posting a comment that includes this exact standalone machine-readable line with that tested head SHA (never a newer head):
`automerge-rejected-head: <full sha>`
The comment must also state the failing tests and concrete evidence showing why that PR caused them. A rejection applies only to that exact head SHA.

Publication gate: immediately before every push, recheck every PR still included in the candidate tree with `gh pr view <N> --json state,headRefOid,baseRefName,isDraft`. Each must still have state `OPEN`, baseRefName `master`, isDraft `false`, and a current `headRefOid` equal to the exact listed SHA merged and tested in this batch. If any PR is closed, is a draft, targets another base, or its head changed, remove it from the candidate: rebuild the branch from the original {base_sha} with only the remaining unchanged, open, non-draft master PRs, then rerun the full suite and recheck every remaining PR. Repeat until all included PRs pass the gate. A changed PR is not rejected; leave its new head eligible for a later batch. Never publish a tree containing a PR head other than the one tested. If no PRs remain, do not push an unchanged branch.

When a non-empty remaining batch passes the full suite and the publication gate, push with exactly `git push origin HEAD:master`. Never force-push. If GitHub rejects the push as non-fast-forward, fetch origin, merge `origin/master` into your branch with a merge commit carrying `Automerge-Batch: {batch_id}`, resolve conflicts while preserving both sides' intent, rerun the full suite, repeat the publication gate, and retry `git push origin HEAD:master`. Never squash or rebase the PR merges.

After pushing, check GitHub and confirm every included PR now shows as merged. Comment on any PR that does not show as merged, describing what remains. Your final assistant message must be a plain-text summary listing landed PRs, rejected PRs with reasons, and baseline failures separately. For an unknown baseline with a failing batch, begin with `{BASELINE_BLOCKED_MARKER}` and clearly state that nothing was rejected or pushed. Do not use Markdown tables.
"""


def new_session_argv(
    row: sqlite3.Row | dict[str, Any], prompt_file: str
) -> list[str]:
    return [
        "new",
        "--workspace", MJ_WORKSPACE,
        "--target", MJ_TARGET,
        "--bundle", MJ_BUNDLE,
        "--model", MJ_MODEL,
        "--subagents", "none",
        "--at", str(row["base_sha"]),
        "--branch", str(row["branch"]),
        "--title", str(row["title"]),
        "--prompt-file", prompt_file,
        "--json",
    ]


def lookup_batch_session(row: sqlite3.Row | dict[str, Any]) -> str | None:
    raw = monitor.require_mj_success(
        ["sessions", "--workspace", MJ_WORKSPACE, "--json"], timeout=30
    )
    try:
        payload = json.loads(raw)
        sessions = payload.get("sessions", []) if isinstance(payload, dict) else payload
        if not isinstance(sessions, list):
            raise TypeError("sessions is not a list")
        matches = [
            session
            for session in sessions
            if isinstance(session, dict) and session.get("title") == row["title"]
        ]
    except (ValueError, TypeError, AttributeError) as exc:
        raise monitor.MjError(f"mj sessions returned invalid workspace JSON: {exc}") from exc
    if not matches:
        return None
    matches.sort(
        key=lambda session: (
            bool(session.get("active")),
            str(session.get("updated_at", "")),
            str(session.get("id", "")),
        ),
        reverse=True,
    )
    session_id = matches[0].get("id")
    if not isinstance(session_id, str) or not session_id:
        raise monitor.MjError("matching Mjolnir batch session has no id")
    return session_id


def launch_batch_session(
    row: sqlite3.Row | dict[str, Any],
    pulls: list[PullRequest],
    *,
    conn: sqlite3.Connection | None = None,
    allow_new: bool = True,
) -> str:
    """Launch once and adopt only after an exact-title workspace lookup."""
    try:
        existing = lookup_batch_session(row)
    except monitor.MjError as exc:
        raise LaunchAttemptError(
            f"could not look up batch launch identity: {exc}",
            ambiguous=True,
            reason="mj_session_lookup_failed",
            absence_proven=False,
        ) from exc
    if existing:
        return existing
    if not allow_new:
        raise LaunchAttemptError(
            f"No Mjolnir session is visible yet for persisted batch launch "
            f"{row['title']!r}; keeping the launch identity unresolved",
            ambiguous=True,
            reason="mj_launch_ambiguous",
            absence_proven=True,
        )
    if conn is not None:
        # Persist the intent before mj new. After a crash, an empty session list
        # is not proof that the daemon did not accept the launch.
        with conn:
            conn.execute(
                "UPDATE automerge_batches SET launch_attempted = 1, "
                "launch_attempted_at = ? WHERE batch_id = ?",
                (utc_now(), row["batch_id"]),
            )
    prompt = build_prompt(str(row["batch_id"]), pulls, str(row["base_sha"]))
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="bifrost-automerge-",
        suffix=".prompt", delete=False,
    ) as handle:
        handle.write(prompt)
        prompt_path = handle.name
    launch_error: monitor.MjError | None = None
    try:
        result = monitor.mj_command(new_session_argv(row, prompt_path), timeout=180)
    except monitor.MjError as exc:
        launch_error = exc
        result = None
    finally:
        Path(prompt_path).unlink(missing_ok=True)

    launch_reason = "mj_launch_ambiguous"
    if result is not None:
        try:
            payload = json.loads(result.stdout or "")
            session_id = payload.get("session_id") if isinstance(payload, dict) else None
        except (ValueError, TypeError):
            session_id = None
        if result.returncode == 0 and isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
        response = monitor.mj_output(result)
        launch_error = monitor.MjError(
            f"mj new exited {result.returncode} or returned no session_id: "
            f"{response or 'no response'}",
            reason=(
                "mj_new_failed"
                if result.returncode != 0 and response.strip()
                else "mj_launch_ambiguous"
            ),
        )
        launch_reason = launch_error.reason
    elif launch_error is not None:
        launch_error = monitor.MjError(
            f"mj new did not return a response: {launch_error}",
            reason="mj_launch_ambiguous",
        )

    # Even a non-zero response can race with session creation. Check the exact
    # launch title, then retain the batch for the grace interval in case the
    # workspace listing is briefly stale.
    try:
        existing = lookup_batch_session(row)
    except monitor.MjError as lookup_error:
        raise LaunchAttemptError(
            f"mj new failed and its session could not be looked up: {lookup_error}",
            ambiguous=True,
            reason="mj_session_lookup_failed",
            absence_proven=False,
        ) from launch_error
    if existing:
        return existing
    raise LaunchAttemptError(
        f"mj new failed; no matching session is visible yet: {launch_error}",
        ambiguous=True,
        reason=launch_reason,
        absence_proven=True,
    ) from launch_error


def send_start_notification(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    row: sqlite3.Row | dict[str, Any],
) -> None:
    if row["start_notification_sent"]:
        return
    pulls = row_pulls(row)
    details = ", ".join(f"#{pull.number} {pull.title}" for pull in pulls)
    ok, thread_ts = monitor.slack_send(
        transport,
        f":arrows_counterclockwise: Bifrost automerge batch {row['batch_id']} "
        f"starting from {row['base_sha'][:8]} with {len(pulls)} PRs: {details}",
    )
    if ok:
        with conn:
            conn.execute(
                "UPDATE automerge_batches SET start_notification_sent = 1, thread_ts = ? "
                "WHERE batch_id = ?",
                (thread_ts, row["batch_id"]),
            )


def notify_blocked_once(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    batch_id: str,
    reason: str,
    details: str,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO automerge_blocked_notifications
                (batch_id, reason, created_at, details)
            VALUES (?, ?, ?, ?)
            """,
            (batch_id, reason, utc_now(), details),
        )
        notice = conn.execute(
            "SELECT slack_notification_attempted FROM automerge_blocked_notifications "
            "WHERE batch_id = ? AND reason = ?",
            (batch_id, reason),
        ).fetchone()
    if notice is None or notice["slack_notification_attempted"]:
        return
    deliver_blocked_notice(conn, transport, batch_id, reason)


def deliver_blocked_notice(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    batch_id: str,
    reason: str,
) -> bool:
    notice = conn.execute(
        "SELECT details, slack_notification_attempted FROM automerge_blocked_notifications "
        "WHERE batch_id = ? AND reason = ?",
        (batch_id, reason),
    ).fetchone()
    if notice is None or notice["slack_notification_attempted"]:
        return True
    batch = conn.execute(
        "SELECT thread_ts FROM automerge_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    try:
        ok, _ = monitor.slack_send(
            transport,
            f":warning: Bifrost automerge batch {batch_id} ({reason}): {notice['details']}",
            thread_ts=batch["thread_ts"] if batch else None,
        )
    except Exception as exc:
        log(f"Slack blocked notification failed for batch {batch_id}: {exc}")
        return False
    with conn:
        conn.execute(
            "UPDATE automerge_blocked_notifications SET slack_notification_attempted = ? "
            "WHERE batch_id = ? AND reason = ?",
            (int(ok), batch_id, reason),
        )
    return ok


def retry_pending_notifications(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
) -> None:
    """Retry unsent batch notices after Slack recovers, without duplicating accepted posts."""
    rows = conn.execute(
        "SELECT batch_id, reason FROM automerge_blocked_notifications "
        "WHERE slack_notification_attempted = 0 ORDER BY created_at"
    ).fetchall()
    for row in rows:
        deliver_blocked_notice(
            conn, transport, str(row["batch_id"]), str(row["reason"])
        )
    rows = conn.execute(
        "SELECT * FROM automerge_batches WHERE start_notification_sent = 0 "
        "ORDER BY created_at"
    ).fetchall()
    for row in rows:
        try:
            send_start_notification(conn, transport, row)
        except Exception as exc:
            log(f"Slack start notification failed for batch {row['batch_id']}: {exc}")


def _read_transcript_page(session_id: str, cursor: int) -> tuple[list[dict[str, Any]], int]:
    result = monitor.mj_command(
        [
            "transcript", "--session", session_id, "--finished-only",
            "--after-seq", str(cursor), "--json",
        ],
        timeout=60,
    )
    if result.returncode != 0:
        detail = monitor.mj_output(result)
        reason = "daemon_unreachable" if monitor.looks_like_daemon_failure(detail) else "mj_supervision_failed"
        raise monitor.MjError(f"mj transcript failed: {detail}", reason=reason)
    try:
        page = json.loads(result.stdout or "")
        items = page.get("items", [])
        next_cursor = int(page.get("next_after_seq", cursor))
        if not isinstance(items, list):
            raise TypeError("items is not a list")
        return items, next_cursor
    except (ValueError, TypeError, AttributeError) as exc:
        raise monitor.MjError(f"mj transcript returned invalid JSON: {exc}") from exc


def drain_transcript(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    batch_id: str,
    session_id: str,
) -> list[str]:
    row = conn.execute(
        "SELECT transcript_after_seq, thread_ts FROM automerge_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise monitor.MjError(f"automerge batch {batch_id} disappeared")
    cursor = int(row["transcript_after_seq"] or 0)
    items, next_cursor = _read_transcript_page(session_id, cursor)
    texts: list[str] = []
    processed_cursor = cursor
    all_processed = True
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            seq = int(item.get("seq", 0))
        except (ValueError, TypeError):
            continue
        text = item.get("text")
        if seq <= cursor or not isinstance(text, str) or not text.strip():
            continue
        stable_id = str(item.get("stable_id") or f"seq:{seq}")
        already_sent = conn.execute(
            "SELECT 1 FROM automerge_relayed_messages "
            "WHERE batch_id = ? AND stable_id = ?",
            (batch_id, stable_id),
        ).fetchone()
        if already_sent:
            processed_cursor = max(processed_cursor, seq)
            continue
        item_text = text.strip()
        if not monitor.relay_text(transport, row["thread_ts"], item_text):
            all_processed = False
            processed_cursor = min(processed_cursor, seq - 1)
            break
        texts.append(item_text)
        processed_cursor = max(processed_cursor, seq)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO automerge_relayed_messages "
                "(batch_id, stable_id, seq) VALUES (?, ?, ?)",
                (batch_id, stable_id, seq),
            )
    if all_processed:
        processed_cursor = max(processed_cursor, next_cursor)
    with conn:
        conn.execute(
            "UPDATE automerge_batches SET transcript_after_seq = ? WHERE batch_id = ?",
            (processed_cursor, batch_id),
        )
    return texts


def supervise_turn(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    batch_id: str,
    session_id: str,
    timeout_seconds: int,
    *,
    first_wait: monitor.TurnResult | None = None,
) -> monitor.TurnResult:
    if first_wait is not None and not first_wait.timed_out:
        drain_transcript(conn, transport, batch_id, session_id)
        return first_wait
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return monitor.TurnResult("running", "timeout", timed_out=True)
        wait_seconds = min(MJ_WAIT_POLL_SECONDS, max(1, int(remaining)))
        turn = monitor.wait_once(session_id, wait_seconds)
        drain_transcript(conn, transport, batch_id, session_id)
        if not turn.timed_out:
            return turn


def interrupt_and_wait(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    batch_id: str,
    session_id: str,
    *,
    grace_seconds: int,
) -> monitor.TurnResult:
    result = monitor.mj_command(
        ["interrupt-turn", "--session", session_id, "--json"], timeout=60
    )
    if result.returncode != 0:
        detail = monitor.mj_output(result)
        already_ended = any(
            marker in detail.lower()
            for marker in ("no active turn", "nothing is running", "turn is not running")
        )
        if not already_ended:
            reason = "daemon_unreachable" if monitor.looks_like_daemon_failure(detail) else "mj_supervision_failed"
            raise monitor.MjError(f"mj interrupt-turn failed: {detail}", reason=reason)
    deadline = time.monotonic() + grace_seconds
    while True:
        turn = monitor.wait_once(session_id, MJ_WAIT_POLL_SECONDS)
        drain_transcript(conn, transport, batch_id, session_id)
        if not turn.timed_out:
            return turn
        if time.monotonic() >= deadline:
            raise monitor.MjError(
                f"session {session_id} did not end after interrupt-turn",
                reason="mj_supervision_failed",
            )


def request_suspend(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    batch_id: str,
    session_id: str,
) -> bool:
    with conn:
        conn.execute(
            "UPDATE automerge_batches SET suspend_pending = 1 WHERE batch_id = ?",
            (batch_id,),
        )
    try:
        result = monitor.mj_command(
            ["suspend", "--session", session_id, "--json"], timeout=60
        )
        warning = monitor.suspend_response_warning(result)
        if warning:
            log(f"Mjolnir suspend warning for batch {batch_id}: {warning}")
        detail = monitor.mj_output(result)
        if result.returncode != 0 and "acknowledge-unpublished-work" in detail:
            result = monitor.mj_command(
                [
                    "suspend", "--session", session_id,
                    "--acknowledge-unpublished-work", "--json",
                ],
                timeout=60,
            )
            detail = monitor.mj_output(result)
        if result.returncode == 0:
            return True
    except monitor.MjError as exc:
        detail = str(exc)
    notify_blocked_once(conn, transport, batch_id, "mj_suspend_failed", detail)
    return False


def check_pending_suspensions(
    conn: sqlite3.Connection, transport: monitor.SlackTransport
) -> None:
    rows = conn.execute(
        "SELECT batch_id, session_id, suspend_verify_failures FROM automerge_batches "
        "WHERE suspend_pending = 1 AND session_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        batch_id, session_id = str(row["batch_id"]), str(row["session_id"])
        try:
            raw = monitor.require_mj_success(
                ["sessions", "--session", session_id, "--json"], timeout=30
            )
            session = json.loads(raw)
            if not isinstance(session, dict):
                raise monitor.MjError("mj sessions returned an unexpected response")
            if monitor.session_is_stopped(session):
                with conn:
                    conn.execute(
                        "UPDATE automerge_batches SET suspend_pending = 0, "
                        "suspend_verify_failures = 0 WHERE batch_id = ?",
                        (batch_id,),
                    )
                continue
            failures = int(row["suspend_verify_failures"] or 0) + 1
            with conn:
                conn.execute(
                    "UPDATE automerge_batches SET suspend_verify_failures = ? "
                    "WHERE batch_id = ?",
                    (failures, batch_id),
                )
            if failures >= 3:
                notify_blocked_once(
                    conn,
                    transport,
                    batch_id,
                    "mj_suspend_not_stopped",
                    f"session {session_id} still reports state {session.get('state')!r}",
                )
                request_suspend(conn, transport, batch_id, session_id)
        except (monitor.MjError, ValueError) as exc:
            failures = int(row["suspend_verify_failures"] or 0) + 1
            with conn:
                conn.execute(
                    "UPDATE automerge_batches SET suspend_verify_failures = ? "
                    "WHERE batch_id = ?",
                    (failures, batch_id),
                )
            if failures >= 3:
                notify_blocked_once(
                    conn, transport, batch_id, "mj_suspend_verify_failed", str(exc)
                )


def _current_batch_pulls(conn: sqlite3.Connection, batch_id: str) -> list[PullRequest]:
    row = conn.execute(
        "SELECT pull_requests_json FROM automerge_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise AutomergeError(f"automerge batch {batch_id} does not exist",
                             reason="database_state_invalid")
    return row_pulls(row)


def detect_batch_outcomes(
    pulls: list[PullRequest],
) -> BatchOutcome:
    merged: list[PullRequestOutcome] = []
    rejected: list[PullRequestOutcome] = []
    pending: list[PullRequestOutcome] = []
    for pull in pulls:
        detail = gh_json(["api", f"repos/{REPO_NAME}/pulls/{pull.number}"])
        if not isinstance(detail, dict):
            raise AutomergeError(f"GitHub returned invalid state for PR #{pull.number}",
                                 reason="github_invalid_response")
        state = str(detail.get("state", "unknown")).lower()
        if detail.get("merged_at") or detail.get("merged") is True:
            merged.append(PullRequestOutcome(pull, "merged"))
            continue
        labels = _labels(detail)
        head = detail.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else detail.get("headRefOid")
        rejection: RejectionMarker | None = None
        if REJECTED_LABEL in labels and isinstance(head_sha, str):
            comments = list_pull_comments(pull.number)
            marker = newest_trusted_rejection(comments)
            if marker is not None and marker.head_sha == head_sha.lower():
                rejection = marker
        if rejection is not None:
            rejected.append(
                PullRequestOutcome(pull, state, True, rejection.evidence)
            )
        else:
            pending.append(PullRequestOutcome(pull, state))
    return BatchOutcome(tuple(merged), tuple(rejected), tuple(pending))


def format_batch_outcome(
    batch_id: str,
    terminal_status: str,
    outcome: BatchOutcome,
    *,
    base_sha: str = "",
    agent_transcript: str = "",
    baseline_marker_note: str = "",
) -> str:
    if terminal_status == "blocked_baseline":
        lines = [
            f":no_entry: Bifrost automerge batch {batch_id} BLOCKED: baseline at "
            f"{base_sha} could not be established. No PR was rejected and nothing was pushed.",
            "Agent report:",
            agent_transcript.strip()[:1200] or "Baseline validation failed; see the session transcript.",
        ]
    else:
        lines = [f"Bifrost automerge batch {batch_id} finished ({terminal_status})."]
    if baseline_marker_note:
        lines.append(baseline_marker_note)
    lines.append("Landed (GitHub confirms merged):")
    lines.extend(
        f"• PR #{item.pull.number} {item.pull.title}"
        for item in outcome.merged
    )
    if not outcome.merged:
        lines.append("• None")
    lines.append("Rejected at the current head:")
    for item in outcome.rejected:
        evidence = item.rejection_evidence.strip()
        evidence = REJECTION_MARKER.sub("", evidence).strip()
        lines.append(f"• PR #{item.pull.number} {item.pull.title}: {evidence or 'see GitHub rejection comment'}")
    if not outcome.rejected:
        lines.append("• None")
    lines.append("Still open or not rejected at its current head:")
    lines.extend(
        f"• PR #{item.pull.number} {item.pull.title} (GitHub state: {item.state})"
        for item in outcome.pending
    )
    if not outcome.pending:
        lines.append("• None")
    return "\n".join(lines)[:SLACK_MESSAGE_LIMIT]


def finish_batch(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    row: sqlite3.Row | dict[str, Any],
) -> None:
    batch_id = str(row["batch_id"])
    try:
        outcome = detect_batch_outcomes(row_pulls(row))
    except (AutomergeError, monitor.CommandError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, AutomergeError) else "github_outcome_failed"
        notify_blocked_once(conn, transport, batch_id, reason, str(exc))
        return

    transcript = str(row["agent_transcript"] or "")
    final_message = str(row["agent_final_message"] or "")
    marker_seen = BASELINE_BLOCKED_MARKER in transcript or BASELINE_BLOCKED_MARKER in final_message
    final_marker = _has_final_baseline_marker(final_message)
    has_github_outcome = bool(outcome.merged or outcome.rejected)
    baseline_blocked = final_marker and not has_github_outcome
    baseline_marker_note = ""
    if baseline_blocked:
        notify_blocked_once(
            conn,
            transport,
            batch_id,
            "baseline_unresolved",
            final_message.strip()[:1200]
            or f"The full suite failed, but baseline {row['base_sha']} could not be established.",
        )
    elif marker_seen and has_github_outcome:
        baseline_marker_note = (
            "Baseline-blocked marker is inconsistent with GitHub: GitHub confirms a PR "
            "landed or was rejected, so the GitHub outcome is reported."
        )
    elif marker_seen:
        baseline_marker_note = (
            "Ignored baseline-blocked marker because it was not the standalone first line "
            "of the final agent message."
        )

    terminal_status = str(row["terminal_status"] or "completed")
    if baseline_blocked:
        terminal_status = "blocked_baseline"
    elif terminal_status == "blocked_baseline":
        # Older rows may have been marked blocked from a transcript substring;
        # the current final message and GitHub state are authoritative.
        terminal_status = "completed"
    summary = format_batch_outcome(
        batch_id,
        terminal_status,
        outcome,
        base_sha=str(row["base_sha"]),
        agent_transcript=final_message or transcript,
        baseline_marker_note=baseline_marker_note,
    )
    ok, _ = monitor.slack_send(transport, summary, thread_ts=row["thread_ts"])
    if not ok:
        notify_blocked_once(
            conn, transport, batch_id, "slack_outcome_failed",
            "The GitHub outcome summary could not be delivered; it will be retried.",
        )
        return
    with conn:
        conn.execute(
            "UPDATE automerge_batches SET status = 'completed', outcome_posted = 1, "
            "terminal_status = ?, finished_at = ? WHERE batch_id = ?",
            (terminal_status, utc_now(), batch_id),
        )


def _session_status(session_id: str) -> dict[str, Any]:
    raw = monitor.require_mj_success(
        ["sessions", "--session", session_id, "--json"], timeout=30
    )
    try:
        session = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise monitor.MjError(f"mj sessions returned invalid JSON: {exc}") from exc
    if not isinstance(session, dict):
        raise monitor.MjError("mj sessions returned an unexpected response")
    return session


def _has_final_baseline_marker(final_message: str) -> bool:
    """Accept the blocked marker only as the exact first line of the final item."""
    return bool(
        re.match(
            rf"\A{re.escape(BASELINE_BLOCKED_MARKER)}(?:\r?\n|\Z)",
            final_message,
        )
    )


def read_final_agent_message(session_id: str) -> str:
    """Read the last agent transcript item, preserving message boundaries."""
    cursor = 0
    order = 0
    latest_messages: dict[str, tuple[int, int, str]] = {}
    for _ in range(10_000):
        result = monitor.mj_command(
            [
                "transcript", "--session", session_id, "--role", "agent",
                "--after-seq", str(cursor), "--json",
            ],
            timeout=60,
        )
        if result.returncode != 0:
            detail = monitor.mj_output(result)
            reason = (
                "daemon_unreachable"
                if monitor.looks_like_daemon_failure(detail)
                else "mj_supervision_failed"
            )
            raise monitor.MjError(f"mj final transcript read failed: {detail}", reason=reason)
        try:
            page = json.loads(result.stdout or "")
            if not isinstance(page, dict) or not isinstance(page.get("items", []), list):
                raise TypeError("transcript page has no item list")
            items = page.get("items", [])
            next_cursor = int(page.get("next_after_seq", cursor))
            latest_seq = int(page.get("latest_seq", next_cursor))
        except (ValueError, TypeError, AttributeError) as exc:
            raise monitor.MjError(
                f"mj final transcript returned invalid JSON: {monitor.mj_output(result)}"
            ) from exc
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                seq = int(item.get("seq", next_cursor))
            except (TypeError, ValueError):
                seq = next_cursor
            stable_id = str(item.get("stable_id") or f"seq:{seq}")
            previous = latest_messages.get(stable_id)
            if previous is None or seq >= previous[0]:
                latest_messages[stable_id] = (seq, order, text.strip())
            order += 1
        if next_cursor >= latest_seq:
            break
        if next_cursor <= cursor:
            raise monitor.MjError(
                f"mj final transcript pagination stopped at sequence {cursor} "
                f"before latest sequence {latest_seq}"
            )
        cursor = next_cursor
    else:
        raise monitor.MjError("mj final transcript exceeded the 10,000-page safety limit")
    if not latest_messages:
        return ""
    return max(latest_messages.values(), key=lambda item: (item[0], item[1]))[2]


def _elapsed_seconds(created_at: str) -> int:
    try:
        return monitor.elapsed_since(created_at)
    except (ValueError, TypeError):
        return 0


def _launch_grace_expired(row: sqlite3.Row | dict[str, Any]) -> bool:
    attempted_at = row["launch_attempted_at"] or row["created_at"]
    return _elapsed_seconds(str(attempted_at)) >= AMBIGUOUS_LAUNCH_GRACE_SECONDS


def _finish_failed_launch(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    batch_id: str,
    reason: str,
    details: str,
) -> None:
    with conn:
        conn.execute(
            "UPDATE automerge_batches SET status = 'failed', terminal_status = ?, "
            "finished_at = ? WHERE batch_id = ?",
            (reason, utc_now(), batch_id),
        )
    notify_blocked_once(conn, transport, batch_id, reason, details)


def process_batch(
    conn: sqlite3.Connection,
    transport: monitor.SlackTransport,
    batch_id: str,
) -> None:
    row = conn.execute(
        "SELECT * FROM automerge_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise AutomergeError(f"automerge batch {batch_id} disappeared",
                             reason="database_state_invalid")
    if row["status"] == "finishing":
        finish_batch(conn, transport, row)
        return

    send_start_notification(conn, transport, row)
    if row["status"] == "launching":
        try:
            session_id = launch_batch_session(
                row,
                row_pulls(row),
                conn=conn,
                allow_new=not bool(row["launch_attempted"]),
            )
        except LaunchAttemptError as exc:
            latest = conn.execute(
                "SELECT * FROM automerge_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if not exc.absence_proven:
                notify_blocked_once(conn, transport, batch_id, exc.reason, str(exc))
                log(
                    f"batch {batch_id} launch remains held because session absence "
                    "could not be verified"
                )
                return
            if not _launch_grace_expired(latest):
                log(f"batch {batch_id} launch remains held for exact-title adoption")
                return
            _finish_failed_launch(
                conn, transport, batch_id, "mj_launch_ambiguous_expired", str(exc)
            )
            return
        with conn:
            conn.execute(
                "UPDATE automerge_batches SET session_id = ?, status = 'running' "
                "WHERE batch_id = ?",
                (session_id, batch_id),
            )
        row = conn.execute(
            "SELECT * FROM automerge_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        first_wait = None
    else:
        session_id = str(row["session_id"] or "")
        if not session_id:
            raise AutomergeError("running batch has no Mjolnir session id",
                                 reason="database_state_invalid")
        session = _session_status(session_id)
        first_wait = None
        if not monitor.active_mj_turn(session):
            first_wait = monitor.wait_once(session_id, 1)
            if first_wait.timed_out:
                first_wait = None

    remaining = BATCH_TIMEOUT_SECONDS - _elapsed_seconds(str(row["created_at"]))
    try:
        turn = supervise_turn(
            conn,
            transport,
            batch_id,
            session_id,
            remaining,
            first_wait=first_wait,
        )
    except (monitor.MjError, monitor.CommandError, sqlite3.Error) as exc:
        reason = exc.reason if isinstance(exc, monitor.MjError) else "automerge_supervision_failed"
        notify_blocked_once(conn, transport, batch_id, reason, str(exc))
        return

    terminal_status = "timed_out" if turn.timed_out else turn.status
    if turn.timed_out:
        try:
            interrupt_and_wait(
                conn, transport, batch_id, session_id,
                grace_seconds=INTERRUPTION_GRACE_SECONDS,
            )
        except (monitor.MjError, monitor.CommandError, sqlite3.Error) as exc:
            reason = exc.reason if isinstance(exc, monitor.MjError) else "mj_interrupt_failed"
            notify_blocked_once(conn, transport, batch_id, reason, str(exc))
        notify_blocked_once(
            conn,
            transport,
            batch_id,
            "batch_timeout",
            f"The two-hour batch budget expired for session {session_id}; "
            "the turn was interrupted and its current GitHub outcome will be checked.",
        )

    transcript = ""
    final_message = ""
    try:
        drain_transcript(conn, transport, batch_id, session_id)
        transcript = monitor.read_complete_agent_transcript(session_id)
    except (monitor.MjError, monitor.CommandError, sqlite3.Error) as exc:
        reason = exc.reason if isinstance(exc, monitor.MjError) else "mj_transcript_failed"
        notify_blocked_once(conn, transport, batch_id, reason, str(exc))
    try:
        final_message = read_final_agent_message(session_id)
    except (monitor.MjError, monitor.CommandError, sqlite3.Error) as exc:
        reason = exc.reason if isinstance(exc, monitor.MjError) else "mj_transcript_failed"
        notify_blocked_once(conn, transport, batch_id, reason, str(exc))

    request_suspend(conn, transport, batch_id, session_id)
    with conn:
        conn.execute(
            "UPDATE automerge_batches SET status = 'finishing', terminal_status = ?, "
            "agent_transcript = ?, agent_final_message = ? WHERE batch_id = ?",
            (terminal_status, transcript, final_message, batch_id),
        )
    latest = conn.execute(
        "SELECT * FROM automerge_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    finish_batch(conn, transport, latest)


def acquire_lock(path: Path = LOCK_PATH):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def active_batch(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM automerge_batches WHERE status IN ('launching', 'running', 'finishing') "
        "ORDER BY created_at, batch_id LIMIT 1"
    ).fetchone()


def run_automerge() -> int:
    global GH_LOGIN_CACHE
    lock_handle = acquire_lock()
    if lock_handle is None:
        return 0
    GH_LOGIN_CACHE = None
    try:
        try:
            transport = monitor.load_slack_transport()
        except (OSError, RuntimeError, ValueError) as exc:
            log(str(exc))
            return 2
        conn = connect_db()
        try:
            retry_pending_notifications(conn, transport)
            check_pending_suspensions(conn, transport)
            row = active_batch(conn)
            if row is None:
                pulls = select_eligible_pull_requests()
                if not pulls:
                    return 0
                base_sha = current_master_sha()
                batch_id = create_batch(conn, pulls, base_sha)
            else:
                batch_id = str(row["batch_id"])
            try:
                process_batch(conn, transport, batch_id)
            except (AutomergeError, monitor.MjError, monitor.CommandError,
                    OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                reason = (
                    exc.reason
                    if isinstance(exc, (AutomergeError, monitor.MjError))
                    else "automerge_failed"
                )
                notify_blocked_once(conn, transport, batch_id, reason, str(exc))
                log(f"batch {batch_id} blocked ({reason}): {exc}")
                return 4
            return 0
        finally:
            conn.close()
    finally:
        lock_handle.close()


def main() -> int:
    try:
        return run_automerge()
    except (AutomergeError, monitor.CommandError, monitor.MjError, OSError,
            RuntimeError, ValueError, sqlite3.Error) as exc:
        log(f"fatal: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
