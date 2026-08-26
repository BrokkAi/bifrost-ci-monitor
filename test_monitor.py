#!/usr/bin/python3
"""Focused tests for the Bifrost CI monitor's episode lifecycle."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import monitor


ISSUE_URL = "https://github.com/BrokkAi/bifrost-dev/issues/2304"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [shutil.which("git") or "/usr/bin/git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    return result.stdout.strip()


def make_run(run_id: int = 42) -> monitor.CiRun:
    return monitor.CiRun(
        workflow="CI",
        sha="deadbeef",
        run_id=run_id,
        url=f"https://github.com/example/actions/runs/{run_id}",
        status="completed",
        conclusion="failure",
        created_at="2026-08-26T00:00:00Z",
        attempt=1,
        updated_at="2026-08-26T00:10:00Z",
    )


class GitFixture:
    def __init__(self, root: Path):
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.repair = root / "repair"
        self.writer = root / "writer"
        git(root, "init", "--bare", "--initial-branch=master", str(self.remote))
        git(root, "init", "--initial-branch=master", str(self.seed))
        self.configure(self.seed)
        (self.seed / "shared.txt").write_text("base\n", encoding="utf-8")
        git(self.seed, "add", "shared.txt")
        git(self.seed, "commit", "-m", "base")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        git(self.seed, "push", "-u", "origin", "master")
        git(root, "clone", str(self.remote), str(self.repair))
        git(root, "clone", str(self.remote), str(self.writer))
        self.configure(self.repair)
        self.configure(self.writer)
        git(
            self.repair,
            "checkout",
            "-b",
            monitor.WORKTREE_BRANCH,
            "--track",
            "origin/master",
        )

    @staticmethod
    def configure(repo: Path) -> None:
        git(repo, "config", "user.name", "CI Monitor Test")
        git(repo, "config", "user.email", "ci-monitor@example.com")

    def commit_local(self, content: str = "local\n") -> tuple[str, str]:
        base = git(self.repair, "rev-parse", "HEAD")
        (self.repair / "local.txt").write_text(content, encoding="utf-8")
        git(self.repair, "add", "local.txt")
        git(self.repair, "commit", "-m", "local repair")
        return base, git(self.repair, "rev-parse", "HEAD")

    def push_remote_file(
        self, name: str = "remote.txt", content: str = "remote\n"
    ) -> str:
        (self.writer / name).write_text(content, encoding="utf-8")
        git(self.writer, "add", name)
        git(self.writer, "commit", "-m", "remote advance")
        git(self.writer, "push", "origin", "master")
        return git(self.writer, "rev-parse", "HEAD")


def workflow_run(
    workflow: str,
    run_id: int,
    created_at: str,
    *,
    status: str = "completed",
    conclusion: str = "success",
    attempt: int = 1,
    updated_at: str | None = None,
) -> str:
    return json.dumps(
        [
            {
                "workflowName": workflow,
                "databaseId": run_id,
                "headSha": f"sha-{run_id}",
                "status": status,
                "conclusion": conclusion,
                "url": f"https://github.com/example/actions/runs/{run_id}",
                "createdAt": created_at,
                "attempt": attempt,
                "updatedAt": updated_at or created_at,
            }
        ]
    )


class PollCiTests(unittest.TestCase):
    @mock.patch.object(monitor, "run_command")
    def test_recent_failure_settles_while_runson_can_request_retry(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-25T17:00:00Z"),
            workflow_run(
                "Hourly CI",
                20,
                "2026-08-25T17:10:00Z",
                conclusion="failure",
                updated_at="2026-08-25T17:28:34Z",
            ),
            workflow_run("Nightly CI", 10, "2026-08-25T08:00:00Z"),
        ]

        result = monitor.poll_ci(
            now=dt.datetime(2026, 8, 25, 17, 30, 10, tzinfo=dt.timezone.utc)
        )

        self.assertEqual(result.state, "settling")
        self.assertIsNotNone(result.run)
        self.assertEqual(result.run.run_id, 20)
        self.assertEqual(result.run.attempt, 1)

    @mock.patch.object(monitor, "run_command")
    def test_unchanged_failure_becomes_actionable_after_settle_window(
        self, run_command
    ):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-25T17:00:00Z"),
            workflow_run(
                "Hourly CI",
                20,
                "2026-08-25T17:10:00Z",
                conclusion="failure",
                attempt=2,
                updated_at="2026-08-25T17:28:34Z",
            ),
            workflow_run("Nightly CI", 10, "2026-08-25T08:00:00Z"),
        ]

        result = monitor.poll_ci(
            now=dt.datetime(2026, 8, 25, 17, 33, 34, tzinfo=dt.timezone.utc)
        )

        self.assertEqual(result.state, "red")
        self.assertIsNotNone(result.run)
        self.assertEqual(result.run.run_id, 20)
        self.assertEqual(result.run.attempt, 2)

    @mock.patch.object(monitor, "run_command")
    def test_replacement_attempt_in_progress_blocks_repair(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-25T17:00:00Z"),
            workflow_run(
                "Hourly CI",
                20,
                "2026-08-25T17:10:00Z",
                status="in_progress",
                conclusion="",
                attempt=2,
                updated_at="2026-08-25T17:31:00Z",
            ),
            workflow_run("Nightly CI", 10, "2026-08-25T08:00:00Z"),
        ]

        result = monitor.poll_ci(
            now=dt.datetime(2026, 8, 25, 17, 35, 0, tzinfo=dt.timezone.utc)
        )

        self.assertEqual(result.state, "in_progress")
        self.assertIsNotNone(result.run)
        self.assertEqual(result.run.attempt, 2)

    @mock.patch.object(monitor, "run_command")
    def test_hourly_failure_takes_precedence_over_newer_green_push(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-20T12:30:00Z"),
            workflow_run(
                "Hourly CI",
                20,
                "2026-08-20T12:00:00Z",
                conclusion="failure",
            ),
            workflow_run("Nightly CI", 10, "2026-08-20T08:00:00Z"),
        ]

        result = monitor.poll_ci()

        self.assertEqual(result.state, "red")
        self.assertIsNotNone(result.run)
        self.assertEqual(result.run.workflow, "Hourly CI")
        self.assertEqual(result.run.run_id, 20)

    @mock.patch.object(monitor, "run_command")
    def test_nightly_failure_is_selected_while_hourly_is_running(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-20T12:30:00Z"),
            workflow_run(
                "Hourly CI",
                20,
                "2026-08-20T12:00:00Z",
                status="in_progress",
                conclusion="",
            ),
            workflow_run(
                "Nightly CI",
                10,
                "2026-08-20T08:00:00Z",
                conclusion="timed_out",
            ),
        ]

        result = monitor.poll_ci()

        self.assertEqual(result.state, "red")
        self.assertIsNotNone(result.run)
        self.assertEqual(result.run.workflow, "Nightly CI")

    @mock.patch.object(monitor, "run_command")
    def test_handled_red_does_not_hide_an_unhandled_red(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-20T12:30:00Z", conclusion="failure"),
            workflow_run("Hourly CI", 20, "2026-08-20T12:00:00Z", conclusion="failure"),
            workflow_run("Nightly CI", 10, "2026-08-20T08:00:00Z"),
        ]

        result = monitor.poll_ci({30})

        self.assertEqual(result.state, "red")
        self.assertIsNotNone(result.run)
        self.assertEqual(result.run.run_id, 20)

    @mock.patch.object(monitor, "run_command")
    def test_all_handled_red_runs_still_prevent_green_reset(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-20T12:30:00Z", conclusion="failure"),
            workflow_run("Hourly CI", 20, "2026-08-20T12:00:00Z", conclusion="failure"),
            workflow_run("Nightly CI", 10, "2026-08-20T08:00:00Z"),
        ]

        result = monitor.poll_ci({20, 30})

        self.assertEqual(result.state, "red")
        self.assertIsNotNone(result.run)
        self.assertEqual(result.run.run_id, 30)

    @mock.patch.object(monitor, "run_command")
    def test_green_requires_all_three_workflows_to_be_terminal(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-20T12:30:00Z"),
            workflow_run("Hourly CI", 20, "2026-08-20T12:00:00Z"),
            workflow_run(
                "Nightly CI", 10, "2026-08-20T08:00:00Z", conclusion="cancelled"
            ),
        ]

        result = monitor.poll_ci()

        self.assertEqual(result.state, "completed:success")
        calls = [call.args[0] for call in run_command.call_args_list[1:]]
        self.assertEqual(
            [call[call.index("--workflow") + 1] for call in calls],
            ["CI", "Hourly CI", "Nightly CI"],
        )
        self.assertIn("--event", calls[0])
        self.assertNotIn("--event", calls[1])
        self.assertNotIn("--event", calls[2])

    @mock.patch.object(monitor, "run_command")
    def test_missing_workflow_does_not_report_green(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-20T12:30:00Z"),
            "[]",
            workflow_run("Nightly CI", 10, "2026-08-20T08:00:00Z"),
        ]

        result = monitor.poll_ci()

        self.assertEqual(result.state, "incomplete")

    @mock.patch.object(monitor, "run_command")
    def test_running_workflow_blocks_green_reset(self, run_command):
        run_command.side_effect = [
            "head-sha",
            workflow_run("CI", 30, "2026-08-20T12:30:00Z"),
            workflow_run(
                "Hourly CI",
                20,
                "2026-08-20T12:00:00Z",
                status="in_progress",
                conclusion="",
            ),
            workflow_run("Nightly CI", 10, "2026-08-20T08:00:00Z"),
        ]

        result = monitor.poll_ci()

        self.assertEqual(result.state, "in_progress")
        self.assertIsNotNone(result.run)
        self.assertEqual(result.run.workflow, "Hourly CI")


def episode_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE escalation_gate (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            sha TEXT NOT NULL,
            signature TEXT NOT NULL DEFAULT '',
            issue_url TEXT,
            thread_ts TEXT,
            last_reported_run_id INTEGER,
            escalated INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL
        )
        """
    )
    return conn


