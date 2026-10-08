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

    def test_legacy_source_revision_survives_migration(self):
        current = service.state(self.conn, self.parent)
        self.assertEqual(current['source_revision'], service.digest([
            current['base_sha'], current['sources'], current['excluded']]))
        s.ensure_schema(self.conn, a.ensure_column)
        self.assertEqual(current['source_revision'], service.state(self.conn, self.parent)['source_revision'])

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
            self.conn.execute('UPDATE automerge_batches SET predecessor_id=NULL WHERE batch_id=?', (self.child,))
        self.assertEqual(a.active_batch(self.conn)['batch_id'], self.child)

    def test_foreground_rebuild_does_not_steal_reserved_sources(self):
        with (mock.patch.object(a, '_recheck_sources', side_effect=lambda pulls, **kw: (pulls, [])),
              mock.patch.object(a, 'select_eligible_pull_requests', return_value=[pull(8, HEAD_TWO)]),
              mock.patch.object(a, '_validation_impact', return_value={'mode': 'impact'})):
            a._request_rebuild(self.conn, self.row(self.parent), [pull()], 'retry')
        self.assertEqual([p.number for p in a.row_pulls(self.row(self.parent))], [7])

    def test_local_pass_parks_instead_of_publishing(self):
        self.assess()
        with (mock.patch.object(a, '_record_agent_exclusions'),
              mock.patch.object(a, '_recheck_sources', side_effect=lambda pulls, **kw: (pulls, []))):
            s.finished(a, self.conn, self.transport, self.row(), 'automerge-local: pass')
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

    def test_promotion_waits_for_build_then_requires_tree_proof(self):
        self.assess()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='waiting_parent' WHERE batch_id=?", (self.child,))
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='merged',"
                              "ci_head_sha=?,integration_merge_commit_sha=? WHERE batch_id=?",
                              (HEAD_THREE, BASE_SHA, self.parent))
        with (mock.patch.object(a, '_session_is_idle', return_value=False),
              mock.patch.object(s, 'commit_tree') as tree):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
            tree.assert_not_called()
        with (mock.patch.object(a, '_session_is_idle', return_value=True),
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
        self.assess()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='waiting_parent' WHERE batch_id=?", (self.child,))
            self.conn.execute("UPDATE automerge_batches SET ci_head_sha=?,integration_merge_commit_sha=? WHERE batch_id=?",
                              (HEAD_THREE, BASE_SHA, self.parent))
        with (mock.patch.object(a, '_session_is_idle', return_value=True),
              mock.patch.object(a, 'compare_commit_ancestry', return_value=True),
              mock.patch.object(a, 'current_master_sha', return_value=HEAD_ONE),
              mock.patch.object(s, 'commit_tree', side_effect=[TREE, HEAD_TWO, HEAD_TWO])):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
        self.assertEqual(self.row()['phase'], 'resetting')
        self.assertEqual(json.loads(self.row()['recovery_json'])['replacement']['head'], HEAD_ONE)

    def test_landed_parent_does_not_promote_an_idle_unfinished_successor(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='merged',"
                              "ci_head_sha=? WHERE batch_id=?", (HEAD_THREE, self.parent))
        with (mock.patch.object(s, 'launch_child'), mock.patch.object(a, 'select_eligible_pull_requests', return_value=[]),
              mock.patch.object(a, '_session_is_idle', return_value=True),
              mock.patch.object(a, 'process_batch') as process, mock.patch.object(s, 'promote') as promote):
            s.tick(a, self.conn, self.transport, self.row(self.parent))
        process.assert_called_once_with(self.conn, self.transport, self.child)
        promote.assert_not_called()

    def test_detached_recovery_dispatches_through_normal_batch_processor(self):
        s.invalidate(a, self.conn, self.row())
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET predecessor_id=NULL WHERE batch_id=?', (self.child,))
        with (mock.patch.object(a, 'send_start_notification'), mock.patch.object(s, 'recover') as recover):
            a.process_batch(self.conn, self.transport, self.child)
        recover.assert_called_once()

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
                                 'tests': '界' * 30000, 'baseline': '界' * 30000}
        with mock.patch.object(a, 'skills_connection_prompt', return_value=''):
            brief = s.reset_brief(a, self.conn, row, recovery)
        self.assertLess(len(json.dumps({'text': brief}).encode()), 96 * 1024)
        self.assertIn(HEAD_TWO, brief)
        self.assertIn('"omitted_characters": 28000', brief)
        self.assertEqual(len(recovery['old_tests']['tests']), 30000)


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
