"""Native transcript updates, durable SQLite state, and threaded heartbeat delivery."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import automerge as a
import merge_heartbeat as heartbeat
import monitor
import read_budget
from test_automerge import BASE_SHA, pull, record_handoff

START = 1_700_000_000
PRIVATE = 'private-tool-input-must-never-be-retained'


def agent(seq=10, seconds=10):
    return {'seq': seq, 'stable_id': f'agent:{seq}', 'role': 'agent',
            'created_at_ms': (START + seconds) * 1000,
            'last_changed_at_ms': (START + seconds) * 1000,
            'text': PRIVATE, 'body': {'kind': 'agent', 'streaming': False}}


def tool(identity, seq, seconds, status='completed'):
    return {'seq': seq, 'stable_id': f'tool:{identity}', 'role': 'tool',
            'created_at_ms': (START + seconds) * 1000,
            'text': PRIVATE, 'body': {'call': {'status': status, 'rawInput': PRIVATE},
                                      'presentation': PRIVATE}}


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch = mock.patch.object(a, 'DB_PATH', Path(directory.name) / 'state.db')
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = a.connect_db()
        self.addCleanup(lambda: self.conn.close())
        self.batch = a.create_batch(self.conn, [pull()], BASE_SHA, ci_mode='async')
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='running',session_id='session',"
                              "thread_ts='thread',created_at=? WHERE batch_id=?",
                              (datetime.fromtimestamp(START, timezone.utc).isoformat(), self.batch))
        self.transport = monitor.SlackTransport('chat', token='unused', channel='unused')
        self.now = START + 600
        self.clock = mock.patch.object(heartbeat.time, 'time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.pages = []
        self.read = mock.patch.object(monitor, 'mj_command', side_effect=self.response)
        self.cli = self.read.start()
        self.addCleanup(self.read.stop)
        self.post = mock.patch.object(monitor, 'relay_text', return_value=True)
        self.send = self.post.start()
        self.addCleanup(self.post.stop)

    def response(self, args, **kwargs):
        if not self.pages:
            self.fail('unexpected transcript read')
        return subprocess.CompletedProcess(args, 0, json.dumps(self.pages.pop(0)), '')

    def page(self, items, cursor=50, latest=None):
        self.pages.append({'items': items, 'next_after_seq': cursor,
                           'latest_seq': latest if latest is not None else cursor})

    def row(self):
        return self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                                 (self.batch,)).fetchone()

    def observe(self, items=()):
        heartbeat.observe(a, self.conn, self.transport, self.row(), list(items))

    def text(self):
        return self.send.call_args.args[2]

    def state(self):
        return json.loads(self.conn.execute('SELECT state_json FROM automerge_activity_heartbeats '
                                            'WHERE batch_id=?', (self.batch,)).fetchone()[0])

    def test_concurrent_calls_updates_and_private_payloads(self):
        self.page([agent(), tool('one', 20, 20), tool('two', 30, 120, 'in_progress'),
                   tool('three', 40, 300, 'pending')])
        before = tuple(self.row())
        self.observe()
        self.assertIn('3 new tool calls since the last assistant update', self.text())
        self.assertIn('2 concurrent pending/in-progress calls: in progress 8m; pending 5m', self.text())
        self.assertEqual(self.send.call_args.args[1], 'thread')
        self.assertNotIn(PRIVATE, self.text())
        stored = self.conn.execute('SELECT state_json FROM automerge_activity_heartbeats').fetchone()[0]
        stored += repr([tuple(r) for r in self.conn.execute('SELECT * FROM automerge_activity_tools')])
        self.assertNotIn(PRIVATE, stored)
        self.assertEqual(tuple(self.row()), before)
        self.now += 300
        self.page([tool('two', 55, 120), tool('four', 60, 890, 'in_progress')], 60)
        self.observe()
        self.assertIn('1 new tool call since the previous heartbeat', self.text())
        self.assertIn('2 concurrent pending/in-progress calls: pending 10m; in progress 0m', self.text())

    def test_zero_new_calls_and_restart_do_not_repeat_or_invent_activity(self):
        self.page([agent(), tool('one', 20, 100, 'in_progress')])
        self.observe()
        self.conn.close()
        self.conn = a.connect_db()
        self.now += 299
        self.observe()
        self.assertEqual(self.send.call_count, 1)
        self.now += 1
        self.page([tool('one', 70, 100, 'in_progress')], 70)
        self.observe()
        self.assertIn('0 new tool calls since the previous heartbeat', self.text())
        self.assertIn('in progress 13m', self.text())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM automerge_activity_tools').fetchone()[0], 1)

    def test_normal_transcript_drain_reports_activity_without_agent_prose(self):
        # The ordinary finished-message relay is empty while tools keep working.
        self.page([], 50)
        self.page([agent(), tool('one', 20, 20, 'in_progress'),
                   tool('two', 30, 120, 'in_progress')])
        self.assertEqual(a.drain_transcript(self.conn, self.transport, self.batch, 'session'), [])
        self.assertIn('2 new tool calls', self.text())
        self.assertIn('2 concurrent pending/in-progress calls', self.text())

    def test_assistant_message_resets_interval_and_count_window(self):
        self.page([agent(), tool('one', 20, 20)])
        self.observe()
        self.now = START + 610
        self.observe([agent(60, 610)])
        self.now = START + 909
        self.observe()
        self.assertEqual(self.send.call_count, 1)
        self.now += 1
        self.page([agent(60, 610), tool('two', 70, 620)], 70)
        self.observe()
        self.assertIn('1 new tool call since the last assistant update', self.text())

    def test_first_scan_recovers_last_assistant_and_suppresses_stale_report(self):
        self.page([tool('one', 20, 20), agent(60, 590)], 60)
        self.observe()
        self.send.assert_not_called()
        self.now = START + 890
        self.page([], 60)
        self.observe()
        self.assertIn('0 new tool calls since the last assistant update', self.text())

    def test_failed_or_ambiguous_send_keeps_counts_and_rate_limit_across_restart(self):
        self.page([agent(), tool('one', 20, 20)])
        self.send.return_value = False
        self.observe()
        self.assertNotIn('reported_tools', self.state())
        self.conn.close()
        self.conn = a.connect_db()
        self.now += 60
        self.observe()
        self.assertEqual(self.send.call_count, 1)
        self.now += 240
        self.send.return_value = True
        self.page([], 50)
        self.observe()
        self.assertIn('1 new tool call since the last assistant update', self.text())

    def test_backfill_is_checkpointed_and_partial_counts_are_not_sent(self):
        self.page([agent(), tool('one', 20, 20)], 100, 500)
        self.page([tool('two', 120, 120)], 200, 500)
        self.observe()
        self.send.assert_not_called()
        self.assertEqual(self.state()['cursor'], 200)
        self.now += 60
        self.page([tool('three', 250, 250)], 500)
        self.observe()
        self.assertIn('3 new tool calls', self.text())
        args = self.cli.call_args.args[0]
        self.assertEqual(args[args.index('--after-seq') + 1], '200')

    def test_new_session_does_not_inherit_previous_calls_or_rate_limit(self):
        self.page([agent(), tool('one', 20, 20)])
        self.observe()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET session_id='replacement' WHERE batch_id=?",
                              (self.batch,))
        self.page([agent(), tool('new', 30, 30)], 30)
        self.observe()
        self.assertIn('1 new tool call since the last assistant update', self.text())
        self.assertNotIn('previous heartbeat', self.text())

    def test_transcript_failure_never_posts_a_guessed_heartbeat_or_breaks_supervision(self):
        self.cli.side_effect = monitor.MjError('private error detail')
        before = tuple(self.row())
        self.observe()
        self.send.assert_not_called()
        self.assertEqual(tuple(self.row()), before)

    def test_handoff_terminal_missing_thread_and_webhook_do_not_emit(self):
        self.transport = monitor.SlackTransport('webhook', webhook='unused')
        self.observe()
        self.transport = monitor.SlackTransport('chat', token='unused', channel='unused')
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET thread_ts=NULL WHERE batch_id=?', (self.batch,))
        self.observe()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET thread_ts='thread',phase='terminal' WHERE batch_id=?",
                              (self.batch,))
        self.observe()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='building' WHERE batch_id=?", (self.batch,))
        record_handoff(self.conn, batch_id=self.batch)
        self.observe()
        self.cli.assert_not_called()
        self.send.assert_not_called()

    def test_existing_read_budget_is_restored(self):
        token = read_budget.deadline.set(heartbeat.time.monotonic() - 1)
        try:
            self.cli.side_effect = read_budget.Deferred('read budget exhausted')
            original = read_budget.deadline.get()
            self.observe()
            self.assertEqual(read_budget.deadline.get(), original)
            self.send.assert_not_called()
        finally:
            read_budget.deadline.reset(token)


if __name__ == '__main__':
    unittest.main()