class GithubIssueStateTests(unittest.TestCase):
    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(monitor, "run_command")
    def test_open_state_returns_without_retry(self, run_command, sleep):
        run_command.return_value = "OPEN\n"

        self.assertEqual(monitor.github_issue_state(ISSUE_URL), "OPEN")

        run_command.assert_called_once()
        sleep.assert_not_called()

    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(monitor, "run_command")
    def test_closed_state_returns_without_retry(self, run_command, sleep):
        run_command.return_value = "closed"

        self.assertEqual(monitor.github_issue_state(ISSUE_URL), "CLOSED")

        run_command.assert_called_once()
        sleep.assert_not_called()

    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(monitor, "run_command")
    def test_transient_failures_retry_three_total_attempts(self, run_command, sleep):
        run_command.side_effect = [
            monitor.CommandError("first"),
            monitor.CommandError("second"),
            "OPEN",
        ]

        self.assertEqual(monitor.github_issue_state(ISSUE_URL), "OPEN")

        self.assertEqual(run_command.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])

    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(
        monitor, "run_command", side_effect=monitor.CommandError("offline")
    )
    def test_three_command_failures_raise_for_next_tick(self, run_command, sleep):
        with self.assertRaisesRegex(monitor.CommandError, "offline"):
            monitor.github_issue_state(ISSUE_URL)

        self.assertEqual(run_command.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])

    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(monitor, "run_command", return_value="MERGED")
    def test_unexpected_state_retries_then_raises(self, run_command, sleep):
        with self.assertRaisesRegex(monitor.CommandError, "unexpected issue state"):
            monitor.github_issue_state(ISSUE_URL)

        self.assertEqual(run_command.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])


class EscalationOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.conn = episode_db()

    def tearDown(self):
        self.conn.close()

    def open_episode(
        self, *, issue_url: str | None = ISSUE_URL, run_id: int = 32188591060
    ) -> sqlite3.Row:
        monitor.open_escalation(
            self.conn,
            "08cb10ba",
            "rust (x86_64-unknown-linux-gnu) ▸ Cargo test",
            issue_url,
            "123.456",
            run_id,
            escalated=True,
        )
        episode = monitor.get_escalation(self.conn)
        assert episode is not None
        return episode

    @mock.patch.object(monitor, "github_issue_state", return_value="OPEN")
    def test_open_issue_preserves_episode(self, issue_state):
        episode = self.open_episode()

        refreshed = monitor.refresh_escalation_ownership(self.conn, episode)

        self.assertIs(refreshed, episode)
        self.assertIsNotNone(monitor.get_escalation(self.conn))
        issue_state.assert_called_once_with(ISSUE_URL)

    @mock.patch.object(monitor, "github_issue_state", return_value="CLOSED")
    def test_closed_issue_retires_even_when_run_was_already_reported(self, issue_state):
        episode = self.open_episode(run_id=32188591060)

        refreshed = monitor.refresh_escalation_ownership(self.conn, episode)

        self.assertIsNone(refreshed)
        self.assertIsNone(monitor.get_escalation(self.conn))
        issue_state.assert_called_once_with(ISSUE_URL)

    @mock.patch.object(monitor, "github_issue_state")
    def test_missing_issue_url_retires_unverifiable_ownership(self, issue_state):
        episode = self.open_episode(issue_url=None)

        refreshed = monitor.refresh_escalation_ownership(self.conn, episode)

        self.assertIsNone(refreshed)
        self.assertIsNone(monitor.get_escalation(self.conn))
        issue_state.assert_not_called()

    @mock.patch.object(
        monitor, "github_issue_state", side_effect=monitor.CommandError("offline")
    )
    def test_lookup_failure_preserves_episode_for_next_tick(self, issue_state):
        episode = self.open_episode()

        with self.assertRaisesRegex(monitor.CommandError, "offline"):
            monitor.refresh_escalation_ownership(self.conn, episode)

        remaining = monitor.get_escalation(self.conn)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["issue_url"], ISSUE_URL)
        issue_state.assert_called_once_with(ISSUE_URL)


