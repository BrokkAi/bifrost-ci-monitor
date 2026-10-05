"""Focused fake-seam tests for the Bifrost PR automerger."""

from __future__ import annotations

import json
import datetime as dt
import sqlite3
import subprocess
from pathlib import Path
from unittest import TestCase, mock

import automerge
import monitor


BASE_SHA = "a" * 40
HEAD_ONE = "1" * 40
HEAD_TWO = "2" * 40
HEAD_OLD = "3" * 40
HEAD_NEW = "4" * 40


def api_pull(
    number: int,
    *,
    title: str | None = None,
    head_sha: str = HEAD_ONE,
    draft: bool = False,
    base: str = "master",
    labels: list[str] | None = None,
    state: str = "open",
) -> dict:
    return {
        "number": number,
        "title": title or f"Change {number}",
        "state": state,
        "draft": draft,
        "base": {"ref": base},
        "head": {"sha": head_sha},
        "labels": [{"name": label} for label in labels or []],
        "html_url": f"https://github.com/{automerge.REPO_NAME}/pull/{number}",
    }


def pull(number: int = 7, head_sha: str = HEAD_ONE) -> automerge.PullRequest:
    return automerge.PullRequest(
        number,
        f"Change {number}",
        head_sha,
        f"https://github.com/{automerge.REPO_NAME}/pull/{number}",
    )


