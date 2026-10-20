"""Durable mm facts, bounded Slack exposure, retries and upgrade boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import automerge as a
import merge_updates as updates
import mm_service
import monitor
from test_automerge import BASE_SHA, HEAD_ONE, pull


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(a, 'DB_PATH', Path(temp.name) / 'state.db')
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = a.connect_db()
        self.addCleanup(lambda: self.conn.close())
        self.batch = a.create_batch(self.conn, [pull()], BASE_SHA, ci_mode='async')
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='running',session_id='session',"
                              "thread_ts='thread' WHERE batch_id=?", (self.batch,))
        self.transport = monitor.SlackTransport('chat', token='unused', channel='unused')
        self.now = 1_800_000_000
        clock = mock.patch.object(updates.time, 'time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        post = mock.patch.object(monitor, 'relay_text', return_value=True)
        self.send = post.start()
        self.addCleanup(post.stop)

    def row(self):
        return self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                                 (self.batch,)).fetchone()

    def event(self, event_kind, **payload):
        with self.conn:
            mm_service.record(self.conn, self.batch, event_kind, payload)

    def deliver(self):
        updates.deliver(a, self.conn, self.transport, self.row())

    def text(self):
        return self.send.call_args.args[2]

    def test_events_project_facts_without_evidence_and_preserve_batch_revision(self):
        secret = 'private-material-never-expose'
        self.event('candidate', head=HEAD_ONE, tree=BASE_SHA, id=secret)
        self.event('exclusion', number=123, head=BASE_SHA, kind='rejected',
                   reason='regression <@U123> GH_TOKEN=ghp_fixture', evidence=secret)
        self.event('tests', head=HEAD_ONE, verdict='pass', tests='12608 tests; 5 known failures',
                   baseline='5 failures reproduced at base', executions=[{'command': secret}])
        self.event('publication', number=321, head=HEAD_ONE, url=secret)
        self.event('ready', head=HEAD_ONE, notes=secret, assessment={'prompt': secret})
        before = tuple(self.row())
        self.deliver()
        text = self.text()
        for expected in ('Candidate checkpoint', 'PR rejection recorded', 'Local test assessment: *PASS*',
                         'Integration PR published', 'Validated handoff recorded', '#123', '#321'):
            self.assertIn(expected, text)
        for hidden in (secret, '<@U123>', 'ghp_fixture'):
            self.assertNotIn(hidden, text)
        self.assertEqual(tuple(self.row()), before)
        self.assertEqual(self.send.call_args.args[1], 'thread')
        self.now += 60
        self.deliver()
        self.send.assert_called_once()

    def test_failed_delivery_retries_after_restart_without_consuming_events(self):
        self.event('candidate', head=HEAD_ONE)
        self.send.return_value = False
        self.deliver()
        original = self.text()
        self.conn.close()
        self.conn = a.connect_db()
        self.now += 59
        updates.retry(a, self.conn, self.transport)
        self.assertEqual(self.send.call_count, 1)
        self.now += 1
        self.send.return_value = True
        updates.retry(a, self.conn, self.transport)
        self.assertEqual(self.text(), original)
        self.now += 60
        updates.retry(a, self.conn, self.transport)
        self.assertEqual(self.send.call_count, 2)

    def test_upgrade_skips_history_once_but_new_connections_keep_future_events(self):
        self.event('candidate', head=BASE_SHA)
        with self.conn:
            self.conn.execute('DROP TABLE automerge_skill_notifications')
            updates.ensure_schema(self.conn)
        self.event('candidate', head=HEAD_ONE)
        self.conn.close()
        self.conn = a.connect_db()
        self.deliver()
        self.assertIn(HEAD_ONE[:10], self.text())
        self.assertNotIn(BASE_SHA[:10], self.text())

    def test_terminal_handoff_retried_by_existing_maintenance(self):
        self.event('ready', head=HEAD_ONE, notes='private')
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',"
                              "start_notification_sent=1,start_pr_list_sent=1")
        before = tuple(self.row())
        a.retry_pending_notifications(self.conn, self.transport)
        self.assertIn('Validated handoff', self.text())
        self.assertEqual(tuple(self.row()), before)

    def test_chunking_keeps_every_long_event_in_order_under_slack_limit(self):
        for index in range(13):
            self.event('tests', head=HEAD_ONE, verdict='fail', tests=f'test {index} ' + '<' * 1000,
                       baseline='x' * 1000)
        for _ in range(10):
            self.deliver()
            self.now += 60
        texts = [call.args[2] for call in self.send.call_args_list]
        self.assertEqual(sum(text.count('Local test assessment') for text in texts), 13)
        self.assertTrue(all(len(text) <= monitor.SLACK_MESSAGE_LIMIT for text in texts))
        self.assertEqual(sum(text.count('test 12 ') for text in texts), 1)

    def test_invalid_events_do_not_block_later_valid_facts_or_leak_payloads(self):
        self.event('candidate', head='ghp_private')
        self.event('unrecognized', head=HEAD_ONE, secret='private')
        self.event('exclusion', number='private', head=HEAD_ONE, kind='rejected')
        self.event('candidate_withdrawal', candidate={'head': HEAD_ONE})
        self.deliver()
        self.assertIn('Candidate checkpoint withdrawn', self.text())
        self.assertNotIn('private', self.text())

    def test_missing_thread_webhook_and_speculative_promotion_do_not_lose_events(self):
        self.event('candidate', head=HEAD_ONE)
        updates.deliver(a, self.conn, monitor.SlackTransport('webhook'), self.row())
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET thread_ts=NULL")
        self.deliver()
        self.send.assert_not_called()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET thread_ts='thread'")
        with mock.patch.object(a.speculation, 'is_speculative', return_value=True):
            self.deliver()
        self.assertIn('Speculative mm updates', self.text())
        self.now += 60
        self.deliver()
        self.send.assert_called_once()

    def test_actual_service_rejects_stale_head_and_reports_only_accepted_exclusion(self):
        state = mm_service.state(self.conn, self.batch)
        source = state['sources'][0]
        payload = dict(revision=state['revision'], number=source['number'], head=HEAD_ONE,
                       kind='rejected', reason='proven regression', evidence='private full evidence')
        payload['head'] = 'f' * 40
        with self.assertRaises(ValueError):
            mm_service.dispatch(self.conn, self.batch, 'exclude', payload)
        self.deliver()
        self.send.assert_not_called()
        payload['head'] = source['head_sha']
        mm_service.dispatch(self.conn, self.batch, 'exclude', payload)
        self.deliver()
        self.assertIn('PR rejection recorded', self.text())
        self.assertNotIn('private full evidence', self.text())


if __name__ == '__main__':
    unittest.main()
