#!/usr/bin/python3
"""Focused tests for the Bifrost CI monitor's episode lifecycle."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

import monitor


ISSUE_URL = "https://github.com/BrokkAi/bifrost-dev/issues/2304"


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




def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def insert_invocation(
    conn,
    *,
    run_id=42,
    status="claimed",
    session_id=None,
    cursor=0,
    output="",
    started_at=None,
    base_sha="base-sha",
    workflow="CI",
):
    now = started_at or monitor.utc_now()
    conn.execute(
        """
        INSERT INTO invocations (
            workflow_run_id, sha, workflow_run_url, conclusion, observed_at,
            started_at, status, base_sha, workflow, codex_session_id,
            mj_transcript_after_seq, output, thread_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "deadbeef",
            f"https://github.com/example/actions/runs/{run_id}",
            "failure",
            now,
            now,
            status,
            base_sha,
            workflow,
            session_id,
            cursor,
            output,
            "thread-1",
        ),
    )
    conn.commit()


class MjRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = mock.patch.object(
            monitor, "DB_PATH", Path(self.temp.name) / "activity.db"
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.conn = monitor.connect_db()
        self.addCleanup(self.conn.close)
        self.transport = monitor.SlackTransport(
            "chat", token="xoxb-test", channel="C0123456789"
        )

    def test_mj_new_argv_has_required_selectors_and_no_profile(self):
        run = make_run()
        argv = monitor.new_session_argv(
            run, "a" * 40, 3, "/tmp/repair.prompt"
        )
        self.assertEqual(
            argv,
            [
                str(monitor.MJ_BIN),
                "new",
                "--workspace",
                "CI",
                "--target",
                "podman",
                "--bundle",
                "bifrost",
                "--model",
                "deepseek-v4-pro",
                "--at",
                "a" * 40,
                "--branch",
                "ci-repair/42-3",
                "--title",
                "CI deadbeef run 42 attempt 3 CI repair",
                "--prompt-file",
                "/tmp/repair.prompt",
                "--json",
            ],
        )
        self.assertNotIn("--profile", argv)
        self.assertNotIn("--effort", argv)

    def test_launch_parses_session_id_through_fake_mj_seam(self):
        run = make_run()
        def fake_mj(args, *, timeout=60):
            if args[0] == "sessions":
                return completed('{"sessions":[]}')
            return completed('{"session_id":"s-42"}')

        with mock.patch.object(monitor, "mj_command", side_effect=fake_mj) as command:
            session_id, branch = monitor.launch_mj_session(run, "b" * 40, 1, None)
        self.assertEqual((session_id, branch), ("s-42", "ci-repair/42-1"))
        argv = command.call_args_list[-1].args[0]
        self.assertEqual(
            argv[:11],
            [
                "new",
                "--workspace",
                "CI",
                "--target",
                "podman",
                "--bundle",
                "bifrost",
                "--model",
                "deepseek-v4-pro",
                "--at",
                "b" * 40,
            ],
        )
        self.assertNotIn("--profile", argv)

    def test_ambiguous_new_adopts_matching_workspace_session(self):
        run = make_run()
        title = monitor.launch_title(run, 2)
        calls = []

        def fake_mj(args, *, timeout=60):
            calls.append(args[0])
            if args[0] == "sessions":
                sessions_call = calls.count("sessions")
                sessions = [] if sessions_call == 1 else [
                    {
                        "id": "adopted-session",
                        "title": title,
                        "state": "running",
                        "active": True,
                    }
                ]
                return completed(json.dumps({"sessions": sessions}))
            return completed("", 1, "connection closed after request")

        with mock.patch.object(monitor, "mj_command", side_effect=fake_mj):
            session_id, branch = monitor.launch_mj_session(run, "b" * 40, 2, None)
        self.assertEqual(session_id, "adopted-session")
        self.assertEqual(branch, "ci-repair/42-2")
        self.assertEqual(calls, ["sessions", "new", "sessions"])

    def test_launch_recovery_adopts_before_retrying_the_attempt(self):
        insert_invocation(self.conn, status="launching", session_id=None)
        run = make_run()
        title = monitor.launch_title(run, 1)
        with mock.patch.object(
            monitor,
            "mj_command",
            return_value=completed(
                json.dumps(
                    {"sessions": [{"id": "recovered-session", "title": title,
                                   "state": "running", "active": True}]}
                )
            ),
        ) as command:
            monitor.recover_launching_invocations(self.conn, self.transport)
        row = self.conn.execute(
            "SELECT status, codex_session_id, attempt_count FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["codex_session_id"], "recovered-session")
        self.assertEqual(row["attempt_count"], 1)
        command.assert_called_once_with(
            ["sessions", "--workspace", "CI", "--json"], timeout=30
        )

    def test_ambiguous_launch_without_visible_session_stays_unresolved_until_adopted(self):
        insert_invocation(self.conn, status="launching", session_id=None)
        run = make_run()
        title = monitor.launch_title(run, 1)
        responses = [
            {"sessions": []},
            {"sessions": [{"id": "late-session", "title": title, "state": "running"}]},
        ]

        def fake_mj(args, *, timeout=60):
            self.assertEqual(args, ["sessions", "--workspace", "CI", "--json"])
            return completed(json.dumps(responses.pop(0)))

        with mock.patch.object(monitor, "mj_command", side_effect=fake_mj), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply")
        ):
            monitor.recover_launching_invocations(self.conn, self.transport)
            row = self.conn.execute(
                "SELECT status, attempt_count, codex_session_id FROM invocations "
                "WHERE workflow_run_id = 42"
            ).fetchone()
            self.assertEqual((row["status"], row["attempt_count"], row["codex_session_id"]),
                             ("launching", 1, None))
            self.assertFalse(monitor.claim_invocation(self.conn, run, "base-sha"))
            monitor.recover_launching_invocations(self.conn, self.transport)

        row = self.conn.execute(
            "SELECT status, attempt_count, codex_session_id FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual((row["status"], row["attempt_count"], row["codex_session_id"]),
                         ("running", 1, "late-session"))
        blocked = self.conn.execute(
            "SELECT slack_notification_attempted FROM blocked_notifications "
            "WHERE workflow_run_id = 42 AND reason = 'mj_launch_ambiguous'"
        ).fetchone()
        self.assertEqual(blocked["slack_notification_attempted"], 1)

    def test_transcript_relay_persists_cursor_and_never_reposts_page(self):
        insert_invocation(self.conn, session_id="s-42")
        pages = [
            completed(
                json.dumps(
                    {
                        "items": [
                            {"seq": 4, "role": "agent", "text": "First finished update"},
                            {"seq": 5, "role": "agent", "text": "Second finished update"},
                        ],
                        "next_after_seq": 8,
                    }
                )
            ),
            completed(json.dumps({"items": [], "next_after_seq": 10})),
        ]
        with mock.patch.object(monitor, "mj_command", side_effect=pages) as command, mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ) as slack:
            self.assertEqual(
                monitor.drain_transcript(self.conn, self.transport, 42, "s-42"),
                ["First finished update", "Second finished update"],
            )
            self.assertEqual(
                monitor.drain_transcript(self.conn, self.transport, 42, "s-42"), []
            )
        row = self.conn.execute(
            "SELECT mj_transcript_after_seq, output FROM invocations WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["mj_transcript_after_seq"], 10)
        self.assertEqual(
            row["output"], "First finished update\n\nSecond finished update\n\n"
        )
        self.assertEqual(slack.call_count, 2)
        self.assertEqual(
            command.call_args_list[0].args[0][-4:],
            ["--finished-only", "--after-seq", "0", "--json"],
        )
        self.assertEqual(
            command.call_args_list[1].args[0][-4:],
            ["--finished-only", "--after-seq", "8", "--json"],
        )

    def test_transcript_post_failure_does_not_advance_cursor(self):
        insert_invocation(self.conn, session_id="s-42")
        page = completed(
            json.dumps(
                {
                    "items": [
                        {"seq": 4, "stable_id": "m4", "text": "Retry me"},
                        {"seq": 5, "stable_id": "m5", "text": "Then me"},
                    ],
                    "next_after_seq": 7,
                }
            )
        )
        posts = [(False, None), (True, "reply-1"), (True, "reply-2")]
        with mock.patch.object(monitor, "mj_command", return_value=page), mock.patch.object(
            monitor, "slack_send", side_effect=posts
        ) as slack:
            self.assertEqual(
                monitor.drain_transcript(self.conn, self.transport, 42, "s-42"), []
            )
            row = self.conn.execute(
                "SELECT mj_transcript_after_seq, output FROM invocations "
                "WHERE workflow_run_id = 42"
            ).fetchone()
            self.assertEqual((row["mj_transcript_after_seq"], row["output"]), (0, ""))
            self.assertEqual(
                monitor.drain_transcript(self.conn, self.transport, 42, "s-42"),
                ["Retry me", "Then me"],
            )
        self.assertEqual(slack.call_count, 3)
        row = self.conn.execute(
            "SELECT mj_transcript_after_seq, output FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["mj_transcript_after_seq"], 7)
        self.assertEqual(row["output"], "Retry me\n\nThen me\n\n")

    def test_transcript_same_sequence_sibling_replays_after_second_post_fails(self):
        insert_invocation(self.conn, session_id="s-42")
        page = completed(
            json.dumps(
                {
                    "items": [
                        {"seq": 4, "stable_id": "sibling-a", "text": "First"},
                        {"seq": 4, "stable_id": "sibling-b", "text": "Second"},
                    ],
                    "next_after_seq": 4,
                }
            )
        )
        with mock.patch.object(monitor, "mj_command", return_value=page), mock.patch.object(
            monitor,
            "slack_send",
            side_effect=[(True, "reply-a"), (False, None), (True, "reply-b")],
        ) as slack:
            self.assertEqual(
                monitor.drain_transcript(self.conn, self.transport, 42, "s-42"),
                ["First"],
            )
            row = self.conn.execute(
                "SELECT mj_transcript_after_seq FROM invocations WHERE workflow_run_id = 42"
            ).fetchone()
            self.assertEqual(row["mj_transcript_after_seq"], 3)
            self.assertEqual(
                monitor.drain_transcript(self.conn, self.transport, 42, "s-42"),
                ["Second"],
            )
        self.assertEqual(slack.call_count, 3)
        row = self.conn.execute(
            "SELECT mj_transcript_after_seq, output FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["mj_transcript_after_seq"], 4)
        self.assertEqual(row["output"], "First\n\nSecond\n\n")

    def test_transcript_stable_id_dedupes_post_close_revision(self):
        insert_invocation(self.conn, session_id="s-42")
        pages = [
            completed(
                json.dumps(
                    {
                        "items": [
                            {"seq": 4, "stable_id": "stable-4", "text": "Original"}
                        ],
                        "next_after_seq": 4,
                    }
                )
            ),
            completed(
                json.dumps(
                    {
                        "items": [
                            {"seq": 6, "stable_id": "stable-4", "text": "Late revision"}
                        ],
                        "next_after_seq": 6,
                    }
                )
            ),
        ]
        with mock.patch.object(monitor, "mj_command", side_effect=pages), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply")
        ) as slack:
            self.assertEqual(
                monitor.drain_transcript(self.conn, self.transport, 42, "s-42"),
                ["Original"],
            )
            self.assertEqual(
                monitor.drain_transcript(self.conn, self.transport, 42, "s-42"), []
            )
        self.assertEqual(slack.call_count, 1)
        row = self.conn.execute(
            "SELECT mj_transcript_after_seq, output FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["mj_transcript_after_seq"], 6)
        self.assertEqual(row["output"], "Original\n\n")

    def test_complete_agent_transcript_reads_every_page(self):
        pages = [
            completed(
                json.dumps(
                    {
                        "items": [{"seq": 2, "stable_id": "m2", "text": "First"}],
                        "next_after_seq": 2,
                        "latest_seq": 4,
                    }
                )
            ),
            completed(
                json.dumps(
                    {
                        "items": [{"seq": 4, "stable_id": "m4", "text": "Last"}],
                        "next_after_seq": 4,
                        "latest_seq": 4,
                    }
                )
            ),
        ]
        with mock.patch.object(monitor, "mj_command", side_effect=pages) as command:
            output = monitor.read_complete_agent_transcript("s-42")
        self.assertEqual(output, "First\n\nLast")
        self.assertEqual(
            [call.args[0][call.args[0].index("--after-seq") + 1]
             for call in command.call_args_list],
            ["0", "2"],
        )

    def test_restart_reattaches_at_saved_cursor_without_reposting(self):
        insert_invocation(
            self.conn,
            status="running",
            session_id="session-live",
            cursor=12,
            output="Already relayed\n\n",
        )

        def fake_mj(args, *, timeout=60):
            if args[0] == "sessions":
                return completed(
                    json.dumps(
                        {
                            "id": "session-live",
                            "state": "running",
                            "chat_phase": "running",
                            "is_idle": False,
                        }
                    )
                )
            if args[0] == "wait":
                return completed(json.dumps({"outcome": "finished"}))
            if args[0] == "transcript":
                if "--role" in args:
                    return completed(
                        json.dumps(
                            {
                                "items": [{"seq": 13, "stable_id": "full", "text": "Complete text"}],
                                "next_after_seq": 13,
                                "latest_seq": 13,
                            }
                        )
                    )
                self.assertIn("--after-seq", args)
                self.assertEqual(args[args.index("--after-seq") + 1], "12")
                return completed(json.dumps({"items": [], "next_after_seq": 12}))
            if args[0] == "suspend":
                return completed("{}")
            self.fail(f"unexpected fake mj command: {args}")

        with mock.patch.object(monitor, "mj_command", side_effect=fake_mj), mock.patch.object(
            monitor,
            "run_command",
            return_value="[]",
        ), mock.patch.object(monitor, "failing_signature", return_value=""), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ) as slack:
            monitor.reattach_running_invocations(self.conn, self.transport)
        row = self.conn.execute(
            "SELECT status, output, mj_transcript_after_seq FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["output"], "Complete text")
        self.assertEqual(row["mj_transcript_after_seq"], 12)
        # Only the invocation outcome was sent; the existing transcript item was not.
        self.assertEqual(slack.call_count, 1)

    def test_restart_finalizes_an_ended_session_from_its_saved_outcome(self):
        insert_invocation(
            self.conn,
            status="running",
            session_id="session-ended",
            cursor=6,
            output="Captured before restart\n\n",
        )
        commands = []

        def fake_mj(args, *, timeout=60):
            commands.append(args[0])
            if args[0] == "sessions":
                return completed(
                    json.dumps(
                        {
                            "id": "session-ended",
                            "state": "suspended",
                            "chat_phase": "closed",
                            "is_idle": True,
                            "last_turn_outcome": {
                                "outcome": {
                                    "kind": "completed",
                                    "stop_reason": "EndTurn",
                                }
                            },
                        }
                    )
                )
            if args[0] == "transcript":
                if "--role" in args:
                    return completed(
                        json.dumps(
                            {
                                "items": [{"seq": 7, "stable_id": "full", "text": "Captured before restart"}],
                                "next_after_seq": 7,
                                "latest_seq": 7,
                            }
                        )
                    )
                self.assertEqual(args[args.index("--after-seq") + 1], "6")
                return completed(json.dumps({"items": [], "next_after_seq": 6}))
            if args[0] == "wait":
                return completed(json.dumps({"outcome": "finished"}))
            if args[0] == "suspend":
                return completed("{}")
            self.fail(f"unexpected fake mj command: {args}")

        with mock.patch.object(monitor, "mj_command", side_effect=fake_mj), mock.patch.object(
            monitor,
            "run_command",
            return_value="[]",
        ), mock.patch.object(monitor, "failing_signature", return_value=""), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ):
            monitor.reattach_running_invocations(self.conn, self.transport)
        row = self.conn.execute(
            "SELECT status, output FROM invocations WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["output"], "Captured before restart")
        self.assertIn("wait", commands)

    def test_restart_uses_normalized_wait_outcome_for_completed_failure(self):
        insert_invocation(
            self.conn,
            status="running",
            session_id="session-failed",
        )

        def fake_mj(args, *, timeout=60):
            if args[0] == "sessions":
                return completed(
                    json.dumps(
                        {
                            "id": "session-failed",
                            "state": "suspended",
                            "chat_phase": "closed",
                            "is_idle": True,
                            "last_turn_outcome": {
                                "outcome": {
                                    "kind": "completed",
                                    "stop_reason": "harness_failed",
                                }
                            },
                        }
                    )
                )
            if args[0] == "wait":
                return completed(
                    json.dumps(
                        {"outcome": "error", "stop_reason": "harness_failed"}
                    ),
                    1,
                )
            if args[0] == "transcript":
                return completed(json.dumps({"items": [], "next_after_seq": 0}))
            if args[0] == "suspend":
                return completed("{}")
            self.fail(f"unexpected fake mj command: {args}")

        with mock.patch.object(monitor, "mj_command", side_effect=fake_mj), mock.patch.object(
            monitor,
            "run_command",
            return_value="[]",
        ), mock.patch.object(monitor, "failing_signature", return_value=""), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ):
            monitor.reattach_running_invocations(self.conn, self.transport)
        row = self.conn.execute(
            "SELECT status FROM invocations WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "error")

    def test_slack_outage_does_not_hide_escalation_in_complete_agent_transcript(self):
        insert_invocation(
            self.conn, status="running", session_id="session-slack-down"
        )
        issue_url = "https://github.com/BrokkAi/bifrost-dev/issues/99"

        def fake_mj(args, *, timeout=60):
            if args[0] == "transcript":
                if "--role" in args:
                    return completed(
                        json.dumps(
                            {
                                "items": [
                                    {
                                        "seq": 2,
                                        "stable_id": "final-agent-message",
                                        "text": f"<@U08P3FAEU3G> filed {issue_url}",
                                    }
                                ],
                                "next_after_seq": 2,
                                "latest_seq": 2,
                            }
                        )
                    )
                return completed(
                    json.dumps(
                        {
                            "items": [
                                {"seq": 1, "stable_id": "relay-item", "text": "Working"}
                            ],
                            "next_after_seq": 1,
                            "latest_seq": 1,
                        }
                    )
                )
            if args[0] == "suspend":
                return completed("{}")
            self.fail(f"unexpected fake mj command: {args}")

        def finish_after_relay_attempt(conn, transport, run_id, session_id, timeout):
            monitor.drain_transcript(conn, transport, run_id, session_id)
            return monitor.TurnResult("completed", "finished")

        with mock.patch.object(
            monitor, "mj_command", side_effect=fake_mj
        ), mock.patch.object(
            monitor, "supervise_turn", side_effect=finish_after_relay_attempt
        ), mock.patch.object(
            monitor, "slack_send", return_value=(False, None)
        ), mock.patch.object(
            monitor, "failing_signature", return_value=""
        ):
            result = monitor.run_session_lifecycle(
                self.conn,
                self.transport,
                make_run(),
                "session-slack-down",
                "ci-repair/42-1",
                3600,
            )
            self.assertIn(issue_url, result.output)
            monitor.finalize_invocation(
                self.conn, self.transport, make_run(), result, None
            )

        self.assertIn(issue_url, result.output)
        row = self.conn.execute(
            "SELECT status, issue_url FROM invocations WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["issue_url"], issue_url)
        cursor = self.conn.execute(
            "SELECT mj_transcript_after_seq FROM invocations WHERE workflow_run_id = 42"
        ).fetchone()[0]
        self.assertEqual(cursor, 0)
        episode = monitor.get_escalation(self.conn)
        self.assertIsNotNone(episode)
        self.assertTrue(episode["escalated"])

    def test_restart_reattaches_to_active_timeout_handoff(self):
        insert_invocation(
            self.conn,
            status="running",
            session_id="session-handoff",
        )
        self.conn.execute(
            "UPDATE invocations SET timed_out = 1, timeout_handoff_status = 'running' "
            "WHERE workflow_run_id = 42"
        )
        self.conn.commit()

        def fake_mj(args, *, timeout=60):
            if args[0] == "sessions":
                return completed(
                    json.dumps(
                        {
                            "id": "session-handoff",
                            "state": "running",
                            "chat_phase": "running",
                            "is_idle": False,
                        }
                    )
                )
            if args[0] == "transcript":
                return completed(json.dumps({"items": [], "next_after_seq": 0}))
            if args[0] == "suspend":
                return completed("{}")
            self.fail(f"unexpected fake mj command: {args}")

        with mock.patch.object(
            monitor, "mj_command", side_effect=fake_mj
        ), mock.patch.object(
            monitor,
            "supervise_turn",
            return_value=monitor.TurnResult("completed", "finished"),
        ) as supervise, mock.patch.object(
            monitor,
            "run_command",
            return_value="[]",
        ), mock.patch.object(
            monitor, "failing_signature", return_value=""
        ), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ):
            monitor.reattach_running_invocations(self.conn, self.transport)

        self.assertEqual(
            supervise.call_args.args[-1], monitor.MJ_HANDOFF_TIMEOUT_SECONDS
        )
        row = self.conn.execute(
            "SELECT status, timeout_handoff_status FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "timed_out")
        self.assertEqual(row["timeout_handoff_status"], "completed")

    def test_timeout_interrupts_hands_off_in_session_and_suspends(self):
        insert_invocation(self.conn, session_id="session-timeout")
        commands = []
        prompts = []

        def fake_mj(args, *, timeout=60):
            commands.append(args[0])
            if args[0] == "interrupt-turn":
                return completed("{}")
            if args[0] == "wait":
                return completed(json.dumps({"outcome": "cancelled"}))
            if args[0] == "transcript":
                return completed(json.dumps({"items": [], "next_after_seq": 0}))
            if args[0] == "prompt":
                prompt_path = Path(args[args.index("--prompt-file") + 1])
                prompts.append(prompt_path.read_text())
                return completed(json.dumps({"session_id": "session-timeout", "turn_id": 2}))
            if args[0] == "suspend":
                if "--acknowledge-unpublished-work" not in args:
                    return completed(
                        "",
                        1,
                        "unpublished work; retry with --acknowledge-unpublished-work",
                    )
                return completed("{}")
            self.fail(f"unexpected fake mj command: {args}")

        timeout = monitor.TurnResult("running", "timeout", timed_out=True)
        finished = monitor.TurnResult("completed", "finished")
        with mock.patch.object(
            monitor, "supervise_turn", side_effect=[timeout, finished]
        ), mock.patch.object(monitor, "mj_command", side_effect=fake_mj):
            result = monitor.run_session_lifecycle(
                self.conn,
                self.transport,
                make_run(),
                "session-timeout",
                "ci-repair/42-1",
                3600,
            )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.handoff_completed)
        self.assertIn("mj resume --session session-timeout", prompts[0])
        self.assertIn("ci-repair/42-1", prompts[0])
        self.assertIn("List any unpushed commits", prompts[0])
        self.assertIn("Do not push any commit", prompts[0])
        self.assertLess(commands.index("interrupt-turn"), commands.index("prompt"))
        self.assertEqual(commands.count("suspend"), 2)

    def test_session_is_suspended_after_success_and_supervision_failure(self):
        insert_invocation(self.conn, session_id="session-outcome")
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.conn.execute(
                    "UPDATE invocations SET mj_transcript_after_seq = 0 WHERE workflow_run_id = 42"
                )
                self.conn.commit()
                calls = []

                def fake_mj(args, *, timeout=60):
                    calls.append(args[0])
                    if args[0] == "transcript":
                        return completed(json.dumps({"items": [], "next_after_seq": 0}))
                    if args[0] == "suspend":
                        return completed("{}")
                    self.fail(f"unexpected fake mj command: {args}")

                wait = mock.Mock(
                    side_effect=monitor.MjError("daemon unavailable")
                    if failure
                    else None,
                    return_value=monitor.TurnResult("completed", "finished"),
                )
                with mock.patch.object(
                    monitor, "supervise_turn", wait
                ), mock.patch.object(monitor, "mj_command", side_effect=fake_mj):
                    if failure:
                        with self.assertRaises(monitor.MjError):
                            monitor.run_session_lifecycle(
                                self.conn, self.transport, make_run(),
                                "session-outcome", "ci-repair/42-1", 3600,
                            )
                    else:
                        monitor.run_session_lifecycle(
                            self.conn, self.transport, make_run(),
                            "session-outcome", "ci-repair/42-1", 3600,
                        )
                self.assertIn("suspend", calls)

    def test_suspend_warning_is_logged_and_posted_to_thread(self):
        insert_invocation(self.conn, session_id="session-warning")
        response = completed(
            json.dumps(
                {
                    "accepted": True,
                    "warning": "one sub-agent has not handed back",
                }
            )
        )
        with mock.patch.object(
            monitor, "mj_command", return_value=response
        ), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply")
        ) as slack:
            self.assertTrue(
                monitor.suspend_session(
                    self.conn, self.transport, 42, "session-warning"
                )
            )
        self.assertIn("one sub-agent has not handed back", slack.call_args.args[1])
        self.assertEqual(slack.call_args.kwargs["thread_ts"], "thread-1")

    def test_pending_suspend_retries_once_then_notifies_once(self):
        insert_invocation(
            self.conn,
            status="completed",
            session_id="session-still-running",
        )
        self.conn.execute(
            "UPDATE invocations SET suspend_requested = 1 WHERE workflow_run_id = 42"
        )
        self.conn.commit()
        commands = []

        def fake_mj(args, *, timeout=60):
            commands.append(args[0])
            if args[0] == "sessions":
                return completed(
                    json.dumps({"id": "session-still-running", "state": "running"})
                )
            if args[0] == "suspend":
                return completed('{"accepted":true}')
            self.fail(f"unexpected fake mj command: {args}")

        with mock.patch.object(monitor, "mj_command", side_effect=fake_mj), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply")
        ) as slack:
            monitor.check_pending_suspensions(self.conn, self.transport)
            monitor.check_pending_suspensions(self.conn, self.transport)
            monitor.check_pending_suspensions(self.conn, self.transport)
        self.assertEqual(commands.count("suspend"), 1)
        self.assertEqual(slack.call_count, 1)
        row = self.conn.execute(
            "SELECT suspend_retry_count, suspend_failure_notified FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["suspend_retry_count"], 1)
        self.assertEqual(row["suspend_failure_notified"], 1)

    def test_suspend_verification_failure_notifies_after_three_ticks(self):
        insert_invocation(
            self.conn,
            status="completed",
            session_id="session-unreachable",
        )
        self.conn.execute(
            "UPDATE invocations SET suspend_requested = 1 WHERE workflow_run_id = 42"
        )
        self.conn.commit()
        with mock.patch.object(
            monitor,
            "mj_command",
            return_value=completed("", 1, "daemon unreachable"),
        ), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply")
        ) as slack:
            for _ in range(3):
                monitor.check_pending_suspensions(self.conn, self.transport)
        self.assertEqual(slack.call_count, 1)
        self.assertIn("3 consecutive times", slack.call_args.args[1])
        row = self.conn.execute(
            "SELECT suspend_verify_failures, suspend_failure_notified FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["suspend_verify_failures"], 3)
        self.assertEqual(row["suspend_failure_notified"], 1)

    def test_pr_detection_queries_the_exact_repair_branch(self):
        expected = monitor.RepairPullRequest(
            151, "https://github.com/BrokkAi/bifrost-dev/pull/151", "OPEN", "head-sha"
        )
        with mock.patch.object(
            monitor,
            "run_command",
            return_value=json.dumps(
                [{
                    "number": expected.number,
                    "url": expected.url,
                    "state": expected.state,
                    "headRefOid": expected.head_ref_oid,
                }]
            ),
        ) as command:
            self.assertEqual(
                monitor.find_repair_pr("ci-repair/42-3"), expected
            )
        self.assertEqual(
            command.call_args.args[0],
            [
                str(monitor.GH_BIN), "pr", "list", "--repo", monitor.REPO_NAME,
                "--head", "ci-repair/42-3", "--state", "all", "--json",
                "number,url,state,headRefOid",
            ],
        )

    def test_ci_fix_listing_requests_open_pr_context(self):
        expected = monitor.QueuedRepairPR(
            301,
            "https://github.com/BrokkAi/bifrost-dev/pull/301",
            "Fix allocator failure",
            "ci-repair/41-1",
            "Fails in test_allocator; evidence points to commit abc123.",
        )
        with mock.patch.object(
            monitor,
            "run_command",
            return_value=json.dumps(
                [{
                    "number": expected.number,
                    "url": expected.url,
                    "title": expected.title,
                    "headRefName": expected.head_ref_name,
                    "body": expected.body,
                }]
            ),
        ) as command:
            self.assertEqual(monitor.list_open_ci_fix_prs(), [expected])
        self.assertEqual(
            command.call_args.args[0],
            [
                str(monitor.GH_BIN), "pr", "list", "--repo", monitor.REPO_NAME,
                "--label", "ci-fix", "--state", "open", "--json",
                "number,url,title,headRefName,body",
            ],
        )

    def test_ci_fix_pr_listing_failure_blocks_before_invocation_claim(self):
        with mock.patch.object(
            monitor, "list_open_ci_fix_prs", side_effect=monitor.CommandError("offline")
        ) as listing, mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ) as slack:
            for _ in range(2):
                self.assertIsNone(
                    monitor.prepare_queued_prs_before_launch(
                        self.conn, self.transport, make_run(), "thread-1"
                    )
                )
        self.assertEqual(listing.call_count, 2)
        notification = self.conn.execute(
            "SELECT reason, details FROM blocked_notifications "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(notification["reason"], "github_pr_list_failed")
        self.assertIn("offline", notification["details"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0], 0
        )
        self.assertEqual(slack.call_count, 1)

    def test_queued_pr_context_is_passed_to_session_prompt(self):
        run = make_run()
        queued_prs = [
            monitor.QueuedRepairPR(
                301,
                "https://github.com/BrokkAi/bifrost-dev/pull/301",
                "Fix allocator failure",
                "ci-repair/41-1",
                "Same failing test and evidence.",
            )
        ]
        with mock.patch.object(
            monitor, "lookup_launch_session", return_value=None
        ), mock.patch.object(
            monitor, "build_prompt", return_value="prompt"
        ) as build_prompt, mock.patch.object(
            monitor, "mj_command", return_value=completed('{"session_id":"s-42"}')
        ):
            session_id, branch = monitor.launch_mj_session_with_queued_prs(
                run, "b" * 40, 1, None, queued_prs
            )
        self.assertEqual((session_id, branch), ("s-42", "ci-repair/42-1"))
        build_prompt.assert_called_once_with(
            run, None, "ci-repair/42-1", queued_prs
        )

    def test_no_pr_runs_escalation_detection(self):
        insert_invocation(self.conn, status="running", session_id="s-42")
        result = monitor.SessionResult(
            "completed",
            "<@U08P3FAEU3G> filed https://github.com/BrokkAi/bifrost-dev/issues/88",
            False,
            False,
        )
        with mock.patch.object(monitor, "failing_signature", return_value=""), mock.patch.object(
            monitor, "detect_escalation"
        ) as detect, mock.patch.object(monitor, "slack_send", return_value=(True, "reply-ts")):
            detect.return_value = (True, "https://github.com/BrokkAi/bifrost-dev/issues/88")
            monitor.finalize_invocation(
                self.conn, self.transport, make_run(), result, None
            )
        detect.assert_called_once()
        row = self.conn.execute(
            "SELECT status, issue_url, repair_pr_url FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(
            row["issue_url"], "https://github.com/BrokkAi/bifrost-dev/issues/88"
        )
        self.assertIsNone(row["repair_pr_url"])

    def test_pr_lookup_failure_retries_next_tick_then_finalizes(self):
        insert_invocation(self.conn, status="running", session_id="s-42")
        result = monitor.SessionResult("completed", "captured", False, False)
        with mock.patch.object(
            monitor, "find_repair_pr", side_effect=monitor.CommandError("GitHub unavailable")
        ), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ):
            found = monitor.detect_and_finalize_pr(
                self.conn, self.transport, make_run(), result, "ci-repair/42-1"
            )
        self.assertFalse(found)
        pending = self.conn.execute(
            "SELECT status, pr_detection_failures FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(pending["status"], "pr_detection_pending")
        self.assertEqual(pending["pr_detection_failures"], 1)
        repair_pr = monitor.RepairPullRequest(
            152, "https://github.com/BrokkAi/bifrost-dev/pull/152", "OPEN", "sha"
        )
        with mock.patch.object(
            monitor, "find_repair_pr", return_value=repair_pr
        ), mock.patch.object(
            monitor, "failing_signature", return_value=""
        ), mock.patch.object(
            monitor, "detect_escalation"
        ) as detect, mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ) as slack:
            monitor.retry_pending_pr_detections(self.conn, self.transport)
        detect.assert_not_called()
        self.assertIn(
            f"opened <{repair_pr.url}|PR #152>", slack.call_args.args[1]
        )
        row = self.conn.execute(
            "SELECT status, issue_url, repair_pr_url FROM invocations "
            "WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["issue_url"])
        self.assertEqual(row["repair_pr_url"], repair_pr.url)

    def test_repeated_pr_lookup_failures_finalize_distinct_status(self):
        insert_invocation(self.conn, status="running", session_id="s-42")
        result = monitor.SessionResult("completed", "captured", False, False)
        with mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ) as slack:
            for _ in range(monitor.PR_DETECTION_FAILURE_THRESHOLD):
                monitor.record_pr_detection_failure(
                    self.conn, self.transport, make_run(), result,
                    monitor.CommandError("GitHub unavailable"),
                )
        row = self.conn.execute(
            "SELECT status, pr_detection_failures, finished_at, "
            "outcome_notification_attempted FROM invocations WHERE workflow_run_id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "pr_detection_failed")
        self.assertEqual(row["pr_detection_failures"], monitor.PR_DETECTION_FAILURE_THRESHOLD)
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(row["outcome_notification_attempted"], 1)
        self.assertIn("escalation detection was skipped", slack.call_args.args[1])

    def test_no_pr_without_mentions_defers_to_queued_pr(self):
        insert_invocation(self.conn, status="running", session_id="s-42")
        queued_pr = monitor.QueuedRepairPR(
            301,
            "https://github.com/BrokkAi/bifrost-dev/pull/301",
            "Fix allocator failure",
            "ci-repair/41-1",
            "The same failing test is addressed here.",
        )
        with self.conn:
            self.conn.execute(
                "UPDATE invocations SET queued_ci_fix_prs_json = ? "
                "WHERE workflow_run_id = 42",
                (monitor.serialize_queued_prs([queued_pr]),),
            )
        result = monitor.SessionResult("completed", "Already covered by queued PR.", False, False)
        with mock.patch.object(
            monitor, "failing_signature", return_value=""
        ), mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply-ts")
        ) as slack:
            monitor.finalize_invocation(
                self.conn, self.transport, make_run(), result, None
            )
        message = slack.call_args.args[1]
        self.assertIn("deferred to the queued PR", message)
        self.assertIn(f"<{queued_pr.url}|PR #301>", message)
        self.assertIn("No new repair PR was opened", message)
        self.assertNotIn("<@", message)

    def test_legacy_rows_are_finished_history_not_recovery_work(self):
        monitor.ensure_column(
            self.conn, "invocations", "recovery_manifest_path", "TEXT"
        )
        monitor.ensure_column(self.conn, "invocations", "recovery_status", "TEXT")
        for run_id, old_status in enumerate(
            ("modified_worktree", "interrupted", "orphaned_candidate"), start=100
        ):
            insert_invocation(
                self.conn,
                run_id=run_id,
                status=old_status,
                session_id="legacy-session",
            )
            self.conn.execute(
                "UPDATE invocations SET recovery_manifest_path = '/old/manifest.json', "
                "recovery_status = 'failed' WHERE workflow_run_id = ?",
                (run_id,),
            )
        self.conn.commit()
        self.assertTrue(monitor.invocation_exists(self.conn, 100))
        self.assertTrue({100, 101, 102}.issubset(monitor.handled_run_ids(self.conn)))
        with mock.patch.object(monitor, "mj_command") as command:
            monitor.reattach_running_invocations(self.conn, self.transport)
        command.assert_not_called()

    def test_blocked_notification_is_once_per_run_and_reason(self):
        run = make_run()
        with mock.patch.object(
            monitor,
            "slack_send",
            side_effect=[(False, None), (True, "thread"), (True, "thread")],
        ) as slack:
            monitor.record_blocked_reason(
                self.conn, self.transport, run, "daemon_unreachable", "not connected"
            )
            monitor.record_blocked_reason(
                self.conn, self.transport, run, "daemon_unreachable", "still not connected"
            )
            monitor.record_blocked_reason(
                self.conn, self.transport, run, "daemon_unreachable", "delivered now"
            )
            monitor.record_blocked_reason(
                self.conn, self.transport, run, "mj_too_old", "missing transcript option"
            )
        rows = self.conn.execute(
            "SELECT workflow_run_id, reason, slack_notification_attempted "
            "FROM blocked_notifications ORDER BY reason"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(slack.call_count, 3)
        self.assertTrue(all(row["slack_notification_attempted"] for row in rows))

    def test_connect_db_adds_cursor_columns_to_existing_invocations(self):
        self.conn.close()
        legacy_path = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute(
                """
                CREATE TABLE invocations (
                    workflow_run_id INTEGER PRIMARY KEY,
                    sha TEXT NOT NULL,
                    workflow_run_url TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
        with mock.patch.object(monitor, "DB_PATH", legacy_path):
            migrated = monitor.connect_db()
        try:
            columns = {
                row["name"]
                for row in migrated.execute("PRAGMA table_info(invocations)")
            }
            self.assertIn("mj_transcript_after_seq", columns)
            self.assertIn("workflow", columns)
            self.assertIn("suspend_requested", columns)
            self.assertIn("suspend_retry_count", columns)
            self.assertIn("suspend_failure_notified", columns)
            self.assertIn("suspend_verify_failures", columns)
            self.assertIn("repair_pr_url", columns)
            self.assertIn("pr_detection_failures", columns)
            self.assertIn("pr_detection_error", columns)
            self.assertIn("session_result_status", columns)
            self.assertIn("queued_ci_fix_prs_json", columns)
        finally:
            migrated.close()


class PromptContractTests(unittest.TestCase):
    def test_repair_prompt_requires_trailer_and_pr_publication(self):
        prompt = monitor.build_prompt(make_run(), branch="ci-repair/42-3")
        self.assertIn("CI-Repair-Run: 42", prompt)
        self.assertIn(
            "git push origin HEAD:refs/heads/ci-repair/42-3", prompt
        )
        self.assertIn(
            "gh pr create --base master --head ci-repair/42-3 --label ci-fix",
            prompt,
        )
        self.assertIn("failing run link", prompt)
        self.assertIn("introducing commit", prompt)
        self.assertIn("evidence", prompt)
        self.assertNotIn("git push origin HEAD:master", prompt)
        self.assertNotIn("merge origin/master", prompt)
        self.assertIn("Never push to master or force-push", prompt)
        self.assertIn("Do not merge the PR yourself", prompt)
        self.assertIn("Every commit you make must include the trailer", prompt)
        self.assertIn("URL of the revert PR", prompt)
        self.assertNotIn("WORKTREE_BRANCH", prompt)
        self.assertNotIn("monitor owns", prompt.lower())

    def test_queued_ci_fix_pr_context_reaches_prompt_with_noop_rules(self):
        queued_pr = monitor.QueuedRepairPR(
            301,
            "https://github.com/BrokkAi/bifrost-dev/pull/301",
            "Fix allocator failure",
            "ci-repair/41-1",
            "Fails in test_allocator; evidence points to commit abc123.",
        )
        prompt = monitor.build_prompt(
            make_run(),
            branch="ci-repair/42-1",
            queued_prs=[queued_pr],
        )
        self.assertIn(queued_pr.url, prompt)
        self.assertIn(queued_pr.title, prompt)
        self.assertIn(queued_pr.head_ref_name, prompt)
        self.assertIn(queued_pr.body, prompt)
        self.assertIn("SAME problem", prompt)
        self.assertIn("make no changes, open no PR or issue, ping no one", prompt)
        self.assertIn("NEW failure on top", prompt)
        self.assertIn("Do not touch, update, close, or merge any queued PR", prompt)

    def test_timeout_handoff_keeps_session_and_forbids_push(self):
        prompt = monitor.build_timeout_handoff_prompt(
            make_run(), "session-42", "ci-repair/42-1"
        )
        self.assertIn("session session-42", prompt)
        self.assertIn("branch ci-repair/42-1", prompt)
        self.assertIn("mj resume --session session-42", prompt)
        self.assertIn("Do not push any commit", prompt)
        self.assertIn("List any unpushed commits", prompt)
        self.assertNotIn("recovery block", prompt.lower())


class GitHubAuthTests(unittest.TestCase):
    def setUp(self):
        monitor.reset_github_auth_cache()
        monitor.REQUIRE_APP_TOKEN = True
        monitor.GH_AUTH_FAILURE_HANDLER = None

    def tearDown(self):
        monitor.reset_github_auth_cache()
        monitor.REQUIRE_APP_TOKEN = True
        monitor.GH_AUTH_FAILURE_HANDLER = None

    def test_monitor_refuses_to_poll_without_app_token_and_notifies_once_per_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = monitor.SlackTransport("webhook", webhook="test")
            token_failure = completed("unknown command github-token", 1)
            with (
                mock.patch.object(monitor, "STATE_DIR", root / "state"),
                mock.patch.object(monitor, "LOCK_PATH", root / "state" / "monitor.lock"),
                mock.patch.object(monitor, "DB_PATH", root / "activity.db"),
                mock.patch.object(monitor, "load_slack_transport", return_value=transport),
                mock.patch.object(monitor, "mj_command", return_value=token_failure),
                mock.patch.object(monitor, "poll_ci") as poll,
                mock.patch.object(monitor, "slack_send", return_value=(True, None)) as slack,
            ):
                self.assertEqual(monitor.run_monitor(), 3)
                self.assertEqual(monitor.run_monitor(), 3)
                poll.assert_not_called()
                self.assertEqual(slack.call_count, 1)

            conn = sqlite3.connect(root / "activity.db")
            try:
                row = conn.execute(
                    "SELECT reason, slack_notification_attempted "
                    "FROM blocked_notifications WHERE workflow_run_id = -1"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, ("github_app_token_unavailable", 1))

    def test_gh_receives_app_token_and_cached_token_refreshes_after_30_minutes(self):
        token_results = [completed("first-token"), completed("second-token")]
        gh_results = [completed("ok"), completed("ok"), completed("ok")]
        with (
            mock.patch.object(monitor, "mj_command", side_effect=token_results) as mj,
            mock.patch.object(monitor.subprocess, "run", side_effect=gh_results) as run,
            mock.patch.object(
                monitor.time, "monotonic", side_effect=[100, 100, 1500, 1901, 1901]
            ),
        ):
            monitor.run_gh(["api", "user"])
            monitor.run_gh(["api", "user"])
            monitor.run_gh(["api", "user"])
        self.assertEqual(mj.call_count, 2)
        self.assertEqual(
            [call.kwargs["env"]["GH_TOKEN"] for call in run.call_args_list],
            ["first-token", "first-token", "second-token"],
        )

    def test_gh_401_forces_a_fresh_app_token_before_retry(self):
        with (
            mock.patch.object(
                monitor,
                "mj_command",
                side_effect=[completed("expired-token"), completed("fresh-token")],
            ) as mj,
            mock.patch.object(
                monitor.subprocess,
                "run",
                side_effect=[completed("HTTP 401: Unauthorized", 1), completed("ok")],
            ) as run,
        ):
            self.assertEqual(monitor.run_gh(["api", "user"]), "ok")
        self.assertEqual(mj.call_count, 2)
        self.assertEqual(
            [call.kwargs["env"]["GH_TOKEN"] for call in run.call_args_list],
            ["expired-token", "fresh-token"],
        )
