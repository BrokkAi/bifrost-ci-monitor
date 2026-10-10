"""Supervisor ordering and retries operate on isolated state, never the live queue."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import automerge as a
import git_ancestry
import monitor
import speculation
import supervisor
import read_budget
import merge_failover
from test_automerge import BASE_SHA, HEAD_ONE, pull


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.patch(a, 'DB_PATH', Path(directory.name) / 'state.db')
        self.patch(a, 'OBSERVATION_DEADLINE', None)
        self.patch(a, 'SPECULATIVE_LOOKAHEAD', True)
        self.conn = a.connect_db()
        self.addCleanup(self.conn.close)
        self.transport = monitor.SlackTransport('chat', token='unused', channel='unused')
        self.patch(a, 'list_open_pull_requests', return_value=[])
        self.select = self.patch(a, 'select_eligible_pull_requests', return_value=[])
        self.patch(a, 'report_dependency_blocks')
        self.alert = self.patch(a, 'notify_blocked_once')
        self.send = self.patch(monitor, 'slack_send', return_value=(True, 'thread'))
        for name in ('retry_pending_notifications', 'retry_pending_aborted_outcomes',
                     'enqueue_active_membership_labels', 'retry_github_outbox',
                     'check_pending_suspensions', 'finish_batch'):
            self.patch(a, name)
        self.patch(monitor, 'update_known_failures')
        self.patch(merge_failover, 'probe')

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def batch(self, checkpoint=False):
        identifier = a.create_batch(self.conn, [pull()], BASE_SHA, ci_mode='async')
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='running',session_id='session' WHERE batch_id=?", (identifier,))
        if checkpoint:
            row = self.row(identifier)
            value = {'id': 'checkpoint', 'head': HEAD_ONE, 'tree': HEAD_ONE,
                     'source_revision': speculation.source_revision(a, row)}
            with self.conn:
                self.conn.execute('UPDATE automerge_batches SET candidate_json=? WHERE batch_id=?',
                                  (json.dumps(value), identifier))
        return identifier

    def row(self, identifier):
        return self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()

    def deliver(self, conn, transport, row):
        with conn:
            conn.execute('UPDATE automerge_batches SET prompt_delivered=1 WHERE batch_id=?', (row['batch_id'],))

    def test_guidance_and_primary_progress_precede_lookahead_selection(self):
        identifier = self.batch(checkpoint=True)
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='fixing',pending_prompt='guidance',"
                              "prompt_delivered=0 WHERE batch_id=?", (identifier,))
        events = []
        def deliver(*args):
            events.append('guidance')
            self.deliver(*args)
        self.patch(a, 'deliver_pending_prompt', side_effect=deliver)
        self.patch(a, 'process_batch', side_effect=lambda *args: events.append('primary'))
        self.patch(speculation, 'launch_child', side_effect=lambda *args: events.append('lookahead'))
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        self.assertEqual(events, ['guidance', 'primary', 'lookahead'])
        self.select.assert_not_called()

    def test_two_provider_errors_move_and_continue_before_normal_observation(self):
        identifier = self.batch()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET agent_configuration='multi-pr' WHERE batch_id=?",
                              (identifier,))
        events = []
        def probe(module, conn, row):
            if merge_failover.state(conn, row['batch_id']).get('stage') == 'completed':
                return
            events.append('errors')
            merge_failover.save(a, conn, row, {'stage': 'moving'})
        def advance(module, conn, row):
            events.append('move')
            a.queue_agent_prompt(conn, row, 'continue saved work')
            merge_failover.save(a, conn, row, {'stage': 'completed', 'notice_sent': True})
        def deliver(*args):
            events.append('guidance')
            self.deliver(*args)
        self.patch(merge_failover, 'probe', side_effect=probe)
        self.patch(merge_failover, 'advance', side_effect=advance)
        self.patch(a, 'deliver_pending_prompt', side_effect=deliver)
        self.patch(a, 'process_batch', side_effect=lambda *args: events.append('observe'))
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual(events, ['errors', 'move', 'guidance', 'observe'])

    def test_move_in_progress_does_not_starve_priority_or_maintenance(self):
        identifier = self.batch()
        merge_failover.save(a, self.conn, self.row(identifier), {'stage': 'moving'})
        advance = self.patch(merge_failover, 'advance')
        observe = self.patch(a, 'process_batch')
        priority = self.patch(a, 'list_open_pull_requests', return_value=[])
        maintenance = self.patch(monitor, 'update_known_failures')
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        advance.assert_called_once()
        observe.assert_not_called()
        priority.assert_called_once()
        maintenance.assert_called_once()

    def test_promotion_instruction_is_delivered_in_the_same_tick(self):
        identifier = self.batch()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='waiting_parent',role_promoted=1 WHERE batch_id=?", (identifier,))
        events = []
        def advance(conn, transport, owner):
            if self.row(owner)['phase'] == 'waiting_parent':
                events.append('promote')
                with conn:
                    conn.execute("UPDATE automerge_batches SET phase='fixing',pending_prompt='incorporate',"
                                 "prompt_command_id='promotion',prompt_delivered=0 WHERE batch_id=?", (owner,))
            else:
                events.append('observe')
        def deliver(*args):
            events.append('guidance')
            self.deliver(*args)
        self.patch(a, 'process_batch', side_effect=advance)
        self.patch(a, 'deliver_pending_prompt', side_effect=deliver)
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual(events, ['promote', 'guidance', 'observe'])

    def test_slow_successor_selection_retains_an_action_without_blocking_primary(self):
        identifier = self.batch(checkpoint=True)
        process = self.patch(a, 'process_batch')
        self.patch(speculation, 'launch_child', side_effect=git_ancestry.Deferred('fetch pending'))
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        process.assert_called_once_with(self.conn, self.transport, identifier)
        saved = self.conn.execute("SELECT * FROM automerge_supervisor_actions WHERE kind='select_successor'").fetchone()
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['last_error'], 'fetch pending')
        self.alert.assert_not_called()
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual(process.call_count, 2)

    def test_changed_attempt_obsoletes_pending_action(self):
        identifier = self.batch()
        process = self.patch(a, 'process_batch', side_effect=git_ancestry.Deferred('unfinished'))
        supervisor.run(a, self.conn, self.transport)
        old = self.conn.execute("SELECT action_id FROM automerge_supervisor_actions WHERE kind='advance_batch'").fetchone()[0]
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET attempt_generation=attempt_generation+1 WHERE batch_id=?', (identifier,))
        process.side_effect = None
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual(self.conn.execute('SELECT status FROM automerge_supervisor_actions WHERE action_id=?', (old,)).fetchone()[0], 'obsolete')
        self.assertEqual(process.call_count, 2)

    def test_quick_fetch_result_resumes_primary_within_the_same_poll(self):
        self.batch()
        self.patch(a, 'OBSERVATION_DEADLINE', time.monotonic() + 20)
        cache = self.patch(a, 'ANCESTRY_CACHE', mock.Mock())
        cache.wait_for_fetch.return_value = True
        process = self.patch(a, 'process_batch', side_effect=[git_ancestry.Deferred('fetch pending'), None])
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        self.assertEqual(process.call_count, 2)
        cache.wait_for_fetch.assert_called_once()
        saved = self.conn.execute("SELECT * FROM automerge_supervisor_actions WHERE kind='advance_batch'").fetchone()
        self.assertEqual(saved['status'], 'complete')
        self.assertEqual(saved['attempts'], 2)
        self.alert.assert_not_called()

    def test_failed_priority_read_cannot_bypass_preemption_on_retry(self):
        self.batch()
        self.patch(a, 'list_open_pull_requests', side_effect=monitor.CommandError('network unavailable'))
        process = self.patch(a, 'process_batch')
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 4)
        supervisor.run(a, self.conn, self.transport)  # Backoff must still fence landing.
        process.assert_not_called()
        self.alert.assert_called_once()

    def test_maintenance_error_does_not_delay_primary(self):
        self.batch()
        process = self.patch(a, 'process_batch')
        self.patch(monitor, 'update_known_failures', side_effect=RuntimeError('upkeep unavailable'))
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 4)
        process.assert_called_once()

    def test_shared_command_helpers_obey_the_supervisor_budget(self):
        token = read_budget.deadline.set(time.monotonic() + 1)
        try:
            with mock.patch.object(monitor.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='ok')) as run:
                self.assertEqual(monitor.run_command(['example'], timeout=60), 'ok')
                self.assertLessEqual(run.call_args.kwargs['timeout'], 1)
            read_budget.deadline.set(time.monotonic() - 1)
            with mock.patch.object(monitor.subprocess, 'run') as run:
                with self.assertRaises(read_budget.Deferred):
                    monitor.mj_command(['sessions'])
                run.assert_not_called()
        finally:
            read_budget.deadline.reset(token)

    def test_idle_summary_is_threaded_and_unchanged_inventory_is_quiet(self):
        for number, draft, reason in [(1, True, None), (2, False, 'blocked by prerequisite PR #3')]:
            item = {'number': number, 'state': 'open', 'draft': draft, 'title': 'change', 'labels': []}
            with self.conn:
                self.conn.execute('INSERT INTO automerge_pr_inventory '
                                  '(number,head_sha,data_json,blocked_reason) VALUES (?,?,?,?)',
                                  (number, HEAD_ONE, json.dumps(item), reason))
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        calls = self.send.call_args_list
        self.assertIn('merge queue idle:', calls[0].args[1])
        self.assertTrue(all(call.args[2] == 'thread' for call in calls[1:]))
        text = '\n'.join(call.args[1] for call in calls[1:])
        self.assertIn('2 open PRs', text)
        self.assertIn('1 draft', text)
        self.assertIn('1 dependency blocked', text)
        count = self.send.call_count
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual(self.send.call_count, count)

    def test_idle_summary_retries_only_unaccepted_thread_replies(self):
        self.send.side_effect = [(True, 'thread'), (False, None)]
        supervisor.idle_summary(a, self.conn, self.transport)
        saved = self.conn.execute('SELECT * FROM automerge_queue_summaries').fetchone()
        self.assertEqual(saved['next_message'], 0)
        self.send.side_effect = None
        self.send.reset_mock()
        supervisor.idle_summary(a, self.conn, self.transport)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[2], 'thread')


if __name__ == '__main__':
    unittest.main()