def make_batch_db(
    *, status: str = "running", batch_id: str = "batch-test", session_id: str = "session-existing"
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE automerge_batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            pull_requests_json TEXT NOT NULL,
            title TEXT NOT NULL,
            branch TEXT NOT NULL,
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
        CREATE TABLE automerge_relayed_messages (
            batch_id TEXT NOT NULL, stable_id TEXT NOT NULL, seq INTEGER NOT NULL,
            PRIMARY KEY (batch_id, stable_id)
        );
        CREATE TABLE automerge_blocked_notifications (
            batch_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
            details TEXT NOT NULL, slack_notification_attempted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (batch_id, reason)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO automerge_batches
            (batch_id, status, base_sha, pull_requests_json, title, branch,
             created_at, session_id, thread_ts, start_notification_sent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            batch_id,
            status,
            BASE_SHA,
            json.dumps([pull().as_json()]),
            f"Bifrost automerge batch {batch_id}",
            f"automerge/{batch_id}",
            monitor.utc_now(),
            session_id,
            "slack-thread",
        ),
    )
    conn.commit()
    return conn


class SelectionTests(TestCase):
    def test_filters_drafts_and_current_head_rejections_and_readmits_changed_head(self):
        rows = [
            api_pull(10, title="Normal", head_sha=HEAD_ONE),
            api_pull(11, title="Draft", head_sha=HEAD_ONE, draft=True),
            api_pull(12, title="Other base", head_sha=HEAD_ONE, base="develop"),
            api_pull(
                13, title="Rejected at current head", head_sha=HEAD_TWO,
                labels=[automerge.REJECTED_LABEL],
            ),
            api_pull(
                14, title="New head after rejection", head_sha=HEAD_NEW,
                labels=[automerge.REJECTED_LABEL],
            ),
        ]
        calls: list[list[str]] = []

        def fake_gh(args: list[str], *, timeout: int = 60) -> str:
            calls.append(args)
            if "/issues/13/comments?" in args[-1]:
                return json.dumps([[{
                    "user": {"login": "bifrost-bot"},
                    "created_at": "2026-10-05T10:00:00Z",
                    "body": f"automerge-rejected-head: {HEAD_TWO}\nFailing test and evidence.",
                }]])
            if "/issues/14/comments?" in args[-1]:
                return json.dumps([[{
                    "user": {"login": "bifrost-bot"},
                    "created_at": "2026-10-05T10:00:00Z",
                    "body": f"automerge-rejected-head: {HEAD_OLD}\nOld failure evidence.",
                }]])
            if args[0:2] == ["pr", "edit"]:
                return ""
            if args[0:2] == ["api", "--paginate"]:
                return json.dumps([rows])
            raise AssertionError(f"unexpected gh call: {args}")

        with (
            mock.patch.object(automerge, "run_gh", side_effect=fake_gh),
            mock.patch.object(automerge, "github_login", return_value="bifrost-bot"),
        ):
            selected = automerge.select_eligible_pull_requests()

        self.assertEqual([item.number for item in selected], [10, 14])
        self.assertEqual(selected[1].head_sha, HEAD_NEW)
        self.assertIn(
            ["pr", "edit", "14", "--repo", automerge.REPO_NAME,
             "--remove-label", automerge.REJECTED_LABEL],
            calls,
        )
        self.assertFalse(any(call[:3] == ["pr", "edit", "13"] for call in calls))

    def test_forged_rejection_marker_from_other_author_is_ignored(self):
        row = api_pull(
            15, title="Forged rejection", head_sha=HEAD_TWO,
            labels=[automerge.REJECTED_LABEL],
        )
        calls: list[list[str]] = []

        def fake_gh(args: list[str], *, timeout: int = 60) -> str:
            calls.append(args)
            if "/issues/15/comments?" in args[-1]:
                return json.dumps([[{
                    "user": {"login": "not-the-bot"},
                    "created_at": "2026-10-05T11:00:00Z",
                    "body": f"automerge-rejected-head: {HEAD_TWO}\nForged evidence.",
                }]])
            if args[0:2] == ["pr", "edit"]:
                return ""
            if args[0:2] == ["api", "--paginate"]:
                return json.dumps([[row]])
            raise AssertionError(f"unexpected gh call: {args}")

        with (
            mock.patch.object(automerge, "run_gh", side_effect=fake_gh),
            mock.patch.object(automerge, "github_login", return_value="bifrost-bot"),
        ):
            selected = automerge.select_eligible_pull_requests()

        self.assertEqual([item.number for item in selected], [15])
        self.assertIn(
            ["pr", "edit", "15", "--repo", automerge.REPO_NAME,
             "--remove-label", automerge.REJECTED_LABEL],
            calls,
        )


class TrustedRejectionTests(TestCase):
    def test_newest_trusted_rejection_marker_supplies_head_and_evidence(self):
        comments = [
            {
                "id": 10,
                "created_at": "2026-10-05T10:00:00Z",
                "user": {"login": "bifrost-bot"},
                "body": f"automerge-rejected-head: {HEAD_ONE}\nOlder trusted evidence.",
            },
            {
                "id": 11,
                "created_at": "2026-10-05T11:00:00Z",
                "user": {"login": "attacker"},
                "body": f"automerge-rejected-head: {HEAD_NEW}\nForged newer evidence.",
            },
            {
                "id": 12,
                "created_at": "2026-10-05T10:30:00Z",
                "user": {"login": "BIFROST-BOT"},
                "body": f"automerge-rejected-head: {HEAD_TWO}\nNewest trusted evidence.",
            },
        ]

        marker = automerge.newest_trusted_rejection(comments, login="bifrost-bot")

        self.assertIsNotNone(marker)
        self.assertEqual(marker.head_sha, HEAD_TWO)
        self.assertIn("Newest trusted evidence", marker.evidence)
        self.assertNotIn("Forged newer evidence", marker.evidence)

    def test_github_login_is_cached_after_first_lookup(self):
        with mock.patch.object(automerge, "GH_LOGIN_CACHE", None):
            with mock.patch.object(
                automerge, "gh_json", return_value={"login": "bifrost-bot"}
            ) as gh_json:
                self.assertEqual(automerge.github_login(), "bifrost-bot")
                self.assertEqual(automerge.github_login(), "bifrost-bot")
            gh_json.assert_called_once_with(["api", "user"], timeout=30)


class PromptAndLaunchTests(TestCase):
    def test_prompt_contains_merge_conflict_trailer_baseline_rejection_and_push_rules(self):
        prompt = automerge.build_prompt(
            "batch-abc", [pull(7, HEAD_ONE), pull(9, HEAD_TWO)], BASE_SHA
        )
        for expected in (
            "PR #7: Change 7",
            HEAD_ONE,
            "git fetch origin pull/<N>/head",
            "If the fetched SHA differs, do not merge or reject that PR",
            "remove it from this batch",
            "rebuild from the original",
            "changed PR stays eligible for a later batch",
            "merge commit (no squash and no rebase)",
            "Resolve every conflict yourself",
            "preserve both sides' intent",
            "Automerge-Batch: batch-abc",
            "full Bifrost test suite once",
            "root CLAUDE.md and AGENTS.md",
            "baseline failure",
            BASE_SHA,
            "never the latest master",
            "run those same failing tests at",
            ".github/workflows",
            "Split the batch as needed",
            "automerge-rejected-head: <full sha>",
            "gh pr view <N> --json state,headRefOid,baseRefName,isDraft",
            "baseRefName `master`",
            "isDraft `false`",
            "is a draft, targets another base",
            "If any PR is closed, is a draft, targets another base, or its head changed",
            "rebuild the branch from the original",
            "A changed PR is not rejected",
            "Never publish a tree containing a PR head other than the one tested",
            automerge.BASELINE_BLOCKED_MARKER,
            "reject nothing and publish nothing",
            "failing tests and concrete evidence",
            "git push origin HEAD:master",
            "non-fast-forward",
            "Never force-push",
            "plain-text summary",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, prompt)

    def test_exact_mj_new_argv_and_prompt_contents(self):
        row = {
            "batch_id": "batch-abc",
            "base_sha": BASE_SHA,
            "branch": "automerge/batch-abc",
            "title": "Bifrost automerge batch batch-abc",
        }
        pulls = [pull(7, HEAD_ONE)]
        captured: dict[str, object] = {}

        def fake_mj(args: list[str], *, timeout: int = 60):
            captured["args"] = args
            prompt_index = args.index("--prompt-file") + 1
            captured["prompt"] = Path(args[prompt_index]).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, '{"session_id":"session-1"}', "")

        with (
            mock.patch.object(automerge, "lookup_batch_session", return_value=None),
            mock.patch.object(monitor, "mj_command", side_effect=fake_mj),
        ):
            result = automerge.launch_batch_session(row, pulls)

        self.assertEqual(result, "session-1")
        args = captured["args"]
        prompt_file = args[args.index("--prompt-file") + 1]
        self.assertEqual(
            args,
            [
                "new", "--workspace", monitor.MJ_WORKSPACE,
                "--target", monitor.MJ_TARGET,
                "--bundle", monitor.MJ_BUNDLE,
                "--model", monitor.MJ_MODEL,
                "--subagents", "none",
                "--at", BASE_SHA,
                "--branch", "automerge/batch-abc",
                "--title", "Bifrost automerge batch batch-abc",
                "--prompt-file", prompt_file, "--json",
            ],
        )
        self.assertIn(f"PR #7: Change 7", captured["prompt"])

    def test_ambiguous_launch_is_persisted_and_not_retried_without_identity(self):
        conn = make_batch_db(status="launching", session_id="")
        row = conn.execute(
            "SELECT * FROM automerge_batches WHERE batch_id = 'batch-test'"
        ).fetchone()
        with (
            mock.patch.object(automerge, "lookup_batch_session", return_value=None),
            mock.patch.object(
                monitor, "mj_command",
                side_effect=monitor.MjError("daemon disconnected", reason="daemon_unreachable"),
            ) as mj_command,
        ):
            with self.assertRaises(automerge.LaunchAttemptError) as raised:
                automerge.launch_batch_session(
                    row, automerge.row_pulls(row), conn=conn, allow_new=True
                )
            self.assertTrue(raised.exception.ambiguous)
            attempted = conn.execute(
                "SELECT launch_attempted FROM automerge_batches WHERE batch_id = 'batch-test'"
            ).fetchone()[0]
            self.assertEqual(attempted, 1)
            mj_command.assert_called_once()

        with (
            mock.patch.object(automerge, "lookup_batch_session", return_value=None),
            mock.patch.object(monitor, "mj_command") as second_mj_command,
        ):
            with self.assertRaisesRegex(
                automerge.LaunchAttemptError, "keeping the launch identity unresolved"
            ):
                automerge.launch_batch_session(
                    row, automerge.row_pulls(row), conn=conn, allow_new=False
                )
        second_mj_command.assert_not_called()
        conn.close()

    def test_error_response_holds_until_session_absence_is_proven_after_grace(self):
        conn = make_batch_db(status="launching", session_id="")
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        with (
            mock.patch.object(automerge, "lookup_batch_session", return_value=None) as lookup,
            mock.patch.object(
                monitor,
                "mj_command",
                return_value=subprocess.CompletedProcess(
                    ["mj", "new"], 1, "", "mj new: invalid workspace configuration"
                ),
            ) as mj_command,
            mock.patch.object(monitor, "slack_send", return_value=(True, None)) as slack_send,
        ):
            automerge.process_batch(conn, transport, "batch-test")
            held = conn.execute(
                "SELECT status, launch_attempted FROM automerge_batches "
                "WHERE batch_id = 'batch-test'"
            ).fetchone()
            self.assertEqual(held["status"], "launching")
            self.assertEqual(held["launch_attempted"], 1)
            self.assertIsNotNone(automerge.active_batch(conn))
            slack_send.assert_not_called()

            expired_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                seconds=automerge.AMBIGUOUS_LAUNCH_GRACE_SECONDS + 5
            )
            conn.execute(
                "UPDATE automerge_batches SET launch_attempted_at = ? "
                "WHERE batch_id = 'batch-test'",
                (expired_at.isoformat(),),
            )
            conn.commit()
            automerge.process_batch(conn, transport, "batch-test")

        saved = conn.execute(
            "SELECT status, terminal_status FROM automerge_batches WHERE batch_id = 'batch-test'"
        ).fetchone()
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["terminal_status"], "mj_launch_ambiguous_expired")
        self.assertIsNone(automerge.active_batch(conn))
        self.assertEqual(lookup.call_count, 3)
        mj_command.assert_called_once()
        slack_send.assert_called_once()
        notice = conn.execute(
            "SELECT reason, slack_notification_attempted FROM automerge_blocked_notifications"
        ).fetchone()
        self.assertEqual(tuple(notice), ("mj_launch_ambiguous_expired", 1))
        conn.close()

    def test_slow_session_listing_adopts_failed_launch_without_duplication(self):
        conn = make_batch_db(status="launching", session_id="")
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        with (
            mock.patch.object(
                automerge, "lookup_batch_session", side_effect=[None, None]
            ) as lookup,
            mock.patch.object(
                monitor,
                "mj_command",
                return_value=subprocess.CompletedProcess(
                    ["mj", "new"], 1, "", "request failed after session acceptance"
                ),
            ) as mj_command,
            mock.patch.object(monitor, "slack_send", return_value=(True, None)) as slack_send,
        ):
            automerge.process_batch(conn, transport, "batch-test")

        held = conn.execute(
            "SELECT status, launch_attempted FROM automerge_batches "
            "WHERE batch_id = 'batch-test'"
        ).fetchone()
        self.assertEqual(held["status"], "launching")
        self.assertEqual(held["launch_attempted"], 1)
        self.assertIsNotNone(automerge.active_batch(conn))
        slack_send.assert_not_called()
        expired_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=automerge.AMBIGUOUS_LAUNCH_GRACE_SECONDS + 5
        )
        conn.execute(
            "UPDATE automerge_batches SET launch_attempted_at = ? "
            "WHERE batch_id = 'batch-test'",
            (expired_at.isoformat(),),
        )
        conn.commit()

        with (
            mock.patch.object(
                automerge, "lookup_batch_session", return_value="late-session"
            ),
            mock.patch.object(
                monitor,
                "require_mj_success",
                return_value=json.dumps({
                    "id": "late-session", "state": "running", "chat_phase": "running"
                }),
            ),
            mock.patch.object(
                automerge,
                "supervise_turn",
                return_value=monitor.TurnResult("completed", "finished"),
            ),
            mock.patch.object(automerge, "drain_transcript"),
            mock.patch.object(monitor, "read_complete_agent_transcript", return_value="Landed"),
            mock.patch.object(automerge, "read_final_agent_message", return_value="Landed"),
            mock.patch.object(automerge, "request_suspend", return_value=True),
            mock.patch.object(automerge, "finish_batch"),
        ):
            automerge.process_batch(conn, transport, "batch-test")

        saved = conn.execute(
            "SELECT status, session_id FROM automerge_batches WHERE batch_id = 'batch-test'"
        ).fetchone()
        self.assertEqual(saved["status"], "finishing")
        self.assertEqual(saved["session_id"], "late-session")
        self.assertEqual(lookup.call_count, 2)
        mj_command.assert_called_once()
        conn.close()

    def test_failed_session_listing_keeps_expired_attempt_held_and_notifies_once(self):
        conn = make_batch_db(status="launching", session_id="")
        expired_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=automerge.AMBIGUOUS_LAUNCH_GRACE_SECONDS + 5
        )
        conn.execute(
            "UPDATE automerge_batches SET launch_attempted = 1, launch_attempted_at = ? "
            "WHERE batch_id = 'batch-test'",
            (expired_at.isoformat(),),
        )
        conn.commit()
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        listing_error = monitor.MjError("workspace sessions unavailable")
        with (
            mock.patch.object(
                automerge, "lookup_batch_session", side_effect=listing_error
            ) as lookup,
            mock.patch.object(monitor, "slack_send", return_value=(True, None)) as slack_send,
        ):
            automerge.process_batch(conn, transport, "batch-test")
            automerge.process_batch(conn, transport, "batch-test")

        saved = conn.execute(
            "SELECT status, launch_attempted FROM automerge_batches "
            "WHERE batch_id = 'batch-test'"
        ).fetchone()
        self.assertEqual(saved["status"], "launching")
        self.assertEqual(saved["launch_attempted"], 1)
        self.assertIsNotNone(automerge.active_batch(conn))
        self.assertEqual(lookup.call_count, 2)
        slack_send.assert_called_once()
        notice = conn.execute(
            "SELECT reason, slack_notification_attempted "
            "FROM automerge_blocked_notifications"
        ).fetchone()
        self.assertEqual(tuple(notice), ("mj_session_lookup_failed", 1))
        conn.close()

    def test_ambiguous_timeout_and_empty_response_expire_after_ten_minutes(self):
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        outcomes = [
            monitor.MjError("mj timed out", reason="daemon_unreachable"),
            subprocess.CompletedProcess(["mj", "new"], 1, "", ""),
        ]
        timeout_cause = subprocess.TimeoutExpired(["mj", "new"], timeout=180)
        outcomes[0].__cause__ = timeout_cause

        for launch_result in outcomes:
            with self.subTest(launch_result=type(launch_result).__name__):
                conn = make_batch_db(status="launching", session_id="")
                mj_patch = (
                    mock.patch.object(
                        monitor, "mj_command", side_effect=launch_result
                    )
                    if isinstance(launch_result, BaseException)
                    else mock.patch.object(
                        monitor, "mj_command", return_value=launch_result
                    )
                )
                with (
                    mock.patch.object(automerge, "lookup_batch_session", return_value=None),
                    mj_patch as mj_command,
                    mock.patch.object(monitor, "slack_send", return_value=(True, None)) as slack_send,
                ):
                    automerge.process_batch(conn, transport, "batch-test")
                    held = conn.execute(
                        "SELECT status, launch_attempted, launch_attempted_at "
                        "FROM automerge_batches WHERE batch_id = 'batch-test'"
                    ).fetchone()
                    self.assertEqual(held["status"], "launching")
                    self.assertEqual(held["launch_attempted"], 1)
                    self.assertIsNotNone(held["launch_attempted_at"])
                    self.assertIsNotNone(automerge.active_batch(conn))
                    slack_send.assert_not_called()

                    expired_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                        seconds=automerge.AMBIGUOUS_LAUNCH_GRACE_SECONDS + 5
                    )
                    conn.execute(
                        "UPDATE automerge_batches SET launch_attempted_at = ? "
                        "WHERE batch_id = 'batch-test'",
                        (expired_at.isoformat(),),
                    )
                    conn.commit()
                    automerge.process_batch(conn, transport, "batch-test")

                failed = conn.execute(
                    "SELECT status, terminal_status FROM automerge_batches "
                    "WHERE batch_id = 'batch-test'"
                ).fetchone()
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["terminal_status"], "mj_launch_ambiguous_expired")
                self.assertIsNone(automerge.active_batch(conn))
                self.assertEqual(mj_command.call_count, 1)
                slack_send.assert_called_once()
                notice = conn.execute(
                    "SELECT reason, slack_notification_attempted "
                    "FROM automerge_blocked_notifications"
                ).fetchone()
                self.assertEqual(tuple(notice), ("mj_launch_ambiguous_expired", 1))
                conn.close()

    def test_ambiguous_launch_adopts_session_by_exact_title(self):
        conn = make_batch_db(status="launching", session_id="")
        row = conn.execute(
            "SELECT * FROM automerge_batches WHERE batch_id = 'batch-test'"
        ).fetchone()
        timeout_error = monitor.MjError("mj timed out", reason="daemon_unreachable")
        timeout_error.__cause__ = subprocess.TimeoutExpired(["mj", "new"], timeout=180)
        with (
            mock.patch.object(
                automerge, "lookup_batch_session", side_effect=[None, "adopted-session"]
            ) as lookup,
            mock.patch.object(monitor, "mj_command", side_effect=timeout_error) as mj_command,
        ):
            session_id = automerge.launch_batch_session(
                row, automerge.row_pulls(row), conn=conn
            )
        self.assertEqual(session_id, "adopted-session")
        self.assertEqual(lookup.call_count, 2)
        mj_command.assert_called_once()
        conn.close()


