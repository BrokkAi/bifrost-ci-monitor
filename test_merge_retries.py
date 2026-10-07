"""Retry membership and drafting regressions using the production SQLite schema."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import automerge
import monitor
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
        self.assertEqual(args[:2], ["pr", "ready"])
        self.assertIn("--undo", args)
        self.states[int(args[2])]["isDraft"] = True
        return ""

    def row(self):
        return self.conn.execute("SELECT * FROM automerge_batches WHERE batch_id='retry'").fetchone()

    def reopen(self):
        self.conn.close()
        self.conn = automerge.connect_db()

    def retry(self, *, only_if_expanded=False):
        return automerge._request_rebuild(self.conn, self.row(), automerge.row_pulls(self.row()),
                                          "retry", only_if_expanded=only_if_expanded)

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
        automerge._append_removed(self.conn, "retry", ["PR #7 Change 7: head changed"])
        self.select.return_value = [pull(7, HEAD_TWO), pull(8, HEAD_THREE)]
        self.assertTrue(self.retry())
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [8])
        self.assertEqual([p.number for p in automerge._all_batch_pulls(self.row())], [7, 8])
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

    def test_last_source_removed_can_be_replaced_by_new_arrival(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.select.return_value = [pull(8, HEAD_THREE)]
        terminal = self.patch(automerge, "_terminal")
        automerge._rebuild_or_finish(self.conn, self.transport, self.row(), [pull()], "head changed")
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [8])
        self.assertTrue(self.states[7]["isDraft"])
        terminal.assert_not_called()

    def test_last_source_removed_at_expansion_cap_finishes_without_reintroducing_it(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET expansion_count=3")
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        terminal = self.patch(automerge, "_terminal")
        automerge._rebuild_or_finish(self.conn, self.transport, self.row(), [pull()], "head changed")
        terminal.assert_called_once()
        self.assertEqual(terminal.call_args.args[3], "no_sources_remain")
        self.assertEqual(automerge.row_pulls(self.row()), [])
        self.select.assert_not_called()

    def test_changed_source_is_drafted_once_and_removed(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        keep, removed = automerge._recheck_sources([pull()])
        self.assertEqual(keep, [])
        self.assertIn("head changed", removed[0])
        automerge._recheck_sources([pull()])
        self.gh.assert_called_once_with(["pr", "ready", "7", "--undo", "--repo", monitor.REPO_NAME])

    def test_invalid_head_never_causes_drafting(self):
        self.states[7] = direct_view(head_sha="")
        with self.assertRaises(automerge.AutomergeError):
            automerge._recheck_sources([pull()])
        self.gh.assert_not_called()

    def test_closed_or_already_draft_source_does_not_issue_draft_mutation(self):
        for view in (direct_view(state="CLOSED", head_sha=HEAD_TWO), direct_view(draft=True, head_sha=HEAD_TWO)):
            self.states[7] = view
            self.assertEqual(automerge._recheck_sources([pull()])[0], [])
        self.gh.assert_not_called()

    def test_drafting_failure_retries_without_consuming_expansion(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.gh.side_effect = monitor.CommandError("GitHub unavailable")
        with self.assertRaises(monitor.CommandError):
            self.retry()
        self.assertEqual(self.row()["expansion_count"], 0)
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [7])
        self.gh.side_effect = self.github_write
        self.select.return_value = [pull(8, HEAD_THREE)]
        self.retry()
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [8])

    def test_direct_path_drafts_changed_head_before_refusing_merge(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.patch(automerge, "direct_pull_request_view", return_value=self.states[7])
        gate, _ = automerge._direct_premerge_check(pull())
        self.assertEqual(gate, "source_changed")
        self.assertTrue(self.states[7]["isDraft"])

    def test_running_agent_is_not_interrupted_for_head_change_or_new_arrival(self):
        self.states[7] = direct_view(head_sha=HEAD_TWO)
        self.select.return_value = [pull(8, HEAD_THREE)]
        self.patch(automerge, "send_start_notification")
        self.patch(automerge, "_wait_agent_turn", return_value=False)
        interrupt = self.patch(automerge, "interrupt_and_wait")
        automerge.process_batch(self.conn, self.transport, "retry")
        self.assertTrue(self.states[7]["isDraft"])
        self.assertEqual(self.row()["expansion_count"], 0)
        self.select.assert_not_called()
        interrupt.assert_not_called()

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
        final = async_local_report() + f"\nautomerge-ejected-pr: 7 {HEAD_ONE}\n"
        self.select.side_effect = monitor.CommandError("GitHub offline")
        with self.assertRaises(monitor.CommandError):
            automerge._finish_async_agent_turn(self.conn, self.transport, self.row(), final)
        self.assertEqual(self.row()["retry_rescan_pending"], 1)
        self.reopen()
        self.select.side_effect = None
        self.select.return_value = [pull(8, HEAD_TWO)]
        automerge._finish_async_agent_turn(self.conn, self.transport, self.row(), final)
        self.assertEqual(self.row()["retry_rescan_pending"], 0)
        self.assertEqual(self.row()["expansion_count"], 1)
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [8])

    def test_interrupted_turn_excludes_rejection_published_before_interruption(self):
        self.patch(automerge, "list_pull_comments", return_value=[{
            "id": 1, "user": {"login": automerge.TRUSTED_REJECTION_LOGIN},
            "body": f"automerge-rejected-head: {HEAD_ONE}\nBroken build evidence.",
            "created_at": "2026-01-01",
        }])
        self.select.return_value = [pull(8, HEAD_TWO)]
        self.patch(automerge, "supervise_turn", return_value=monitor.TurnResult("cancelled", "cancelled"))
        automerge._wait_agent_turn(self.conn, self.transport, self.row(), "live-session")
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [8])
