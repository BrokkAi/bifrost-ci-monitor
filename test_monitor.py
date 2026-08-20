#!/usr/bin/python3
"""Focused tests for the Bifrost CI monitor's episode lifecycle."""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

import monitor


ISSUE_URL = "https://github.com/BrokkAi/bifrost-dev/issues/2304"


def workflow_run(
    workflow: str,
    run_id: int,
    created_at: str,
    *,
    status: str = "completed",
    conclusion: str = "success",
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
            }
        ]
    )


class PollCiTests(unittest.TestCase):
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
            workflow_run(
                "CI", 30, "2026-08-20T12:30:00Z", conclusion="failure"
            ),
            workflow_run(
                "Hourly CI", 20, "2026-08-20T12:00:00Z", conclusion="failure"
            ),
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
            workflow_run(
                "CI", 30, "2026-08-20T12:30:00Z", conclusion="failure"
            ),
            workflow_run(
                "Hourly CI", 20, "2026-08-20T12:00:00Z", conclusion="failure"
            ),
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


if __name__ == "__main__":
    unittest.main()