class SessionLifecycleTests(TestCase):
    def test_restart_reattaches_to_recorded_session_without_launching_another(self):
        conn = make_batch_db()
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        with (
            mock.patch.object(
                monitor, "require_mj_success",
                return_value=json.dumps({"id": "session-existing", "state": "running", "chat_phase": "running"}),
            ),
            mock.patch.object(automerge, "launch_batch_session") as launch,
            mock.patch.object(automerge, "supervise_turn", return_value=monitor.TurnResult("completed", "finished")) as supervise,
            mock.patch.object(automerge, "drain_transcript"),
            mock.patch.object(monitor, "read_complete_agent_transcript", return_value="Landed PR #7"),
            mock.patch.object(automerge, "read_final_agent_message", return_value="Landed PR #7"),
            mock.patch.object(automerge, "request_suspend", return_value=True),
            mock.patch.object(automerge, "finish_batch") as finish,
        ):
            automerge.process_batch(conn, transport, "batch-test")

        launch.assert_not_called()
        supervise.assert_called_once()
        self.assertEqual(supervise.call_args.args[3], "session-existing")
        finish.assert_called_once()
        row = conn.execute(
            "SELECT status, terminal_status FROM automerge_batches WHERE batch_id = 'batch-test'"
        ).fetchone()
        self.assertEqual(row["status"], "finishing")
        self.assertEqual(row["terminal_status"], "completed")
        conn.close()

    def test_timeout_interrupts_notifies_then_suspends(self):
        conn = make_batch_db()
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        events: list[str] = []

        def notify(_conn, _transport, _batch_id, reason, _details):
            if reason == "batch_timeout":
                events.append("notify")

        with (
            mock.patch.object(
                monitor, "require_mj_success",
                return_value=json.dumps({"id": "session-existing", "state": "running", "chat_phase": "running"}),
            ),
            mock.patch.object(
                automerge, "supervise_turn",
                return_value=monitor.TurnResult("running", "timeout", timed_out=True),
            ),
            mock.patch.object(automerge, "interrupt_and_wait", side_effect=lambda *a, **k: events.append("interrupt") or monitor.TurnResult("completed", "finished")),
            mock.patch.object(automerge, "notify_blocked_once", side_effect=notify),
            mock.patch.object(automerge, "drain_transcript"),
            mock.patch.object(monitor, "read_complete_agent_transcript", return_value=""),
            mock.patch.object(automerge, "read_final_agent_message", return_value=""),
            mock.patch.object(automerge, "request_suspend", side_effect=lambda *a, **k: events.append("suspend") or True),
            mock.patch.object(automerge, "finish_batch"),
        ):
            automerge.process_batch(conn, transport, "batch-test")

        self.assertEqual(events, ["interrupt", "notify", "suspend"])
        row = conn.execute(
            "SELECT status, terminal_status FROM automerge_batches WHERE batch_id = 'batch-test'"
        ).fetchone()
        self.assertEqual(row["status"], "finishing")
        self.assertEqual(row["terminal_status"], "timed_out")
        conn.close()

    def test_final_agent_message_uses_last_transcript_item_across_pages(self):
        marker_message = f"{automerge.BASELINE_BLOCKED_MARKER}\nIntermediate status."
        pages = [
            subprocess.CompletedProcess(
                ["mj", "transcript"],
                0,
                json.dumps({
                    "items": [{"stable_id": "agent-1", "seq": 1, "text": marker_message}],
                    "next_after_seq": 1,
                    "latest_seq": 2,
                }),
                "",
            ),
            subprocess.CompletedProcess(
                ["mj", "transcript"],
                0,
                json.dumps({
                    "items": [{"stable_id": "agent-2", "seq": 2, "text": "Final report."}],
                    "next_after_seq": 2,
                    "latest_seq": 2,
                }),
                "",
            ),
        ]
        with mock.patch.object(monitor, "mj_command", side_effect=pages) as mj_command:
            final_message = automerge.read_final_agent_message("session-existing")

        self.assertEqual(final_message, "Final report.")
        self.assertEqual(mj_command.call_count, 2)
        self.assertEqual(mj_command.call_args_list[1].args[0][-2], "1")

    def test_unresolved_base_failure_blocks_without_rejection_or_publication(self):
        conn = make_batch_db()
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        agent_report = (
            f"{automerge.BASELINE_BLOCKED_MARKER}\n"
            "test_new_behavior failed; the base checkout could not build."
        )
        with (
            mock.patch.object(
                monitor,
                "require_mj_success",
                return_value=json.dumps({
                    "id": "session-existing", "state": "running", "chat_phase": "running"
                }),
            ),
            mock.patch.object(
                automerge,
                "supervise_turn",
                return_value=monitor.TurnResult("completed", "finished"),
            ),
            mock.patch.object(automerge, "drain_transcript"),
            mock.patch.object(monitor, "read_complete_agent_transcript", return_value=agent_report),
            mock.patch.object(automerge, "read_final_agent_message", return_value=agent_report),
            mock.patch.object(automerge, "request_suspend", return_value=True),
            mock.patch.object(
                automerge, "detect_batch_outcomes",
                return_value=automerge.BatchOutcome((), (), ()),
            ),
            mock.patch.object(monitor, "slack_send", return_value=(True, "summary-ts")) as slack_send,
        ):
            automerge.process_batch(conn, transport, "batch-test")

        row = conn.execute(
            "SELECT status, terminal_status, agent_transcript FROM automerge_batches "
            "WHERE batch_id = 'batch-test'"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["terminal_status"], "blocked_baseline")
        self.assertIn(automerge.BASELINE_BLOCKED_MARKER, row["agent_transcript"])
        self.assertEqual(slack_send.call_count, 2)
        blocked_summary = slack_send.call_args_list[1].args[1]
        self.assertIn(f"baseline at {BASE_SHA} could not be established", blocked_summary)
        self.assertIn("No PR was rejected and nothing was pushed", blocked_summary)
        self.assertIn("test_new_behavior failed", blocked_summary)
        notice = conn.execute(
            "SELECT reason, slack_notification_attempted FROM automerge_blocked_notifications"
        ).fetchone()
        self.assertEqual(tuple(notice), ("baseline_unresolved", 1))
        conn.close()

    def test_marker_echoed_in_earlier_message_is_not_accepted_as_final(self):
        conn = make_batch_db()
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        earlier_message = (
            f"{automerge.BASELINE_BLOCKED_MARKER}\n"
            "This was an intermediate status, not the final report."
        )
        with (
            mock.patch.object(
                monitor,
                "require_mj_success",
                return_value=json.dumps({
                    "id": "session-existing", "state": "running", "chat_phase": "running"
                }),
            ),
            mock.patch.object(
                automerge,
                "supervise_turn",
                return_value=monitor.TurnResult("completed", "finished"),
            ),
            mock.patch.object(automerge, "drain_transcript"),
            mock.patch.object(monitor, "read_complete_agent_transcript", return_value=earlier_message),
            mock.patch.object(automerge, "read_final_agent_message", return_value="Tests completed."),
            mock.patch.object(automerge, "request_suspend", return_value=True),
            mock.patch.object(
                automerge, "detect_batch_outcomes",
                return_value=automerge.BatchOutcome((), (), ()),
            ),
            mock.patch.object(monitor, "slack_send", return_value=(True, "summary-ts")) as slack_send,
        ):
            automerge.process_batch(conn, transport, "batch-test")

        row = conn.execute(
            "SELECT terminal_status FROM automerge_batches WHERE batch_id = 'batch-test'"
        ).fetchone()
        self.assertEqual(row["terminal_status"], "completed")
        self.assertEqual(slack_send.call_count, 1)
        summary = slack_send.call_args.args[1]
        self.assertIn("finished (completed)", summary)
        self.assertNotIn("BLOCKED: baseline", summary)
        self.assertIn("not the standalone first line of the final agent message", summary)
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM automerge_blocked_notifications "
                "WHERE reason = 'baseline_unresolved'"
            ).fetchone()
        )
        conn.close()


