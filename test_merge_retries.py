"""Retry membership and drafting regressions using the production SQLite schema."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import automerge
import mm_service
import monitor
import speculation
from test_automerge import BASE_SHA, HEAD_ONE, HEAD_TWO, HEAD_THREE, async_local_report, direct_view, pull


class MergeRetryTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.patch(automerge, "DB_PATH", Path(directory.name) / "activity.db")
        self.conn = automerge.connect_db()
        self.addCleanup(lambda: self.conn.close())
        automerge.create_batch(self.conn, [pull()], BASE_SHA, batch_id="retry", ci_mode="async")
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='running',session_id='live-session',"
                              "ci_head_sha=?,integration_pr_number=211", (HEAD_THREE,))
        self.states = {}
        self.source = self.patch(automerge, "_source_pr_state", side_effect=lambda p:
                                 dict(self.states.get(p.number, direct_view(head_sha=p.head_sha))))
        self.select = self.patch(automerge, "select_eligible_pull_requests", return_value=[])
        self.patch(automerge, 'check_source_dependencies',
                   side_effect=lambda pulls, *args, **kwargs: (pulls, {}))
        self.gh = self.patch(automerge, "run_gh", side_effect=self.github_write)
        self.patch(monitor, "run_gh", side_effect=AssertionError("unexpected real GitHub call"))
        self.patch(monitor, "mj_command", side_effect=AssertionError("unexpected real mj call"))
        self.patch(automerge, "list_pull_comments", return_value=[])
        self.patch(automerge, "run_ci_impact", side_effect=lambda base, heads: {
            "base_sha": base, "heads": sorted(set(heads)), "mode": "impact",
        })
        self.transport = monitor.SlackTransport("webhook", webhook="unused")

    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def github_write(self, args, **kwargs):
        if args[:2] == ["pr", "close"]:
            return ""
        self.assertEqual(args[:2], ["pr", "ready"])
        self.assertIn("--undo", args)
        self.states[int(args[2])]["isDraft"] = True
        return ""

    def row(self):
        return self.conn.execute("SELECT * FROM automerge_batches WHERE batch_id='retry'").fetchone()

    def draft_intents(self):
        return self.conn.execute("SELECT * FROM automerge_github_outbox "
                                 "WHERE kind='draft_changed_head'").fetchall()

    def reopen(self):
        self.conn.close()
        self.conn = automerge.connect_db()

    def retry(self, *, only_if_expanded=False):
        return automerge._request_rebuild(self.conn, self.row(), automerge.row_pulls(self.row()),
                                          "retry", only_if_expanded=only_if_expanded)

    def add_source(self, number=8, head=HEAD_TWO):
        sources = automerge._all_batch_pulls(self.row()) + [pull(number, head)]
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET pull_requests_json=?,active_pull_requests_json=?",
                              (json.dumps([p.as_json() for p in sources]),) * 2)

    def cleanup_mocks(self):
        self.patch(automerge, "send_start_notification")
        self.patch(automerge, "_session_status", return_value={"state": "running", "is_idle": False})
        self.stop = self.patch(speculation, "stop_work", return_value=True)
        self.suspend = self.patch(automerge, "request_suspend", return_value=True)
        self.patch(automerge, "integration_pr_view", return_value={
            "state": "OPEN", "headRefOid": HEAD_THREE, "url": "https://github.test/pr/211"})
        self.patch(automerge, "post_verdict_status")
        self.patch(automerge, "finish_batch")

    def test_three_expansions_persist_across_restart_then_membership_freezes(self):
        for number in (8, 9, 10):
            self.select.return_value = [pull(number, f"{number:040x}")]
            self.assertTrue(self.retry())
            self.reopen()
            row = self.row()
            self.assertEqual(row["expansion_count"], number - 7)
            self.assertIn(f"PR #{number}", row["pending_prompt"])
            self.assertEqual(row["phase"], "fixing")
            self.assertEqual(row["prompt_delivered"], 0)
        self.select.reset_mock()
        self.select.return_value = [pull(11, "b" * 40)]
        self.assertTrue(self.retry())
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [7, 8, 9, 10])
        self.assertEqual(self.row()["expansion_count"], 3)
        self.select.assert_not_called()

    def test_empty_rescan_does_not_consume_expansion_or_retest_passed_tree(self):
        self.assertFalse(self.retry(only_if_expanded=True))
        self.assertEqual(self.row()["expansion_count"], 0)
        self.assertIsNone(self.row()["pending_prompt"])

    def test_expansion_records_all_sources_for_ancestry_and_final_accounting(self):
        self.select.return_value = [pull(8, HEAD_TWO), pull(9, HEAD_THREE)]
        self.retry()
        self.assertEqual(self.row()["expansion_count"], 1)
        self.assertEqual([p.number for p in automerge._all_batch_pulls(self.row())], [7, 8, 9])
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [7, 8, 9])

    def test_removed_pr_does_not_reenter_even_at_a_new_head(self):
        self.add_source()
        automerge._append_removed(self.conn, "retry", ["PR #7 Change 7: head changed"])
        self.select.return_value = [pull(7, HEAD_TWO), pull(9, HEAD_THREE)]
        self.assertTrue(self.retry())
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [8, 9])
        self.assertEqual([p.number for p in automerge._all_batch_pulls(self.row())], [7, 8, 9])
        self.assertEqual(json.loads(self.row()["excluded_source_heads_json"])[0]["head_sha"], HEAD_ONE)

    def test_priority_expansion_only_admits_priority_prs(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET priority=1")
        self.select.return_value = [pull(8, HEAD_TWO), pull(9, HEAD_THREE, priority=True)]
        self.retry()
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [7, 9])

    def test_normal_batch_does_not_absorb_new_priority_pr(self):
        self.select.return_value = [pull(8, HEAD_TWO, priority=True)]
        self.assertFalse(self.retry(only_if_expanded=True))
        self.assertEqual(self.row()["expansion_count"], 0)

    def test_last_source_removed_finishes_without_absorbing_new_arrival(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.select.return_value = [pull(8, HEAD_THREE)]
        self.cleanup_mocks()
        automerge._rebuild_or_finish(self.conn, self.transport, self.row(), [pull()], "head changed")
        self.assertEqual(automerge.row_pulls(self.row()), [])
        self.assertFalse(self.states[7]["isDraft"])
        self.assertEqual(len(self.draft_intents()), 1)
        self.assertEqual(self.row()["terminal_status"], "no_sources_remain")
        self.assertEqual(self.row()["phase"], "terminal")
        self.assertEqual(self.row()["expansion_count"], 0)
        self.select.assert_not_called()
        self.suspend.assert_called_once_with(self.conn, self.transport, "retry", "live-session")

    def test_last_source_removed_at_expansion_cap_finishes_without_reintroducing_it(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET expansion_count=3")
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.cleanup_mocks()
        automerge._rebuild_or_finish(self.conn, self.transport, self.row(), [pull()], "head changed")
        self.assertEqual(self.row()["terminal_status"], "no_sources_remain")
        self.assertEqual(automerge.row_pulls(self.row()), [])
        self.select.assert_not_called()

    def test_changed_source_is_drafted_once_and_removed(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        keep, removed = automerge._recheck_sources([pull()], conn=self.conn, batch_id="retry")
        self.assertEqual(keep, [])
        self.assertIn("head changed", removed[0])
        automerge._recheck_sources([pull()], conn=self.conn, batch_id="retry")
        self.assertEqual(len(self.draft_intents()), 1)
        self.gh.assert_not_called()

    def test_invalid_head_never_causes_drafting(self):
        self.states[7] = direct_view(head_sha="")
        with self.assertRaises(automerge.AutomergeError):
            automerge._recheck_sources([pull()], conn=self.conn, batch_id="retry")
        self.gh.assert_not_called()

    def test_closed_or_already_draft_source_does_not_issue_draft_mutation(self):
        for view in (direct_view(state="CLOSED", head_sha=HEAD_TWO), direct_view(draft=True, head_sha=HEAD_TWO)):
            self.states[7] = view
            self.assertEqual(automerge._recheck_sources([pull()], conn=self.conn, batch_id="retry")[0], [])
        self.gh.assert_not_called()

    def test_drafting_failure_retries_in_outbox_without_consuming_expansion(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.assertFalse(self.retry())
        self.assertEqual(len(self.draft_intents()), 1)
        with mock.patch.object(automerge, "deliver_github_write",
                               side_effect=monitor.CommandError("GitHub unavailable")):
            automerge.retry_github_outbox(self.conn, self.transport)
        self.assertEqual(self.draft_intents()[0]["attempts"], 1)
        self.assertEqual(self.row()["expansion_count"], 0)
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [])
        self.select.return_value = [pull(8, HEAD_THREE)]
        self.select.reset_mock()
        self.assertFalse(self.retry())
        self.assertEqual(automerge.row_pulls(self.row()), [])
        self.select.assert_not_called()

    def test_direct_path_drafts_changed_head_before_refusing_merge(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.patch(automerge, "direct_pull_request_view", return_value=self.states[7])
        gate, _ = automerge._direct_premerge_check(pull(), conn=self.conn, batch_id="retry")
        self.assertEqual(gate, "source_changed")
        self.assertEqual(len(self.draft_intents()), 1)

    def test_running_agent_with_retained_source_is_not_interrupted_for_head_change(self):
        self.add_source()
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.select.return_value = [pull(9, HEAD_THREE)]
        self.patch(automerge, "send_start_notification")
        self.patch(automerge, "_wait_agent_turn", return_value=False)
        interrupt = self.patch(automerge, "interrupt_and_wait")
        automerge.process_batch(self.conn, self.transport, "retry")
        self.assertEqual(len(self.draft_intents()), 1)
        self.assertEqual(self.row()["expansion_count"], 0)
        self.select.assert_not_called()
        interrupt.assert_not_called()

    def test_poll_ends_batch_as_soon_as_last_source_becomes_ineligible(self):
        self.cleanup_mocks()
        wait = self.patch(automerge, "_wait_agent_turn", return_value=False)
        launch = self.patch(automerge, "launch_batch_session")
        for view in (direct_view(head_sha=HEAD_TWO), direct_view(draft=True), direct_view(state="CLOSED")):
            with self.subTest(view=view), self.conn:
                self.conn.execute("UPDATE automerge_batches SET phase='building',status='running',"
                                  "terminal_status=NULL,abort_reason=NULL,active_pull_requests_json=?,"
                                  "excluded_source_heads_json='[]'", (json.dumps([pull().as_json()]),))
                self.states[7] = view
            automerge.process_batch(self.conn, self.transport, "retry")
            self.assertEqual(self.row()["terminal_status"], "no_sources_remain")
            self.assertEqual(self.row()["status"], "completed")
            self.assertIsNone(automerge.active_batch(self.conn))
        wait.assert_not_called()
        launch.assert_not_called()
        self.select.assert_not_called()

    def test_empty_batch_cleanup_fences_writes_and_resumes_after_restart(self):
        self.cleanup_mocks()
        self.stop.side_effect = [monitor.MjError("daemon temporarily unavailable"), False, True]
        automerge._append_removed(self.conn, "retry", ["PR #7 Change 7: head changed"])
        with self.assertRaises(monitor.MjError):
            automerge.process_batch(self.conn, self.transport, "retry")
        self.assertEqual(self.row()["phase"], "aborting")
        self.assertEqual(self.row()["terminal_status"], "no_sources_remain")
        state = mm_service.state(self.conn, "retry")
        with self.assertRaisesRegex(ValueError, "no longer accepting"):
            mm_service.checked(self.conn, "retry", state["revision"])
        self.reopen()
        automerge.process_batch(self.conn, self.transport, "retry")
        self.assertEqual(self.row()["phase"], "aborting")
        self.suspend.assert_not_called()
        automerge.process_batch(self.conn, self.transport, "retry")
        self.assertEqual(self.row()["phase"], "terminal")
        self.assertEqual(self.row()["terminal_status"], "no_sources_remain")
        self.suspend.assert_called_once()
        self.select.assert_not_called()

    def test_mm_db_last_rejection_stops_running_turn_and_preserves_outbox(self):
        self.cleanup_mocks()
        current = mm_service.state(self.conn, "retry")
        mm_service.dispatch(self.conn, "retry", "exclude", {
            "revision": current["revision"], "number": 7, "head": HEAD_ONE,
            "kind": "rejected", "reason": "standalone regression", "evidence": "captured failure"})
        wait = self.patch(automerge, "_wait_agent_turn")
        automerge.process_batch(self.conn, self.transport, "retry")
        self.assertEqual(self.row()["terminal_status"], "no_sources_remain")
        wait.assert_not_called()
        rejection = self.conn.execute("SELECT * FROM automerge_github_outbox WHERE kind='reject_head'").fetchone()
        self.assertEqual(rejection["head_sha"], HEAD_ONE)
        self.assertIsNone(rejection["cancelled_at"])
        self.assertEqual(json.loads(rejection["payload_json"])["evidence"], "captured failure")

    def test_final_report_rejecting_every_source_finishes_in_both_ci_modes(self):
        self.cleanup_mocks()
        self.select.return_value = [pull(8, HEAD_TWO)]
        for mode in ("async", "sync"):
            for verdict in ("pass", "fail"):
                with self.subTest(mode=mode, verdict=verdict), self.conn:
                    self.conn.execute("UPDATE automerge_batches SET phase='building',status='running',"
                                      "terminal_status=NULL,abort_reason=NULL,active_pull_requests_json=?,"
                                      "excluded_source_heads_json='[]',ci_mode=?",
                                      (json.dumps([pull().as_json()]), mode))
                final = async_local_report(verdict) + f"\nmergemarshall:ejected-pr: 7 {HEAD_ONE}\n"
                with mock.patch.object(automerge, "_store_agent_result", return_value=final):
                    automerge._agent_turn_finished(self.conn, self.transport, self.row(), "live-session")
                self.assertEqual(self.row()["terminal_status"], "no_sources_remain")
                self.assertEqual(self.row()["phase"], "terminal")
        self.select.assert_not_called()

    def test_ambiguous_launch_is_adopted_before_empty_batch_cleanup(self):
        self.cleanup_mocks()
        automerge._append_removed(self.conn, "retry", ["PR #7 Change 7: closed"])
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET session_id=NULL,launch_attempted=1,launch_attempted_at=?",
                              (automerge.utc_now(),))
        lookup = self.patch(automerge, "lookup_batch_session", side_effect=[None, "adopted-session"])
        automerge.process_batch(self.conn, self.transport, "retry")
        self.assertEqual(self.row()["phase"], "aborting")
        self.stop.assert_not_called()
        automerge.process_batch(self.conn, self.transport, "retry")
        self.assertEqual(self.row()["phase"], "terminal")
        self.assertEqual(self.row()["session_id"], "adopted-session")
        self.assertEqual(self.stop.call_args.args[1]["session_id"], "adopted-session")
        self.suspend.assert_called_once_with(self.conn, self.transport, "retry", "adopted-session")
        self.assertEqual(lookup.call_count, 2)

    def test_next_normal_tick_selects_new_batch_and_session(self):
        self.cleanup_mocks()
        self.states[7] = direct_view(draft=True)
        automerge.process_batch(self.conn, self.transport, "retry")
        self.select.return_value = [pull(8, HEAD_TWO)]
        self.patch(automerge, "acquire_lock", return_value=mock.Mock())
        self.patch(monitor, "load_slack_transport", return_value=self.transport)
        for name in ("retry_pending_notifications", "retry_pending_aborted_outcomes", "retry_github_outbox",
                     "check_pending_suspensions", "report_dependency_blocks"):
            self.patch(automerge, name)
        self.patch(monitor, "update_known_failures")
        self.patch(automerge, "ensure_runtime_binaries", return_value=True)
        self.patch(automerge, "ensure_github_auth", return_value=True)
        self.patch(automerge, "list_open_pull_requests", return_value=[])
        self.patch(automerge, "_ancestry_cache", return_value=mock.Mock())
        self.patch(automerge, "compare_pr_behind_by", return_value=1)
        self.patch(automerge, "current_master_sha", return_value=BASE_SHA)
        self.patch(automerge, "launch_batch_session", return_value="fresh-session")
        self.patch(automerge, "_wait_agent_turn", return_value=False)
        self.patch(speculation, "tick")
        self.assertEqual(automerge.run_automerge(), 0)
        fresh = automerge.active_batch(self.conn)
        self.assertNotEqual(fresh["batch_id"], "retry")
        self.assertEqual(fresh["session_id"], "fresh-session")
        self.assertEqual([p.number for p in automerge.row_pulls(fresh)], [8])
        self.assertEqual(self.row()["phase"], "terminal")

    def test_interrupted_turn_queues_same_session_with_new_arrivals(self):
        self.select.return_value = [pull(8, HEAD_TWO)]
        self.patch(automerge, "supervise_turn", return_value=monitor.TurnResult("cancelled", "cancelled"))
        suspend = self.patch(automerge, "request_suspend")
        self.assertFalse(automerge._wait_agent_turn(self.conn, self.transport, self.row(), "live-session"))
        self.assertEqual(self.row()["session_id"], "live-session")
        self.assertEqual(self.row()["phase"], "fixing")
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [7, 8])
        suspend.assert_not_called()

    def test_ejection_rescan_survives_github_failure_and_restart(self):
        self.add_source()
        final = async_local_report("fail") + f"\nautomerge-ejected-pr: 7 {HEAD_ONE}\n"
        self.select.side_effect = monitor.CommandError("GitHub offline")
        with self.assertRaises(monitor.CommandError):
            automerge._finish_async_agent_turn(self.conn, self.transport, self.row(), final)
        self.assertEqual(self.row()["retry_rescan_pending"], 1)
        self.reopen()
        self.select.side_effect = None
        self.select.return_value = [pull(9, HEAD_THREE)]
        automerge._finish_async_agent_turn(self.conn, self.transport, self.row(), final)
        self.assertEqual(self.row()["retry_rescan_pending"], 0)
        self.assertEqual(self.row()["expansion_count"], 1)
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [8, 9])

    def test_interrupted_turn_excludes_rejection_published_before_interruption(self):
        self.add_source()
        self.patch(automerge, "list_pull_comments", return_value=[{
            "id": 1, "user": {"login": automerge.TRUSTED_REJECTION_LOGIN},
            "body": f"automerge-rejected-head: {HEAD_ONE}\nBroken build evidence.",
            "created_at": "2026-01-01",
        }])
        self.select.return_value = [pull(9, HEAD_THREE)]
        self.patch(automerge, "supervise_turn", return_value=monitor.TurnResult("cancelled", "cancelled"))
        automerge._wait_agent_turn(self.conn, self.transport, self.row(), "live-session")
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [8, 9])
