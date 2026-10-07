"""Informational source labels use isolated state and mocked GitHub writes."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import automerge
import mm_service
import monitor
import pr_dependencies
from test_automerge import BASE_SHA, HEAD_ONE, HEAD_TWO, HEAD_THREE, api_pull, direct_view, pull


class MembershipLabelTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.patch(automerge, "DB_PATH", Path(directory.name) / "state.db")
        self.conn = automerge.connect_db()
        self.addCleanup(lambda: self.conn.close())
        self.details = {}
        self.label_exists = True
        self.writes = []
        self.patch(automerge, "gh_json", side_effect=self.read)
        self.write = self.patch(automerge, "github_api_write", side_effect=self.apply_write)
        self.gh = self.patch(automerge, "run_gh", side_effect=AssertionError("unexpected GitHub command"))
        self.patch(monitor, "run_gh", side_effect=AssertionError("unexpected external write"))
        self.patch(monitor, "mj_command", side_effect=AssertionError("unexpected session creation"))
        self.slack = self.patch(monitor, "slack_send", side_effect=AssertionError("unexpected Slack send"))
        self.patch(automerge, "finish_batch")
        self.patch(automerge, "notify_blocked_once")
        self.transport = monitor.SlackTransport("webhook", webhook="unused")

    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def create(self, batch_id="batch", pulls=None, **kwargs):
        pulls = pulls or [pull()]
        for source in pulls:
            self.details.setdefault(source.number, {
                "state": "open", "head": {"sha": source.head_sha}, "labels": [],
            })
        automerge.create_batch(self.conn, pulls, BASE_SHA, batch_id=batch_id, ci_mode="async", **kwargs)
        return self.row(batch_id)

    def row(self, batch_id="batch"):
        return self.conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (batch_id,)).fetchone()

    def intents(self, batch_id="batch"):
        return self.conn.execute("SELECT * FROM automerge_github_outbox "
                                 "WHERE kind='membership_label' AND batch_id=?", (batch_id,)).fetchall()

    def read(self, args, **kwargs):
        endpoint = args[1]
        if "/labels/" in endpoint:
            if not self.label_exists:
                raise monitor.CommandError("gh: Not Found (HTTP 404)")
            return {"name": automerge.IN_PROGRESS_LABEL}
        return self.details[int(endpoint.rsplit("/", 1)[1])]

    def apply_write(self, endpoint, method, payload=None):
        self.writes.append((endpoint, method, payload))
        if endpoint == "labels":
            self.label_exists = True
            return payload
        number = int(endpoint.split("/")[1])
        if method == "POST":
            self.details[number]["labels"] = [{"name": automerge.IN_PROGRESS_LABEL}]
        else:
            self.assertEqual(method, "DELETE")
            self.details[number]["labels"] = []

    def drain(self):
        automerge.retry_github_outbox(self.conn, self.transport, limit=100)

    def tagged(self):
        return {number for number, detail in self.details.items()
                if automerge.IN_PROGRESS_LABEL in automerge._labels(detail)}

    def test_selection_records_intents_without_network_for_batch_and_direct(self):
        for kind, source in (("batch", "priority"), ("direct", "queue"), ("direct", "operator")):
            with self.subTest(kind=kind, source=source):
                batch_id = f"{kind}-{source}"
                self.create(batch_id, kind=kind, source=source)
                self.assertEqual([json.loads(row["payload_json"]) for row in self.intents(batch_id)],
                                 [{"selected": True}])
        self.gh.assert_not_called()
        self.write.assert_not_called()
        self.drain()
        self.assertEqual(self.tagged(), {7})
        self.assertEqual(len(self.writes), 1)

    def test_worker_creates_missing_repository_label(self):
        self.label_exists = False
        self.create()
        self.drain()
        self.assertTrue(self.label_exists)
        self.assertEqual([endpoint for endpoint, _, _ in self.writes], ["labels", "issues/7/labels"])
        self.assertEqual(self.tagged(), {7})

    def test_removal_cleans_pr_and_blocked_descendant_without_network(self):
        child = automerge.PullRequest(8, "child", HEAD_TWO, "https://example.invalid/8",
                                     dependencies=(pr_dependencies.Dependency(7, HEAD_ONE),))
        self.create(pulls=[pull(), child, pull(9, HEAD_THREE)])
        self.drain()
        self.write.reset_mock()
        automerge._persist_excluded_source_heads(self.conn, self.row(), [
            {"number": 7, "head_sha": HEAD_ONE, "kind": "rejected"},
        ])
        self.write.assert_not_called()
        self.assertEqual([p.number for p in automerge.row_pulls(self.row())], [9])
        self.drain()
        self.assertEqual(self.tagged(), {9})
        self.assertEqual({row["number"] for row in self.intents()
                          if not json.loads(row["payload_json"])["selected"]}, {7, 8})

    def test_expansion_records_new_source_in_same_membership_transaction(self):
        self.create()
        self.patch(automerge, "_source_pr_state", side_effect=lambda p: direct_view(head_sha=p.head_sha))
        self.patch(automerge, "list_pull_comments", return_value=[])
        self.patch(automerge, "check_source_dependencies", side_effect=lambda sources, *a, **k: (sources, {}))
        self.patch(automerge, "select_eligible_pull_requests", return_value=[pull(8, HEAD_TWO)])
        self.patch(automerge, "run_ci_impact", return_value={"mode": "impact"})
        self.assertTrue(automerge._request_rebuild(self.conn, self.row(), [pull()], "retry"))
        self.assertEqual({row["number"] for row in self.intents()}, {7, 8})
        self.assertEqual(self.row()["expansion_count"], 1)
        self.write.assert_not_called()
        self.details[8] = {"state": "open", "head": {"sha": HEAD_TWO}, "labels": []}
        self.drain()
        self.assertEqual(self.tagged(), {7, 8})

    def test_merged_direct_and_integration_records_queue_cleanup(self):
        for kind in ("batch", "direct"):
            with self.subTest(kind=kind):
                self.create(kind, kind=kind)
                self.drain()
                self.assertEqual(self.tagged(), {7})
                automerge._complete_landed_batch(self.conn, self.transport, self.row(kind), 211,
                                                merge_commit_sha=HEAD_THREE)
                self.assertEqual(self.row(kind)["terminal_status"], "merged")
                self.assertTrue(any(not json.loads(row["payload_json"])["selected"]
                                    for row in self.intents(kind)))
                self.drain()
                self.assertEqual(self.tagged(), set())

    def test_abort_failed_launch_and_terminal_paths_queue_cleanup(self):
        for ending in ("abort", "launch_failed", "no_sources", "direct_failed"):
            with self.subTest(ending=ending):
                self.create(ending)
                self.drain()
                row = self.row(ending)
                if ending == "abort":
                    automerge._complete_abort(self.conn, self.transport, row)
                elif ending == "launch_failed":
                    automerge._finish_failed_launch(self.conn, self.transport, ending, "launch_failed", "error")
                elif ending == "no_sources":
                    automerge._terminal(self.conn, self.transport, row, "no_sources")
                else:
                    automerge._direct_terminal(self.conn, self.transport, row, "master_advanced",
                                               "retry", verdict_state=None)
                self.assertTrue(any(not json.loads(intent["payload_json"])["selected"]
                                    for intent in self.intents(ending)))
                self.drain()
                self.assertEqual(self.tagged(), set())

    def test_delayed_add_after_removal_does_not_label_pr(self):
        self.create()
        automerge._persist_excluded_source_heads(self.conn, self.row(), [
            {"number": 7, "head_sha": HEAD_ONE, "kind": "removed"},
        ])
        self.drain()
        self.write.assert_not_called()
        self.assertTrue(all(row["delivered_at"] for row in self.intents()))

    def test_delayed_cleanup_preserves_label_after_reselection(self):
        self.create()
        self.drain()
        automerge._complete_landed_batch(self.conn, self.transport, self.row(), 211,
                                        merge_commit_sha=HEAD_THREE)
        self.create("next")
        self.drain()
        self.assertEqual(self.tagged(), {7})
        self.assertEqual(len(self.writes), 1)  # No remove/re-add flicker.

    def test_closed_or_changed_head_is_not_tagged_by_old_selection(self):
        for change in ({"state": "closed"}, {"head": {"sha": HEAD_TWO}}):
            with self.subTest(change=change):
                self.create(str(change))
                self.details[7].update(change)
                self.details[7]["labels"] = [{"name": automerge.IN_PROGRESS_LABEL}]
                self.drain()
                self.assertEqual(self.tagged(), set())
                self.details[7].update(state="open", head={"sha": HEAD_ONE})

    def test_lost_add_and_remove_replies_are_reconciled_without_repeated_writes(self):
        self.create()

        def accepted_but_lost(*args, **kwargs):
            self.apply_write(*args, **kwargs)
            raise OSError("response lost")

        for selected in (True, False):
            if not selected:
                automerge._complete_landed_batch(self.conn, self.transport, self.row(), 211,
                                                merge_commit_sha=HEAD_THREE)
            self.write.side_effect = accepted_but_lost
            self.drain()
            pending = [row for row in self.intents() if row["delivered_at"] is None]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["attempts"], 1)
            self.assertIsNotNone(pending[0]["next_attempt_at"])
            with self.conn:
                self.conn.execute("UPDATE automerge_github_outbox SET next_attempt_at=NULL")
            self.write.side_effect = self.apply_write
            self.drain()
            self.assertTrue(all(row["delivered_at"] for row in self.intents()))
        self.assertEqual(len(self.writes), 2)

    def test_label_outage_does_not_change_revision_or_block_completion(self):
        self.create()
        mm_service.ensure_schema(self.conn)
        before = mm_service.state(self.conn, "batch")
        self.assertEqual(before["pending_github_writes"], [])
        self.write.side_effect = monitor.CommandError("GitHub labels unavailable")
        for attempt in range(1, 5):
            with self.conn:
                self.conn.execute("UPDATE automerge_github_outbox SET next_attempt_at=NULL")
            self.drain()
            automerge.enqueue_active_membership_labels(self.conn)
            self.assertEqual(self.intents()[0]["attempts"], attempt)
            self.assertIsNotNone(self.intents()[0]["next_attempt_at"])
            self.assertEqual(mm_service.state(self.conn, "batch")["revision"], before["revision"])
        self.slack.assert_not_called()
        automerge._complete_landed_batch(self.conn, self.transport, self.row(), 211,
                                        merge_commit_sha=HEAD_THREE)
        self.assertEqual(self.row()["terminal_status"], "merged")

    def test_restart_adopts_preexisting_batch_without_resetting_retries(self):
        self.create()
        with self.conn:
            self.conn.execute("DELETE FROM automerge_github_outbox")
        self.conn.close()
        self.conn = automerge.connect_db()
        automerge.enqueue_active_membership_labels(self.conn)
        self.drain()
        self.assertEqual(self.tagged(), {7})
        automerge.enqueue_active_membership_labels(self.conn)
        self.assertEqual(len(self.intents()), 1)
        self.assertIsNotNone(self.intents()[0]["delivered_at"])

    def test_informational_label_does_not_change_readiness(self):
        self.patch(automerge, "list_pull_comments", return_value=[])
        self.assertTrue(automerge._queue_ready(api_pull(7, labels=[automerge.IN_PROGRESS_LABEL]), conn=self.conn))

    def test_membership_delivery_does_not_delay_other_outbox_writes(self):
        self.create()
        with self.conn:
            automerge.enqueue_github_write(self.conn, "batch", "issue_comment", 4519, "", {"body": "diagnosis"})
        with mock.patch.object(automerge, "deliver_github_write") as deliver:
            automerge.retry_github_outbox(self.conn, self.transport, limit=1)
        self.assertEqual(deliver.call_args.args[0]["kind"], "issue_comment")