class OutcomeTests(TestCase):
    def test_github_outcomes_override_final_baseline_marker(self):
        conn = make_batch_db(status="finishing")
        final_marker = f"{automerge.BASELINE_BLOCKED_MARKER}\nThe base could not be built."
        conn.execute(
            "UPDATE automerge_batches SET terminal_status = 'completed', "
            "agent_transcript = ?, agent_final_message = ? WHERE batch_id = 'batch-test'",
            (final_marker, final_marker),
        )
        conn.commit()
        outcome = automerge.BatchOutcome(
            merged=(automerge.PullRequestOutcome(pull(7), "merged"),),
            rejected=(
                automerge.PullRequestOutcome(
                    pull(8, HEAD_TWO), "open", True, "regression in test suite"
                ),
            ),
            pending=(),
        )
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        with (
            mock.patch.object(automerge, "detect_batch_outcomes", return_value=outcome),
            mock.patch.object(monitor, "slack_send", return_value=(True, "summary-ts")) as slack_send,
        ):
            row = conn.execute(
                "SELECT * FROM automerge_batches WHERE batch_id = 'batch-test'"
            ).fetchone()
            automerge.finish_batch(conn, transport, row)

        summary = slack_send.call_args.args[1]
        self.assertIn("finished (completed)", summary)
        self.assertNotIn("BLOCKED: baseline", summary)
        self.assertIn("PR #7 Change 7", summary)
        self.assertIn("PR #8 Change 8", summary)
        self.assertIn("marker is inconsistent with GitHub", summary)
        self.assertEqual(slack_send.call_count, 1)
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM automerge_blocked_notifications "
                "WHERE reason = 'baseline_unresolved'"
            ).fetchone()
        )
        conn.close()

    def test_outcome_comes_from_github_merge_and_current_head_rejection_state(self):
        pulls = [pull(1, HEAD_ONE), pull(2, HEAD_TWO), pull(3, HEAD_OLD)]
        calls: list[list[str]] = []

        def fake_gh(args: list[str], *, timeout: int = 60) -> str:
            calls.append(args)
            if args[0] != "api":
                raise AssertionError(args)
            path = args[-1]
            if path.endswith("/pulls/1"):
                return json.dumps({"state": "closed", "merged_at": "2026-10-05T00:00:00Z"})
            if path.endswith("/pulls/2"):
                return json.dumps({
                    "state": "open", "merged_at": None,
                    "head": {"sha": HEAD_TWO},
                    "labels": [{"name": automerge.REJECTED_LABEL}],
                })
            if path.endswith("/pulls/3"):
                return json.dumps({
                    "state": "open", "merged_at": None,
                    "head": {"sha": HEAD_NEW},
                    "labels": [{"name": automerge.REJECTED_LABEL}],
                })
            if "/issues/2/comments?" in path:
                return json.dumps([[
                    {
                        "id": 20,
                        "created_at": "2026-10-05T10:00:00Z",
                        "user": {"login": "bifrost-bot"},
                        "body": f"automerge-rejected-head: {HEAD_TWO}\nThe failing test proves the regression.",
                    }
                ]])
            if "/issues/3/comments?" in path:
                return json.dumps([[
                    {
                        "id": 30,
                        "created_at": "2026-10-05T10:00:00Z",
                        "user": {"login": "bifrost-bot"},
                        "body": f"automerge-rejected-head: {HEAD_OLD}\nThis only rejected the old head.",
                    }
                ]])
            raise AssertionError(args)

        with (
            mock.patch.object(automerge, "run_gh", side_effect=fake_gh),
            mock.patch.object(automerge, "github_login", return_value="bifrost-bot"),
        ):
            outcome = automerge.detect_batch_outcomes(pulls)

        self.assertEqual([item.pull.number for item in outcome.merged], [1])
        self.assertEqual([item.pull.number for item in outcome.rejected], [2])
        self.assertIn("failing test", outcome.rejected[0].rejection_evidence)
        self.assertEqual([item.pull.number for item in outcome.pending], [3])
        self.assertEqual(len(calls), 5)


