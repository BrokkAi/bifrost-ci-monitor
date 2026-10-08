"""Isolated state-machine and Git regressions for merge lookahead."""
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import automerge as a
import mm_service as service
import monitor
import speculation as s
from test_automerge import make_db, pull, row_for, BASE_SHA, HEAD_ONE, HEAD_TWO, HEAD_THREE
from test_mm_skills import GitFixture, merge, db


TREE = '4' * 40


class StopWorkTests(unittest.TestCase):
    def test_children_are_cleaned_up_even_when_their_turns_already_ended(self):
        busy = {'state': 'running', 'chat_phase': 'idle', 'is_idle': False}
        child = dict(busy, id='child')
        with (mock.patch.object(s, 'mj', return_value={}) as command,
              mock.patch.object(s, 'mj_api', return_value={'subagents': [{'session': child}]}),
              mock.patch.object(a, '_session_status', side_effect=lambda identifier:
                  {'state': 'stopped'} if identifier == 'child' else busy),
              mock.patch.object(monitor, 'mj_command', return_value=subprocess.CompletedProcess(
                  [], 1, '', 'this session has no turn to cancel')) as interrupt):
            self.assertTrue(s.stop_work(a, {'session_id': 'parent'}))
        self.assertEqual([call.args[0][2] for call in interrupt.call_args_list], ['parent', 'child'])
        commands = [call.args[1] for call in command.call_args_list]
        self.assertIn(['stop-task', '--session', 'child', '--all', '--json'], commands)
        self.assertIn(['suspend', '--session', 'child', '--acknowledge-unpublished-work', '--json'], commands)
        self.assertIn(['stop-task', '--session', 'parent', '--all', '--json'], commands)

    def test_remaining_tasks_and_child_suspension_still_block_reset(self):
        for state, children in [({'background_tasks': [{'id': 'task'}]}, []),
                                ({'background_work': {'tasks': [{'id': 'task'}]}}, []),
                                ({}, [{'session': {'id': 'child', 'state': 'stopping'}}])]:
            with (self.subTest(state=state, children=children),
                  mock.patch.object(s, 'mj', return_value={}),
                  mock.patch.object(s, 'mj_api', return_value={'subagents': children}),
                  mock.patch.object(monitor, 'interrupt_turn'),
                  mock.patch.object(a, '_session_status', side_effect=lambda identifier:
                      {'state': 'stopping'} if identifier == 'child' else state)):
                self.assertFalse(s.stop_work(a, {'session_id': 'parent'}))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(ci_mode='async', integration_pr_number=None)
        self.addCleanup(self.conn.close)
        service.ensure_schema(self.conn)
        self.transport = monitor.SlackTransport('webhook', webhook='unused')
        self.parent = 'batch-test'
        self.register(self.parent, HEAD_THREE)
        self.child = a.create_batch(self.conn, [pull(8, HEAD_TWO)], HEAD_THREE,
                                   batch_id='child-test', ci_mode='async',
                                   predecessor_id=self.parent,
                                   predecessor_candidate=s.candidate(a, self.row(self.parent)))
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='running',session_id='child-session' "
                              "WHERE batch_id=?", (self.child,))

    def row(self, identifier=None):
        return self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                                 (identifier or self.child,)).fetchone()

    def register(self, identifier, head):
        row = self.row(identifier)
        checkpoint = {'id': head, 'head': head, 'tree': TREE, 'branch': row['branch'],
                      'source_revision': s.source_revision(a, row),
                      'attempt_generation': row['attempt_generation']}
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET candidate_json=? WHERE batch_id=?',
                              (json.dumps(checkpoint), identifier))
        return checkpoint

    def assess(self, head=HEAD_TWO):
        self.register(self.child, head)
        current = service.state(self.conn, self.child)
        return service.dispatch(self.conn, self.child, 'tests',
                                {'revision': current['revision'], 'head': head, 'verdict': 'pass',
                                 'tests': 'focused check passed', 'baseline': 'none'})

    def land_parent(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='merged',"
                              "ci_head_sha=?,integration_merge_commit_sha=? WHERE batch_id=?",
                              (HEAD_THREE, BASE_SHA, self.parent))

    def successor(self):
        return a.create_batch(self.conn, [pull(9, HEAD_ONE)], HEAD_TWO, batch_id='successor-test',
                              ci_mode='async', predecessor_id=self.child,
                              predecessor_candidate=s.candidate(a, self.row()))

    def handoff(self):
        current = service.state(self.conn, self.child)
        with mock.patch.object(service, 'gh_api', return_value={'object': {'sha': HEAD_TWO}}):
            service.dispatch(self.conn, self.child, 'ready', {'revision': current['revision'], 'head': HEAD_TWO})
        a._consume_ready_candidate(self.conn, self.transport, self.row())

    def test_legacy_source_revision_survives_migration(self):
        current = service.state(self.conn, self.parent)
        self.assertEqual(current['source_revision'], service.digest([
            current['base_sha'], current['sources'], current['excluded']]))
        s.ensure_schema(self.conn, a.ensure_column)
        self.assertEqual(current['source_revision'], service.state(self.conn, self.parent)['source_revision'])
        self.assertEqual(self.row()['role_promoted'], 0)
        self.assertEqual(self.row(self.parent)['role_promoted'], 0)

    def test_registration_verifies_remote_before_recording_and_opens_no_pr(self):
        current = service.state(self.conn, self.child)
        with (mock.patch.object(s, 'verify_candidate', return_value=TREE) as verify,
              mock.patch.object(service, 'reconcile_publication') as publish):
            result = service.dispatch(self.conn, self.child, 'candidate',
                                      {'revision': current['revision'], 'head': HEAD_TWO})
        verify.assert_called_once()
        publish.assert_not_called()
        self.assertEqual(result['candidate']['head'], HEAD_TWO)

    def test_withdrawal_immediately_fences_child_writes(self):
        parent = service.state(self.conn, self.parent)
        stale = service.state(self.conn, self.child)
        service.dispatch(self.conn, self.parent, 'candidate', {'revision': parent['revision'], 'withdraw': True})
        self.assertFalse(s.parent_current(a, self.conn, self.row()))
        with self.assertRaisesRegex(ValueError, 'stale revision|invalidated'):
            service.dispatch(self.conn, self.child, 'comment', {'revision': stale['revision'],
                                                               'number': 8, 'body': 'old result'})

    def test_publication_is_blocked_in_both_modes(self):
        for mode in ['sync', 'async']:
            with self.subTest(mode=mode), self.conn:
                self.conn.execute('UPDATE automerge_batches SET ci_mode=? WHERE batch_id=?', (mode, self.child))
                current = self.assess()
                with mock.patch.object(service, 'reconcile_publication') as publish:
                    with self.assertRaisesRegex(ValueError, 'blocked until'):
                        service.dispatch(self.conn, self.child, 'publish', {'revision': current['revision'], 'head': HEAD_TWO})
                    publish.assert_not_called()

    def test_independent_ejection_and_rejection_survive_invalidation(self):
        current = service.state(self.conn, self.child)
        service.dispatch(self.conn, self.child, 'exclude', {'revision': current['revision'], 'number': 8,
            'head': HEAD_TWO, 'kind': 'rejected', 'reason': 'standalone defect', 'evidence': 'inspection'})
        s.invalidate(a, self.conn, self.row())
        rejection = self.conn.execute("SELECT cancelled_at FROM automerge_github_outbox WHERE kind='reject_head'").fetchone()
        self.assertIsNone(rejection[0])
        self.assertEqual(a._excluded_source_heads(self.row())[0]['head_sha'], HEAD_TWO)

    def test_reset_fences_assessments_and_preserves_existing_environment(self):
        previous = self.assess()
        old = self.row()
        s.invalidate(a, self.conn, old)
        s.invalidate(a, self.conn, self.row())  # Idempotent across ticks.
        row = self.row()
        self.assertEqual(row['attempt_generation'], 1)
        self.assertEqual(row['session_id'], old['session_id'])
        self.assertEqual(row['branch'], old['branch'])
        self.assertEqual(row['phase'], 'resetting')
        self.assertIsNone(service.state(self.conn, self.child)['tests'])
        with self.assertRaisesRegex(ValueError, 'no longer accepting'):
            service.checked(self.conn, self.child, previous['revision'])
        self.assertEqual(json.loads(row['recovery_json'])['old_tests']['head'], HEAD_TWO)

    def test_clear_and_restart_reuse_durable_command_ids_after_lost_replies(self):
        s.invalidate(a, self.conn, self.row())
        with (mock.patch.object(s, 'stop_work', return_value=True),
              mock.patch.object(s, 'mj', return_value={'latest_seq': 99})):
            s.recover_step(a, self.conn, self.row())
        expected = 'mm-clear-child-test-1'
        with mock.patch.object(s, 'send_once', side_effect=monitor.MjError('lost reply')) as send:
            with self.assertRaises(monitor.MjError):
                s.recover_step(a, self.conn, self.row())
            self.assertEqual(send.call_args.args[-1], expected)
        with mock.patch.object(s, 'send_once', return_value={'turn_id': 100}) as send:
            s.recover_step(a, self.conn, self.row())
            self.assertEqual(send.call_args.args[-1], expected)
        divider = {'stable_id': 'context-boundary:' + expected, 'seq': 101}
        with (mock.patch.object(s, 'mj', return_value={'items': [divider]}),
              mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact', 'base_sha': HEAD_THREE}),
              mock.patch.object(a, 'skills_connection_prompt', return_value='')):
            s.recover_step(a, self.conn, self.row())
        self.assertEqual(self.row()['report_after_seq'], 101)
        with mock.patch.object(s, 'send_once', return_value={'turn_id': 102}) as send:
            s.recover_step(a, self.conn, self.row())
            self.assertEqual(send.call_args.args[-1], 'mm-restart-child-test-1')
        self.assertEqual(self.row()['phase'], 'building')
        self.assertEqual(self.row()['session_id'], 'child-session')

    def test_already_ended_non_idle_session_advances_to_existing_clear_boundary(self):
        s.invalidate(a, self.conn, self.row())
        state = {'state': 'running', 'chat_phase': 'idle', 'is_idle': False,
                 'activity_state': {'state': 'expecting'}, 'background_work': {'known': None, 'tasks': []}}
        with (mock.patch.object(s, 'mj', return_value={'latest_seq': 99}),
              mock.patch.object(s, 'mj_api', return_value={'subagents': []}),
              mock.patch.object(a, '_session_status', return_value=state),
              mock.patch.object(monitor, 'mj_command', return_value=subprocess.CompletedProcess(
                  [], 1, '', '409 Conflict: this session has no turn to cancel')),
              mock.patch.object(monitor, 'active_mj_turn', side_effect=AssertionError('no idle heuristic'))):
            s.recover_step(a, self.conn, self.row())
        self.assertEqual(json.loads(self.row()['recovery_json'])['stage'], 'clear')
        self.assertEqual(self.row()['session_id'], 'child-session')

    def test_clear_busy_reply_retries_same_identity_after_cancellation_finishes(self):
        s.invalidate(a, self.conn, self.row())
        with (mock.patch.object(s, 'stop_work', return_value=True),
              mock.patch.object(s, 'mj', return_value={'latest_seq': 99})):
            s.recover_step(a, self.conn, self.row())
        expected = s.clear_id(self.row(), json.loads(self.row()['recovery_json']))
        with mock.patch.object(s, 'send_once', side_effect=monitor.MjError('turn still running')) as send:
            with self.assertRaises(monitor.MjError):
                s.recover_step(a, self.conn, self.row())
            self.assertEqual(send.call_args.args[-1], expected)
        self.assertEqual(json.loads(self.row()['recovery_json'])['stage'], 'clear')
        with mock.patch.object(s, 'send_once', return_value={'turn_id': 100}) as send:
            s.recover_step(a, self.conn, self.row())
            self.assertEqual(send.call_args.args[-1], expected)
        self.assertEqual(json.loads(self.row()['recovery_json'])['stage'], 'cleared')

    def test_boundary_scan_advances_instead_of_rereading_old_history(self):
        s.invalidate(a, self.conn, self.row())
        recovery = json.loads(self.row()['recovery_json'])
        recovery.update(stage='cleared', clear_turn=100)
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?',
                              (json.dumps(recovery), self.child))
        with (mock.patch.object(s, 'mj', return_value={'items': [], 'next_after_seq': 80}),
              mock.patch.object(s, 'command_outcome', return_value=(None, 0))):
            s.recover_step(a, self.conn, self.row())
        self.assertEqual(json.loads(self.row()['recovery_json'])['clear_cursor'], 80)

    def test_completed_notification_does_not_block_promoted_child(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='merged' "
                              "WHERE batch_id=?", (self.parent,))
        self.assertEqual(a.active_batch(self.conn)['batch_id'], self.parent)
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET role_promoted=1 WHERE batch_id=?', (self.child,))
        self.assertEqual(a.active_batch(self.conn)['batch_id'], self.child)
        self.assertIsNone(s.child(self.conn, self.parent))
        self.assertIsNotNone(self.row()['predecessor_id'])  # Incorporation still pending.

    def test_foreground_rebuild_does_not_steal_reserved_sources(self):
        with (mock.patch.object(a, '_recheck_sources', side_effect=lambda pulls, **kw: (pulls, [])),
              mock.patch.object(a, 'select_eligible_pull_requests', return_value=[pull(8, HEAD_TWO)]),
              mock.patch.object(a, '_validation_impact', return_value={'mode': 'impact'})):
            a._request_rebuild(self.conn, self.row(self.parent), [pull()], 'retry')
        self.assertEqual([p.number for p in a.row_pulls(self.row(self.parent))], [7])

    def test_local_pass_parks_instead_of_publishing(self):
        current = self.assess()
        self.assertIsNone(a.ready_candidate(self.row()))
        with mock.patch.object(service, 'gh_api', return_value={'object': {'sha': HEAD_TWO}}):
            ready = service.dispatch(self.conn, self.child, 'ready',
                                     {'revision': current['revision'], 'head': HEAD_TWO})
        again = service.dispatch(self.conn, self.child, 'ready',
                                 {'revision': current['revision'], 'head': HEAD_TWO})
        self.assertEqual(ready['ready'], again['ready'])
        with self.assertRaisesRegex(ValueError, 'handed to supervisor'):
            service.dispatch(self.conn, self.child, 'tests',
                             {'revision': ready['revision'], 'head': HEAD_TWO, 'verdict': 'pass',
                              'tests': 'another check', 'baseline': 'none'})
        with (mock.patch.object(a, '_record_agent_exclusions'),
              mock.patch.object(a, '_recheck_sources', side_effect=lambda pulls, **kw: (pulls, []))):
            a._consume_ready_candidate(self.conn, self.transport, self.row())
        self.assertEqual(self.row()['phase'], 'waiting_parent')
        self.assertIsNone(self.row()['integration_pr_number'])

    def test_checkpoint_and_withdrawal_retries_do_not_create_new_identities(self):
        request = {'revision': service.state(self.conn, self.child)['revision'], 'head': HEAD_TWO}
        with mock.patch.object(s, 'verify_candidate', return_value=TREE) as verify:
            first = service.dispatch(self.conn, self.child, 'candidate', request)
            second = service.dispatch(self.conn, self.child, 'candidate', request)
        identity = first['candidate']['id']
        self.assertEqual(first['candidate']['id'], second['candidate']['id'])
        verify.assert_called_once()
        withdrawal = {'revision': second['revision'], 'withdraw': True}
        first = service.dispatch(self.conn, self.child, 'candidate', withdrawal)
        second = service.dispatch(self.conn, self.child, 'candidate', withdrawal)
        self.assertEqual(first['revision'], second['revision'])
        with mock.patch.object(s, 'verify_candidate', return_value=TREE):
            replacement = service.dispatch(self.conn, self.child, 'candidate',
                                          {'revision': second['revision'], 'head': HEAD_TWO})
        self.assertNotEqual(replacement['candidate']['id'], identity)
        with self.assertRaisesRegex(ValueError, 'stale revision'):
            service.dispatch(self.conn, self.child, 'candidate', withdrawal)

    def test_ready_recovery_advances_in_one_tick_and_survives_database_reopen(self):
        s.invalidate(a, self.conn, self.row())
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'state.db'
            disk = sqlite3.connect(path)
            self.conn.backup(disk)
            disk.close()
            disk = sqlite3.connect(path)
            disk.row_factory = sqlite3.Row
            try:
                def transcript(*args):
                    return {'latest_seq': 99, 'items': [{'seq': 101, 'stable_id': 'context-boundary:mm-clear-child-test-1'}]}
                with (mock.patch.object(s, 'stop_work', return_value=True),
                      mock.patch.object(s, 'mj', side_effect=transcript),
                      mock.patch.object(s, 'send_once', return_value={'turn_id': 100}) as send,
                      mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact'}),
                      mock.patch.object(a, 'skills_connection_prompt', return_value='')):
                    s.recover(a, disk, disk.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (self.child,)).fetchone())
                row = disk.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (self.child,)).fetchone()
                self.assertEqual(row['phase'], 'building')
                self.assertEqual(row['report_after_seq'], 101)
                self.assertEqual([call.args[-1] for call in send.call_args_list],
                                 ['mm-clear-child-test-1', 'mm-restart-child-test-1'])
            finally:
                disk.close()

    def test_confirmed_clear_failure_rotates_id_but_unknown_outcome_does_not(self):
        s.invalidate(a, self.conn, self.row())
        recovery = json.loads(self.row()['recovery_json'])
        recovery.update(stage='cleared', clear_cursor=99)
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?', (json.dumps(recovery), self.child))
        with (mock.patch.object(s, 'mj', return_value={'items': []}),
              mock.patch.object(s, 'command_outcome', return_value=(None, 10))):
            s.recover_step(a, self.conn, self.row())
        self.assertEqual(s.clear_id(self.row(), json.loads(self.row()['recovery_json'])), 'mm-clear-child-test-1')
        with (mock.patch.object(s, 'mj', return_value={'items': []}),
              mock.patch.object(s, 'command_outcome', return_value=({'outcome': 'failed'}, 11))):
            with self.assertRaisesRegex(a.AutomergeError, 'clear failed'):
                s.recover_step(a, self.conn, self.row())
        recovery = json.loads(self.row()['recovery_json'])
        self.assertEqual(recovery['stage'], 'stop')
        self.assertEqual(s.clear_id(self.row(), recovery), 'mm-clear-child-test-1-retry-1')

    def test_ambiguous_restart_then_parent_change_requires_another_clear(self):
        s.invalidate(a, self.conn, self.row())
        recovery = json.loads(self.row()['recovery_json'])
        recovery.update(stage='restart', boundary_seq=101)
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='restarting',recovery_json=?,pending_prompt='brief' WHERE batch_id=?",
                              (json.dumps(recovery), self.child))
        with mock.patch.object(s, 'send_once', side_effect=monitor.MjError('lost restart reply')):
            with self.assertRaises(monitor.MjError):
                s.recover_step(a, self.conn, self.row())
        self.assertTrue(json.loads(self.row()['recovery_json'])['restart_attempted'])
        self.register(self.parent, HEAD_ONE)
        with mock.patch.object(s, 'send_once') as send:
            s.recover_step(a, self.conn, self.row())
        send.assert_not_called()
        self.assertEqual(self.row()['attempt_generation'], 2)
        self.assertEqual(json.loads(self.row()['recovery_json'])['stage'], 'stop')

    def test_parent_abort_cascades_and_preserves_child_rejections(self):
        current = service.state(self.conn, self.child)
        service.dispatch(self.conn, self.child, 'exclude', {'revision': current['revision'], 'number': 8,
                         'head': HEAD_TWO, 'kind': 'rejected', 'reason': 'standalone', 'evidence': 'inspection'})
        with (mock.patch.object(a, '_session_status', return_value={'state': 'suspended'}),
              mock.patch.object(a, 'request_suspend', return_value=True),
              mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')),
              mock.patch.object(monitor, 'runtime_binary_issues', return_value=[]),
              mock.patch.object(a, 'finish_batch'), mock.patch.object(a, 'lookup_batch_session', return_value=None)):
            a.abort_batch_locked(self.conn, self.transport, self.row(self.parent), 'operator abort')
        self.assertEqual(self.row()['terminal_status'], 'aborted')
        rejection = self.conn.execute("SELECT cancelled_at FROM automerge_github_outbox WHERE kind='reject_head'").fetchone()
        self.assertIsNone(rejection[0])

    def test_empty_successor_ends_without_recovery_or_promotion(self):
        current = service.state(self.conn, self.child)
        service.dispatch(self.conn, self.child, 'exclude', {'revision': current['revision'], 'number': 8,
                         'head': HEAD_TWO, 'kind': 'rejected', 'reason': 'standalone', 'evidence': 'inspection'})
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='waiting_parent' WHERE batch_id=?", (self.child,))
        with (mock.patch.object(a, 'send_start_notification'),
              mock.patch.object(a, '_session_status', return_value={'state': 'running'}),
              mock.patch.object(s, 'stop_work', return_value=True) as stop,
              mock.patch.object(a, 'request_suspend', return_value=True) as suspend,
              mock.patch.object(a, 'finish_batch'), mock.patch.object(s, 'launch_child'),
              mock.patch.object(s, 'invalidate') as reset, mock.patch.object(s, 'promote') as promote):
            s.tick(a, self.conn, self.transport, self.row(self.parent))
        self.assertEqual(self.row()['terminal_status'], 'no_sources_remain')
        self.assertEqual(self.row()['phase'], 'terminal')
        self.assertEqual(self.row(self.parent)['status'], 'running')
        reset.assert_not_called()
        promote.assert_not_called()
        stop.assert_called_once()
        suspend.assert_called_once_with(self.conn, self.transport, self.child, 'child-session')
        rejection = self.conn.execute("SELECT cancelled_at FROM automerge_github_outbox WHERE kind='reject_head'").fetchone()
        self.assertIsNone(rejection[0])

    def test_promotion_waits_for_build_then_requires_tree_proof(self):
        current = self.assess()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='waiting_parent' WHERE batch_id=?", (self.child,))
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='merged',"
                              "ci_head_sha=?,integration_merge_commit_sha=? WHERE batch_id=?",
                              (HEAD_THREE, BASE_SHA, self.parent))
        with (mock.patch.object(a, '_session_is_idle', return_value=False),
              mock.patch.object(s, 'commit_tree') as tree):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
            tree.assert_not_called()
        with mock.patch.object(service, 'gh_api', return_value={'object': {'sha': HEAD_TWO}}):
            # Accept handoff while still building, then park via the shared supervisor path.
            with self.conn:
                self.conn.execute("UPDATE automerge_batches SET phase='building' WHERE batch_id=?", (self.child,))
            current = service.state(self.conn, self.child)
            service.dispatch(self.conn, self.child, 'ready', {'revision': current['revision'], 'head': HEAD_TWO})
            a._consume_ready_candidate(self.conn, self.transport, self.row())
        with (mock.patch.object(a, '_session_is_idle', side_effect=AssertionError('ACP is not a gate')),
              mock.patch.object(a, 'compare_commit_ancestry', return_value=True),
              mock.patch.object(s, 'commit_tree', return_value=TREE),
              mock.patch.object(a, 'current_master_sha', return_value=BASE_SHA),
              mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact'})):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
        row = self.row()
        self.assertIsNone(row['predecessor_id'])
        self.assertEqual(row['base_sha'], BASE_SHA)
        self.assertIsNone(service.state(self.conn, self.child)['tests'])
        self.assertEqual(json.loads(row['promotion_json'])['previous_tests']['head'], HEAD_TWO)

    def test_actual_master_advance_uses_fresh_attempt(self):
        current = self.assess()
        with mock.patch.object(service, 'gh_api', return_value={'object': {'sha': HEAD_TWO}}):
            service.dispatch(self.conn, self.child, 'ready', {'revision': current['revision'], 'head': HEAD_TWO})
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='waiting_parent' WHERE batch_id=?", (self.child,))
        self.land_parent()
        with (mock.patch.object(a, '_session_is_idle', return_value=True),
              mock.patch.object(a, 'compare_commit_ancestry', return_value=True),
              mock.patch.object(a, 'current_master_sha', return_value=HEAD_ONE),
              mock.patch.object(s, 'commit_tree', side_effect=[TREE, HEAD_TWO, HEAD_TWO])):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
        self.assertEqual(self.row()['phase'], 'resetting')
        self.assertEqual(json.loads(self.row()['recovery_json'])['replacement']['head'], HEAD_ONE)

    def test_landed_parent_promotes_running_successor_and_refills_lookahead(self):
        before = self.assess()  # Passing evidence alone is not a handoff.
        self.land_parent()
        original = self.row()
        with (mock.patch.object(a, 'select_eligible_pull_requests', return_value=[]),
              mock.patch.object(s, 'selection', return_value=[pull(9, HEAD_ONE)]),
              mock.patch.object(s, 'require_recovery_controls'),
              mock.patch.object(a, 'compare_commit_ancestry', return_value=True),
              mock.patch.object(s, 'commit_tree', return_value=TREE),
              mock.patch.object(a, 'current_master_sha', return_value=BASE_SHA),
              mock.patch.object(a, '_session_is_idle', side_effect=AssertionError('do not wait for checks')),
              mock.patch.object(s, 'stop_work', side_effect=AssertionError('keep running work')),
              mock.patch.object(a, 'process_batch') as process, mock.patch.object(s, 'promote') as promote):
            s.tick(a, self.conn, self.transport, self.row(self.parent))
        row = self.row()
        self.assertEqual(a.active_batch(self.conn)['batch_id'], self.child)
        self.assertEqual(row['role_promoted'], 1)
        self.assertFalse(s.is_speculative(row))
        self.assertEqual(service.state(self.conn, self.child)['role'], 'primary')
        for name in ['session_id', 'base_sha', 'phase', 'candidate_json', 'ready_json', 'attempt_generation', 'pending_prompt']:
            self.assertEqual(row[name], original[name], name)
        self.assertEqual(s.source_revision(a, row), before['source_revision'])
        self.assertEqual(service.state(self.conn, self.child)['tests'], before['tests'])
        next_row = s.child(self.conn, self.child)
        self.assertEqual(next_row['base_sha'], HEAD_TWO)
        self.assertTrue(s.is_speculative(next_row))
        self.assertEqual([c.args[-1] for c in process.call_args_list], [self.child, next_row['batch_id']])
        promote.assert_not_called()

    def test_unpromoted_successor_cannot_start_a_third_batch(self):
        self.register(self.child, HEAD_TWO)
        with mock.patch.object(s, 'selection') as select:
            s.launch_child(a, self.conn, self.row())
        select.assert_not_called()
        self.assertIsNone(s.child(self.conn, self.child))

    def test_primary_without_checkpoint_waits_to_refill_lookahead(self):
        self.land_parent()
        with (mock.patch.object(a, 'select_eligible_pull_requests', return_value=[]),
              mock.patch.object(a, 'compare_commit_ancestry', return_value=True),
              mock.patch.object(s, 'commit_tree', return_value=TREE),
              mock.patch.object(a, 'current_master_sha', return_value=BASE_SHA),
              mock.patch.object(s, 'selection') as select,
              mock.patch.object(a, 'process_batch')):
            s.tick(a, self.conn, self.transport, self.row(self.parent))
        self.assertEqual(a.active_batch(self.conn)['batch_id'], self.child)
        self.assertIsNone(s.child(self.conn, self.child))
        select.assert_not_called()

    def test_ready_priority_work_still_supersedes_ordinary_lookahead(self):
        self.land_parent()
        with (mock.patch.object(a, 'select_eligible_pull_requests', return_value=[pull(10, HEAD_ONE, priority=True)]),
              mock.patch.object(a, 'abort_batch_locked') as abort,
              mock.patch.object(s, 'promote_role') as promote):
            s.tick(a, self.conn, self.transport, self.row(self.parent))
        abort.assert_called_once()
        promote.assert_not_called()
        self.assertEqual(self.row()['role_promoted'], 0)

    def test_primary_progress_uses_normal_wording_before_ancestry_incorporation(self):
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET role_promoted=1 WHERE batch_id=?', (self.child,))
        with mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')) as send:
            a.send_start_notification(self.conn, self.transport, self.row())
        self.assertNotIn('SPECULATIVE', send.call_args.args[1])
        self.assertNotIn('Preparing ahead', send.call_args.args[1])

    def begin_incorporation(self):
        self.assess()
        successor = self.successor()
        self.land_parent()
        with (mock.patch.object(a, 'compare_commit_ancestry', return_value=True),
              mock.patch.object(s, 'commit_tree', return_value=TREE),
              mock.patch.object(a, 'current_master_sha', return_value=BASE_SHA),
              mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact'})):
            s.promote_role(a, self.conn, self.row(), self.row(self.parent))
            self.handoff()
            with mock.patch.object(a, 'send_start_notification'):
                a.process_batch(self.conn, self.transport, self.child)
        return successor

    def test_primary_incorporation_preserves_pinned_tree_and_requires_fresh_assessment(self):
        successor = self.begin_incorporation()
        self.assertIsNone(self.row()['predecessor_id'])
        self.assertTrue(s.parent_current(a, self.conn, self.row(successor)))
        current = service.state(self.conn, self.child)
        self.assertIsNone(current['tests'])
        self.assertIsNone(current['ready'])
        with self.assertRaisesRegex(ValueError, 'incorporated landed base'):
            service.dispatch(self.conn, self.child, 'publish', {'revision': current['revision'], 'head': HEAD_TWO})
        new_head = '5' * 40
        with (mock.patch.object(s, 'verify_candidate', return_value=TREE),
              mock.patch.object(a, 'compare_commit_ancestry', return_value=True)):
            updated = service.dispatch(self.conn, self.child, 'candidate',
                                       {'revision': current['revision'], 'head': new_head})
        self.assertEqual(updated['candidate']['id'], current['candidate']['id'])
        self.assertIn(HEAD_TWO, updated['candidate']['equivalent_heads'])
        self.assertTrue(s.parent_current(a, self.conn, self.row(successor)))
        with self.assertRaisesRegex(ValueError, 'passing assessment'):
            service.dispatch(self.conn, self.child, 'publish', {'revision': updated['revision'], 'head': new_head})
        updated = service.dispatch(self.conn, self.child, 'tests',
                                  {'revision': updated['revision'], 'head': new_head, 'verdict': 'pass',
                                   'tests': 'reused focused check at ' + HEAD_TWO + '; same tree and settings', 'baseline': 'none'})
        self.assertEqual(updated['tests']['head'], new_head)
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET terminal_status='merged',status='completed',phase='terminal',"
                              "ci_head_sha=? WHERE batch_id=?", (new_head, self.child))
        self.assertTrue(s.parent_current(a, self.conn, self.row(successor)))
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET ci_head_sha=? WHERE batch_id=?', (HEAD_TWO, self.child))
        self.assertFalse(s.parent_current(a, self.conn, self.row(successor)))

    def test_changed_tree_or_non_descendant_checkpoint_invalidates_successor(self):
        successor = self.begin_incorporation()
        original = self.row()['candidate_json']
        for tree, ancestry in [(HEAD_ONE, True), (TREE, False)]:
            with self.subTest(tree=tree, ancestry=ancestry):
                with self.conn:
                    promotion = json.loads(self.row()['promotion_json'])
                    promotion['stage'] = 'incorporating'
                    self.conn.execute('UPDATE automerge_batches SET candidate_json=?,promotion_json=? WHERE batch_id=?',
                                      (original, json.dumps(promotion), self.child))
                current = service.state(self.conn, self.child)
                with (mock.patch.object(s, 'verify_candidate', return_value=tree),
                      mock.patch.object(a, 'compare_commit_ancestry', return_value=ancestry)):
                    service.dispatch(self.conn, self.child, 'candidate',
                                     {'revision': current['revision'], 'head': '5' * 40})
                self.assertFalse(s.parent_current(a, self.conn, self.row(successor)))

    def test_incorporation_alias_cannot_survive_membership_change_withdrawal_or_reset(self):
        successor = self.begin_incorporation()
        current = service.state(self.conn, self.child)
        service.dispatch(self.conn, self.child, 'candidate', {'revision': current['revision'], 'withdraw': True})
        self.assertFalse(s.parent_current(a, self.conn, self.row(successor)))
        with self.conn:
            old = json.loads(self.row()['promotion_json'])['old_candidate']
            self.conn.execute('UPDATE automerge_batches SET candidate_json=? WHERE batch_id=?', (json.dumps(old), self.child))
        current = service.state(self.conn, self.child)
        service.dispatch(self.conn, self.child, 'exclude', {'revision': current['revision'], 'number': 8,
                         'head': HEAD_TWO, 'kind': 'rejected', 'reason': 'standalone defect', 'evidence': 'inspection'})
        self.assertFalse(s.parent_current(a, self.conn, self.row(successor)))
        self.assertIsNone(s.candidate(a, self.row()))
        s.invalidate(a, self.conn, self.row())
        self.assertEqual(json.loads(self.row()['promotion_json']), {})
        self.assertEqual(self.row()['role_promoted'], 1)

    def test_master_drift_during_running_validation_recovers_in_same_primary_session(self):
        before = self.assess()
        self.land_parent()
        with (mock.patch.object(a, 'select_eligible_pull_requests', return_value=[]),
              mock.patch.object(a, 'compare_commit_ancestry', return_value=True),
              mock.patch.object(s, 'commit_tree', side_effect=lambda _, head: TREE if head == BASE_SHA else HEAD_ONE),
              mock.patch.object(a, 'current_master_sha', return_value=HEAD_TWO),
              mock.patch.object(a, 'process_batch'), mock.patch.object(s, 'selection') as select):
            s.tick(a, self.conn, self.transport, self.row(self.parent))
        row = self.row()
        self.assertEqual(a.active_batch(self.conn)['batch_id'], self.child)
        self.assertEqual(row['phase'], 'resetting')
        self.assertEqual(row['session_id'], 'child-session')
        self.assertEqual(row['attempt_generation'], 1)
        recovery = json.loads(row['recovery_json'])
        self.assertEqual(recovery['old_tests'], before['tests'])
        self.assertEqual(recovery['replacement']['head'], HEAD_TWO)
        self.assertIsNone(s.candidate(a, row))
        select.assert_not_called()

    def test_primary_abort_stops_its_own_lookahead_after_role_promotion(self):
        self.register(self.child, HEAD_TWO)
        successor = self.successor()
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET role_promoted=1 WHERE batch_id=?', (self.child,))
        with (mock.patch.object(a, '_session_status', return_value={'state': 'suspended'}),
              mock.patch.object(a, 'request_suspend', return_value=True),
              mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')),
              mock.patch.object(monitor, 'runtime_binary_issues', return_value=[]),
              mock.patch.object(a, 'finish_batch'), mock.patch.object(a, 'lookup_batch_session', return_value=None)):
            a.abort_batch_locked(self.conn, self.transport, self.row(), 'operator abort')
        self.assertEqual(self.row(successor)['terminal_status'], 'aborted')
        self.assertEqual(self.row()['terminal_status'], 'aborted')
        self.assertIsNone(self.row(self.parent)['terminal_status'])

    def test_detached_recovery_dispatches_through_normal_batch_processor(self):
        s.invalidate(a, self.conn, self.row())
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET predecessor_id=NULL WHERE batch_id=?', (self.child,))
        with (mock.patch.object(a, 'send_start_notification'), mock.patch.object(s, 'recover') as recover):
            a.process_batch(self.conn, self.transport, self.child)
        recover.assert_called_once()

    def test_primary_recovery_before_initial_launch_detaches_landed_predecessor(self):
        s.invalidate(a, self.conn, self.row())
        recovery = json.loads(self.row()['recovery_json'])
        recovery['replacement'] = {'id': 'master-' + BASE_SHA, 'head': BASE_SHA, 'tree': TREE}
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET session_id=NULL,role_promoted=1,recovery_json=? WHERE batch_id=?',
                              (json.dumps(recovery), self.child))
        s.recover_step(a, self.conn, self.row())
        self.assertIsNone(self.row()['predecessor_id'])
        self.assertEqual(self.row()['base_sha'], BASE_SHA)
        self.assertEqual(self.row()['phase'], 'building')

    def test_old_reports_are_bounded_by_the_new_attempt_context_divider(self):
        reply = subprocess.CompletedProcess([], 0, json.dumps({'items': []}), '')
        with mock.patch.object(monitor, 'mj_command', return_value=reply) as read:
            # Exercise the public transcript reader, never session data.
            self.assertEqual(a.read_final_agent_message('child-session', after_seq=101), '')
        self.assertIn('101', read.call_args.args[0])

    def test_generated_brief_bounds_large_evidence_without_losing_exact_heads(self):
        s.invalidate(a, self.conn, self.row())
        row = self.row()
        recovery = json.loads(row['recovery_json'])
        recovery['old_tests'] = {'head': HEAD_TWO, 'verdict': 'pass',
                                 'tests': '界' * 30000, 'baseline': '界' * 30000,
                                 'executions': [{'id': 'f' * 32, 'command': '界' * 30000}]}
        with mock.patch.object(a, 'skills_connection_prompt', return_value=''):
            brief = s.reset_brief(a, self.conn, row, recovery)
        self.assertLess(len(json.dumps({'text': brief}).encode()), 96 * 1024)
        self.assertIn(HEAD_TWO, brief)
        self.assertIn('"omitted_characters": 28000', brief)
        self.assertEqual(len(recovery['old_tests']['tests']), 30000)
        self.assertIn('f' * 32, brief)
        self.assertIn('mm-db executions', brief)
        self.assertEqual(len(recovery['old_tests']['executions'][0]['command']), 30000)