class TimeoutHandoffTests(unittest.TestCase):
    def test_extracts_thread_started_session_id(self):
        self.assertEqual(
            monitor.extract_session_id(
                {"type": "thread.started", "thread_id": "session-123"}
            ),
            "session-123",
        )
        self.assertIsNone(monitor.extract_session_id({"type": "turn.started"}))

    def test_handoff_prompt_forbids_more_repair_work(self):
        run = monitor.CiRun(
            workflow="CI",
            sha="deadbeef",
            run_id=42,
            url="https://github.com/example/actions/runs/42",
            status="completed",
            conclusion="failure",
            created_at="2026-08-23T00:00:00Z",
            attempt=1,
            updated_at="2026-08-23T00:30:00Z",
        )

        prompt = monitor.build_timeout_handoff_prompt(run)

        self.assertIn("taken over one hour", prompt)
        self.assertIn("Do not investigate further", prompt)
        self.assertIn("gh issue create", prompt)
        self.assertIn(run.url, prompt)

    def test_finds_new_issue_url_in_handoff_output(self):
        old = "https://github.com/BrokkAi/bifrost-dev/issues/100"
        new = "https://github.com/BrokkAi/bifrost-dev/issues/101"

        self.assertEqual(
            monitor.find_issue_url(f"existing {old}\nfiled {new}", exclude_url=old),
            new,
        )

    @mock.patch.object(monitor.os, "read")
    @mock.patch.object(monitor.select, "select")
    @mock.patch.object(monitor.subprocess, "Popen")
    def test_resume_uses_exact_session_and_captures_jsonl(
        self, popen, select_call, os_read
    ):
        process = mock.Mock()
        process.pid = 4321
        process.returncode = 0
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        process.stdout.fileno.return_value = 99
        process.wait.return_value = 0
        popen.return_value = process
        select_call.return_value = ([99], [], [])
        os_read.side_effect = [
            b'{"type":"thread.started","thread_id":"session-123"}\n',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"filed"}}\n',
            b"",
        ]
        sessions = []
        messages = []

        result = monitor.invoke_codex_stream(
            "handoff",
            messages.append,
            timeout_seconds=600,
            resume_session_id="session-123",
            on_session=sessions.append,
        )

        args = popen.call_args.args[0]
        self.assertEqual(args[args.index("resume") + 1 :][-2:], ["session-123", "-"])
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.session_id, "session-123")
        self.assertEqual(messages, ["filed"])
        self.assertEqual(sessions, ["session-123"])
        process.stdin.write.assert_called_once_with(b"handoff")

    @mock.patch.object(monitor, "git_is_ancestor")
    @mock.patch.object(monitor, "run_command")
    def test_timeout_recovery_stashes_untracked_and_resets(self, run_command, ancestor):
        def command_result(args, **kwargs):
            if args[1:3] == ["branch", "--show-current"]:
                return monitor.WORKTREE_BRANCH
            if args[1:3] == ["status", "--porcelain"]:
                command_result.status_calls += 1
                return " M file\n?? new" if command_result.status_calls == 1 else ""
            if args[1:3] == ["rev-parse", "HEAD"]:
                command_result.head_calls += 1
                return "base" if command_result.head_calls == 1 else "remote"
            if args[1:3] == ["rev-parse", "origin/master"]:
                return "remote"
            return ""

        command_result.status_calls = 0
        command_result.head_calls = 0
        run_command.side_effect = command_result
        ancestor.return_value = True

        ok, detail = monitor.recover_timeout_worktree(42, "deadbeef", "session-123")

        self.assertTrue(ok, detail)
        calls = [call.args[0] for call in run_command.call_args_list]
        self.assertTrue(any(call[1:3] == ["stash", "push"] for call in calls))
        self.assertTrue(any(call[1:3] == ["reset", "--hard"] for call in calls))

    @mock.patch.object(monitor, "run_command")
    def test_timeout_recovery_never_resets_when_stash_fails(self, run_command):
        run_command.side_effect = [
            monitor.WORKTREE_BRANCH,
            " M file",
            monitor.CommandError("stash failed"),
        ]

        ok, detail = monitor.recover_timeout_worktree(42, "deadbeef", "session-123")

        self.assertFalse(ok)
        self.assertIn("stash failed", detail)
        calls = [call.args[0] for call in run_command.call_args_list]
        self.assertFalse(any(call[1:3] == ["reset", "--hard"] for call in calls))

    def test_connect_db_migrates_timeout_handoff_columns_additively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "activity.db"
            legacy = sqlite3.connect(db_path)
            legacy.execute(
                "CREATE TABLE invocations (workflow_run_id INTEGER PRIMARY KEY)"
            )
            legacy.commit()
            legacy.close()
            with mock.patch.object(monitor, "DB_PATH", db_path):
                conn = monitor.connect_db()
                try:
                    columns = {
                        row["name"]
                        for row in conn.execute(
                            "PRAGMA table_info(invocations)"
                        ).fetchall()
                    }
                finally:
                    conn.close()

        self.assertTrue(
            {
                "codex_session_id",
                "issue_url",
                "timeout_handoff_status",
                "codex_pid",
                "base_sha",
                "candidate_sha",
                "reconcile_round",
                "attempt_count",
            }
            <= columns
        )