class LockTests(TestCase):
    def test_lock_is_nonblocking_and_exclusive(self):
        with __import__("tempfile").TemporaryDirectory() as directory:
            path = Path(directory) / "automerge.lock"
            first = automerge.acquire_lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(automerge.acquire_lock(path))
            first.close()
            third = automerge.acquire_lock(path)
            self.assertIsNotNone(third)
            third.close()


class NotificationTests(TestCase):
    def test_blocked_notice_retries_until_accepted_then_deduplicates(self):
        conn = make_batch_db()
        transport = monitor.SlackTransport("webhook", webhook="https://example.invalid")
        with mock.patch.object(
            monitor, "slack_send", side_effect=[(False, None), (True, None)]
        ) as slack_send:
            automerge.notify_blocked_once(
                conn, transport, "batch-test", "github_failed", "temporary outage"
            )
            automerge.retry_pending_notifications(conn, transport)
            automerge.notify_blocked_once(
                conn, transport, "batch-test", "github_failed", "temporary outage"
            )

        self.assertEqual(slack_send.call_count, 2)
        posted = conn.execute(
            "SELECT slack_notification_attempted FROM automerge_blocked_notifications "
            "WHERE batch_id = 'batch-test' AND reason = 'github_failed'"
        ).fetchone()[0]
        self.assertEqual(posted, 1)
        conn.close()


if __name__ == "__main__":
    import unittest

    unittest.main()
