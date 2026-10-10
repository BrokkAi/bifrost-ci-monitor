"""Failover uses isolated supervisor state and mocked native operations."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import automerge as a
import merge_failover as f
import monitor
import read_budget
import speculation
import supervisor
from test_automerge import BASE_SHA, HEAD_ONE, pull


ERROR = 'API Error: 503 Bedrock is unable to process your request.'


def item(seq, text=ERROR, kind='agent', identity=None, status=None):
    body = {'kind': kind}
    if status:
        body['call'] = {'status': status}
    return {'seq': seq, 'stable_id': identity or str(seq), 'text': text, 'body': body}


class FailoverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.patch(a, 'DB_PATH', Path(directory.name) / 'state.db')
        self.patch(a, 'OBSERVATION_DEADLINE', None)
        self.conn = a.connect_db()
        self.addCleanup(self.conn.close)
        self.batch = a.create_batch(self.conn, [pull(), pull(2, HEAD_ONE)], BASE_SHA, ci_mode='async')
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='running',session_id='session',"
                              "thread_ts='thread' WHERE batch_id=?", (self.batch,))
        self.session = self.patch(a, '_session_status', return_value={
            'id': 'session', 'target_id': a.MJ_TARGET, 'profile_id': 'bedrock'})
        self.items = self.patch(f, 'read_items', return_value={
            'items': [item(1), item(2)], 'next_after_seq': 2, 'latest_seq': 2})
        self.native = self.patch(f, 'read_move', return_value=None)
        self.prepare = self.patch(f, 'prepare', return_value={'in_place': True})
        self.submit = self.patch(f, 'submit_move')
        self.command = self.patch(monitor, 'require_mj_success', return_value=json.dumps({
            'config_options': [{'id': 'model', 'current_value': f.MODEL}]}))

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def row(self):
        return self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                                 (self.batch,)).fetchone()

    def value(self):
        return f.state(self.conn, self.batch)

    def trigger(self):
        f.probe(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'moving')

    def operation(self, phase='completed'):
        return {'operation_id': 'native-move', 'phase': phase, 'in_place': True,
                'selection': {'session_id': 'session', 'profile_id': f.PROFILE}}

    def test_one_error_waits_for_a_distinct_second_error_across_polls(self):
        self.items.return_value = {'items': [item(1)], 'next_after_seq': 1, 'latest_seq': 1}
        f.probe(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'observing')
        # A replay or updated rendering of the same error isn't another failure.
        self.items.return_value = {'items': [item(2, identity='1')], 'next_after_seq': 2, 'latest_seq': 2}
        f.probe(a, self.conn, self.row())
        self.assertEqual(self.value()['errors'], ['1'])
        self.items.return_value = {'items': [item(3)], 'next_after_seq': 3, 'latest_seq': 3}
        self.trigger()
        self.submit.assert_not_called()

    def test_successful_progress_and_different_errors_break_the_streak(self):
        for progress in (item(2, 'Continuing.'), item(2, 'curl HTTP 503', 'tool', status='completed'),
                         item(2, 'API Error: 429 Bedrock rate limited')):
            with self.subTest(progress=progress):
                value = {'cursor': 0, 'errors': []}
                f.observe_items(value, [item(1), progress, item(3)])
                self.assertEqual(value['errors'], ['3'])

    def test_only_actual_agent_provider_errors_count(self):
        value = {'cursor': 0, 'errors': []}
        f.observe_items(value, [item(1, ERROR, 'user'), item(2, ERROR, 'tool'),
                                item(3, '> ' + ERROR), item(4, 'API Error: 503 GitHub unavailable'),
                                item(5, 'Our issue mentions ' + ERROR)])
        self.assertEqual(value['errors'], [])

    def test_retry_prompts_and_lifecycle_notices_do_not_break_error_streak(self):
        value = {'cursor': 0, 'errors': []}
        f.observe_items(value, [item(1), item(2, 'Continue', 'user'),
                                item(3, 'retry scheduled', 'system'), item(4)])
        self.assertEqual(value['errors'], ['1', '4'])

    def test_old_errors_cannot_trigger_before_the_transcript_tail(self):
        self.items.return_value['latest_seq'] = 3
        f.probe(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'observing')
        self.items.return_value = {'items': [item(3, 'Recovered.')], 'next_after_seq': 3, 'latest_seq': 3}
        f.probe(a, self.conn, self.row())
        self.assertEqual(self.value()['errors'], [])
        self.native.assert_not_called()

    def test_non_bedrock_and_already_moved_sessions_are_not_probed(self):
        for session in ({'target_id': 'podman', 'profile_id': 'bedrock'},
                        {'target_id': a.MJ_TARGET, 'profile_id': f.PROFILE}):
            self.session.return_value = session
            f.probe(a, self.conn, self.row())
        self.items.assert_not_called()

    def test_valid_handoff_wins_over_provider_errors(self):
        with mock.patch.object(a, 'ready_candidate', return_value={'head': HEAD_ONE}):
            f.probe(a, self.conn, self.row())
        self.items.assert_not_called()
        self.session.assert_not_called()

    def test_prior_move_cannot_be_adopted_as_the_new_failover(self):
        self.native.return_value = self.operation()
        self.trigger()
        self.assertEqual(self.value()['prior_operation_id'], 'native-move')
        self.prepare.return_value = {'in_place': False}
        with self.assertRaisesRegex(monitor.MjError, 'retain this workspace'):
            f.advance(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'moving')

    def test_timeout_adopts_native_move_and_next_poll_continues_same_work_once(self):
        self.trigger()
        before = {key: self.row()[key] for key in ('session_id', 'base_sha', 'pull_requests_json',
                  'attempt_generation', 'candidate_json', 'ready_json')}
        self.native.side_effect = [None, self.operation('resuming_destination'),
                                  self.operation('resuming_destination')]
        self.submit.side_effect = monitor.MjError('CLI observation timed out')
        f.advance(a, self.conn, self.row())
        self.assertEqual(self.value()['operation_id'], 'native-move')
        self.assertEqual(self.value()['stage'], 'moving')
        self.native.side_effect = None
        self.native.return_value = self.operation()
        self.session.return_value = {'id': 'session', 'profile_id': f.PROFILE}
        f.advance(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'completed')
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual({key: self.row()[key] for key in before}, before)
        request_id = self.row()['prompt_command_id']
        self.assertEqual(self.row()['agent_configuration'], 'flash-luna')
        self.assertIn('reuse matching validation', self.row()['pending_prompt'])
        f.advance(a, self.conn, self.row())
        self.assertEqual(self.row()['prompt_command_id'], request_id)

    def test_lost_reply_on_move_completion_is_reconciled_from_journal(self):
        self.trigger()
        self.native.side_effect = [None, self.operation(), self.operation()]
        self.submit.side_effect = read_budget.Deferred('poll budget ended')
        self.session.return_value = {'id': 'session', 'profile_id': f.PROFILE}
        f.advance(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'completed')
        self.submit.assert_called_once_with('session', timeout=15)

    def test_no_second_move_when_acceptance_is_unknown_or_native_move_failed(self):
        self.trigger()
        value = self.value()
        value['move_attempted'] = True
        f.save(a, self.conn, self.row(), value)
        with self.assertRaisesRegex(monitor.MjError, 'acceptance is uncertain'):
            f.advance(a, self.conn, self.row())
        self.native.return_value = self.operation('failed')
        with self.assertRaisesRegex(monitor.MjError, 'Move failed'):
            f.advance(a, self.conn, self.row())
        self.submit.assert_not_called()

    def test_environment_transfer_is_refused_before_interruption(self):
        self.trigger()
        self.prepare.return_value = {'in_place': False}
        with self.assertRaisesRegex(monitor.MjError, 'retain this workspace in place'):
            f.advance(a, self.conn, self.row())
        self.submit.assert_not_called()
        self.assertFalse(self.value()['move_attempted'])

    def test_exhausted_poll_budget_does_not_checkpoint_an_unsubmitted_move(self):
        self.trigger()
        with mock.patch.object(read_budget, 'timeout', side_effect=read_budget.Deferred('budget')):
            with self.assertRaises(read_budget.Deferred):
                f.advance(a, self.conn, self.row())
        self.assertFalse(self.value()['move_attempted'])
        self.submit.assert_not_called()

    def test_late_handoff_is_not_overwritten_by_the_continuation(self):
        self.trigger()
        self.native.return_value = self.operation()
        self.session.return_value = {'id': 'session', 'profile_id': f.PROFILE}
        # Receipt arrives between our observation and queue_agent_prompt's write.
        with mock.patch.object(a, 'ready_candidate', side_effect=[None, {'head': HEAD_ONE}]):
            f.advance(a, self.conn, self.row())
        self.assertIsNone(self.row()['prompt_command_id'])
        self.assertIsNone(self.row()['pending_prompt'])
        self.assertEqual(self.value()['stage'], 'completed')

    def test_model_configuration_must_be_confirmed_before_continuation(self):
        self.trigger()
        self.native.return_value = self.operation()
        self.session.return_value = {'id': 'session', 'profile_id': f.PROFILE}
        self.command.side_effect = [json.dumps({'config_options': [{'id': 'model', 'current_value': 'deepseek-v4-pro'}]}),
                                   json.dumps({'models': [{'name': f.MODEL, 'value': f.MODEL}]}), '{}']
        f.advance(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'configuring')
        self.assertIsNone(self.row()['pending_prompt'])
        self.command.side_effect = None
        f.advance(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'completed')

    def test_continuation_and_failover_checkpoint_roll_back_together(self):
        self.trigger()
        self.native.return_value = self.operation()
        self.session.return_value = {'id': 'session', 'profile_id': f.PROFILE}
        self.conn.execute("CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON automerge_provider_failovers "
                          "WHEN NEW.state_json LIKE '%continuation_queued%' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaisesRegex(a.sqlite3.Error, 'injected'):
            f.advance(a, self.conn, self.row())
        self.assertIsNone(self.row()['prompt_command_id'])
        self.assertIsNone(self.row()['pending_prompt'])
        self.assertEqual(self.row()['agent_configuration'], 'multi-pr')
        self.assertEqual(self.value()['stage'], 'continuing')
        self.conn.execute('DROP TRIGGER fail_checkpoint')
        f.advance(a, self.conn, self.row())
        self.assertEqual(self.value()['stage'], 'completed')

    def test_notification_retries_without_repeating_an_accepted_notice(self):
        self.trigger()
        value = self.value()
        value['stage'] = 'completed'
        f.save(a, self.conn, self.row(), value)
        transport = monitor.SlackTransport('chat', token='unused', channel='unused')
        with mock.patch.object(monitor, 'relay_text', side_effect=[False, True]) as relay:
            f.notify(a, self.conn, transport)
            f.notify(a, self.conn, transport)
            f.notify(a, self.conn, transport)
        self.assertEqual(relay.call_count, 2)
        self.assertTrue(self.value()['notice_sent'])

    def test_pending_move_fences_guidance_but_independent_work_remains_planned(self):
        self.trigger()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='fixing',pending_prompt='old',"
                              "prompt_delivered=0 WHERE batch_id=?", (self.batch,))
        options = supervisor.plan(a, self.conn, priority_checked=False, queue_checked=False,
                                  maintenance_done=False, reconciled=set())
        kinds = [option[2] for option in options]
        self.assertIn('advance_provider_failover', kinds)
        self.assertNotIn('deliver_guidance', kinds)
        self.assertNotIn('advance_batch', kinds)
        self.assertIn('check_priority', kinds)
        self.assertIn('maintenance', kinds)

    def test_failover_configuration_survives_membership_changes(self):
        for configuration, policy in [('flash-luna', 'single-model'), ('flash-none', 'none')]:
            with self.conn:
                self.conn.execute('UPDATE automerge_batches SET agent_configuration=?, '
                                  'active_pull_requests_json=? WHERE batch_id=?',
                                  (configuration, json.dumps([pull().as_json()]), self.batch))
            args = a.new_session_argv(self.row(), 'prompt')
            self.assertEqual(args[args.index('--model') + 1], f.MODEL)
            self.assertEqual(args[args.index('--subagents') + 1], policy)


class NativeCommandTests(unittest.TestCase):
    def test_profile_only_move_preserves_delegation_and_environment(self):
        with mock.patch.object(monitor, 'mj_command', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({'outcome': 'completed', 'operation_id': 'move'}))) as command:
            f.submit_move('session')
        args = command.call_args.args[0]
        self.assertIn('deepseek', args)
        self.assertIn('--yes', args)
        self.assertNotIn('--target', args)
        self.assertNotIn('--subagents', args)
        self.assertNotIn('--clear-resources', args)


if __name__ == '__main__':
    unittest.main()
