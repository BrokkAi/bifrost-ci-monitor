"""Supervisor ordering and retries operate on isolated state, never the live queue."""
import json
import contextlib
import io
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
        self.report_blocks_impl = a.report_dependency_blocks
        self.retry_outbox_impl = a.retry_github_outbox
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

    def campaign(self):
        with self.conn:
            self.conn.execute("INSERT INTO stabilize_campaigns "
                              "(id,branch,anchor_sha,tip_sha,status,created_at,updated_at) "
                              "VALUES ('test','stabilize/test',?,?,'running','now','now')",
                              (BASE_SHA, BASE_SHA))
            self.conn.execute("INSERT INTO stabilize_issues "
                              "(campaign_id,issue_number,stage,source,round,state,added_at,updated_at) "
                              "VALUES ('test',1,'push','test',0,'pr_ready','now','now')")

    def campaign_batch(self):
        source = a.PullRequest(50, 'fix', HEAD_ONE, 'url',
                               base_ref='stabilize/test', target_ref='stabilize/test')
        return a.create_batch(self.conn, [source], BASE_SHA, ci_mode='sync')

    def test_campaign_selection_captures_campaign_tip_and_forces_async(self):
        self.campaign()
        target = 'stabilize/test'
        source = a.PullRequest(50, 'fix', HEAD_ONE, 'url', base_ref=target, target_ref=target)
        self.select.side_effect = lambda **kw: [source] if kw.get('target_ref') == target else []
        remote = self.patch(a, 'remote_branch_sha', return_value=BASE_SHA)
        self.patch(a, 'current_master_sha', side_effect=AssertionError('master capture'))
        self.patch(a, '_ancestry_cache', return_value=mock.Mock(behind=mock.Mock(return_value=1)))
        self.patch(a, 'CI_MODE', 'sync')
        self.patch(a, 'process_batch')
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        self.assertIsNone(a.active_batch(self.conn))
        row = a.active_campaign_batch(self.conn)
        self.assertEqual(row['target_ref'], target)
        self.assertEqual(row['base_sha'], BASE_SHA)
        self.assertEqual(row['ci_mode'], 'async')
        remote.assert_called_with(target)

    def test_campaign_does_not_occupy_master_primary_or_create_successor(self):
        identifier = self.campaign_batch()
        options = supervisor.plan(a, self.conn, priority_checked=True, queue_checked=False,
                                  maintenance_done=False, reconciled=set())
        self.assertIn('select_primary', [entry[2] for entry in options])
        self.assertNotIn('select_successor', [entry[2] for entry in options])
        self.assertIn('advance_batch', [entry[2] for entry in options])
        self.assertIsNone(a.active_batch(self.conn))
        self.assertEqual(a.active_campaign_batch(self.conn)['batch_id'], identifier)
        master = self.batch()
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET target_ref=? WHERE batch_id=?', (a.BASE_BRANCH, master))
        self.assertEqual(a.active_batch(self.conn)['batch_id'], master)
        self.assertEqual(a.active_campaign_batch(self.conn)['batch_id'], identifier)

    def three_batches(self):
        primary = self.batch()
        successor = a.create_batch(self.conn, [a.PullRequest(2, 'next', HEAD_ONE, 'url')],
                                   BASE_SHA, ci_mode='async')
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET predecessor_id=? WHERE batch_id=?',
                              (primary, successor))
        campaign = self.campaign_batch()
        self.patch(speculation, 'reconcile', return_value=True)
        return primary, successor, campaign

    def test_master_primary_successor_and_campaign_all_advance_in_order(self):
        expected = self.three_batches()
        observed = []
        self.patch(a, 'process_batch', side_effect=lambda conn, transport, owner: observed.append(owner))
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        self.assertEqual(observed, list(expected))

    def test_deferred_or_failed_lane_does_not_block_other_lane(self):
        for failing_campaign in (True, False):
            for error in (git_ancestry.Deferred('pending'), RuntimeError('stalled')):
                with self.subTest(campaign=failing_campaign, error=type(error).__name__):
                    primary, successor, campaign = self.three_batches()
                    observed = []
                    def process(conn, transport, owner):
                        observed.append(owner)
                        if owner == (campaign if failing_campaign else primary):
                            raise error
                        with conn:
                            conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',"
                                         "terminal_status='merged' WHERE batch_id=?", (owner,))
                    self.patch(a, 'process_batch', side_effect=process)
                    supervisor.run(a, self.conn, self.transport)
                    other = primary if failing_campaign else campaign
                    self.assertEqual(self.row(other)['terminal_status'], 'merged')
                    self.assertIn(other, observed)
                    with self.conn:
                        self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal'")

    def test_immediate_preemption_leaves_campaign_running(self):
        master = self.batch()
        campaign = self.campaign_batch()
        incoming = a.PullRequest(99, 'urgent', HEAD_ONE, 'url', True, True)
        self.patch(a, 'list_open_pull_requests', return_value=[{'number': 99, 'labels': [{'name': 'mergemarshall:immediate'}]}])
        self.select.side_effect = lambda **kw: [incoming] if kw.get('priority_only') else []
        aborted = []
        def abort(conn, transport, row, reason):
            aborted.append(row['batch_id'])
            with conn:
                conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',"
                             "terminal_status='aborted' WHERE batch_id=?", (row['batch_id'],))
        self.patch(a, 'abort_batch_locked', side_effect=abort)
        self.patch(a, 'process_batch')
        self.patch(a, 'target_sha', return_value=BASE_SHA)
        self.patch(a, 'create_selected_batch')
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual(aborted, [master])
        self.assertEqual(self.row(campaign)['status'], 'launching')
        self.assertIsNone(a._priority_preemption_target(self.row(campaign), [incoming]))

    def test_preempting_priority_closure_admitted_without_unrelated_discovery(self):
        previous = self.batch()
        prerequisite = a.PullRequest(98, 'prerequisite', HEAD_ONE, 'url')
        incoming = a.PullRequest(99, 'urgent', HEAD_ONE, 'url', True, True,
            dependencies=(a.pr_dependencies.Dependency(98, HEAD_ONE),))
        self.patch(a, 'list_open_pull_requests', return_value=[
            {'number': 99, 'labels': [{'name': 'mergemarshall:immediate'}]}])
        def select(**kwargs):
            if kwargs.get('priority_only'):
                return [prerequisite, incoming]
            raise git_ancestry.Deferred('unrelated history is cold')
        self.select.side_effect = select
        def abort(conn, transport, row, reason):
            with conn:
                conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',"
                             "terminal_status='aborted' WHERE batch_id=?", (row['batch_id'],))
        self.patch(a, 'abort_batch_locked', side_effect=abort)
        self.patch(a, 'target_sha', return_value=BASE_SHA)
        self.patch(a, 'process_batch')
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        self.assertEqual(self.row(previous)['terminal_status'], 'aborted')
        selected = a.active_batch(self.conn)
        self.assertIsNotNone(selected)
        self.assertEqual([p.number for p in a.row_pulls(selected)], [98, 99])
        self.select.assert_called_once_with(conn=self.conn, priority_only=True)
        a.retry_github_outbox.assert_any_call(self.conn, self.transport)
        monitor.update_known_failures.assert_called_once()

    def test_campaign_slot_and_gate_are_independent_of_master_and_successor(self):
        self.campaign()
        primary = self.batch()
        successor = a.create_batch(self.conn, [a.PullRequest(2, 'next', HEAD_ONE, 'url')], BASE_SHA)
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET predecessor_id=? WHERE batch_id=?', (primary, successor))
        self.select.return_value = [a.PullRequest(50, 'fix', HEAD_ONE, 'url', base_ref='stabilize/test', target_ref='stabilize/test')]
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='open'")
        self.assertEqual(a.select_campaign_pull_requests(self.conn), [])
        self.select.assert_not_called()
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='pr_ready'")
        self.assertEqual([p.number for p in a.select_campaign_pull_requests(self.conn)], [50])
        self.campaign_batch()
        self.select.reset_mock()
        self.assertEqual(a.select_campaign_pull_requests(self.conn), [])
        self.select.assert_not_called()

    def test_observation_budget_reserves_time_for_each_live_batch(self):
        expected = self.three_batches()
        clock = [100.0]
        self.patch(time, 'monotonic', side_effect=lambda: clock[0])
        self.patch(a, 'OBSERVATION_DEADLINE', 150.0)
        observed = []
        def process(conn, transport, owner):
            observed.append((owner, a.OBSERVATION_DEADLINE - clock[0]))
            self.assertEqual(read_budget.deadline.get(), a.OBSERVATION_DEADLINE)
            clock[0] = a.OBSERVATION_DEADLINE
            raise git_ancestry.Deferred('slice exhausted')
        self.patch(a, 'process_batch', side_effect=process)
        self.patch(a, 'ANCESTRY_CACHE', None)
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual([entry[0] for entry in observed], list(expected))
        self.assertTrue(all(slice_seconds >= supervisor.UNSERVED_BATCH_SECONDS for _, slice_seconds in observed))
        self.assertEqual(a.OBSERVATION_DEADLINE, 150.0)

    def test_priority_read_deferral_keeps_master_fence_and_advances_campaign(self):
        master = self.batch()
        campaign = self.campaign_batch()
        self.patch(a, 'list_open_pull_requests', side_effect=git_ancestry.Deferred('priority unreadable'))
        observed = []
        self.patch(a, 'process_batch', side_effect=lambda conn, transport, owner: observed.append(owner))
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual(observed, [campaign])
        self.assertNotIn(master, observed)

    def test_blocked_priority_notice_does_not_wait_for_candidate_or_gate_advancement(self):
        identifier = self.batch()  # No checkpoint, so no successor selection.
        self.patch(a, 'list_open_pull_requests', return_value=[{
            'number': 99, 'labels': [{'name': 'mergemarshall:immediate'}]}])

        def discover(**kwargs):
            self.assertTrue(kwargs['priority_only'])
            with self.conn:
                self.conn.execute('INSERT INTO automerge_pr_inventory '
                                  '(number,head_sha,data_json,blocked_reason) VALUES (?,?,?,?) '
                                  'ON CONFLICT(number) DO UPDATE SET blocked_reason=excluded.blocked_reason',
                                  (99, HEAD_ONE, json.dumps({'number': 99, 'labels': []}),
                                   'blocked by prerequisite PR #100'))
            return []

        self.select.side_effect = discover
        self.patch(a, 'report_dependency_blocks', side_effect=self.report_blocks_impl)
        self.patch(a, 'retry_github_outbox', side_effect=self.retry_outbox_impl)
        order = []

        def process(conn, transport, owner):
            self.assertEqual(owner, identifier)
            notice = conn.execute("SELECT * FROM automerge_github_outbox "
                                  "WHERE kind='dependency_notice' AND number=99").fetchone()
            self.assertIsNotNone(notice)
            self.assertIsNone(notice['delivered_at'])
            order.append('advance')

        self.patch(a, 'process_batch', side_effect=process)

        def deliver(row, *, conn):
            if row['kind'] != 'dependency_notice':
                return None
            order.append('deliver')
            raise monitor.CommandError('temporary GitHub outage')

        delivery = self.patch(a, 'deliver_github_write', side_effect=deliver)
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        self.assertEqual(order, ['advance', 'deliver'])
        notice = self.conn.execute("SELECT * FROM automerge_github_outbox "
                                   "WHERE kind='dependency_notice' AND number=99").fetchone()
        self.assertEqual(notice['attempts'], 1)
        self.assertIsNotNone(notice['next_attempt_at'])
        self.assertIsNone(notice['delivered_at'])
        self.alert.assert_not_called()
        # A later poll preserves the one intent and its failed-delivery backoff.
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        self.assertEqual(order, ['advance', 'deliver', 'advance'])
        self.assertEqual(sum(call.args[0]['kind'] == 'dependency_notice'
                             for call in delivery.call_args_list), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM automerge_github_outbox "
                                          "WHERE kind='dependency_notice' AND number=99").fetchone()[0], 1)

    def test_check_shows_master_successor_and_campaign(self):
        primary, successor, campaign = self.three_batches()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(a.check_only(), 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result['active_batch']['batch_id'], primary)
        self.assertEqual(result['active_batch']['speculative_batch']['batch_id'], successor)
        self.assertEqual(result['campaign_batch']['batch_id'], campaign)
        self.assertEqual(result['campaign_batch']['target_ref'], 'stabilize/test')

    def test_operator_abort_selects_either_lane_by_id(self):
        master = self.batch()
        campaign = self.campaign_batch()
        self.patch(a, 'acquire_lock_wait', return_value=mock.Mock())
        self.patch(monitor, 'runtime_binary_issues', return_value=[])
        self.patch(monitor, 'load_slack_transport', return_value=self.transport)
        self.patch(a, 'ensure_github_auth', return_value=True)
        abort = self.patch(a, 'abort_batch_locked')
        for identifier in (master, campaign):
            self.assertEqual(a.run_abort_batch(identifier, 'stop'), 0)
            self.assertEqual(abort.call_args.args[2]['batch_id'], identifier)

    def test_campaign_direct_selection_runs_beside_master_and_successor(self):
        self.campaign()
        primary = self.batch()
        successor = a.create_batch(self.conn, [a.PullRequest(2, 'next', HEAD_ONE, 'url')], BASE_SHA)
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET predecessor_id=? WHERE batch_id=?', (primary, successor))
        self.patch(speculation, 'reconcile', return_value=True)
        self.patch(a, 'process_batch')
        target = 'stabilize/test'
        source = a.PullRequest(50, 'fix', HEAD_ONE, 'url', base_ref=target, target_ref=target)
        self.select.side_effect = lambda **kw: [source] if kw.get('target_ref') == target else []
        self.patch(a, 'remote_branch_sha', return_value=BASE_SHA)
        cache = self.patch(a, '_ancestry_cache', return_value=mock.Mock(behind=mock.Mock(return_value=0)))
        self.assertEqual(supervisor.run(a, self.conn, self.transport), 0)
        campaign = a.active_campaign_batch(self.conn)
        self.assertEqual(campaign['target_ref'], target)
        self.assertEqual(campaign['kind'], 'direct')
        self.assertEqual(campaign['ci_mode'], 'async')
        self.assertEqual(campaign['base_sha'], BASE_SHA)
        self.assertIsNone(campaign['predecessor_id'])
        self.assertEqual(a.active_batch(self.conn)['batch_id'], primary)
        cache.return_value.behind.assert_called_with(HEAD_ONE, BASE_SHA)

    def test_land_now_refuses_campaign_even_when_gate_is_open(self):
        self.campaign()
        self.patch(a, 'acquire_lock_wait', return_value=mock.Mock())
        self.patch(monitor, 'runtime_binary_issues', return_value=[])
        self.patch(monitor, 'load_slack_transport', return_value=self.transport)
        self.patch(a, 'ensure_github_auth', return_value=True)
        self.patch(a, 'direct_pull_request_view', return_value={
            'state': 'OPEN', 'headRefOid': HEAD_ONE, 'baseRefName': 'stabilize/test', 'isDraft': False})
        process = self.patch(a, 'process_batch')
        self.assertEqual(a.run_land_now(50), 2)
        process.assert_not_called()
        self.assertIsNone(a.active_campaign_batch(self.conn))

    def test_land_now_refuses_source_reserved_by_campaign_after_base_changes(self):
        self.campaign_batch()
        self.patch(a, 'acquire_lock_wait', return_value=mock.Mock())
        self.patch(monitor, 'runtime_binary_issues', return_value=[])
        self.patch(monitor, 'load_slack_transport', return_value=self.transport)
        self.patch(a, 'ensure_github_auth', return_value=True)
        self.patch(a, 'direct_pull_request_view', return_value={
            'state': 'OPEN', 'headRefOid': HEAD_ONE, 'baseRefName': 'master', 'isDraft': False})
        process = self.patch(a, 'process_batch')
        self.assertEqual(a.run_land_now(50), 2)
        process.assert_not_called()

    def test_reserved_campaign_sources_and_their_dependents_cannot_preempt_master(self):
        master = self.batch()
        self.campaign_batch()
        incoming = a.PullRequest(99, 'urgent', HEAD_ONE, 'url', True, True,
                                 dependencies=(a.pr_dependencies.Dependency(50, HEAD_ONE),))
        reserved = a.PullRequest(50, 'retargeted campaign fix', HEAD_ONE, 'url')
        self.patch(a, 'list_open_pull_requests', return_value=[{'number': 99, 'labels': [{'name': 'mergemarshall:immediate'}]}])
        self.select.side_effect = lambda **kw: [reserved, incoming] if kw.get('priority_only') else []
        abort = self.patch(a, 'abort_batch_locked')
        self.patch(a, 'process_batch')
        supervisor.run(a, self.conn, self.transport)
        abort.assert_not_called()
        self.assertEqual(a.active_batch(self.conn)['batch_id'], master)

    def test_idle_summary_omits_campaign_prs_and_uses_master_thread(self):
        self.campaign()
        primary = self.batch()
        campaign = self.campaign_batch()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET thread_ts='master-thread' WHERE batch_id=?", (primary,))
            self.conn.execute("UPDATE automerge_batches SET thread_ts='campaign-thread',created_at='z' WHERE batch_id=?", (campaign,))
            for number, base in ((1, 'master'), (50, 'stabilize/test')):
                item = {'number': number, 'state': 'open', 'draft': True, 'title': 'change',
                        'base': {'ref': base}}
                self.conn.execute('INSERT INTO automerge_pr_inventory(number,head_sha,data_json) VALUES (?,?,?)',
                                  (number, HEAD_ONE, json.dumps(item)))
        supervisor.idle_summary(a, self.conn, self.transport)
        replies = self.send.call_args_list
        self.assertTrue(all(call.args[2] == 'master-thread' for call in replies))
        self.assertIn('1 open PRs', replies[0].args[1])
        self.assertNotIn('/pull/50', str(replies))
        self.assertEqual(sum(len(group) for group in supervisor.queue_groups(a, self.conn).values()), 2)

    def test_repeated_master_actions_cannot_spend_campaign_observation_share(self):
        primary, successor, campaign = self.three_batches()
        clock = [100.0]
        self.patch(time, 'monotonic', side_effect=lambda: clock[0])
        self.patch(a, 'OBSERVATION_DEADLINE', 150.0)
        observed = []
        def process(conn, transport, owner):
            observed.append((owner, a.OBSERVATION_DEADLINE - clock[0]))
            clock[0] = min(a.OBSERVATION_DEADLINE, clock[0] + 5)
            if owner == primary:
                with conn:
                    conn.execute('UPDATE automerge_batches SET attempt_generation=attempt_generation+1 WHERE batch_id=?', (owner,))
                if clock[0] >= a.OBSERVATION_DEADLINE:
                    raise git_ancestry.Deferred('slice exhausted')
        self.patch(a, 'process_batch', side_effect=process)
        self.patch(a, 'ANCESTRY_CACHE', None)
        supervisor.run(a, self.conn, self.transport)
        campaign_observation = next(seconds for owner, seconds in observed if owner == campaign)
        self.assertGreaterEqual(campaign_observation, supervisor.UNSERVED_BATCH_SECONDS)
        self.assertTrue(any(owner == successor for owner, _ in observed))

    def master_budget_case(self, *, successor=False, empty_campaign=False, transitions=False):
        master = self.batch(checkpoint=successor)
        if successor:
            checkpoint = speculation.candidate(a, self.row(master))
            a.create_batch(self.conn, [a.PullRequest(2, 'next', 'b' * 40, 'url')], checkpoint['head'],
                           predecessor_id=master, predecessor_candidate=checkpoint)
        if empty_campaign:
            self.campaign()
        clock = [100.0]
        observations = []
        self.patch(time, 'monotonic', side_effect=lambda: clock[0])
        self.patch(a, 'OBSERVATION_DEADLINE', 150.0)
        self.patch(speculation, 'reconcile', return_value=True)
        def process(conn, transport, owner):
            if owner != master:
                return
            observations.append(a.OBSERVATION_DEADLINE - clock[0])
            if transitions:
                clock[0] += 5
                with conn:
                    conn.execute('UPDATE automerge_batches SET attempt_generation=attempt_generation+1 WHERE batch_id=?', (owner,))
        self.patch(a, 'process_batch', side_effect=process)
        token = read_budget.deadline.set(150.0)
        try:
            supervisor.run(a, self.conn, self.transport)
        finally:
            read_budget.deadline.reset(token)
        return observations

    def test_zero_campaign_primary_with_successor_retains_head_budget(self):
        self.assertEqual(self.master_budget_case(successor=True)[0], 50)

    def test_zero_campaign_repeated_master_advancement_retains_head_capacity(self):
        self.assertEqual(self.master_budget_case(successor=True, transitions=True), list(range(50, 0, -5)))

    def test_live_campaign_without_batch_does_not_cut_master_budget(self):
        self.assertEqual(self.master_budget_case(empty_campaign=True)[0], 50)

    def test_successor_guidance_survives_priority_read_failure(self):
        parent = self.batch()
        child = a.create_batch(self.conn, [a.PullRequest(2, 'next', 'b' * 40, 'url')], BASE_SHA)
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='running',session_id='child',predecessor_id=?,"
                              "pending_prompt='saved guidance',prompt_delivered=0 WHERE batch_id=?", (parent, child))
        self.patch(a, 'list_open_pull_requests', side_effect=monitor.CommandError('priority unavailable'))
        delivered = self.patch(a, 'deliver_pending_prompt', side_effect=self.deliver)
        process = self.patch(a, 'process_batch')
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual([call.args[2]['batch_id'] for call in delivered.call_args_list], [child])
        process.assert_not_called()

    def test_successor_recovery_survives_priority_read_failure(self):
        parent = self.batch()
        child = a.create_batch(self.conn, [a.PullRequest(2, 'next', 'b' * 40, 'url')], BASE_SHA)
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET predecessor_id=?,phase='resetting' WHERE batch_id=?", (parent, child))
        self.patch(a, 'list_open_pull_requests', side_effect=monitor.CommandError('priority unavailable'))
        process = self.patch(a, 'process_batch')
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual([call.args[2] for call in process.call_args_list], [child])

    def test_unused_primary_reservation_is_reclaimed_by_successor(self):
        primary, successor, campaign = self.three_batches()
        clock = [100.0]
        self.patch(time, 'monotonic', side_effect=lambda: clock[0])
        self.patch(a, 'OBSERVATION_DEADLINE', 150.0)
        observations = []
        def process(conn, transport, owner):
            observations.append((owner, a.OBSERVATION_DEADLINE - clock[0]))
            if owner == primary:
                clock[0] += 1
        self.patch(a, 'process_batch', side_effect=process)
        supervisor.run(a, self.conn, self.transport)
        self.assertEqual(observations, [(primary, 30), (successor, 39), (campaign, 49)])

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