class MergeReconciliationIntegrationTests(unittest.TestCase):
    def test_repair_prompt_assigns_pull_and_push_to_monitor(self):
        prompt = monitor.build_prompt(make_run())

        self.assertIn("Do not push", prompt)
        self.assertIn("monitor owns the pull-and-push reconciliation", prompt)
        self.assertIn("never pull or merge remote changes yourself", prompt)

    def test_conflict_free_pull_merges_and_pushes_without_codex(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = GitFixture(Path(temp_dir))
            base, candidate = fixture.commit_local()
            remote_advance = fixture.push_remote_file()
            initial = monitor.CodexResult("completed", 0, False, "fixed", "session-123")

            with (
                mock.patch.object(monitor, "WORKTREE", fixture.repair),
                mock.patch.object(
                    monitor, "GIT_BIN", Path(shutil.which("git") or "/usr/bin/git")
                ),
                mock.patch.object(monitor, "invoke_codex_stream") as invoke,
            ):
                resolution = monitor.reconcile_repair(
                    make_run(),
                    base,
                    initial,
                    lambda _text: None,
                    deadline=time.monotonic() + 60,
                )

            self.assertEqual(resolution.result.status, "completed")
            self.assertIsNotNone(resolution.pushed_sha)
            self.assertEqual(git(fixture.repair, "status", "--porcelain"), "")
            self.assertEqual(
                git(fixture.repair, "rev-parse", "HEAD"),
                git(fixture.repair, "rev-parse", "origin/master"),
            )
            parents = git(fixture.repair, "show", "-s", "--format=%P", "HEAD").split()
            self.assertEqual(set(parents), {candidate, remote_advance})
            invoke.assert_not_called()

    def test_remote_advance_during_push_pulls_again_without_codex(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = GitFixture(Path(temp_dir))
            base, candidate = fixture.commit_local()
            initial = monitor.CodexResult("completed", 0, False, "fixed", "session-123")
            real_run_command = monitor.run_command
            raced = False
            remote_advance = ""

            def run_with_push_race(args, **kwargs):
                nonlocal raced, remote_advance
                if args[1:3] == ["push", "origin"] and not raced:
                    remote_advance = fixture.push_remote_file("race.txt", "race\n")
                    raced = True
                return real_run_command(args, **kwargs)

            with (
                mock.patch.object(monitor, "WORKTREE", fixture.repair),
                mock.patch.object(
                    monitor, "GIT_BIN", Path(shutil.which("git") or "/usr/bin/git")
                ),
                mock.patch.object(
                    monitor, "run_command", side_effect=run_with_push_race
                ),
                mock.patch.object(monitor, "invoke_codex_stream") as invoke,
            ):
                resolution = monitor.reconcile_repair(
                    make_run(),
                    base,
                    initial,
                    lambda _text: None,
                    deadline=time.monotonic() + 60,
                )

            self.assertTrue(raced)
            self.assertEqual(resolution.result.status, "completed")
            self.assertIsNotNone(resolution.pushed_sha)
            parents = git(fixture.repair, "show", "-s", "--format=%P", "HEAD").split()
            self.assertEqual(set(parents), {candidate, remote_advance})
            invoke.assert_not_called()

    def test_content_conflict_resumes_same_session_then_pushes_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = GitFixture(Path(temp_dir))
            base = git(fixture.repair, "rev-parse", "HEAD")
            (fixture.repair / "shared.txt").write_text("local\n", encoding="utf-8")
            git(fixture.repair, "add", "shared.txt")
            git(fixture.repair, "commit", "-m", "local repair")
            candidate = git(fixture.repair, "rev-parse", "HEAD")
            remote_advance = fixture.push_remote_file("shared.txt", "remote\n")
            initial = monitor.CodexResult("completed", 0, False, "fixed", "session-123")

            def resolve_conflict(*_args, **kwargs):
                self.assertEqual(kwargs["resume_session_id"], "session-123")
                self.assertIn(
                    "shared.txt",
                    git(fixture.repair, "diff", "--name-only", "--diff-filter=U"),
                )
                (fixture.repair / "shared.txt").write_text(
                    "local and remote\n", encoding="utf-8"
                )
                git(fixture.repair, "add", "shared.txt")
                git(fixture.repair, "commit", "--no-edit")
                return monitor.CodexResult(
                    "completed", 0, False, "resolved", "session-123"
                )

            with (
                mock.patch.object(monitor, "WORKTREE", fixture.repair),
                mock.patch.object(
                    monitor, "GIT_BIN", Path(shutil.which("git") or "/usr/bin/git")
                ),
                mock.patch.object(
                    monitor, "invoke_codex_stream", side_effect=resolve_conflict
                ) as invoke,
            ):
                resolution = monitor.reconcile_repair(
                    make_run(),
                    base,
                    initial,
                    lambda _text: None,
                    deadline=time.monotonic() + 60,
                )

            self.assertEqual(resolution.result.status, "completed")
            self.assertIsNotNone(resolution.pushed_sha)
            self.assertEqual(
                (fixture.repair / "shared.txt").read_text(), "local and remote\n"
            )
            parents = git(fixture.repair, "show", "-s", "--format=%P", "HEAD").split()
            self.assertEqual(set(parents), {candidate, remote_advance})
            self.assertEqual(invoke.call_count, 1)

    def test_preflight_preserves_orphan_and_resets_to_remote(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = GitFixture(Path(temp_dir))
            _base, candidate = fixture.commit_local()
            remote_advance = fixture.push_remote_file()

            with (
                mock.patch.object(monitor, "WORKTREE", fixture.repair),
                mock.patch.object(
                    monitor, "GIT_BIN", Path(shutil.which("git") or "/usr/bin/git")
                ),
            ):
                preflight = monitor.preflight_worktree()

            self.assertEqual(preflight.base_sha, remote_advance)
            self.assertEqual(preflight.recovered_sha, candidate)
            self.assertIsNotNone(preflight.recovered_tag)
            self.assertEqual(
                git(fixture.repair, "rev-parse", preflight.recovered_tag or ""),
                candidate,
            )
            self.assertEqual(git(fixture.repair, "status", "--porcelain"), "")

    def test_recovery_aborts_conflict_preserves_candidate_and_resets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = GitFixture(Path(temp_dir))
            (fixture.repair / "shared.txt").write_text("local\n", encoding="utf-8")
            git(fixture.repair, "add", "shared.txt")
            git(fixture.repair, "commit", "-m", "local repair")
            candidate = git(fixture.repair, "rev-parse", "HEAD")
            remote_advance = fixture.push_remote_file("shared.txt", "remote\n")
            with self.assertRaises(subprocess.CalledProcessError):
                git(
                    fixture.repair,
                    "pull",
                    "--no-rebase",
                    "--no-edit",
                    "origin",
                    "master",
                )

            with (
                mock.patch.object(monitor, "WORKTREE", fixture.repair),
                mock.patch.object(
                    monitor, "GIT_BIN", Path(shutil.which("git") or "/usr/bin/git")
                ),
            ):
                ok, detail = monitor.recover_timeout_worktree(
                    42, "deadbeef", "session-123"
                )

            self.assertTrue(ok, detail)
            self.assertIn("aborted conflicted merge", detail)
            self.assertIn("tagged unpushed commit", detail)
            self.assertEqual(git(fixture.repair, "rev-parse", "HEAD"), remote_advance)
            self.assertEqual(
                git(
                    fixture.repair,
                    "rev-parse",
                    f"bifrost-ci-recovery/42-{candidate[:12]}",
                ),
                candidate,
            )
            self.assertEqual(git(fixture.repair, "status", "--porcelain"), "")


class RetryableInvocationTests(unittest.TestCase):
    @mock.patch.object(monitor, "slack_send")
    @mock.patch.object(monitor, "git_is_ancestor", return_value=True)
    @mock.patch.object(monitor, "worktree_status", return_value="")
    @mock.patch.object(monitor, "fetch_remote_sha", return_value="remote-sha")
    @mock.patch.object(monitor, "terminate_recorded_codex", return_value=True)
    def test_restart_after_successful_push_verifies_instead_of_retriaging(
        self, terminate, fetch, status, ancestor, slack
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "activity.db"
            with mock.patch.object(monitor, "DB_PATH", db_path):
                conn = monitor.connect_db()
                try:
                    run = make_run()
                    self.assertTrue(monitor.claim_invocation(conn, run, "base"))
                    conn.execute(
                        "UPDATE invocations SET status = 'reconciling', "
                        "candidate_sha = 'candidate', thread_ts = '123.456' "
                        "WHERE workflow_run_id = ?",
                        (run.run_id,),
                    )
                    conn.commit()

                    monitor.recover_interrupted(
                        conn, monitor.SlackTransport("webhook", webhook="unused")
                    )
                    row = conn.execute(
                        "SELECT * FROM invocations WHERE workflow_run_id = ?",
                        (run.run_id,),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["outcome_notification_attempted"], 1)
        self.assertIn("verified candidate candidate", row["output"])
        ancestor.assert_called_once_with("candidate", "origin/master")
        slack.assert_called_once()

    def test_orphaned_invocation_can_be_claimed_again_without_losing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "activity.db"
            with mock.patch.object(monitor, "DB_PATH", db_path):
                conn = monitor.connect_db()
                try:
                    run = make_run()
                    self.assertTrue(monitor.claim_invocation(conn, run, "base-1"))
                    conn.execute(
                        "UPDATE invocations SET status = 'orphaned_candidate', "
                        "output = 'first attempt', thread_ts = '123.456' "
                        "WHERE workflow_run_id = ?",
                        (run.run_id,),
                    )
                    conn.commit()

                    self.assertNotIn(run.run_id, monitor.handled_run_ids(conn))
                    self.assertTrue(monitor.claim_invocation(conn, run, "base-2"))
                    row = conn.execute(
                        "SELECT * FROM invocations WHERE workflow_run_id = ?",
                        (run.run_id,),
                    ).fetchone()
                finally:
                    conn.close()

            self.assertEqual(row["status"], "claimed")
            self.assertEqual(row["attempt_count"], 2)
            self.assertEqual(row["base_sha"], "base-2")
            self.assertIn("first attempt", row["output"])
            self.assertIn("--- retry", row["output"])


if __name__ == "__main__":
    unittest.main()