class RecoveryGitTests(GitFixture):
    def call(self, method):
        with mock.patch.object(merge, 'git', side_effect=lambda *args, **kw: db.git(*args, cwd=self.repo, **kw)):
            return method(self.state)

    def test_reset_saves_dirty_source_work_and_keeps_cache(self):
        original = self.source(1, 'own', 'own\n')
        self.assemble()
        old = self.run_git('rev-parse', 'HEAD')
        (self.repo / 'marker').write_text('pending edit\n')
        (self.repo / 'new-source').write_text('pending new source\n')
        git_dir = self.repo / '.git'
        (git_dir / 'rr-cache').mkdir(exist_ok=True)
        (git_dir / 'rr-cache/sentinel').write_text('cache')
        (git_dir / 'mergemarshall-progress.md').write_text('old next action')
        self.state['attempt_generation'] = 1
        result = self.call(merge.restart)
        archive = Path(result['archive'])
        self.assertEqual((archive / 'files/marker').read_text(), 'pending edit\n')
        self.assertEqual((archive / 'files/new-source').read_text(), 'pending new source\n')
        self.assertEqual(self.run_git('rev-parse', 'refs/mm-recovery/batch-test/attempt-0'), old)
        self.assertEqual(self.run_git('rev-parse', 'HEAD'), self.base)
        self.assertEqual((git_dir / 'rr-cache/sentinel').read_text(), 'cache')
        self.assertFalse((git_dir / 'mergemarshall-progress.md').exists())
        self.assemble()
        rebuilt = self.run_git('rev-parse', 'HEAD')
        self.assertTrue(self.call(merge.restart)['already_reset'])
        self.assertEqual(self.run_git('rev-parse', 'HEAD'), rebuilt)
        self.assertIn(original, self.run_git('rev-list', 'HEAD'))

    def test_promotion_retains_tree_and_both_histories(self):
        parent = self.source(1, 'parent', 'parent\n')
        self.state['sources'] = [self.state['sources'][0]]
        self.assemble()
        candidate = self.run_git('rev-parse', 'HEAD')
        parent_tree = self.run_git('rev-parse', 'HEAD^{tree}')
        landed = self.run_git('commit-tree', parent_tree, '-p', self.base, '-p', candidate, '-m', 'GitHub merge')
        (self.repo / 'child').write_text('child\n')
        self.run_git('add', 'child')
        self.run_git('commit', '-m', 'child')
        before = self.run_git('rev-parse', 'HEAD')
        tree = self.run_git('rev-parse', 'HEAD^{tree}')
        self.state.update(base_sha=landed, predecessor=None,
                          promotion={'previous_tests': {'head': before, 'verdict': 'pass', 'tests': 'check'}})
        result = self.call(merge.promote)
        self.assertTrue(result['tree_unchanged'])
        self.assertTrue(result['evidence_reusable'])
        self.assertEqual(self.run_git('rev-parse', 'HEAD^{tree}'), tree)
        history = self.run_git('rev-list', 'HEAD')
        self.assertIn(candidate, history)
        self.assertIn(landed, history)
        self.assertIn(parent, history)

        # C pinned B before incorporation. Exercise the actual state service
        # using these real commit trees, not a permissive ancestry mock.
        conn = make_db(pulls=[pull(8, before)], ci_mode='async', integration_pr_number=None)
        self.addCleanup(conn.close)
        service.ensure_schema(conn)
        with conn:
            conn.execute('UPDATE automerge_batches SET base_sha=? WHERE batch_id=?', (candidate, 'batch-test'))
        row = row_for(conn)
        checkpoint = {'id': 'pinned-b', 'head': before, 'tree': tree, 'branch': row['branch'],
                      'source_revision': s.source_revision(a, row), 'attempt_generation': 0}
        with conn:
            conn.execute('UPDATE automerge_batches SET candidate_json=? WHERE batch_id=?',
                         (json.dumps(checkpoint), 'batch-test'))
        successor = a.create_batch(conn, [pull(9, HEAD_ONE)], before, batch_id='successor-test',
                                   predecessor_id='batch-test', predecessor_candidate=checkpoint)
        promotion = {'stage': 'incorporating', 'base': landed, 'old_base': candidate, 'old_candidate': checkpoint}
        with conn:
            conn.execute('UPDATE automerge_batches SET base_sha=?,promotion_json=?,phase=\'fixing\' WHERE batch_id=?',
                         (landed, json.dumps(promotion), 'batch-test'))
        child = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (successor,)).fetchone()
        self.assertTrue(s.parent_current(a, conn, child))
        current = service.state(conn, 'batch-test')

        def ancestor(base, head):
            return subprocess.run(['git', '-C', str(self.repo), 'merge-base', '--is-ancestor', base, head],
                                  capture_output=True).returncode == 0

        def api(args):
            if '/git/ref/heads/' in args[1]:
                return {'object': {'sha': result['head']}}
            if '/git/commits/' in args[1]:
                return {'tree': {'sha': self.run_git('rev-parse', args[1].rsplit('/', 1)[1] + '^{tree}')}}
            self.fail('unexpected GitHub request: ' + str(args))

        with (mock.patch.object(a, 'compare_commit_ancestry', side_effect=ancestor),
              mock.patch.object(a, 'gh_json', side_effect=api)):
            updated = service.dispatch(conn, 'batch-test', 'candidate',
                                       {'revision': current['revision'], 'head': result['head']})
            self.assertEqual(updated['candidate']['id'], checkpoint['id'])
            self.assertIn(before, updated['candidate']['equivalent_heads'])
            self.assertTrue(s.parent_current(a, conn, child))
            landed_b = self.run_git('commit-tree', tree, '-p', landed, '-p', result['head'], '-m', 'Land B')
            with conn:
                conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='merged',"
                             "ci_head_sha=?,integration_merge_commit_sha=? WHERE batch_id=?",
                             (result['head'], landed_b, 'batch-test'))
            with mock.patch.object(a, 'current_master_sha', return_value=landed_b):
                self.assertEqual(s.landed_base(a, conn, child, row_for(conn)), landed_b)
            self.assertEqual(json.loads(child['predecessor_candidate_json'])['head'], before)

    def test_rerere_is_enabled_without_automatic_staging(self):
        self.source(1, 'one', 'one\n')
        self.assemble()
        self.assertEqual(self.run_git('config', 'rerere.enabled'), 'true')
        self.assertEqual(self.run_git('config', 'rerere.autoupdate'), 'false')

    def test_rebuild_reuses_resolution_without_old_predecessor_or_automatic_staging(self):
        old_parent = self.source(1, 'marker', 'parent\n')
        own = self.source(2, 'marker', 'own\n')
        # Distinct parent commit with the same conflict input, as after N rebuilds.
        parent_tree = self.run_git('rev-parse', old_parent + '^{tree}')
        new_parent = self.run_git('commit-tree', parent_tree, '-p', self.base, '-m', 'replacement parent')
        self.state.update(base_sha=old_parent, sources=[self.state['sources'][1]])
        self.run_git('reset', '--hard', old_parent)
        self.assertTrue(self.assemble(manual=True)['manual_required'])
        (self.repo / 'marker').write_text('resolved both intents\n')
        self.run_git('add', 'marker')
        self.run_git('commit', '--no-edit')
        self.state.update(base_sha=new_parent, attempt_generation=1)
        self.call(merge.restart)
        self.assertTrue(self.assemble(manual=True)['manual_required'])
        self.assertEqual((self.repo / 'marker').read_text(), 'resolved both intents\n')
        self.assertIn('UU marker', self.run_git('status', '--porcelain'))
        self.run_git('add', 'marker')
        self.run_git('commit', '--no-edit')
        history = self.run_git('rev-list', 'HEAD')
        self.assertIn(new_parent, history)
        self.assertIn(own, history)
        self.assertNotIn(old_parent, history)


if __name__ == '__main__':
    unittest.main()