class QueueGroupsTests(unittest.TestCase):
    setUp = SupervisorTests.setUp
    patch = SupervisorTests.patch

    def test_groups_are_read_only_and_idle_message_bytes_are_unchanged(self):
        import sqlite3
        entries = [(8, False, None, 'ordinary'), (2, False, 'needs <base>', 'blocked & title'),
                   (4, True, 'draft reason', 'draft'), (3, False, 'rejection reason', 'rejected'),
                   (5, True, None, 'draft second')]
        for number, draft, reason, title in entries:
            item = {'state': 'open', 'draft': draft, 'title': title, 'labels': []}
            with self.conn:
                self.conn.execute('INSERT INTO automerge_pr_inventory '
                                  '(number,head_sha,data_json,blocked_reason) VALUES (?,?,?,?)',
                                  (number, HEAD_ONE, json.dumps(item), reason))
        with self.conn:
            a.enqueue_github_write(self.conn, '__queue__', 'reject_head', 3, HEAD_ONE, {})
            for number, item in [(9, {'state': 'closed'}),
                                 (10, {'state': 'open', 'labels': [{'name': a.INTEGRATION_LABEL}]})]:
                self.conn.execute('INSERT INTO automerge_pr_inventory (number,head_sha,data_json) VALUES (?,?,?)',
                                  (number, HEAD_ONE, json.dumps(item)))
        expected = {
            'dependency blocked': [(2, HEAD_ONE, 'blocked & title', 'needs <base>')],
            'rejected': [(3, HEAD_ONE, 'rejected', 'rejection reason')],
            'draft': [(4, HEAD_ONE, 'draft', 'draft reason'), (5, HEAD_ONE, 'draft second', None)],
            'otherwise ineligible': [(8, HEAD_ONE, 'ordinary', None)],
        }
        with sqlite3.connect(f'file:{a.DB_PATH}?mode=ro', uri=True) as readonly:
            readonly.row_factory = sqlite3.Row
            readonly.execute('PRAGMA query_only=1')
            before = readonly.total_changes
            self.assertEqual(supervisor.queue_groups(a, readonly), expected)
            self.assertEqual(readonly.total_changes, before)
        self.send.assert_not_called()
        supervisor.idle_summary(a, self.conn, self.transport)
        messages = [call.args[1] for call in self.send.call_args_list[1:]]
        self.assertEqual(messages, [
            'No eligible PRs to start. 5 open PRs: 1 dependency blocked, 1 rejected, 2 draft, 1 otherwise ineligible.',
            '*Dependency blocked*\n• <https://github.com/BrokkAi/bifrost-dev/pull/2|#2> blocked &amp; title — needs &lt;base&gt;',
            '*Rejected*\n• <https://github.com/BrokkAi/bifrost-dev/pull/3|#3> rejected — rejection reason',
            '*Draft*\n• <https://github.com/BrokkAi/bifrost-dev/pull/4|#4> draft — draft reason\n• <https://github.com/BrokkAi/bifrost-dev/pull/5|#5> draft second',
            '*Otherwise ineligible*\n• <https://github.com/BrokkAi/bifrost-dev/pull/8|#8> ordinary',
        ])
        saved = self.conn.execute('SELECT * FROM automerge_queue_summaries').fetchone()
        self.assertEqual(saved['summary_id'], supervisor.digest(expected))
        self.assertEqual(saved['messages_json'], json.dumps(messages))
        count = self.send.call_count
        supervisor.idle_summary(a, self.conn, self.transport)
        self.assertEqual(self.send.call_count, count)


if __name__ == '__main__':
    unittest.main()
