"""Isolated state-machine and Git regressions for merge lookahead."""
import json
from contextlib import ExitStack
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from dataclasses import replace

import automerge as a
import mm_service as service
import monitor
import speculation as s
import supervisor
import pr_dependencies
from test_automerge import make_db, pull, row_for, BASE_SHA, HEAD_ONE, HEAD_TWO, HEAD_THREE
from test_mm_skills import GitFixture, merge, db


TREE = '4' * 40


class StopWorkTests(unittest.TestCase):
    def test_parked_children_are_suspended_without_waking_their_workers(self):
        parent = {'state': 'running', 'chat_phase': 'idle', 'is_idle': False}
        child = {'id': 'child', 'state': 'parked'}

        def command(_a, args):
            if args[2] == 'child' and args[0] != 'suspend':
                self.fail('parked child has no worker for ' + args[0])
            return {}

        with (mock.patch.object(s, 'mj', side_effect=command) as mj,
              mock.patch.object(s, 'mj_api', return_value={'subagents': [{'session': child}]}),
              mock.patch.object(monitor, 'interrupt_turn') as interrupt,
              mock.patch.object(a, '_session_status', side_effect=lambda identifier:
                  {'state': 'stopped'} if identifier == 'child' else parent)):
            self.assertTrue(s.stop_work(a, {'session_id': 'parent'}))
        interrupt.assert_called_once_with('parent')
        self.assertIn(mock.call(a, ['suspend', '--session', 'child',
                                  '--acknowledge-unpublished-work', '--json']), mj.call_args_list)

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
    def test_immediate_batch_has_primary_role_and_never_has_a_successor(self):
        identifier = a.create_batch(self.conn, [pull(99, HEAD_ONE, immediate=True)], BASE_SHA, lane='immediate')
        self.register(identifier, HEAD_THREE)
        row = self.row(identifier)
        self.assertEqual(service.state(self.conn, identifier)['role'], 'primary')
        self.assertIsNone(row['predecessor_id'])
        with mock.patch.object(a, 'dependency_graph', side_effect=AssertionError('no lookahead discovery')):
            self.assertEqual(s.selection(a, self.conn, row), [])
            s.launch_child(a, self.conn, row)
        with self.assertRaisesRegex(ValueError, 'predecessors'):
            a.create_batch(self.conn, [pull(100, HEAD_ONE)], BASE_SHA, predecessor_id=identifier)
        with self.assertRaisesRegex(ValueError, 'predecessors'):
            a.create_batch(self.conn, [pull(100, HEAD_ONE)], BASE_SHA, lane='immediate', predecessor_id=self.parent)
        self.assertNotIn('lookahead', a.batch_speculation_guidance(row))
        with mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')) as send:
            a.send_start_notification(self.conn, self.transport, row)
        self.assertTrue(send.call_args_list[0].args[1].startswith('*bifrost-dev* immediate batch:'))

    def test_campaign_parent_never_selects_or_launches_a_successor(self):
        row = dict(self.row(self.parent), target_ref='stabilize/test')
        with (mock.patch.object(a, 'dependency_graph', side_effect=AssertionError('discovery')),
              mock.patch.object(a, 'create_batch', side_effect=AssertionError('launch'))):
            self.assertEqual(s.selection(a, self.conn, row), [])
            s.launch_child(a, self.conn, row)

    def test_master_successor_excludes_campaign_target(self):
        with self.conn:
            pr_dependencies.ensure_schema(self.conn)
        master = pr_dependencies.Node(90, HEAD_ONE, 'master', 'ordinary', a.REPO_NAME, a.REPO_NAME,
                                      {'state': 'open'}, True)
        campaign = pr_dependencies.Node(91, HEAD_TWO, 'stabilize/test', 'fix', a.REPO_NAME, a.REPO_NAME,
                                        {'state': 'open'}, True)
        actual = pr_dependencies.Graph({90: master, 91: campaign}, {90: [HEAD_ONE], 91: [HEAD_TWO]},
                                      BASE_SHA, lambda x, y: x == y,
                                      targets={90: ('master', BASE_SHA), 91: ('stabilize/test', BASE_SHA)}).discover()
        with (mock.patch.object(a, 'dependency_graph', return_value=actual),
              mock.patch.object(a, 'compare_commit_ancestry', side_effect=lambda x, y: x == y),
              mock.patch.object(s, 'reserved', return_value=set())):
            selected = s.selection(a, self.conn, self.row(self.parent))
        self.assertEqual([p.number for p in selected], [90])
        self.assertTrue(all(p.target_ref == 'master' for p in selected))

    def test_launch_uses_fresh_master_and_waits_for_reserved_prerequisite(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal' WHERE batch_id=?", (self.child,))
            pr_dependencies.ensure_schema(self.conn)
        parent_source = a.row_pulls(self.row(self.parent))[0]
        nodes = {
            parent_source.number: pr_dependencies.Node(parent_source.number, parent_source.head_sha, 'master', 'parent',
                a.REPO_NAME, a.REPO_NAME, {'state': 'open'}, True),
            90: pr_dependencies.Node(90, HEAD_TWO, 'master', 'independent',
                a.REPO_NAME, a.REPO_NAME, {'state': 'open'}, True),
            91: pr_dependencies.Node(91, '9' * 40, 'master', 'stacked',
                a.REPO_NAME, a.REPO_NAME, {'state': 'open'}, True),
        }
        fresh_master = 'a' * 40
        def ancestor(base, head):
            return base == head or (base == parent_source.head_sha and head == '9' * 40)
        graph = pr_dependencies.Graph(nodes, {n: [node.head] for n, node in nodes.items()},
                                      fresh_master, ancestor).discover()
        with (mock.patch.object(a, 'current_master_sha', return_value=fresh_master),
              mock.patch.object(a, 'dependency_graph', return_value=graph) as discover,
              mock.patch.object(s, 'require_recovery_controls'),
              mock.patch.object(a, 'create_batch') as create):
            s.launch_child(a, self.conn, self.row(self.parent))
        self.assertEqual([p.number for p in create.call_args.args[1]], [90])
        self.assertEqual(create.call_args.args[2], fresh_master)
        self.assertEqual(create.call_args.kwargs['predecessor_id'], self.parent)
        self.assertEqual(create.call_args.kwargs['predecessor_candidate'], {})
        self.assertEqual(discover.call_args.kwargs['master'], fresh_master)

    def setUp(self):
        self.master_patch = mock.patch.object(a, 'current_master_sha', return_value=BASE_SHA)
        self.master_patch.start()
        self.addCleanup(self.master_patch.stop)
        self.ancestry_patch = mock.patch.object(a, 'compare_commit_ancestry', return_value=True)
        self.ancestry_patch.start()
        self.addCleanup(self.ancestry_patch.stop)
        # Landing tests reach the async quick policy gate; test_automerge covers the lookup.
        self.quick_policy_patch = mock.patch.object(a, 'check_quick_policy', return_value=('success', ''))
        self.quick_policy_patch.start()
        self.addCleanup(self.quick_policy_patch.stop)
        self.conn = make_db(ci_mode='async', integration_pr_number=None)
        self.addCleanup(self.conn.close)
        service.ensure_schema(self.conn)
        self.transport = monitor.SlackTransport('webhook', webhook='unused')
        self.parent = 'batch-test'
        self.register(self.parent, HEAD_THREE)
        self.child = a.create_batch(self.conn, [pull(8, HEAD_TWO)], BASE_SHA,
                                   batch_id='child-test', ci_mode='async',
                                   predecessor_id=self.parent,
                                   predecessor_candidate={})
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='running',session_id='child-session' "
                              "WHERE batch_id=?", (self.child,))

    def row(self, identifier=None):
        return self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                                 (identifier or self.child,)).fetchone()

    def test_multi_pr_successor_starts_opus_high_with_straightforward_flash(self):
        identifier = a.create_batch(self.conn, [pull(10), pull(11, HEAD_TWO)], BASE_SHA,
                                    batch_id='multi-successor', ci_mode='async',
                                    predecessor_id=self.parent,
                                    predecessor_candidate={})
        row = self.row(identifier)
        self.assertEqual(row['agent_configuration'], 'opus-high-dsf-high')
        self.assertTrue(a._batch_subagents(row))
        argv = a.new_session_argv(row, '/tmp/prompt')
        for flag, value in (('--model', 'opus'), ('--target', 'bedrock-podman'),
                            ('--effort', 'high'), ('--subagents', 'mjolnir'),
                            ('--subagent-straightforward', 'deepseek-flash'),
                            ('--subagent-straightforward-effort', 'high')):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertFalse(any(arg.startswith('--subagent-subtle') for arg in argv))

    def test_single_pr_successor_recovery_retains_opus_straightforward_policy(self):
        s.invalidate(a, self.conn, self.row())
        row = self.row()
        self.assertEqual(row['agent_configuration'], 'opus-high-dsf-high')
        argv = a.new_session_argv(row, '/tmp/prompt')
        self.assertEqual(argv[argv.index('--model') + 1], 'opus')
        self.assertEqual(argv[argv.index('--subagents') + 1], 'mjolnir')
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        with mock.patch.object(a, 'skills_connection_prompt', return_value=''):
            prompt = s.reset_brief(a, self.conn, row, json.loads(row['recovery_json']))
        self.assertIn('There are no subtle subagents', prompt)
        self.assertNotIn('Luna', prompt)
        self.assertNotIn('delegate independent', prompt)

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
        return a.create_batch(self.conn, [pull(9, HEAD_ONE)], BASE_SHA, batch_id='successor-test',
                              ci_mode='async', predecessor_id=self.child,
                              predecessor_candidate={})

    def handoff(self):
        current = service.state(self.conn, self.child)
        with mock.patch.object(a, 'remote_branch_sha', return_value=HEAD_TWO):
            service.dispatch(self.conn, self.child, 'ready', {'revision': current['revision'], 'head': HEAD_TWO})
        a._consume_ready_candidate(self.conn, self.transport, self.row())

    def lifecycle_seams(self, stack, master=BASE_SHA, head=HEAD_TWO):
        """Keep scheduler, receipts and landing gates real; fake external I/O."""
        for name in ('send_start_notification', 'finish_batch', 'drain_transcript'):
            stack.enter_context(mock.patch.object(a, name))
        stack.enter_context(mock.patch.object(a, '_recheck_sources', side_effect=lambda pulls, **kw: (pulls, [])))
        stack.enter_context(mock.patch.object(a, '_record_trusted_rejection_markers', return_value=False))
        stack.enter_context(mock.patch.object(a, 'verify_source_ancestry', return_value=(True, 'verified')))
        stack.enter_context(mock.patch.object(a, 'target_sha', return_value=master))
        stack.enter_context(mock.patch.object(a, 'current_master_sha', return_value=master))
        stack.enter_context(mock.patch.object(a, 'check_pr_verification', return_value='success'))
        stack.enter_context(mock.patch.object(a, 'remote_branch_sha', return_value=head))
        stack.enter_context(mock.patch.object(a, '_wait_agent_turn', return_value=False))
        stack.enter_context(mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact'}))
        stack.enter_context(mock.patch.object(service, 'reconcile_publication', return_value={
            'number': 211, 'url': 'https://example.invalid/211', 'head': head}))
        merged = {'value': False}
        def view(number):
            return {'state': 'MERGED' if merged['value'] else 'OPEN', 'isDraft': False,
                    'baseRefName': 'master', 'baseRefOid': master, 'headRefOid': head,
                    'mergedAt': 'now' if merged['value'] else None, 'mergeCommit': {'oid': 'e' * 40}}
        def write(args, **kwargs):
            self.assertEqual(args[:2], ['pr', 'merge'])
            self.assertEqual(args[-2:], ['--match-head-commit', head])
            merged['value'] = True
            return ''
        stack.enter_context(mock.patch.object(a, 'integration_pr_view', side_effect=view))
        merge_write = stack.enter_context(mock.patch.object(a, 'run_gh', side_effect=write))
        verdict = stack.enter_context(mock.patch.object(a, '_try_post_verdict_status', return_value=True))
        return merge_write, verdict

    def test_aborted_same_base_publishes_existing_handoff_in_both_ci_modes(self):
        self.assess()
        self.handoff()
        original = dict(self.row())
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='aborted' "
                              "WHERE batch_id=?", (self.parent,))
        with ExitStack() as stack:
            write, verdict = self.lifecycle_seams(stack)
            s.reconcile(a, self.conn, self.transport, self.row())
            a.process_batch(self.conn, self.transport, self.child)
        self.assertEqual(self.row()['terminal_status'], 'merged')
        self.assertEqual(service.state(self.conn, self.child)['ready']['kind'], 'publish')
        self.assertEqual(self.row()['attempt_generation'], original['attempt_generation'])
        self.assertIsNone(self.row()['predecessor_id'])
        self.assertEqual(self.row()['candidate_json'], original['candidate_json'])
        write.assert_called_once()
        verdict.assert_called_once()
        # Reuse a separate batch to exercise synchronous CI after detachment.
        identifier = self.successor()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET ci_mode='sync',status='running',session_id='sync' "
                              "WHERE batch_id=?", (identifier,))
        self.child = identifier
        self.assess()
        self.handoff()
        with ExitStack() as stack:
            write, _ = self.lifecycle_seams(stack)
            a.process_batch(self.conn, self.transport, self.child)
            if self.row()['phase'] != 'terminal':
                a.process_batch(self.conn, self.transport, self.child)
        self.assertEqual(self.row()['terminal_status'], 'merged')
        write.assert_called_once()

    def test_terminal_predecessors_incorporate_newest_master_then_publish_and_land(self):
        self.assess()
        self.handoff()
        for outcome in ('merged', 'aborted', 'failed', 'missing'):
            with self.subTest(outcome=outcome):
                # Restore the same passing successor to the role-release boundary.
                if outcome == 'merged':
                    original = dict(self.row())
                with self.conn:
                    self.conn.execute("UPDATE automerge_batches SET " + ','.join(k + '=?' for k in original) +
                                      " WHERE batch_id=?", list(original.values()) + [self.child])
                    if outcome == 'missing':
                        self.conn.execute('DELETE FROM automerge_batches WHERE batch_id=?', (self.parent,))
                    else:
                        self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status=? "
                                          "WHERE batch_id=?", (outcome, self.parent))
                newest = 'b' * 40
                with ExitStack() as stack:
                    write, _ = self.lifecycle_seams(stack, master=newest, head=HEAD_ONE)
                    supervisor.ensure_schema(self.conn)
                    a.merge_failover.ensure_schema(self.conn)
                    stack.enter_context(mock.patch.object(a.merge_failover, 'probe'))
                    stack.enter_context(mock.patch.object(a.merge_failover, 'notify'))
                    for name in ('retry_pending_notifications', 'retry_pending_aborted_outcomes',
                                 'enqueue_active_membership_labels', 'retry_github_outbox',
                                 'check_pending_suspensions', 'report_dependency_blocks'):
                        stack.enter_context(mock.patch.object(a, name))
                    stack.enter_context(mock.patch.object(a, 'list_open_pull_requests', return_value=[]))
                    stack.enter_context(mock.patch.object(a, 'select_eligible_pull_requests', return_value=[]))
                    stack.enter_context(mock.patch.object(monitor, 'update_known_failures'))
                    stack.enter_context(mock.patch.object(supervisor, 'idle_summary'))
                    delivered = []
                    def deliver(conn, transport, row):
                        if row['prompt_delivered']:
                            return
                        delivered.append(row['pending_prompt'])
                        with conn:
                            conn.execute('UPDATE automerge_batches SET prompt_delivered=1 WHERE batch_id=?', (self.child,))
                    stack.enter_context(mock.patch.object(a, 'deliver_pending_prompt', side_effect=deliver))
                    self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
                    self.assertEqual(len(delivered), 1)
                    self.assertIn('mm-merge --promote', delivered[0])
                    self.assertIn(newest, delivered[0])
                    self.assertEqual(self.row()['base_sha'], newest)
                    self.assertEqual(self.row()['attempt_generation'], original['attempt_generation'])
                    self.assertIsNone(a.ready_candidate(self.row()))
                    # Agent completes incorporation, re-registers and assesses resulting HEAD.
                    current = service.state(self.conn, self.child)
                    with mock.patch.object(s, 'verify_candidate', return_value=TREE):
                        current = service.dispatch(self.conn, self.child, 'candidate',
                            {'revision': current['revision'], 'head': HEAD_ONE})
                    current = service.dispatch(self.conn, self.child, 'tests', {
                        'revision': current['revision'], 'head': HEAD_ONE, 'verdict': 'pass',
                        'tests': 'diff-selected checks passed', 'baseline': 'none'})
                    service.dispatch(self.conn, self.child, 'publish',
                        {'revision': current['revision'], 'head': HEAD_ONE})
                    self.assertIsNotNone(a.ready_candidate(self.row()))
                    a.process_batch(self.conn, self.transport, self.child)
                    self.assertEqual(self.row()['terminal_status'], 'merged')
                    write.assert_called_once()

    def test_sibling_changes_preserve_running_attempt_until_it_lands(self):
        self.assess()
        original = dict(self.row())
        # Primary rebuild/withdrawal/ejection never contributes to successor inputs.
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET candidate_json='{}',phase='fixing',"
                              "pending_prompt='rebuild',active_pull_requests_json='[]' WHERE batch_id=?", (self.parent,))
        s.reconcile(a, self.conn, self.transport, self.row())
        for key in ('attempt_generation', 'pending_prompt', 'prompt_command_id', 'base_sha'):
            self.assertEqual(self.row()[key], original[key])
        self.land_parent()
        self.handoff()
        with ExitStack() as stack:
            write, _ = self.lifecycle_seams(stack)
            a.process_batch(self.conn, self.transport, self.child)
        self.assertEqual(self.row()['terminal_status'], 'merged')
        write.assert_called_once()

    def test_publish_handoff_remains_valid_with_ended_predecessor_metadata(self):
        self.assess()
        self.land_parent()
        current = service.state(self.conn, self.child)
        publication = {'number': 211, 'url': 'https://example.invalid/211', 'head': HEAD_TWO}
        with mock.patch.object(service, 'reconcile_publication', return_value=publication):
            result = service.dispatch(self.conn, self.child, 'publish',
                                      {'revision': current['revision'], 'head': HEAD_TWO})
            self.assertEqual(result['ready']['kind'], 'publish')
            retried = service.dispatch(self.conn, self.child, 'publish',
                                       {'revision': current['revision'], 'head': HEAD_TWO})
        self.assertEqual(result['ready']['id'], retried['ready']['id'])
        with ExitStack() as stack:
            write, _ = self.lifecycle_seams(stack)
            a.process_batch(self.conn, self.transport, self.child)
        self.assertEqual(self.row()['terminal_status'], 'merged')
        write.assert_called_once()

    def test_same_base_detach_retries_publication_without_losing_receipt(self):
        self.assess()
        self.handoff()
        self.land_parent()
        original = a.ready_candidate(self.row())
        with ExitStack() as stack:
            self.lifecycle_seams(stack)
            with mock.patch.object(service, 'reconcile_publication', side_effect=monitor.CommandError('reply lost')):
                with self.assertRaises(monitor.CommandError):
                    a.process_batch(self.conn, self.transport, self.child)
            self.assertIsNone(self.row()['predecessor_id'])
            self.assertEqual(a.ready_candidate(self.row())['id'], original['id'])
            a.process_batch(self.conn, self.transport, self.child)
        self.assertEqual(self.row()['terminal_status'], 'merged')

    def test_master_update_withdraws_checkpoint_and_ready_with_identity(self):
        self.assess()
        self.handoff()
        before = service.state(self.conn, self.child)
        for mode in ('sync', 'async'):
            with self.subTest(mode=mode):
                with self.conn:
                    self.conn.execute("UPDATE automerge_batches SET ci_mode=?,base_sha=?,candidate_json=?,ready_json=? "
                                      "WHERE batch_id=?", (mode, BASE_SHA, json.dumps(before['candidate']),
                                      json.dumps(before['ready']), self.child))
                with (mock.patch.object(a, '_try_post_verdict_status', return_value=True),
                      mock.patch.object(a, '_known_failures_prompt', return_value=[]),
                      mock.patch.object(a, '_validation_impact', return_value={'mode': 'impact'}),
                      mock.patch.object(a, '_request_rebuild', return_value=False)):
                    a._queue_master_update(self.conn, self.transport, self.row(), HEAD_ONE, HEAD_TWO)
                self.assertEqual(json.loads(self.row()['candidate_json']), {})
                self.assertEqual(json.loads(self.row()['ready_json']), {})
                withdrawal = service.latest(self.conn, self.child, 'candidate_withdrawal')
                self.assertEqual(withdrawal['candidate']['id'], before['candidate']['id'])
                self.assertEqual(withdrawal['source_revision'], before['source_revision'])
                self.assertIn('a master merge alone never justifies the full suite', self.row()['pending_prompt'])
                self.assertIn('fresh assessment', self.row()['pending_prompt'])
                self.assertIn('re-register', self.row()['pending_prompt'])

    def test_publication_migration_restores_ended_metadata_and_detached_handoffs(self):
        self.assess()
        self.land_parent()
        evidence = service.state(self.conn, self.child)['tests']
        publication = {'number': 211, 'url': 'https://example.invalid/211', 'head': HEAD_TWO}
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET integration_pr_number=211 WHERE batch_id=?', (self.child,))
            service.record(self.conn, self.child, 'publication', publication)
        a._restore_publication_handoffs(self.conn)
        self.assertEqual(a.ready_candidate(self.row())['kind'], 'publish')
        with self.conn:
            self.conn.execute("DELETE FROM known_failure_state WHERE key='automerge_ready_handoffs_v1'")
            self.conn.execute("UPDATE automerge_batches SET predecessor_id=NULL,phase='waiting_parent',ready_json='{}' "
                              "WHERE batch_id=?", (self.child,))
        a._restore_publication_handoffs(self.conn)
        self.assertEqual(a.ready_candidate(self.row())['kind'], 'ready')
        self.assertEqual(a.ready_candidate(self.row())['assessment'], evidence)

    def test_ended_predecessor_legacy_report_recovers_handoff_without_reset(self):
        self.assess()
        self.land_parent()
        with (mock.patch.object(a, '_store_agent_result', return_value='local pass'),
              mock.patch.object(a, '_record_agent_exclusions'),
              mock.patch.object(a, '_recheck_sources', side_effect=lambda pulls, **kw: (pulls, []))):
            a._agent_turn_finished(self.conn, self.transport, self.row(), 'child-session')
        self.assertEqual(self.row()['phase'], 'waiting_parent')
        self.assertEqual(self.row()['attempt_generation'], 0)
        self.assertEqual(self.row()['role_promoted'], 1)
        self.assertIsNotNone(a.ready_candidate(self.row()))

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

    def test_parent_withdrawal_leaves_successor_writes_valid(self):
        parent = service.state(self.conn, self.parent)
        service.dispatch(self.conn, self.parent, 'candidate', {'revision': parent['revision'], 'withdraw': True})
        self.assertTrue(s.parent_current(a, self.conn, self.row()))
        current = service.state(self.conn, self.child)
        service.checked(self.conn, self.child, current['revision'])

    def test_publication_is_blocked_in_both_modes(self):
        for mode in ['sync', 'async']:
            with self.subTest(mode=mode), self.conn:
                self.conn.execute('UPDATE automerge_batches SET ci_mode=? WHERE batch_id=?', (mode, self.child))
                current = self.assess()
                with mock.patch.object(service, 'reconcile_publication') as publish:
                    with self.assertRaisesRegex(ValueError, 'blocked while'):
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
        with mock.patch.object(a, 'remote_branch_sha', return_value=HEAD_TWO):
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

    def test_dependency_refresh_recovers_stale_handoff_before_role_promotion(self):
        self.assess()
        self.handoff()
        before = self.row()
        self.assertIsNotNone(a.ready_candidate(before))
        receipt = json.loads(before['ready_json'])

        def refresh(pulls, **kwargs):
            updated = [replace(p, base_ref='new-prerequisite-branch') for p in pulls]
            a._capture_batch_dependencies(self.conn, self.child, updated)
            return updated, []

        with mock.patch.object(a, '_recheck_sources', side_effect=refresh):
            s.recheck_waiting(a, self.conn, self.transport, before)
        recovered = self.row()
        self.assertEqual(recovered['phase'], 'fixing')
        self.assertIn('handoff became stale', recovered['pending_prompt'])
        self.assertIn('reuse applicable execution evidence', recovered['pending_prompt'])
        self.assertEqual(recovered['session_id'], before['session_id'])
        self.assertEqual(recovered['candidate_json'], before['candidate_json'])
        command_id = recovered['prompt_command_id']
        s.recheck_waiting(a, self.conn, self.transport, recovered)
        self.assertEqual(self.row()['prompt_command_id'], command_id)
        self.assertEqual(s.latest_tests(self.conn, self.child)['head'], receipt['head'])

    def test_promoted_primary_with_stale_handoff_resumes_without_new_session(self):
        self.assess()
        self.handoff()
        before = self.row()
        a._capture_batch_dependencies(self.conn, self.child,
                                     [replace(pull(8, HEAD_TWO), base_ref='new-prerequisite-branch')])
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET role_promoted=1 WHERE batch_id=?', (self.child,))
        self.assertIsNone(a.ready_candidate(self.row()))
        with (mock.patch.object(a, 'send_start_notification'),
              mock.patch.object(s, 'promote') as promote,
              mock.patch.object(a, 'launch_batch_session') as launch):
            a.process_batch(self.conn, self.transport, self.child)
        recovered = self.row()
        self.assertEqual(recovered['phase'], 'fixing')
        self.assertEqual(recovered['session_id'], before['session_id'])
        self.assertEqual(recovered['candidate_json'], before['candidate_json'])
        self.assertIn('handoff became stale', recovered['pending_prompt'])
        promote.assert_not_called()
        launch.assert_not_called()

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

    def test_ambiguous_restart_ignores_parent_candidate_change(self):
        s.invalidate(a, self.conn, self.row())
        recovery = json.loads(self.row()['recovery_json'])
        recovery.update(stage='restart', boundary_seq=101, restart_attempted=True)
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='restarting',recovery_json=?,pending_prompt='brief' WHERE batch_id=?",
                              (json.dumps(recovery), self.child))
        self.register(self.parent, HEAD_ONE)
        with mock.patch.object(s, 'send_once') as send:
            s.recover_step(a, self.conn, self.row())
        send.assert_called_once()
        self.assertEqual(self.row()['attempt_generation'], 1)
        self.assertEqual(self.row()['phase'], 'building')

    def test_parent_abort_preserves_child_rejections(self):
        current = service.state(self.conn, self.child)
        service.dispatch(self.conn, self.child, 'exclude', {'revision': current['revision'], 'number': 8,
                         'head': HEAD_TWO, 'kind': 'rejected', 'reason': 'standalone', 'evidence': 'inspection'})
        with (mock.patch.object(a, '_session_status', return_value={'state': 'suspended'}),
              mock.patch.object(a, 'request_suspend', return_value=True),
              mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')),
              mock.patch.object(monitor, 'runtime_binary_issues', return_value=[]),
              mock.patch.object(a, 'finish_batch'), mock.patch.object(a, 'lookup_batch_session', return_value=None)):
            a.abort_batch_locked(self.conn, self.transport, self.row(self.parent), 'operator abort')
        self.assertIsNone(self.row()['terminal_status'])
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

    def test_promotion_without_handoff_withdraws_checkpoint_and_requires_fresh_assessment(self):
        self.assess()
        self.land_parent()
        self.assertIsNone(a.ready_candidate(self.row()))
        with (mock.patch.object(a, 'current_master_sha', return_value=HEAD_ONE),
              mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact'})):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
        row = self.row()
        self.assertIsNone(row['predecessor_id'])
        self.assertEqual(row['base_sha'], HEAD_ONE)
        self.assertEqual(row['candidate_json'], '{}')
        self.assertEqual(row['ready_json'], '{}')
        self.assertIsNone(service.state(self.conn, self.child)['tests'])
        self.assertEqual(json.loads(row['promotion_json'])['previous_tests']['head'], HEAD_TWO)
        withdrawal = service.latest(self.conn, self.child, 'candidate_withdrawal')
        self.assertEqual(withdrawal['candidate']['head'], HEAD_TWO)
        self.assertIn('mm-merge --promote', row['pending_prompt'])
        self.assertIn('A master merge alone never justifies the full suite', row['pending_prompt'])
        self.assertIn('the crate list mm-merge --promote prints', row['pending_prompt'])

    def test_active_promotion_withdraws_latest_concurrently_registered_checkpoint(self):
        self.assess()
        self.land_parent()
        def impact(base, heads):
            self.register(self.child, HEAD_THREE)
            return {'mode': 'impact'}
        with (mock.patch.object(a, 'current_master_sha', return_value=HEAD_ONE),
              mock.patch.object(a, 'run_ci_impact', side_effect=impact)):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
        withdrawal = service.latest(self.conn, self.child, 'candidate_withdrawal')
        self.assertEqual(withdrawal['candidate']['id'], HEAD_THREE)
        promotion = json.loads(self.row()['promotion_json'])
        self.assertEqual(promotion['old_candidate']['id'], HEAD_THREE)
        self.assertEqual(promotion['previous_tests']['head'], HEAD_TWO)
        self.assertEqual(self.row()['candidate_json'], '{}')

    def test_active_promotion_defers_changed_sources_until_fresh_impact(self):
        self.assess()
        self.land_parent()
        def change_sources(base, heads):
            with self.conn:
                self.conn.execute('UPDATE automerge_batches SET active_pull_requests_json=? WHERE batch_id=?',
                                  (json.dumps([pull(8, HEAD_THREE).as_json()]), self.child))
            return {'mode': 'impact'}
        with (mock.patch.object(a, 'current_master_sha', return_value=HEAD_ONE),
              mock.patch.object(a, 'run_ci_impact', side_effect=change_sources)):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
        self.assertEqual(self.row()['base_sha'], BASE_SHA)
        self.assertEqual(self.row()['promotion_json'], '{}')
        self.assertIsNone(self.row()['pending_prompt'])
        with (mock.patch.object(a, 'current_master_sha', return_value=HEAD_ONE),
              mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact'}) as impact):
            s.reconcile(a, self.conn, self.transport, self.row())
        impact.assert_called_once_with(HEAD_ONE, [HEAD_THREE])
        self.assertEqual(self.row()['base_sha'], HEAD_ONE)
        self.assertIn('mm-merge --promote', self.row()['pending_prompt'])

    def test_unchanged_master_needs_no_incorporation_after_failed_parent(self):
        self.assess()
        self.handoff()
        original = dict(self.row())
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='failed' WHERE batch_id=?",
                              (self.parent,))
        s.reconcile(a, self.conn, self.transport, self.row())
        with mock.patch.object(a, 'run_ci_impact', side_effect=AssertionError('no incorporation')):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
        row = self.row()
        self.assertIsNone(row['predecessor_id'])
        self.assertEqual(row['promotion_json'], '{}')
        for key in ('base_sha', 'candidate_json', 'ready_json', 'attempt_generation', 'pending_prompt'):
            self.assertEqual(row[key], original[key], key)

    def test_actual_master_advance_promotes_to_newest_tip(self):
        self.assess()
        self.handoff()
        self.land_parent()
        with (mock.patch.object(a, 'current_master_sha', return_value=HEAD_ONE),
              mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact'})):
            s.promote(a, self.conn, self.transport, self.row(), self.row(self.parent))
        self.assertEqual(self.row()['phase'], 'fixing')
        self.assertEqual(self.row()['base_sha'], HEAD_ONE)
        self.assertEqual(self.row()['attempt_generation'], 0)

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
              mock.patch.object(a, '_session_status', side_effect=AssertionError('do not wait for checks')),
              mock.patch.object(s, 'stop_work', side_effect=AssertionError('keep running work')),
              mock.patch.object(a, 'process_batch') as process):
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
        self.assertEqual(next_row['base_sha'], BASE_SHA)
        self.assertTrue(s.is_speculative(next_row))
        self.assertEqual([c.args[-1] for c in process.call_args_list], [self.child, next_row['batch_id']])

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

    def test_high_priority_work_waits_for_the_promoted_lookahead(self):
        self.land_parent()
        with (mock.patch.object(a, 'select_eligible_pull_requests', return_value=[pull(10, HEAD_ONE, priority=True)]),
              mock.patch.object(a, 'compare_commit_ancestry', return_value=True),
              mock.patch.object(s, 'commit_tree', return_value=TREE),
              mock.patch.object(a, 'current_master_sha', return_value=BASE_SHA),
              mock.patch.object(s, 'selection'),
              mock.patch.object(a, 'process_batch'),
              mock.patch.object(a, 'abort_batch_locked') as abort):
            s.tick(a, self.conn, self.transport, self.row(self.parent))
        abort.assert_not_called()
        self.assertEqual(a.active_batch(self.conn)['batch_id'], self.child)

    def test_immediate_work_does_not_discard_successor_at_landing(self):
        self.land_parent()
        with mock.patch.object(a, 'abort_batch_locked') as abort:
            s.reconcile(a, self.conn, self.transport, self.row(),
                        priority_pulls=[pull(10, HEAD_ONE, priority=True, immediate=True)])
        abort.assert_not_called()
        self.assertEqual(self.row()['role_promoted'], 1)

    def test_primary_progress_uses_normal_wording_before_ancestry_incorporation(self):
        self.assess()
        transport = monitor.SlackTransport('chat', token='token', channel='channel')
        with mock.patch.object(monitor, 'slack_send', side_effect=[
            (True, 'spec-root'), (True, 'spec-details'), (False, None),
            (True, 'primary-root'), (False, None), (True, 'primary-details'),
        ]) as send:
            a.send_start_notification(self.conn, transport, self.row())
            self.assertTrue(send.call_args_list[0].args[1].startswith('*bifrost-dev* speculative batch:'))
            with self.conn:
                self.conn.execute('UPDATE automerge_batches SET transcript_after_seq=100,role_promoted=1 WHERE batch_id=?',
                                  (self.child,))
                self.conn.execute('INSERT INTO automerge_relayed_messages VALUES (?,?,?)',
                                  (self.child, 'old-message', 100))
            original = self.row()
            evidence = service.state(self.conn, self.child)
            a.send_start_notification(self.conn, transport, original)
            self.assertEqual(self.row()['speculative_thread_ts'], 'spec-root')
            self.assertIsNone(self.row()['thread_ts'])
            self.assertEqual(self.row()['transcript_after_seq'], 100)
            with mock.patch.object(a, '_read_transcript_page') as read:
                self.assertEqual(a.drain_transcript(self.conn, transport, self.child, 'child-session'), [])
            read.assert_not_called()
            a.retry_pending_notifications(self.conn, transport)
            self.assertEqual(self.row()['thread_ts'], 'primary-root')
            self.assertEqual(self.row()['start_pr_list_sent'], 0)
            # Retry with the stale pre-transition snapshot; only the reply repeats.
            a.send_start_notification(self.conn, transport, original)
            a.retry_pending_notifications(self.conn, transport)
        self.assertEqual(send.call_count, 6)
        self.assertNotIn('thread_ts', send.call_args_list[3].kwargs)
        self.assertTrue(send.call_args_list[3].args[1].startswith('*bifrost-dev* promoted batch:'))
        self.assertEqual(send.call_args_list[-1].kwargs['thread_ts'], 'primary-root')
        self.assertEqual(self.row()['start_notification_role'], 'primary')
        self.assertEqual(self.row()['start_pr_list_sent'], 1)
        after = service.state(self.conn, self.child)
        self.assertEqual(after['revision'], evidence['revision'])
        for key in ('source_revision', 'tests', 'candidate', 'attempt_generation'):
            self.assertEqual(after[key], evidence[key], key)
        with (mock.patch.object(a, '_read_transcript_page', return_value=([
                {'seq': 100, 'stable_id': 'old-message', 'text': 'old progress'},
                {'seq': 101, 'stable_id': 'new-message', 'text': 'new progress'},
              ], 101)), mock.patch.object(monitor, 'relay_text', return_value=True) as relay):
            self.assertEqual(a.drain_transcript(self.conn, transport, self.child, 'child-session'), ['new progress'])
        relay.assert_called_once_with(transport, 'primary-root', 'new progress')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM automerge_relayed_messages WHERE batch_id=?',
                                          (self.child,)).fetchone()[0], 2)

        # An initial primary notification without a speculative root is normal.
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET start_notification_role=NULL,start_notification_sent=0,"
                              "start_pr_list_sent=0,thread_ts=NULL WHERE batch_id=?", (self.child,))
        with mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')) as send:
            a.send_start_notification(self.conn, self.transport, self.row())
        self.assertNotIn('SPECULATIVE', send.call_args.args[1])
        self.assertNotIn('Preparing ahead', send.call_args.args[1])

    def test_parent_changes_leave_successor_untouched(self):
        self.assess()
        original = dict(self.row())
        for change in ('withdrawal', 'replacement', 'changed tree', 'failed tests', 'ejection'):
            with self.subTest(change=change):
                checkpoint = self.register(self.parent, HEAD_ONE)
                current = service.state(self.conn, self.parent)
                if change == 'withdrawal':
                    service.dispatch(self.conn, self.parent, 'candidate', {'revision': current['revision'], 'withdraw': True})
                elif change == 'changed tree':
                    checkpoint['tree'] = 'f' * 40
                    with self.conn:
                        self.conn.execute('UPDATE automerge_batches SET candidate_json=? WHERE batch_id=?',
                                          (json.dumps(checkpoint), self.parent))
                elif change == 'failed tests':
                    service.dispatch(self.conn, self.parent, 'tests', {'revision': current['revision'], 'head': HEAD_ONE,
                                     'verdict': 'fail', 'tests': 'failed', 'baseline': 'none'})
                elif change == 'ejection':
                    source = a.row_pulls(self.row(self.parent))[0]
                    service.dispatch(self.conn, self.parent, 'exclude', {'revision': current['revision'],
                                     'number': source.number, 'head': source.head_sha, 'kind': 'rejected',
                                     'reason': 'standalone defect', 'evidence': 'inspection'})
                s.reconcile(a, self.conn, self.transport, self.row())
                for key in ('base_sha', 'session_id', 'candidate_json', 'ready_json', 'attempt_generation', 'pending_prompt'):
                    self.assertEqual(self.row()[key], original[key], key)

    def test_parent_terminal_outcomes_promote_without_incorporation(self):
        self.assess()
        original = dict(self.row())
        for outcome in ('aborted', 'failed', 'merged', None):
            with self.subTest(outcome=outcome), self.conn:
                self.conn.execute('UPDATE automerge_batches SET role_promoted=0 WHERE batch_id=?', (self.child,))
                if outcome is None:
                    self.conn.execute('DELETE FROM automerge_batches WHERE batch_id=?', (self.parent,))
                else:
                    self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status=? WHERE batch_id=?",
                                      (outcome, self.parent))
            s.reconcile(a, self.conn, self.transport, self.row())
            self.assertEqual(self.row()['role_promoted'], 1)
            for key in ('base_sha', 'session_id', 'candidate_json', 'ready_json', 'attempt_generation', 'pending_prompt'):
                self.assertEqual(self.row()[key], original[key], key)

    def test_upgrade_reset_of_legacy_pin_happens_once(self):
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET base_sha=?,predecessor_candidate_json=? WHERE batch_id=?',
                              (HEAD_THREE, json.dumps(s.candidate(a, self.row(self.parent))), self.child))
        with mock.patch.object(a, 'compare_commit_ancestry', return_value=False):
            s.reconcile(a, self.conn, self.transport, self.row())
            s.reconcile(a, self.conn, self.transport, self.row())
        self.assertEqual(self.row()['attempt_generation'], 1)
        recovery = json.loads(self.row()['recovery_json'])
        self.assertEqual(recovery['replacement']['head'], BASE_SHA)
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET session_id=NULL WHERE batch_id=?', (self.child,))
        s.recover_step(a, self.conn, self.row())
        self.assertEqual(self.row()['base_sha'], BASE_SHA)
        self.assertEqual(self.row()['predecessor_id'], self.parent)
        s.reconcile(a, self.conn, self.transport, self.row())
        self.assertEqual(self.row()['attempt_generation'], 1)
        self.assess()
        self.handoff()
        self.land_parent()
        with ExitStack() as stack:
            write, _ = self.lifecycle_seams(stack)
            s.reconcile(a, self.conn, self.transport, self.row())
            a.process_batch(self.conn, self.transport, self.child)
        self.assertEqual(self.row()['attempt_generation'], 1)
        self.assertEqual(self.row()['terminal_status'], 'merged')
        write.assert_called_once()

    def test_master_drift_during_validation_preserves_work_and_queues_incorporation(self):
        self.assess()
        self.land_parent()
        original = dict(self.row())
        with (mock.patch.object(a, 'current_master_sha', return_value=HEAD_ONE),
              mock.patch.object(a, 'run_ci_impact', return_value={'mode': 'impact'}),
              mock.patch.object(s, 'stop_work', side_effect=AssertionError('no cancellation'))):
            s.reconcile(a, self.conn, self.transport, self.row())
        self.assertEqual(self.row()['role_promoted'], 1)
        self.assertEqual(self.row()['base_sha'], HEAD_ONE)
        self.assertEqual(self.row()['phase'], 'fixing')
        self.assertEqual(self.row()['candidate_json'], '{}')
        self.assertIn('mm-merge --promote', self.row()['pending_prompt'])
        for key in ('session_id', 'attempt_generation'):
            self.assertEqual(self.row()[key], original[key], key)

    def test_primary_abort_preserves_its_own_lookahead_after_role_promotion(self):
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
        self.assertIsNone(self.row(successor)['terminal_status'])
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
        own = self.source(2, 'child', 'child\n')
        self.state['sources'] = [self.state['sources'][1]]
        self.assemble()
        before = self.run_git('rev-parse', 'HEAD')
        # A newer actual master includes the predecessor and independent work.
        self.run_git('checkout', '--detach', parent)
        (self.repo / 'independent').write_text('new master work\n')
        self.run_git('add', 'independent')
        self.run_git('commit', '-m', 'independent master advance')
        master = self.run_git('rev-parse', 'HEAD')
        self.run_git('checkout', self.state['branch'])
        self.state.update(base_sha=master, predecessor=None,
                          promotion={'previous_tests': {'head': before, 'verdict': 'pass', 'tests': 'check'}})
        result = self.call(merge.promote)
        self.assertFalse(result['manual_required'])
        self.assertTrue(result['fresh_assessment_required'])
        self.assertEqual(result['diff']['from'], before)
        self.assertEqual(result['diff']['changed_paths'], ['independent', 'parent'])
        self.assertEqual((self.repo / 'child').read_text(), 'child\n')
        self.assertEqual((self.repo / 'parent').read_text(), 'parent\n')
        self.assertEqual((self.repo / 'independent').read_text(), 'new master work\n')
        history = self.run_git('rev-list', 'HEAD')
        for head in (before, own, parent, master):
            self.assertIn(head, history)

        conn = make_db(pulls=[pull(8, own)], ci_mode='async', integration_pr_number=None)
        self.addCleanup(conn.close)
        service.ensure_schema(conn)
        old = {'id': 'old-checkpoint', 'head': before, 'tree': self.run_git('rev-parse', before + '^{tree}'),
               'branch': row_for(conn)['branch'], 'source_revision': s.source_revision(a, row_for(conn)),
               'attempt_generation': 0}
        promotion = {'stage': 'incorporating', 'base': master, 'old_base': self.base, 'old_candidate': old}
        with conn:
            conn.execute("UPDATE automerge_batches SET base_sha=?,promotion_json=?,candidate_json='{}',phase='fixing' WHERE batch_id=?",
                         (master, json.dumps(promotion), 'batch-test'))
        current = service.state(conn, 'batch-test')
        def ancestor(base, head):
            return subprocess.run(['git', '-C', str(self.repo), 'merge-base', '--is-ancestor', base, head],
                                  capture_output=True).returncode == 0
        with (mock.patch.object(a, 'compare_commit_ancestry', side_effect=ancestor),
              mock.patch.object(a, 'remote_branch_sha', return_value=result['head']),
              mock.patch.object(a, 'commit_tree_sha', side_effect=lambda head: self.run_git('rev-parse', head + '^{tree}'))):
            updated = service.dispatch(conn, 'batch-test', 'candidate',
                                       {'revision': current['revision'], 'head': result['head']})
        self.assertNotEqual(updated['candidate']['id'], old['id'])
        self.assertNotIn('equivalent_heads', updated['candidate'])
        with self.assertRaisesRegex(ValueError, 'passing assessment'):
            service.dispatch(conn, 'batch-test', 'publish', {'revision': updated['revision'], 'head': result['head']})

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
