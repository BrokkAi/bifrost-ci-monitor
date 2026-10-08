"""Dependency regressions use real commit ancestry and isolated external writes."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from unittest import TestCase, mock
from urllib.parse import parse_qs, urlsplit

import automerge
import mm_service
import monitor
import pr_dependencies
import speculation


class DependencyTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'master')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.base = self.commit('base', 'base')
        self.items = {}
        self.x = self.add_pr(30, 'x', self.base)
        self.y = self.add_pr(20, 'y', self.x, base='x')
        self.z = self.add_pr(10, 'z', self.y, base='y')
        self.w = self.add_pr(40, 'w', self.base)
        self.patch(automerge, 'DB_PATH', self.root / 'state.db')
        self.conn = automerge.connect_db()
        self.addCleanup(lambda: self.conn.close())
        self.patch(automerge, 'list_open_pull_requests', side_effect=lambda: [copy.deepcopy(p) for p in self.items.values()
                  if p['state'] == 'open'])
        self.patch(automerge, 'current_master_sha', side_effect=lambda: self.git('rev-parse', 'master'))
        self.patch(automerge, 'compare_commit_ancestry', side_effect=self.ancestor)
        self.patch(automerge, 'compare_pr_behind_by', return_value=0)
        self.patch(automerge, 'gh_json', side_effect=self.read)
        self.patch(automerge, 'list_pull_comments', return_value=[])
        self.patch(automerge, 'run_gh', side_effect=AssertionError('unexpected external command'))
        self.patch(automerge, 'run_ci_impact', return_value={'mode': 'impact'})
        self.patch(automerge, '_known_failures_prompt', return_value='')
        self.patch(automerge, '_source_pr_state', side_effect=lambda p: self.view(p.number))
        self.patch(monitor, 'slack_send', return_value=(True, 'thread'))
        self.patch(automerge, 'github_api_write', side_effect=self.write)
        self.writes = []

    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def ancestor(self, a, b):
        return subprocess.run(['git', '-C', str(self.repo), 'merge-base', '--is-ancestor', a, b],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def commit(self, name, text):
        (self.repo / name).write_text(text)
        self.git('add', name)
        self.git('commit', '-qm', name)
        return self.git('rev-parse', 'HEAD')

    def add_pr(self, number, branch, start, *, base='master'):
        self.git('checkout', '-qb', branch, start)
        head = self.commit(branch, branch)
        self.items[number] = dict(number=number, title=branch, state='open', draft=False,
                                  head={'sha': head, 'ref': branch, 'repo': {'full_name': automerge.REPO_NAME}},
                                  base={'ref': base, 'repo': {'full_name': automerge.REPO_NAME}}, labels=[])
        return head

    def read(self, args, **kwargs):
        endpoint = next((s for s in args if s.startswith('repos/')), '')
        if '/pulls?' in endpoint:
            branch = parse_qs(urlsplit(endpoint).query)['head'][0].split(':', 1)[1]
            return [[copy.deepcopy(p) for p in self.items.values() if p['head']['ref'] == branch]]
        number = int(endpoint.rsplit('/', 1)[1])
        return copy.deepcopy(self.items[number])

    def write(self, endpoint, method, payload=None):
        self.writes.append((endpoint, method, copy.deepcopy(payload)))
        number = int(endpoint.split('/')[1])
        if method == 'PATCH':
            self.items[number]['base']['ref'] = payload['base']
        return self.items[number]

    def view(self, number):
        item = self.items[number]
        return {'state': item['state'].upper(), 'isDraft': item['draft'],
                'headRefOid': item['head']['sha'], 'baseRefName': item['base']['ref'],
                'labels': item['labels'], 'title': item['title']}

    def selected(self):
        return automerge.select_eligible_pull_requests(conn=self.conn)

    def reject(self, number):
        with self.conn:
            automerge.enqueue_github_write(self.conn, 'test', 'reject_head', number,
                self.items[number]['head']['sha'], {'reason': 'broken', 'evidence': 'isolated failure'})

    def restart(self):
        self.conn.close()
        self.conn = automerge.connect_db()

    def land(self, head):
        self.git('checkout', '-q', 'master')
        self.git('merge', '--no-ff', '-qm', 'integration', head)

    def test_dependency_batch_orders_prerequisites_before_lower_numbered_descendants(self):
        selected = self.selected()
        self.assertEqual([p.number for p in selected], [30, 20, 10, 40])
        self.assertEqual({d.number for d in selected[2].dependencies}, {20, 30})
        self.assertEqual(self.writes, [])

    def test_lookahead_selects_disjoint_dependency_closure_without_promoting_unlanded_work(self):
        foreground = [p for p in self.selected() if p.number == 30]
        identifier = automerge.create_batch(self.conn, foreground, self.base, batch_id='parent', ci_mode='sync')
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()
        checkpoint = dict(id='candidate-1', head=self.x, tree=self.git('rev-parse', self.x + '^{tree}'),
                          source_revision=speculation.source_revision(automerge, row))
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET candidate_json=? WHERE batch_id=?', (json.dumps(checkpoint), identifier))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()
        before = self.git('rev-parse', 'master')
        with (mock.patch.object(automerge, 'SPECULATIVE_LOOKAHEAD', True),
              mock.patch.object(automerge, 'CI_MODE', 'async'),
              mock.patch.object(speculation, 'require_recovery_controls')):
            speculation.launch_child(automerge, self.conn, row)
            speculation.launch_child(automerge, self.conn, row)
        child = speculation.child(self.conn, identifier)
        self.assertEqual([p.number for p in automerge.row_pulls(child)], [20, 10, 40])
        self.assertEqual(child['base_sha'], self.x)
        self.assertEqual(child['ci_mode'], 'async')
        self.assertEqual(self.git('rev-parse', 'master'), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM automerge_batches').fetchone()[0], 2)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM automerge_github_outbox WHERE kind='promote_dependency'").fetchone()[0], 0)

    def test_master_targeting_descendant_cannot_import_rejected_head_before_selection(self):
        self.items[20]['base']['ref'] = 'master'
        self.reject(30)
        self.assertEqual([p.number for p in self.selected()], [40])
        self.assertIn('prerequisite', automerge.DEPENDENCY_BLOCKS[0]['reason'])

    def test_cancelled_rejection_is_not_recreated_by_its_visible_github_marker(self):
        self.reject(30)
        self.items[30]['labels'] = [{'name': automerge.REJECTED_LABEL}]
        with mock.patch.object(automerge, 'list_pull_comments', return_value=[{
                'user': {'login': automerge.TRUSTED_REJECTION_LOGIN},
                'body': f'automerge-rejected-head: {self.x}\noriginal evidence'}]):
            self.selected()
            self.conn.execute("UPDATE automerge_github_outbox SET cancelled_at='now' WHERE kind='reject_head'")
            self.conn.commit()
            self.assertEqual([p.number for p in self.selected()], [30, 20, 10, 40])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM automerge_github_outbox WHERE kind='reject_head' "
                                           "AND cancelled_at IS NULL").fetchone()[0], 0)

    def test_read_only_inspection_does_not_capture_or_enqueue_dependencies(self):
        changes = self.conn.total_changes
        selected = automerge.select_eligible_pull_requests(conn=self.conn, dry_run=True)
        self.assertEqual([p.number for p in selected], [30, 20, 10, 40])
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM automerge_pr_dependencies').fetchone()[0], 0)

    def test_draft_and_closed_prerequisites_block_only_descendants(self):
        for state, draft in [('open', True), ('closed', False)]:
            with self.subTest(state=state):
                self.items[30].update(state=state, draft=draft)
                self.assertEqual([p.number for p in self.selected()], [40])

    def test_closed_prerequisite_is_discovered_from_non_master_base_without_prior_state(self):
        self.items[30]['state'] = 'closed'
        self.assertEqual([p.number for p in self.selected()], [40])
        captured = self.conn.execute('SELECT prerequisite_head FROM automerge_pr_dependencies '
                                    'WHERE number=20 AND prerequisite_number=30').fetchone()
        self.assertEqual(captured[0], self.x)

    def test_matching_branch_names_from_different_repositories_are_not_dependencies(self):
        self.items[30]['head']['repo']['full_name'] = 'someone/fork'
        self.git('checkout', '-qb', 'isolated-y', self.base)
        self.items[20]['head']['sha'] = self.commit('isolated-y', 'independent')
        selected = self.selected()
        self.assertNotIn(20, [p.number for p in selected])
        self.assertIn('unresolved', next(p['reason'] for p in automerge.DEPENDENCY_BLOCKS if p['number'] == 20))

    def test_repaired_prerequisite_requires_descendants_to_contain_new_head(self):
        self.reject(30)
        self.selected()
        self.git('checkout', '-q', 'x')
        repaired = self.commit('fix', 'fixed')
        self.items[30]['head']['sha'] = repaired
        self.assertEqual([p.number for p in self.selected()], [30, 40])
        self.git('checkout', '-q', 'y')
        self.git('merge', '--no-ff', '-qm', 'update prerequisite', 'x')
        self.items[20]['head']['sha'] = self.git('rev-parse', 'HEAD')
        self.assertEqual([p.number for p in self.selected()], [30, 20, 40])
        self.git('checkout', '-q', 'z')
        self.git('merge', '--no-ff', '-qm', 'update prerequisite', 'y')
        self.items[10]['head']['sha'] = self.git('rev-parse', 'HEAD')
        self.assertEqual([p.number for p in self.selected()], [30, 20, 10, 40])

    def test_priority_lane_includes_ordinary_prerequisite_closure(self):
        for label in ['mergemarshall:high', 'mergemarshall:immediate']:
            with self.subTest(label=label):
                self.items[20]['labels'] = [{'name': label}]
                selected = self.selected()
                self.assertEqual([p.number for p in selected], [30, 20])
                self.assertFalse(selected[0].priority)
                self.assertTrue(selected[1].priority)
                self.reject(30)
                self.assertEqual([p.number for p in self.selected()], [40])
                self.conn.execute('DELETE FROM automerge_github_outbox')
                self.conn.commit()

    def test_ambiguous_branch_owners_are_reported(self):
        self.add_pr(50, 'another-x', self.base)
        self.items[50]['head']['ref'] = 'x'
        self.assertNotIn(20, [p.number for p in self.selected()])
        self.assertTrue(any('ambiguous' in item['reason'] for item in automerge.DEPENDENCY_BLOCKS))

    def test_immediate_preemption_requires_eligible_dependency_closure(self):
        self.items[20]['labels'] = [{'name': 'mergemarshall:immediate'}]
        batch = automerge.create_batch(self.conn, [automerge.PullRequest(40, 'w', self.w, 'url')], self.base)
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        with mock.patch.object(automerge, '_complete_abort') as abort:
            self.assertTrue(automerge.preempt_batch_for_priority(self.conn, monitor.SlackTransport('chat'), row, self.selected()))
            abort.assert_called_once()
        self.reject(30)
        with mock.patch.object(automerge, '_complete_abort') as abort:
            self.assertFalse(automerge.preempt_batch_for_priority(self.conn, monitor.SlackTransport('chat'), row, self.selected()))
            abort.assert_not_called()

    def test_retargeting_and_restart_do_not_erase_captured_prerequisite(self):
        self.selected()
        self.items[20]['base']['ref'] = 'master'
        self.items[30]['draft'] = True
        self.restart()
        self.assertEqual([p.number for p in self.selected()], [40])
        rows = self.conn.execute('SELECT prerequisite_number,prerequisite_head FROM automerge_pr_dependencies '
                                 'WHERE number=20 AND head_sha=?', (self.y,)).fetchall()
        self.assertIn((30, self.x), [tuple(r) for r in rows])

    def test_ambiguous_cyclic_and_unresolved_branches_report_without_blocking_unrelated_pr(self):
        self.items[30]['base']['ref'] = 'y'
        self.items[10]['base']['ref'] = 'missing'
        self.assertEqual([p.number for p in self.selected()], [40])
        reasons = [p['reason'] for p in automerge.DEPENDENCY_BLOCKS]
        self.assertTrue(any('cyclic' in r for r in reasons))
        self.assertTrue(any('unresolved' in r for r in reasons))

    def test_exclusion_cascades_without_rejecting_descendants(self):
        batch = automerge.create_batch(self.conn, self.selected(), self.base, batch_id='test-batch')
        self.conn.execute("UPDATE automerge_batches SET status='running' WHERE batch_id=?", (batch,))
        self.conn.commit()
        mm_service.ensure_schema(self.conn)
        state = mm_service.state(self.conn, batch)
        updated = mm_service.dispatch(self.conn, batch, 'exclude', dict(revision=state['revision'],
            number=30, head=self.x, kind='rejected', reason='broken', evidence='base passes, X fails'))
        self.assertEqual([p['number'] for p in updated['sources']], [40])
        self.assertEqual({p['number']: p['kind'] for p in updated['excluded']},
                         {30: 'rejected', 20: 'blocked', 10: 'blocked'})
        self.assertEqual([r[0] for r in self.conn.execute("SELECT number FROM automerge_github_outbox WHERE kind='reject_head'")], [30])

    def test_expansion_includes_priority_prerequisite_and_rejects_orphaned_arrival(self):
        batch = automerge.create_batch(self.conn, [automerge.PullRequest(40, 'w', self.w, 'url', priority=True)],
                                      self.base, batch_id='expansion')
        self.items[20]['labels'] = [{'name': 'mergemarshall:high'}]
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertTrue(automerge._request_rebuild(self.conn, row, automerge.row_pulls(row), 'new arrivals'))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertEqual([p.number for p in automerge.row_pulls(row)], [40, 30, 20])
        self.assertEqual(row['expansion_count'], 1)
        automerge._persist_excluded_source_heads(self.conn, row, [{'number': 30, 'head_sha': self.x, 'kind': 'removed'}])
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.items[10]['labels'] = [{'name': 'mergemarshall:high'}]
        self.assertTrue(automerge._request_rebuild(self.conn, row, automerge.row_pulls(row), 'removed prerequisite'))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertEqual([p.number for p in automerge.row_pulls(row)], [40])
        self.assertEqual(row['expansion_count'], 1)

    def test_expansion_limit_does_not_admit_a_new_dependency_after_three_rescans(self):
        batch = automerge.create_batch(self.conn, [automerge.PullRequest(40, 'w', self.w, 'url')], self.base)
        self.conn.execute('UPDATE automerge_batches SET expansion_count=3 WHERE batch_id=?', (batch,))
        self.conn.commit()
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertTrue(automerge._request_rebuild(self.conn, row, automerge.row_pulls(row), 'retry'))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertEqual([p.number for p in automerge.row_pulls(row)], [40])

    def test_direct_and_operator_landing_refuse_unlanded_prerequisite(self):
        self.items[20]['base']['ref'] = 'master'
        graph = automerge.dependency_graph(self.conn)
        pull = automerge.dependency_pull(graph, 20)
        self.patch(automerge, 'direct_pull_request_view', side_effect=self.view)
        self.assertEqual(automerge._direct_premerge_check(pull, conn=self.conn)[0], 'dependency_blocked')
        self.patch(automerge, 'acquire_lock_wait', return_value=mock.Mock())
        self.patch(automerge, 'connect_db', return_value=self.conn)
        self.patch(automerge, 'ensure_github_auth', return_value=True)
        self.patch(monitor, 'runtime_binary_issues', return_value=[])
        self.patch(monitor, 'load_slack_transport', return_value=monitor.SlackTransport('chat'))
        created = self.patch(automerge, 'create_batch')
        self.assertEqual(automerge.run_land_now(20), 2)
        created.assert_not_called()

    def test_promotion_after_combined_merge_uses_ancestry_before_indirect_bookkeeping(self):
        self.selected()
        self.land(self.y)
        self.restart()
        self.assertEqual([p.number for p in self.selected()], [10, 40])
        intents = self.conn.execute("SELECT * FROM automerge_github_outbox WHERE kind='promote_dependency'").fetchall()
        self.assertEqual({r['number'] for r in intents}, {20, 10})
        for row in intents:
            automerge.deliver_github_write(row)
        self.assertEqual(self.items[20]['base']['ref'], 'master')
        self.assertEqual(self.items[10]['base']['ref'], 'master')
        self.assertEqual(len(self.writes), 2)

    def test_promotion_lost_acknowledgement_recovers_after_restart_without_duplicate_patch(self):
        self.selected()
        self.land(self.x)
        self.selected()
        writer = automerge.github_api_write
        def lost_ack(*args, **kwargs):
            writer(*args, **kwargs)
            raise monitor.CommandError('response lost')
        with mock.patch.object(automerge, 'github_api_write', side_effect=lost_ack):
            automerge.retry_github_outbox(self.conn, monitor.SlackTransport('chat'))
        self.restart()
        self.conn.execute('UPDATE automerge_github_outbox SET next_attempt_at=NULL')
        self.conn.commit()
        automerge.retry_github_outbox(self.conn, monitor.SlackTransport('chat'))
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM automerge_github_outbox WHERE delivered_at IS NULL").fetchone()[0], 0)

    def test_indirect_merged_flag_is_not_proof_of_prerequisite_ancestry(self):
        self.items[30].update(state='closed', merged_at='now')
        self.assertEqual([p.number for p in self.selected()], [40])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM automerge_github_outbox WHERE kind='promote_dependency'").fetchone()[0], 0)

    def test_changed_prerequisite_removes_descendants_from_active_batch(self):
        selected = self.selected()
        batch = automerge.create_batch(self.conn, selected, self.base)
        self.git('checkout', '-q', 'x')
        self.items[30]['head']['sha'] = self.commit('fix', 'fix')
        keep, removed = automerge._recheck_sources(selected, conn=self.conn, batch_id=batch)
        self.assertEqual([p.number for p in keep], [40])
        self.assertEqual(len(removed), 3)
        drafts = self.conn.execute("SELECT number FROM automerge_github_outbox WHERE kind='draft_changed_head'").fetchall()
        self.assertEqual([r[0] for r in drafts], [30])

    def test_global_rejected_ancestor_is_checked_at_landing(self):
        self.reject(30)
        batch = automerge.create_batch(self.conn, [automerge.PullRequest(40, 'w', self.w, 'url')], self.base)
        self.git('checkout', '-qb', 'candidate', self.w)
        self.git('merge', '--no-ff', '-qm', 'smuggled prerequisite', self.x)
        head = self.git('rev-parse', 'HEAD')
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        ok, reason = automerge.verify_source_ancestry(row, head, conn=self.conn)
        self.assertFalse(ok)
        self.assertIn('rejected prerequisite', reason)

    def test_eligible_repair_can_retain_old_rejected_ancestry(self):
        self.reject(30)
        self.git('checkout', '-q', 'x')
        self.items[30]['head']['sha'] = self.commit('fix', 'fixed')
        self.git('checkout', '-q', 'y')
        self.git('merge', '--no-ff', '-qm', 'update prerequisite', 'x')
        self.items[20]['head']['sha'] = self.git('rev-parse', 'HEAD')
        sources = [p for p in self.selected() if p.number in {30, 20}]
        self.assertEqual([p.number for p in sources], [30, 20])
        batch = automerge.create_batch(self.conn, sources, self.base)
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertTrue(automerge.verify_source_ancestry(row, self.items[20]['head']['sha'], conn=self.conn)[0])
