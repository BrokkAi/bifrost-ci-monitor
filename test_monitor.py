#!/usr/bin/python3
"""Focused tests for the Bifrost CI monitor's episode lifecycle."""

from __future__ import annotations

import datetime as dt
import hashlib
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


class InterruptTurnTests(unittest.TestCase):
    def test_already_ended_replies_are_idempotent(self):
        for detail in ['no active turn', 'nothing is running', 'turn is not running',
                       '409 Conflict: this session has no turn to cancel']:
            with self.subTest(detail=detail), mock.patch.object(monitor, 'mj_command',
                    return_value=subprocess.CompletedProcess([], 1, '', detail)) as command:
                monitor.interrupt_turn('session')
                command.assert_called_once_with(
                    ['interrupt-turn', '--session', 'session', '--json'], timeout=60)

    def test_other_conflicts_and_daemon_failures_still_raise(self):
        for detail, reason in [('409 Conflict: session operation in progress', 'mj_supervision_failed'),
                               ('connection refused', 'daemon_unreachable')]:
            with self.subTest(detail=detail), mock.patch.object(monitor, 'mj_command',
                    return_value=subprocess.CompletedProcess([], 1, '', detail)):
                with self.assertRaises(monitor.MjError) as error:
                    monitor.interrupt_turn('session')
                self.assertEqual(error.exception.reason, reason)


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
        dossier = mock.patch.object(monitor, "repair_dossier", return_value="test repair dossier")
        self.dossier = dossier.start()
        self.addCleanup(dossier.stop)
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

    def test_mj_new_argv_uses_single_model_subagents_when_configured(self):
        with mock.patch.object(monitor, "MJ_SUBAGENT_MODEL", "gpt-6-luna"):
            argv = monitor.new_session_argv(make_run(), "a" * 40, 1, "/tmp/p")
        start = argv.index("--subagents")
        self.assertEqual(
            argv[start:start + 4],
            ["--subagents", "single-model", "--subagent-model", "gpt-6-luna"],
        )

    def test_mj_new_argv_has_required_selectors_and_no_profile(self):
        run = make_run()
        self.assertEqual(monitor.MJ_MODEL, "deepseek-flash")
        self.assertIsNone(monitor.MJ_SUBAGENT_MODEL)
        self.assertEqual(monitor.AGENT_LABEL, "DeepSeek Flash (mj)")
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
                "--cpus",
                "32",
                "--memory-gib",
                "28",
                "--model",
                "deepseek-flash",
                "--subagents",
                "none",
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
            prompt = Path(args[args.index("--prompt-file") + 1]).read_text()
            self.assertIn("test repair dossier", prompt)
            return completed('{"session_id":"s-42"}')

        with mock.patch.object(monitor, "mj_command", side_effect=fake_mj) as command:
            session_id, branch = monitor.launch_mj_session(run, "b" * 40, 1, None)
        self.assertEqual((session_id, branch), ("s-42", "ci-repair/42-1"))
        self.dossier.assert_called_once_with(run, "b" * 40)
        argv = command.call_args_list[-1].args[0]
        self.assertEqual(
            argv[:17],
            [
                "new",
                "--workspace",
                "CI",
                "--target",
                "podman",
                "--bundle",
                "bifrost",
                "--cpus",
                "32",
                "--memory-gib",
                "28",
                "--model",
                "deepseek-flash",
                "--subagents",
                "none",
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
        suspend_argv = []
        prompts = []

        def fake_mj(args, *, timeout=60):
            commands.append(args[0])
            if args[0] == "interrupt-turn":
                return completed("{}")
            if args[0] == "wait":
                return completed(json.dumps({"outcome": "cancelled"}))
            if args[0] == "transcript":
                return completed(json.dumps({"items": [], "next_after_seq": 0}))
            if args[0] == "suspend":
                suspend_argv.append(args)
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
        def message(session_id, text, **kwargs):
            commands.append('message')
            prompts.append(text)
            self.assertTrue(kwargs['request_id'].startswith('ci-timeout-'))
            return {'session_id': session_id, 'via': 'mailbox'}
        with mock.patch.object(
            monitor, "supervise_turn", side_effect=[timeout, finished]
        ), mock.patch.object(monitor, "mj_command", side_effect=fake_mj), \
             mock.patch.object(monitor, 'send_session_message', side_effect=message):
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
        self.assertLess(commands.index("interrupt-turn"), commands.index("message"))
        self.assertEqual(commands.count("suspend"), 1)
        self.assertIn("--acknowledge-unpublished-work", suspend_argv[0])

    def test_session_is_suspended_after_success_but_kept_live_on_supervision_failure(self):
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
                self.assertEqual("suspend" in calls, not failure)

    def test_unlimited_repair_survives_poll_timeouts_without_interrupt_or_handoff(self):
        insert_invocation(self.conn, status="running", session_id="long-repair")
        with self.conn:
            self.conn.execute("UPDATE invocations SET started_at='2020-01-01T00:00:00Z'")
        with (
            mock.patch.object(monitor, "supervise_turn", side_effect=[
                monitor.TurnResult("running", "timeout", timed_out=True),
                monitor.TurnResult("running", "timeout", timed_out=True),
                monitor.TurnResult("completed", "finished"),
            ]) as wait,
            mock.patch.object(monitor, "interrupt_and_wait") as interrupt,
            mock.patch.object(monitor, "send_session_message") as prompt,
            mock.patch.object(monitor, "drain_transcript"),
            mock.patch.object(monitor, "read_complete_agent_transcript", return_value="Fixed"),
            mock.patch.object(monitor, "suspend_session") as suspend,
        ):
            result = monitor.run_session_lifecycle(
                self.conn, self.transport, make_run(), "long-repair", "ci-repair/42-1", None,
            )
        self.assertEqual(wait.call_count, 3)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.status, "completed")
        interrupt.assert_not_called()
        prompt.assert_not_called()
        suspend.assert_called_once()

    def test_restart_reattaches_old_repair_without_a_remaining_time_budget(self):
        insert_invocation(self.conn, status="running", session_id="long-repair")
        with self.conn:
            self.conn.execute("UPDATE invocations SET started_at='2020-01-01T00:00:00Z'")
        with (
            mock.patch.object(monitor, "require_mj_success", return_value=json.dumps({
                "state": "running", "is_idle": False, "chat_phase": "running",
            })),
            mock.patch.object(monitor, "run_session_lifecycle", return_value=monitor.SessionResult(
                "completed", "Fixed", False, False,
            )) as lifecycle,
            mock.patch.object(monitor, "detect_and_finalize_pr"),
        ):
            monitor.reattach_running_invocations(self.conn, self.transport)
        self.assertIsNone(lifecycle.call_args.args[5])

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
        ) as mj, mock.patch.object(
            monitor, "slack_send", return_value=(True, "reply")
        ) as slack:
            self.assertTrue(
                monitor.suspend_session(
                    self.conn, self.transport, 42, "session-warning"
                )
            )
        self.assertIn("--acknowledge-unpublished-work", mj.call_args.args[0])
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
            run, None, "ci-repair/42-1", queued_prs,
            known_failures_context="",
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
        self.assertEqual(monitor.MJ_CPUS, 32)
        self.assertEqual(monitor.MJ_MEMORY_GIB, 28)
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
        for expected in (
            "`eatmydata cargo nextest run ...`",
            "do not export LD_PRELOAD for the whole session",
            "a missing `./target` in the checkout is expected",
            ".github/workflows/AGENTS.md",
            "Disk sync writes",
        ):
            self.assertIn(expected, prompt)
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
                mock.patch.object(monitor, "runtime_binary_issues", return_value=[]),
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

    def test_missing_runtime_binaries_are_reported_once_per_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conn_patch = mock.patch.object(monitor, "DB_PATH", root / "activity.db")
            conn_patch.start()
            self.addCleanup(conn_patch.stop)
            conn = monitor.connect_db()
            self.addCleanup(conn.close)
            transport = monitor.SlackTransport("webhook", webhook="test")
            with (
                mock.patch.object(monitor, "GH_BIN", root / "missing-gh"),
                mock.patch.object(monitor, "MJ_BIN", root / "missing-mj"),
                mock.patch.object(monitor, "slack_send", return_value=(True, None)) as slack,
            ):
                self.assertFalse(monitor.ensure_runtime_binaries(conn, transport))
                self.assertFalse(monitor.ensure_runtime_binaries(conn, transport))
            self.assertEqual(slack.call_count, 2)
            reasons = [row[0] for row in conn.execute(
                "SELECT reason FROM blocked_notifications WHERE workflow_run_id=-1 ORDER BY reason"
            )]
            self.assertEqual(reasons, ["gh_missing", "mj_missing"])

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


class KnownFailureLedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        monitor.ensure_known_failure_schema(self.conn)
        self.transport = monitor.SlackTransport("webhook", webhook="https://hooks.slack.com/services/test")

    def tearDown(self):
        self.conn.close()

    def test_job_name_normalizer_removes_runs_on_and_keeps_matrix_values(self):
        runner_job = (
            "os matrix / extension boundary "
            "(runs-on=37051646884-1-hourly-extension-windows-x64/"
            "image=windows25-full-x64/family=m7i+m7a/cpu=4/ram=16/"
            "volume=60gb/extras=s3-cache)"
        )
        self.assertEqual(
            monitor.normalize_ci_job_name(runner_job),
            "os matrix / extension boundary",
        )
        self.assertEqual(
            monitor.normalize_ci_job_name("compile (x86_64-unknown-linux-gnu)"),
            "compile (x86_64-unknown-linux-gnu)",
        )
        self.assertEqual(
            monitor.normalize_ci_job_name("compile (windows-latest)"),
            "compile (windows-latest)",
        )
        self.assertEqual(
            monitor.normalize_ci_job_name(
                "compile (x86_64-unknown-linux-gnu, runs-on=123/image=linux)"
            ),
            "compile (x86_64-unknown-linux-gnu)",
        )
        self.assertEqual(
            monitor.normalize_ci_job_name("compile runs-on=123/image=linux"),
            "compile",
        )

    def test_job_name_migration_merges_duplicates_and_removes_pr_verification(self):
        runner_one = "extension boundary (runs-on=111/image=windows/family=m7i)"
        runner_two = "extension boundary (runs-on=222/image=windows/family=m7a)"

        def insert_row(
            job: str, *, first_sha: str, first_id: int, first_at: str,
            last_sha: str, last_id: int, last_at: str, status: str,
            fixed_at: str | None = None, fixed_sha: str | None = None,
            pr: str | None = None, pr_state: str = "OPEN",
            issue: str | None = None, diagnosis: str | None = None,
            diagnosis_source: str | None = None, updated_at: str,
            identity: str = "Build",
        ):
            self.conn.execute(
                "INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
                "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
                "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,"
                "last_seen_failed_steps_json,status,fixed_at,fixed_by_sha,linked_pr_url,"
                "linked_pr_state,linked_issue_url,linked_issue_state,diagnosis,"
                "diagnosis_source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("Hourly CI", job, "step", identity, first_sha, first_id, f"run-{first_id}",
                 first_at, last_sha, last_id, f"run-{last_id}", last_at, '["Build"]',
                 status, fixed_at, fixed_sha, pr, pr_state, issue, "OPEN", diagnosis,
                 diagnosis_source, updated_at),
            )

        insert_row(
            runner_one, first_sha="a" * 40, first_id=1, first_at="2026-01-01T00:00:00+00:00",
            last_sha="b" * 40, last_id=2, last_at="2026-01-02T00:00:00+00:00",
            status="fixed", fixed_at="2026-01-03T00:00:00+00:00", fixed_sha="c" * 40,
            pr="https://github.com/example/pull/1", pr_state="CLOSED",
            diagnosis="known compiler issue", diagnosis_source="automerge",
            updated_at="2026-01-03T00:00:00+00:00",
        )
        insert_row(
            runner_two, first_sha="d" * 40, first_id=3, first_at="2026-01-02T00:00:00+00:00",
            last_sha="e" * 40, last_id=4, last_at="2026-01-04T00:00:00+00:00",
            status="open", issue="https://github.com/example/issues/8",
            updated_at="2026-01-04T00:00:00+00:00",
        )
        insert_row(
            "PR verification", first_sha="f" * 40, first_id=5,
            first_at="2026-01-04T00:00:00+00:00", last_sha="f" * 40,
            last_id=5, last_at="2026-01-04T00:00:00+00:00", status="open",
            updated_at="2026-01-04T00:00:00+00:00", identity="Required checks",
        )
        previous_body = monitor._known_failure_issue_body(self.conn)
        monitor._set_known_failure_state(self.conn, "issue_number", "4519")
        monitor._set_known_failure_state(self.conn, "issue_labeled", "1")
        monitor._set_known_failure_state(self.conn, "issue_pinned", "1")
        monitor._set_known_failure_state(
            self.conn, "issue_body_sha256",
            hashlib.sha256(previous_body.encode("utf-8")).hexdigest(),
        )
        self.conn.execute(
            "DELETE FROM known_failure_state WHERE key=?",
            (monitor.KNOWN_FAILURE_JOB_NAME_MIGRATION,),
        )
        self.conn.commit()

        monitor.ensure_known_failure_schema(self.conn)
        rows = self.conn.execute("SELECT * FROM known_failures").fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["job_name"], "extension boundary")
        self.assertEqual(row["first_seen_sha"], "a" * 40)
        self.assertEqual(row["first_seen_run_id"], 1)
        self.assertEqual(row["first_seen_run_url"], "run-1")
        self.assertEqual(row["first_seen_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(row["last_seen_sha"], "e" * 40)
        self.assertEqual(row["last_seen_run_id"], 4)
        self.assertEqual(row["last_seen_run_url"], "run-4")
        self.assertEqual(row["last_seen_at"], "2026-01-04T00:00:00+00:00")
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["fixed_at"])
        self.assertEqual(row["linked_pr_url"], "https://github.com/example/pull/1")
        self.assertEqual(row["linked_issue_url"], "https://github.com/example/issues/8")
        self.assertEqual(row["diagnosis"], "known compiler issue")
        self.assertEqual(row["diagnosis_source"], "automerge")
        with mock.patch.object(monitor, "run_gh", return_value="4519") as gh:
            monitor._sync_known_failure_issue(self.conn)
        gh.assert_called_once()
        patch_args = gh.call_args.args[0]
        body = patch_args[patch_args.index("--raw-field") + 1]
        self.assertIn("extension boundary", body)
        self.assertNotIn("runs-on=", body)

        monitor.ensure_known_failure_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM known_failures").fetchone()[0], 1)

    def test_ledger_excludes_aggregate_pr_verification_job(self):
        import automerge

        report = automerge.FailureReport(
            frozenset({"CI/PR verification", "CI/unit"}),
            {
                "CI/PR verification": automerge.FailedJobDetails(
                    frozenset({"Verify required jobs"}), frozenset()
                ),
                "CI/unit (runs-on=123/image=windows)": automerge.FailedJobDetails(
                    frozenset({"Run tests"}), frozenset({"pytest:tests/test_api.py::test_bad"})
                ),
            },
            "logs",
        )
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=report):
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(301, "a" * 40))
        rows = self.conn.execute("SELECT job_name FROM known_failures").fetchall()
        self.assertEqual([row["job_name"] for row in rows], ["unit"])

    @staticmethod
    def run_item(run_id: int, sha: str, conclusion: str = "failure") -> dict:
        return {
            "databaseId": run_id,
            "headSha": sha,
            "url": f"https://github.com/BrokkAi/bifrost-dev/actions/runs/{run_id}",
            "conclusion": conclusion,
        }

    def test_ledger_upserts_parser_identity_and_open_to_fixed(self):
        import automerge

        report = automerge.FailureReport(
            frozenset({"CI/unit"}),
            {"CI/unit": automerge.FailedJobDetails(
                frozenset({"Run tests"}), frozenset({"pytest:tests/test_api.py::test_old"})
            )},
            "pytest log",
        )
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=report):
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(101, "a" * 40))
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(102, "b" * 40))
        row = self.conn.execute("SELECT * FROM known_failures").fetchone()
        self.assertEqual(row["first_seen_sha"], "a" * 40)
        self.assertEqual(row["last_seen_sha"], "b" * 40)
        self.assertEqual(row["last_seen_run_id"], 102)
        self.assertEqual(row["status"], "open")

        passing = automerge.FailureReport(
            frozenset(), {}, "", frozenset({"CI/unit"})
        )
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=passing):
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(103, "c" * 40, "success"))
        fixed = self.conn.execute("SELECT * FROM known_failures").fetchone()
        self.assertEqual(fixed["status"], "fixed")
        self.assertEqual(fixed["fixed_by_sha"], "c" * 40)
        self.assertTrue(fixed["fixed_at"])

    def test_cancelled_run_teaches_nothing(self):
        import automerge

        report = automerge.FailureReport(
            frozenset({"CI/unit"}),
            {"CI/unit": automerge.FailedJobDetails(
                frozenset({"Run tests"}), frozenset({"pytest:tests/test_api.py::test_old"})
            )},
            "",
        )
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=report):
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(201, "a" * 40))
        with mock.patch.object(automerge, "collect_failure_report_for_run") as collect:
            monitor._process_known_failure_run(
                self.conn, "CI", self.run_item(202, "b" * 40, "cancelled")
            )
        collect.assert_not_called()
        row = self.conn.execute("SELECT status,last_seen_sha FROM known_failures").fetchone()
        self.assertEqual(tuple(row), ("open", "a" * 40))

    def test_interrupted_and_unrelated_failed_steps_do_not_clear_prior_tests(self):
        import automerge

        old = "pytest:tests/test_api.py::test_old"
        initial = automerge.FailureReport(frozenset({"CI/unit"}), {
            "CI/unit": automerge.FailedJobDetails(frozenset({"Run tests"}), frozenset({old}))
        }, "failed tests")
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=initial):
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(101, "a" * 40))

        reports = [
            # Runner acquisition/loss: no test result.
            automerge.FailureReport(frozenset({"CI/unit"}), {
                "CI/unit": automerge.FailedJobDetails()
            }, "", incomplete_jobs=frozenset({"CI/unit"})),
            # Partial output contains a different failing test, then the runner dies.
            automerge.FailureReport(frozenset({"CI/unit"}), {
                "CI/unit": automerge.FailedJobDetails(frozenset({"Run tests"}),
                    frozenset({"pytest:tests/test_api.py::test_new"}))
            }, "partial tests", incomplete_jobs=frozenset({"CI/unit"})),
            # The job fails in checkout before the test step executes.
            automerge.FailureReport(frozenset({"CI/unit"}), {
                "CI/unit": automerge.FailedJobDetails(frozenset({"Checkout"}))
            }, "checkout error"),
            # Concluded failure with unavailable logs cannot show an old test passed.
            automerge.FailureReport(frozenset({"CI/unit"}), {
                "CI/unit": automerge.FailedJobDetails(frozenset({"Run tests"}))
            }, "", incomplete_jobs=frozenset({"CI/unit"})),
        ]
        for index, report in enumerate(reports, start=102):
            with self.subTest(run=index), mock.patch.object(
                automerge, "collect_failure_report_for_run", return_value=report
            ):
                monitor._process_known_failure_run(self.conn, "CI", self.run_item(index, "b" * 40))
                row = self.conn.execute("SELECT status,last_seen_sha,last_seen_run_id,fixed_at "
                                        "FROM known_failures WHERE identity=?", (old,)).fetchone()
                self.assertEqual(tuple(row), ("open", "a" * 40, 101, None))

    def test_completed_test_results_retire_absent_same_step_failures(self):
        import automerge

        old = "pytest:tests/test_api.py::test_old"
        new = "pytest:tests/test_api.py::test_new"
        for run_id, identity in [(101, old), (102, new)]:
            report = automerge.FailureReport(frozenset({"CI/unit"}), {
                "CI/unit": automerge.FailedJobDetails(frozenset({"Run tests"}), frozenset({identity}))
            }, "completed test results")
            with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=report):
                monitor._process_known_failure_run(self.conn, "CI", self.run_item(run_id, "a" * 40))
        rows = {r['identity']: r['status'] for r in self.conn.execute('SELECT * FROM known_failures')}
        self.assertEqual(rows, {old: 'fixed', new: 'open'})

    def test_passing_step_is_recovery_evidence_even_when_later_step_is_interrupted(self):
        import automerge

        report = automerge.FailureReport(frozenset({"CI/unit"}), {
            "CI/unit": automerge.FailedJobDetails(frozenset({"Run tests"}), frozenset({"pytest:test_old"}))
        }, "tests failed")
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=report):
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(101, "a" * 40))
        interrupted = automerge.FailureReport(frozenset({"CI/unit"}), {
            "CI/unit": automerge.FailedJobDetails()
        }, "", successful_steps={"CI/unit": frozenset({"Run tests"})},
            incomplete_jobs=frozenset({"CI/unit"}))
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=interrupted):
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(102, "b" * 40))
        row = self.conn.execute("SELECT status FROM known_failures WHERE identity='pytest:test_old'").fetchone()
        self.assertEqual(row['status'], 'fixed')

    def test_infrastructure_classification_is_scoped_to_the_observation(self):
        import automerge

        report = automerge.FailureReport(frozenset({"CI/unit"}), {
            "CI/unit": automerge.FailedJobDetails(frozenset({"Run tests"}))
        }, "")
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=report):
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(101, "a" * 40))
            self.conn.execute("UPDATE known_failures SET triage_outcome='infrastructure',diagnosis='Spot interruption',"
                              "diagnosis_source='triage'")
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(102, "a" * 40))
            self.assertEqual(self.conn.execute('SELECT triage_outcome FROM known_failures').fetchone()[0], 'infrastructure')
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(103, "b" * 40))
            self.assertIsNone(self.conn.execute('SELECT triage_outcome FROM known_failures').fetchone()[0])
            self.assertEqual(tuple(self.conn.execute('SELECT diagnosis,diagnosis_source FROM known_failures').fetchone()),
                             (None, None))
            self.conn.execute("UPDATE known_failures SET triage_outcome='infrastructure',"
                              "last_seen_failed_steps_json='[\"Run tests\",\"Upload results\"]'")
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(104, "b" * 40))
            self.assertIsNone(self.conn.execute('SELECT triage_outcome FROM known_failures').fetchone()[0])
            self.conn.execute("UPDATE known_failures SET triage_outcome='infrastructure',status='fixed'")
            monitor._process_known_failure_run(self.conn, "CI", self.run_item(105, "b" * 40))
            self.assertIsNone(self.conn.execute('SELECT triage_outcome FROM known_failures').fetchone()[0])

    def test_upkeep_five_minute_guard_is_shared_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.db"
            first = sqlite3.connect(path)
            second = sqlite3.connect(path)
            first.row_factory = second.row_factory = sqlite3.Row
            monitor.ensure_known_failure_schema(first)
            monitor.ensure_known_failure_schema(second)
            now = dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc)
            with (
                mock.patch.object(monitor, "_known_failure_runs", return_value=[]) as list_runs,
                mock.patch.object(monitor, "_sync_known_failure_issue"),
            ):
                self.assertTrue(monitor.update_known_failures(first, self.transport, now=now))
                self.assertFalse(monitor.update_known_failures(
                    second, self.transport, now=now + dt.timedelta(minutes=4, seconds=59)
                ))
            self.assertEqual(list_runs.call_count, len(monitor.TRACKED_WORKFLOWS))
            first.close()
            second.close()

    def test_initial_backfill_is_capped_to_five_runs_from_last_24_hours(self):
        now = dt.datetime(2026, 10, 6, 12, tzinfo=dt.timezone.utc)
        items = []
        for index, hours_ago in enumerate([6, 2, 48, 1, 5, 3, 4]):
            created = now - dt.timedelta(hours=hours_ago)
            items.append({
                "databaseId": 700 + index,
                "headSha": f"{index:040x}",
                "url": f"https://example.test/runs/{700 + index}",
                "conclusion": "success",
                "headBranch": "master",
                "createdAt": created.isoformat(),
            })

        with (
            mock.patch.object(monitor, "_known_failure_runs", return_value=items) as list_runs,
            mock.patch.object(monitor, "_process_known_failure_run") as process_run,
            mock.patch.object(monitor, "refresh_known_failure_link_states"),
            mock.patch.object(monitor, "_sync_known_failure_issue"),
        ):
            self.assertTrue(monitor.update_known_failures(
                self.conn, self.transport, now=now
            ))

        self.assertEqual(
            [(call.args[0], call.kwargs["limit"]) for call in list_runs.call_args_list],
            [(workflow, 5) for workflow, _event in monitor.TRACKED_WORKFLOWS],
        )
        calls_by_workflow = {}
        for call in process_run.call_args_list:
            calls_by_workflow.setdefault(call.args[1], []).append(
                call.args[2]["databaseId"]
            )
        expected_ids = {701, 703, 704, 705, 706}
        self.assertEqual(set(calls_by_workflow), {
            workflow for workflow, _event in monitor.TRACKED_WORKFLOWS
        })
        for selected_ids in calls_by_workflow.values():
            self.assertEqual(set(selected_ids), expected_ids)
            self.assertEqual(len(selected_ids), 5)

    def test_diagnosis_lines_update_only_existing_parser_seen_rows(self):
        now = monitor.utc_now()
        self.conn.execute(
            "INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
            "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
            "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,updated_at) "
            "VALUES ('CI','unit','test','pytest:tests/test_api.py::test_old',?,?,?,?,?,?,?, ?,?)",
            ("a" * 40, 9, "run-url", now, "a" * 40, 9, "run-url", now, now),
        )
        message = (
            "known-failure: CI | unit (runs-on=123/image=linux) | "
            "pytest:tests/test_api.py::test_old | old fixture contract\n"
            "known-failure: CI | unit | forged-test | should not be stored"
        )
        self.assertEqual(
            monitor.store_known_failure_diagnoses(self.conn, message, "ci-repair"), 1
        )
        rows = self.conn.execute("SELECT identity,diagnosis,diagnosis_source FROM known_failures").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["diagnosis"], "old fixture contract")
        self.assertEqual(rows[0]["diagnosis_source"], "ci-repair")

    def test_repair_links_only_parser_observed_open_failures(self):
        now = monitor.utc_now()
        self.conn.execute(
            "INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
            "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
            "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,updated_at) "
            "VALUES ('CI','unit','test','pytest:case_a',?,?,?,?,?,?,?, ?,?)",
            ("a" * 40, 9, "run", now, "a" * 40, 9, "run", now, now),
        )
        self.conn.execute(
            "INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
            "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
            "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,updated_at) "
            "VALUES ('CI','unit','test','pytest:unseen',?,?,?,?,?,?,?, ?,?)",
            ("a" * 40, 9, "run", now, "a" * 40, 9, "run", now, now),
        )
        self.conn.execute(
            "INSERT INTO known_failure_runs(workflow,run_id,sha,url,conclusion,processed_at,"
            "failure_identities_json) VALUES ('CI',9,?,'run','failure',?,?)",
            ("a" * 40, now, json.dumps([{
                "job": "unit", "kind": "test", "identity": "pytest:case_a", "steps": []
            }])),
        )
        self.assertEqual(monitor.link_known_failures_to_work(
            self.conn, 9, pr_url="https://github.com/x/pull/9"
        ), 1)
        rows = self.conn.execute(
            "SELECT identity,linked_pr_url,linked_pr_state FROM known_failures ORDER BY identity"
        ).fetchall()
        self.assertEqual(rows[0]["linked_pr_url"], "https://github.com/x/pull/9")
        self.assertEqual(rows[0]["linked_pr_state"], "OPEN")
        self.assertIsNone(rows[1]["linked_pr_url"])

    def test_prompt_context_is_capped_and_injected(self):
        now = monitor.utc_now()
        for index in range(42):
            self.conn.execute(
                "INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
                "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
                "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,updated_at) "
                "VALUES ('CI','unit','test',?,?,?,?,?,?,?,?,?,?)",
                (f"pytest:test_{index}", "a" * 40, index, "run", now,
                 "a" * 40, index, "run", now, now),
            )
        context = monitor.render_known_failures_prompt(self.conn)
        self.assertIn("2 additional open failures omitted", context)
        self.assertEqual(sum(1 for line in context.splitlines() if line.startswith("- CI /")), 40)
        self.conn.execute(
            "UPDATE known_failures SET linked_pr_url='https://github.com/x/pull/1', "
            "linked_pr_state='CLOSED' WHERE identity='pytest:test_0'"
        )
        self.conn.execute(
            "UPDATE known_failures SET linked_issue_url='https://github.com/x/issues/2', "
            "linked_issue_state='OPEN' WHERE identity='pytest:test_1'"
        )
        monitor_prompt_context = monitor.render_known_failures_prompt(
            self.conn, omit_linked=True
        )
        self.assertIn("pytest:test_0", monitor_prompt_context)
        self.assertNotIn("pytest:test_1 ", monitor_prompt_context)
        import automerge
        pull = automerge.PullRequest(7, "Change", "b" * 40, "url")
        automerge_prompt = automerge.build_prompt(
            "ledger-test", [pull], "a" * 40, known_failures_context=context
        )
        self.assertIn("Known failures on master", automerge_prompt)
        monitor_prompt = monitor.build_prompt(
            make_run(), known_failures_context=context
        )
        self.assertIn("Known failures on master", monitor_prompt)
        self.assertIn("A triage issue documents a failure available for you to repair", monitor_prompt)

    def test_known_failures_issue_updates_only_when_rendered_set_changes(self):
        now = monitor.utc_now()
        self.conn.execute(
            "INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
            "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
            "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,updated_at) "
            "VALUES ('CI','unit','step','Build',?,?,?,?,?,?,?, ?,?)",
            ("a" * 40, 9, "run-url", now, "a" * 40, 9, "run-url", now, now),
        )
        monitor._set_known_failure_state(self.conn, "issue_number", "22")
        monitor._set_known_failure_state(self.conn, "issue_labeled", "1")

        def github(args):
            if args[:2] == ["api", f"repos/{monitor.REPO_NAME}/issues/22"]:
                return "22"
            if args[:3] == ["issue", "pin", "22"]:
                return "ok"
            raise AssertionError(f"unexpected GitHub call: {args[:3]}")

        with mock.patch.object(monitor, "run_gh", side_effect=github) as gh:
            monitor._sync_known_failure_issue(self.conn)
            self.assertNotIn("Master has no known failures.", monitor._known_failure_issue_body(self.conn))
            gh.reset_mock()
            monitor._sync_known_failure_issue(self.conn)
            gh.assert_not_called()
            self.conn.execute(
                "UPDATE known_failures SET diagnosis='known linkage' WHERE workflow='CI'"
            )
            monitor._sync_known_failure_issue(self.conn)
            gh.assert_called_once()
            patch_args = gh.call_args.args[0]
            self.assertIn("PATCH", patch_args)
            body = patch_args[patch_args.index("--raw-field") + 1]
        self.assertIn("| Workflow | Job |", monitor._known_failure_issue_body(self.conn))
        self.assertIn("known linkage", body)

    def test_transient_issue_update_error_preserves_number_for_retry(self):
        monitor._set_known_failure_state(self.conn, "issue_number", "4519")
        monitor._set_known_failure_state(self.conn, "issue_labeled", "1")
        with mock.patch.object(
            monitor, "run_gh", side_effect=monitor.CommandError("GitHub HTTP 503"),
        ) as gh:
            with self.assertRaises(monitor.CommandError):
                monitor._sync_known_failure_issue(self.conn)
        self.assertEqual(monitor._known_failure_state(self.conn, "issue_number"), "4519")
        gh.assert_called_once()
        self.assertIn("PATCH", gh.call_args.args[0])

    def test_ambiguous_issue_update_retries_the_same_idempotent_patch(self):
        monitor._set_known_failure_state(self.conn, "issue_number", "4519")
        monitor._set_known_failure_state(self.conn, "issue_labeled", "1")
        monitor._set_known_failure_state(self.conn, "issue_pinned", "1")
        desired = monitor._known_failure_issue_body(self.conn)
        with mock.patch.object(monitor, "run_gh", side_effect=[
            monitor.CommandError("response lost"), "4519",
        ]) as gh:
            with self.assertRaises(monitor.CommandError):
                monitor._sync_known_failure_issue(self.conn)
            monitor._sync_known_failure_issue(self.conn)
        self.assertEqual(gh.call_count, 2)
        self.assertEqual(gh.call_args_list[0].args, gh.call_args_list[1].args)
        self.assertEqual(
            monitor._known_failure_state(self.conn, "issue_body_sha256"),
            hashlib.sha256(desired.encode()).hexdigest(),
        )

    def test_failed_ledger_publication_retries_cached_body_without_rendering(self):
        monitor._set_known_failure_state(self.conn, 'issue_number', '4519')
        monitor._set_known_failure_state(self.conn, 'issue_labeled', '1')
        monitor._set_known_failure_state(self.conn, 'issue_pinned', '1')
        desired = monitor._known_failure_issue_body(self.conn)
        with mock.patch.object(monitor, 'run_gh', side_effect=monitor.CommandError('HTTP 503')):
            with self.assertRaises(monitor.CommandError):
                monitor._sync_known_failure_issue(self.conn)
        self.assertEqual(monitor._known_failure_state(self.conn, 'issue_pending_body'), desired)
        with mock.patch.object(monitor, '_known_failure_issue_body', side_effect=AssertionError('must use cached body')), \
             mock.patch.object(monitor, 'run_gh', return_value='4519') as gh:
            monitor._sync_known_failure_issue(self.conn)
        self.assertIn('body=' + desired, gh.call_args.args[0])
        self.assertFalse(monitor._known_failure_state(self.conn, 'issue_pending_body'))

    def test_rate_limited_upkeep_still_retries_cached_publication_without_processing_runs(self):
        now = dt.datetime.now(dt.timezone.utc)
        monitor._set_known_failure_state(self.conn, 'last_upkeep_at', now.isoformat())
        monitor._set_known_failure_state(self.conn, 'issue_number', '4519')
        monitor._set_known_failure_state(self.conn, 'issue_labeled', '1')
        monitor._set_known_failure_state(self.conn, 'issue_pinned', '1')
        monitor._set_known_failure_state(self.conn, 'issue_pending_body', 'cached ledger view')
        with mock.patch.object(monitor, '_known_failure_issue_body', side_effect=AssertionError('must not render')), \
             mock.patch.object(monitor, '_known_failure_runs', side_effect=AssertionError('must not refetch runs')), \
             mock.patch.object(monitor, 'run_gh', return_value='4519') as gh:
            self.assertFalse(monitor.update_known_failures(self.conn, self.transport, now=now))
        gh.assert_called_once()
        self.assertIn('body=cached ledger view', gh.call_args.args[0])

    def test_missing_issue_is_created_labeled_stored_and_pinned(self):
        with mock.patch.object(
            monitor, "run_gh",
            side_effect=["[]", "https://github.com/BrokkAi/bifrost-dev/issues/55", "ok"],
        ) as gh:
            monitor._sync_known_failure_issue(self.conn)
        self.assertEqual(monitor._known_failure_state(self.conn, "issue_number"), "55")
        self.assertEqual(monitor._known_failure_state(self.conn, "issue_pinned"), "1")
        create_args = gh.call_args_list[1].args[0]
        self.assertIn("--label", create_args)
        self.assertIn("known-ci-failures", create_args)
        self.assertEqual(gh.call_args_list[2].args[0][:3], ["issue", "pin", "55"])

    def test_repeated_ledger_upkeep_error_notifies_once_per_reason(self):
        with mock.patch.object(monitor, "slack_send", return_value=(True, None)) as slack:
            for index in range(4):
                monitor._record_known_failure_error(
                    self.conn, self.transport, "github_rate_limited", f"failure {index}"
                )
        slack.assert_called_once()
        row = self.conn.execute(
            "SELECT failure_count,notified_at FROM known_failure_errors WHERE reason=?",
            ("github_rate_limited",),
        ).fetchone()
        self.assertEqual(row["failure_count"], 4)
        self.assertTrue(row["notified_at"])

    def test_successful_ledger_upkeep_clears_previous_error_episode(self):
        with mock.patch.object(monitor, "slack_send", return_value=(True, None)):
            for _ in range(3):
                monitor._record_known_failure_error(
                    self.conn, self.transport, "github_command_failed", "old error"
                )
        with (
            mock.patch.object(monitor, "_known_failure_runs", return_value=[]),
            mock.patch.object(monitor, "refresh_known_failure_link_states"),
            mock.patch.object(monitor, "_sync_known_failure_issue"),
        ):
            self.assertTrue(monitor.update_known_failures(self.conn, self.transport, force=True))
        count = self.conn.execute("SELECT COUNT(*) FROM known_failure_errors").fetchone()[0]
        self.assertEqual(count, 0)
