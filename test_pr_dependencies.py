"""Dependency regressions use real commit ancestry and isolated external writes."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from unittest import TestCase, mock
from urllib.parse import parse_qs, urlsplit

import automerge
import mm_service
import monitor
import pr_dependencies
import speculation
import git_ancestry
import time


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
        self.patch(pr_dependencies, 'REVIEWED_SUPERSESSIONS', ())
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
        self.comments = {}
        self.patch(automerge, 'list_pull_comments', side_effect=lambda number: self.comments.get(number, []))
        self.patch(automerge, 'ensure_repository_label')
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
        elif '/labels' in endpoint:
            if method == 'POST':
                names = automerge._labels(self.items[number]) | set(payload['labels'])
            else:
                names = automerge._labels(self.items[number]) - {automerge.DEPENDENCY_BLOCKED_LABEL}
            self.items[number]['labels'] = [{'name': name} for name in sorted(names)]
        elif endpoint.endswith('/comments'):
            self.comments.setdefault(number, []).append({
                'user': {'login': automerge.TRUSTED_REJECTION_LOGIN}, 'body': payload['body'],
            })
        return self.items[number]

    def view(self, number):
        item = self.items[number]
        return {'state': item['state'].upper(), 'isDraft': item['draft'],
                'headRefOid': item['head']['sha'], 'baseRefName': item['base']['ref'],
                'labels': item['labels'], 'title': item['title']}

    def selected(self, target_ref=None):
        return automerge.select_eligible_pull_requests(conn=self.conn, target_ref=target_ref)

    def reject(self, number):
        with self.conn:
            automerge.enqueue_github_write(self.conn, 'test', 'reject_head', number,
                self.items[number]['head']['sha'], {'reason': 'broken', 'evidence': 'isolated failure'})

    def restart(self):
        self.conn.close()
        self.conn = automerge.connect_db()

    def reviewed_pair(self):
        """Build the B4785/C4788 shape with real local Git ancestry."""
        retired = self.items.pop(30)
        retired['number'] = 4788
        retired['state'] = 'closed'
        retired['draft'] = False
        retired.pop('merged_at', None)
        self.items[4788] = retired
        replacement = self.add_pr(4785, 'replacement', self.x)
        self.git('merge', '--no-ff', '-qm', 'external prerequisite', 'w')
        replacement = self.git('rev-parse', 'HEAD')
        self.items[4785]['head']['sha'] = replacement
        self.git('checkout', '-q', 'y')
        self.git('merge', '--no-ff', '-qm', 'reviewed replacement', 'replacement')
        self.items[20]['head']['sha'] = self.git('rev-parse', 'HEAD')
        return pr_dependencies.ReviewedSupersession(
            pr_dependencies.CANONICAL_REPOSITORY, 4788, self.x, 4785, replacement)

    def reviewed_graph(self, record, verify=True):
        nodes = {}
        histories = {}
        for number, item in self.items.items():
            head = item['head']['sha']
            nodes[number] = pr_dependencies.Node(
                number, head, item['base']['ref'], item['head']['ref'],
                item['head']['repo']['full_name'], item['base']['repo']['full_name'],
                copy.deepcopy(item), item['state'] == 'open' and not item['draft'])
            histories[number] = [head]
        callback = (lambda _record, _retired, _replacement: True) if verify is True else verify
        return pr_dependencies.Graph(
            nodes, histories, self.git('rev-parse', 'master'), self.ancestor,
            supersessions=(record,), verify_supersession=callback).discover()

    def land(self, head):
        self.git('checkout', '-q', 'master')
        self.git('merge', '--no-ff', '-qm', 'integration', head)

    def campaign(self, *, status='running', state='pr_ready', tip=None):
        tip = tip or self.base
        self.git('branch', 'stabilize/test', tip)
        with self.conn:
            self.conn.execute("INSERT INTO stabilize_campaigns "
                              "(id,branch,anchor_sha,tip_sha,status,created_at,updated_at) "
                              "VALUES ('test','stabilize/test',?,?,?,?,?)",
                              (self.base, tip, status, 'now', 'now'))
            self.conn.execute("INSERT INTO stabilize_issues "
                              "(campaign_id,issue_number,stage,source,round,state,added_at,updated_at) "
                              "VALUES ('test',900,'push','test',0,?,'now','now')", (state,))
        self.patch(automerge, 'remote_branch_sha', side_effect=lambda branch: self.git('rev-parse', branch))

    def lane_campaign_setup(self):
        with self.conn:
            self.conn.execute("INSERT INTO stabilize_campaigns(id,branch,anchor_sha,tip_sha,status,created_at,updated_at) VALUES ('test','stabilize/test',?,?,'running','now','now')", (self.base, self.base))
            self.conn.execute("INSERT INTO stabilize_issues(campaign_id,issue_number,stage,source,round,state,added_at,updated_at) VALUES ('test',1,'push','test',0,'pr_ready','now','now')")
        self.items[40]['base']['ref'] = 'stabilize/test'
        self.patch(automerge, 'remote_branch_sha', return_value=self.base)
        self.assertFalse(self.ancestor(self.w, self.x))
        self.assertFalse(self.ancestor(self.x, self.w))

    def test_unrelated_campaign_comment_failure_does_not_block_master_selection(self):
        self.lane_campaign_setup()
        self.items[40]['labels'] = [{'name': automerge.REJECTED_LABEL}]
        self.patch(automerge, 'list_pull_comments', side_effect=lambda number: (_ for _ in ()).throw(monitor.CommandError('campaign comments unavailable')) if number == 40 else [])
        pulls = automerge.select_eligible_pull_requests(conn=self.conn)
        self.assertEqual([p.number for p in pulls], [30, 20, 10])

    def test_unrelated_campaign_comment_failure_does_not_block_master_landing_recheck(self):
        self.lane_campaign_setup()
        selected = automerge.select_eligible_pull_requests(conn=self.conn)
        self.assertEqual([p.number for p in selected], [30, 20, 10])
        batch = automerge.create_batch(self.conn, selected, self.base)
        self.items[40]['labels'] = [{'name': automerge.REJECTED_LABEL}]
        self.patch(automerge, 'list_pull_comments', side_effect=lambda number: (_ for _ in ()).throw(monitor.CommandError('campaign comments unavailable')) if number == 40 else [])
        keep, removed = automerge._recheck_sources(selected, conn=self.conn, batch_id=batch)
        self.assertEqual(keep, selected)
        self.assertEqual(removed, [])

    def test_unrelated_master_comment_failure_does_not_block_campaign_selection(self):
        self.lane_campaign_setup()
        self.items[30]['labels'] = [{'name': automerge.REJECTED_LABEL}]
        self.patch(automerge, 'list_pull_comments', side_effect=lambda number: (_ for _ in ()).throw(monitor.CommandError('master comments unavailable')) if number == 30 else [])
        pulls = automerge.select_campaign_pull_requests(self.conn)
        self.assertEqual([p.number for p in pulls], [40])

    def test_master_ref_failure_does_not_block_campaign_only_selection(self):
        self.lane_campaign_setup()
        self.items = {40: self.items[40]}
        self.patch(automerge, 'current_master_sha', side_effect=monitor.CommandError('master ref unavailable'))
        pulls = automerge.select_campaign_pull_requests(self.conn)
        self.assertEqual([p.number for p in pulls], [40])


    def test_related_campaign_comment_failure_keeps_historical_fence(self):
        self.lane_campaign_setup()
        self.add_pr(50, 'imports-campaign', self.w)
        self.items[40]['labels'] = [{'name': automerge.REJECTED_LABEL}]
        def comments(number):
            if number == 40:
                raise monitor.CommandError('required campaign comments unavailable')
            return []
        self.patch(automerge, 'list_pull_comments', side_effect=comments)
        with self.assertRaisesRegex(monitor.CommandError, 'required campaign comments'):
            self.selected()

    def test_unrelated_disappeared_campaign_metadata_is_not_refreshed_by_master(self):
        self.lane_campaign_setup()
        self.selected()
        self.items[40]['state'] = 'closed'
        def read(args, **kwargs):
            if args[-1].endswith('/40'):
                raise monitor.CommandError('campaign metadata unavailable')
            return self.read(args, **kwargs)
        reads = self.patch(automerge, 'gh_json', side_effect=read)
        self.assertEqual([p.number for p in self.selected()], [30, 20, 10])
        reads.assert_not_called()

    def test_historical_campaign_head_remains_required_after_current_head_changes(self):
        self.lane_campaign_setup()
        self.add_pr(50, 'imports-old-campaign', self.w)
        self.selected()  # Retains the original historical campaign head.
        self.reject(40)
        new_head = self.add_pr(60, 'unrelated-replacement', self.base, base='stabilize/test')
        self.items.pop(60)
        self.items[40]['head']['sha'] = new_head
        self.items[40]['labels'] = [{'name': automerge.REJECTED_LABEL}]
        def comments(number):
            if number == 40:
                raise monitor.CommandError('historical campaign comments unavailable')
            return []
        self.patch(automerge, 'list_pull_comments', side_effect=comments)
        with self.assertRaisesRegex(monitor.CommandError, 'historical campaign comments'):
            self.selected()

    def test_campaign_closed_gate_holds_selection_without_dependency_notice(self):
        self.campaign(state='open')
        self.add_pr(50, 'campaign-fix', self.base, base='stabilize/test')
        self.assertEqual([p.number for p in self.selected()], [30, 20, 10, 40])
        self.assertEqual(automerge.DEPENDENCY_BLOCKS, [])
        automerge.report_dependency_blocks(self.conn, None)
        self.assertEqual(self.writes, [])
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='pr_ready'")
        selected = self.selected('stabilize/test')
        self.assertEqual([p.number for p in selected], [50])
        self.assertEqual(automerge.pulls_target(self.conn, selected), 'stabilize/test')

    def test_campaign_selection_is_independent_of_master_priority_and_never_preempts(self):
        self.campaign()
        self.add_pr(50, 'campaign-fix', self.base, base='stabilize/test')
        self.items[40]['labels'] = [{'name': 'mergemarshall:high'}]
        self.assertEqual([p.number for p in self.selected()], [40])
        self.assertEqual([p.number for p in self.selected('stabilize/test')], [50])
        self.items[40]['labels'] = [{'name': 'mergemarshall:immediate'}]
        self.assertEqual([p.number for p in self.selected()], [40])
        self.items[50]['labels'] = [{'name': 'mergemarshall:immediate'}]
        priority = automerge.select_eligible_pull_requests(conn=self.conn, priority_only=True)
        self.assertEqual([p.number for p in priority], [40])
        self.items[40]['labels'] = []
        selected = self.selected('stabilize/test')
        batch = automerge.create_batch(self.conn, [automerge.dependency_pull(
            automerge.dependency_graph(self.conn), 40)], self.base)
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertIsNone(automerge._priority_preemption_target(row, selected))

    def test_campaign_lane_includes_all_ready_heads_regardless_of_priority_labels(self):
        self.campaign()
        self.add_pr(50, 'campaign-first', self.base, base='stabilize/test')
        self.add_pr(51, 'campaign-second', self.base, base='stabilize/test')
        self.items[50]['labels'] = [{'name': 'mergemarshall:high'}]
        self.assertEqual([p.number for p in self.selected('stabilize/test')], [50, 51])
        batch = automerge.create_batch(self.conn, self.selected('stabilize/test'), self.base)
        self.add_pr(52, 'campaign-third', self.base, base='stabilize/test')
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertFalse(automerge._request_rebuild(self.conn, row, automerge.row_pulls(row), 'expand', only_if_expanded=True))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertEqual([p.number for p in automerge.row_pulls(row)], [50, 51])

    def test_finished_campaign_is_quiet_and_visible_as_an_exclusion(self):
        self.campaign(status='aborted')
        self.add_pr(50, 'campaign-fix', self.base, base='stabilize/test')
        self.assertNotIn(50, [p.number for p in self.selected()])
        self.assertEqual(automerge.DEPENDENCY_BLOCKS, [])
        self.assertEqual(automerge.CAMPAIGN_EXCLUSIONS[0]['number'], 50)
        automerge.report_dependency_blocks(self.conn, None)
        self.assertEqual(self.writes, [])
        self.assertIsNone(self.conn.execute('SELECT blocked_reason FROM automerge_pr_inventory '
                                            'WHERE number=50').fetchone()[0])

    def test_campaign_dependencies_use_campaign_tip_and_keep_single_target(self):
        self.campaign(tip=self.x)
        self.items[30].update(state='closed', merged_at='now')
        self.items[30]['base']['ref'] = 'stabilize/test'
        self.add_pr(50, 'campaign-fix', self.x, base='stabilize/test')
        self.add_pr(51, 'campaign-stack', self.items[50]['head']['sha'], base='campaign-fix')
        selected = self.selected('stabilize/test')
        self.assertEqual([p.number for p in selected], [50])
        self.assertTrue(all(p.target_ref == 'stabilize/test' for p in selected))
        self.assertNotIn(30, {d.number for p in selected for d in p.dependencies})
        with self.assertRaisesRegex(ValueError, 'one landing target'):
            automerge.create_batch(self.conn, selected + [automerge.PullRequest(99, 'master', self.w, 'url')], self.x)

    def test_gate_snapshot_satisfies_captured_merged_campaign_fix(self):
        self.campaign(tip=self.x)
        self.items[30].update(state='closed', merged_at='now')
        self.items[30]['base']['ref'] = 'stabilize/test'
        # Observe the closed fix and preserve a captured relationship from an
        # earlier discovery before the campaign merge was recorded.
        pr_dependencies.save_observation(self.conn, 30, self.x, self.items[30], [self.x], False)
        self.add_pr(60, 'gate', self.x)
        self.git('branch', 'stabilize-gate/test/push', self.x)
        self.items[60]['head']['ref'] = 'stabilize-gate/test/push'
        self.items[60]['head']['sha'] = self.x
        self.items[60]['labels'] = [{'name': 'mergemarshall:high'}]
        with self.conn:
            self.conn.execute("INSERT INTO stabilize_gate_merges VALUES "
                              "('test','push',?,'stabilize-gate/test/push',60,'open','now','now')", (self.x,))
            self.conn.execute('INSERT INTO automerge_pr_dependencies VALUES (60,?,30,?)', (self.x, self.x))
        selected = self.selected()
        self.assertEqual([p.number for p in selected], [60])
        self.assertEqual(selected[0].dependencies, ())
        self.assertEqual(automerge.check_source_dependencies(selected, self.conn)[1], {})
        # A campaign fix based on the snapshot must remain a campaign PR, never
        # a stacked child of the gate PR targeting master.
        self.add_pr(50, 'campaign-next', self.x, base='stabilize/test')
        selected = self.selected('stabilize/test')
        self.assertEqual([p.number for p in selected], [50])
        self.assertNotIn(60, {d.number for d in selected[0].dependencies})

    def test_campaign_rebuild_does_not_admit_arrivals_when_gate_changes(self):
        self.campaign()
        self.add_pr(50, 'campaign-first', self.base, base='stabilize/test')
        selected = self.selected('stabilize/test')
        batch = automerge.create_batch(self.conn, selected, self.base)
        self.add_pr(51, 'campaign-second', self.base, base='stabilize/test')
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='open'")
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertFalse(automerge._request_rebuild(self.conn, row, selected, 'expand', only_if_expanded=True))
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='pr_ready'")
        self.assertFalse(automerge._request_rebuild(self.conn, row, selected, 'expand', only_if_expanded=True))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertEqual([p.number for p in automerge.row_pulls(row)], [50])
        self.assertEqual(row['target_ref'], 'stabilize/test')

    def gate_repair_fixture(self):
        self.items.clear()
        rejected = self.add_pr(50, 'repair', self.base, base='stabilize/test')
        repaired = self.commit('repair-fixed', 'fixed')
        self.campaign()
        self.git('checkout', '-q', 'stabilize/test')
        self.git('merge', '--no-ff', '-qm', 'land campaign repair', repaired)
        landed = self.git('rev-parse', 'HEAD')
        self.git('branch', 'gate', landed)
        self.items[50]['head']['sha'] = repaired
        self.items[50].update(state='closed', merged=True, merged_at='now', merge_commit_sha=landed)
        self.items[60] = dict(number=60, title='gate', state='open', draft=False,
                             head={'sha': landed, 'ref': 'gate', 'repo': {'full_name': automerge.REPO_NAME}},
                             base={'ref': 'master', 'repo': {'full_name': automerge.REPO_NAME}}, labels=[])
        with self.conn:
            self.conn.execute('UPDATE stabilize_campaigns SET tip_sha=?', (landed,))
            self.conn.execute("INSERT INTO stabilize_gate_merges VALUES "
                              "('test','push',?,'gate',60,'open','now','now')", (landed,))
            automerge.enqueue_github_write(self.conn, 'rejected', 'reject_head', 50, rejected,
                                          {'reason': 'broken old head', 'evidence': 'old regression'})
        pr_dependencies.save_observation(self.conn, 50, repaired, self.items[50], [rejected, repaired], False)
        pr_dependencies.save_observation(self.conn, 60, landed, self.items[60], [landed], True)
        source = automerge.PullRequest(60, 'gate', landed, 'url')
        batch = automerge.create_batch(self.conn, [source], self.base)
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        return row, rejected, repaired, landed

    def test_gate_rejected_ancestor_is_allowed_after_same_pr_repair_lands_in_campaign(self):
        row, _, _, landed = self.gate_repair_fixture()
        with mock.patch.object(automerge, 'gh_json', side_effect=self.read) as read:
            self.assertTrue(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])
        read.assert_called_once_with(['api', f'repos/{automerge.REPO_NAME}/pulls/50'])
        self.assertEqual(self.writes, [])

    def test_gate_rejected_ancestor_without_landed_descendant_is_refused(self):
        row, rejected, _, landed = self.gate_repair_fixture()
        self.items[50].update(state='open', merged=False, merged_at=None)
        allowed, reason = automerge.verify_source_ancestry(row, landed, conn=self.conn)
        self.assertFalse(allowed)
        self.assertIn('rejected prerequisite PR #50', reason)
        # A merged PR at exactly the rejected head is not a later repair.
        self.items[50].update(state='closed', merged=True, merged_at='now')
        self.items[50]['head']['sha'] = rejected
        self.assertFalse(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])

    def test_non_gate_with_same_repaired_campaign_history_keeps_rejection_fence(self):
        row, _, _, landed = self.gate_repair_fixture()
        self.items[60]['head']['ref'] = 'ordinary'
        pr_dependencies.save_observation(self.conn, 60, landed, self.items[60], [landed], True)
        with mock.patch.object(automerge, 'gh_json', side_effect=AssertionError('non-gate repair read')):
            self.assertFalse(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])

    def test_gate_repair_must_land_to_its_campaign_and_be_in_immutable_tip(self):
        row, rejected, repaired, landed = self.gate_repair_fixture()
        self.items[50]['base']['ref'] = 'master'
        self.assertFalse(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])
        self.items[50]['base']['ref'] = 'stabilize/test'
        with self.conn:
            self.conn.execute('UPDATE stabilize_gate_merges SET tip_sha=?', (rejected,))
        self.assertFalse(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])
        with self.conn:
            self.conn.execute('UPDATE stabilize_gate_merges SET tip_sha=?', (landed,))
        self.items[50]['head']['sha'] = self.w  # Not a descendant of the rejected head.
        self.assertFalse(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])
        self.items[50]['head']['sha'] = repaired
        self.items[50]['merge_commit_sha'] = self.base  # This merge did not contain the repair.
        self.assertFalse(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])

    def test_gate_repair_does_not_authorize_non_gate_source_in_same_batch(self):
        row, _, _, landed = self.gate_repair_fixture()
        sources = automerge.row_pulls(row) + [automerge.PullRequest(61, 'ordinary', landed, 'url')]
        batch = automerge.create_batch(self.conn, sources, self.base)
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertFalse(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])

    def test_gate_repair_unavailable_reads_defer_without_using_cached_merge_metadata(self):
        row, rejected, repaired, landed = self.gate_repair_fixture()
        with (mock.patch.object(automerge, 'gh_json', side_effect=git_ancestry.Deferred('repair read pending')),
              self.assertRaises(git_ancestry.Deferred)):
            automerge.verify_source_ancestry(row, landed, conn=self.conn)
        def ancestor(a, b):
            if (a, b) == (rejected, repaired):
                raise git_ancestry.Deferred('repair ancestry pending')
            return self.ancestor(a, b)
        with (mock.patch.object(automerge, 'compare_commit_ancestry', side_effect=ancestor),
              self.assertRaises(git_ancestry.Deferred)):
            automerge.verify_source_ancestry(row, landed, conn=self.conn)
        self.items[50]['merge_commit_sha'] = None
        with self.assertRaises(automerge.AutomergeError):
            automerge.verify_source_ancestry(row, landed, conn=self.conn)

    def test_gate_repair_operator_path_resolves_source_branch_without_inventory(self):
        row, _, _, landed = self.gate_repair_fixture()
        with self.conn:
            self.conn.execute('DELETE FROM automerge_pr_inventory WHERE number=60')
        self.assertTrue(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])

    def test_dependency_batch_orders_prerequisites_before_lower_numbered_descendants(self):
        selected = self.selected()
        self.assertEqual([p.number for p in selected], [30, 20, 10, 40])
        self.assertEqual({d.number for d in selected[2].dependencies}, {20, 30})
        self.assertEqual(self.writes, [])

    def test_unchanged_discovery_reuses_every_pair_and_new_heads_only_add_missing_pairs(self):
        self.selected()
        with mock.patch.object(automerge, 'compare_commit_ancestry', side_effect=self.ancestor) as compare:
            self.selected()
        compare.assert_not_called()
        added = self.add_pr(50, 'new', self.base)
        with mock.patch.object(automerge, 'compare_commit_ancestry', side_effect=self.ancestor) as compare:
            self.selected()
        self.assertTrue(compare.called)
        self.assertTrue(all(added in call.args for call in compare.call_args_list))

    def test_source_recheck_discovers_only_its_dependency_closure(self):
        graph = automerge.dependency_graph(self.conn, roots=[20])
        self.assertEqual(set(graph.dependencies), {20, 30})
        self.assertNotIn(10, graph.dependencies)
        self.assertNotIn(40, graph.dependencies)

    def test_priority_discovery_omits_unrelated_ready_prs(self):
        self.items[20]['labels'] = [{'name': 'mergemarshall:immediate'}]
        selected = automerge.select_eligible_pull_requests(conn=self.conn, priority_only=True)
        self.assertEqual([p.number for p in selected], [30, 20])

    def test_successful_pairs_survive_an_interrupted_discovery(self):
        graph = pr_dependencies.Graph({}, {}, self.base, self.ancestor, conn=self.conn,
                                      persist_comparisons=True)
        self.assertTrue(graph.ancestor(self.base, self.x))
        self.assertFalse(graph.ancestor(self.w, self.x))
        with mock.patch.object(graph, '_ancestor', side_effect=git_ancestry.Deferred('fetch pending')):
            with self.assertRaises(git_ancestry.Deferred):
                graph.ancestor(self.z, self.x)
        self.restart()
        graph = pr_dependencies.Graph({}, {}, self.base, mock.Mock(side_effect=AssertionError('cached')),
                                      conn=self.conn, persist_comparisons=True)
        self.assertTrue(graph.ancestor(self.base, self.x))
        self.assertFalse(graph.ancestor(self.w, self.x))
        self.assertIsNone(self.conn.execute('SELECT present FROM automerge_commit_ancestry '
                                            'WHERE ancestor=? AND descendant=?', (self.z, self.x)).fetchone())

    def test_interrupted_closed_pr_refresh_resumes_after_saved_observations(self):
        self.selected()
        self.items[10]['state'] = self.items[20]['state'] = 'closed'
        def read(args, **kwargs):
            if args[-1].endswith('/20'):
                raise git_ancestry.Deferred('read budget exhausted')
            return self.read(args, **kwargs)
        with mock.patch.object(automerge, 'gh_json', side_effect=read):
            with self.assertRaises(git_ancestry.Deferred):
                self.selected()
        saved = pr_dependencies.inventory(self.conn)
        self.assertEqual(saved[10][0]['state'], 'closed')
        self.assertEqual(saved[20][0]['state'], 'open')
        with mock.patch.object(automerge, 'gh_json', side_effect=self.read) as reads:
            self.selected()
        self.assertFalse(any(call.args[0][-1].endswith('/10') for call in reads.call_args_list))

    def test_pinned_production_rechecks_persist_pairs_without_changing_inventory(self):
        selected = self.selected()
        inventory = self.conn.execute('SELECT * FROM automerge_pr_inventory ORDER BY number').fetchall()
        count = self.conn.execute('SELECT count(*) FROM automerge_commit_ancestry').fetchone()[0]
        self.git('checkout', '-qb', 'candidate', self.x)
        candidate = self.commit('candidate', 'candidate')
        automerge.check_source_dependencies(selected, self.conn, master=candidate)
        self.assertGreater(self.conn.execute('SELECT count(*) FROM automerge_commit_ancestry').fetchone()[0], count)
        self.assertEqual([tuple(r) for r in inventory], [tuple(r) for r in self.conn.execute(
            'SELECT * FROM automerge_pr_inventory ORDER BY number')])

    def test_local_cache_matches_real_git_without_github_or_blobs(self):
        cache = git_ancestry.Cache(self.root / 'ancestry', str(self.repo), lambda: None)
        self.assertFalse(cache.prepare([self.x, self.y, self.w]))
        deadline = time.monotonic() + 5
        while cache.missing([self.x, self.y, self.w]) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(cache.missing([self.x, self.y, self.w]))
        self.assertTrue(cache.wait_for_fetch(5))
        cache.worker.wait(timeout=5)
        fallback = mock.Mock(side_effect=AssertionError('unexpected GitHub comparison'))
        self.assertTrue(cache.ancestor(self.x, self.y, fallback))
        self.assertFalse(cache.ancestor(self.w, self.y, fallback))
        self.assertTrue(cache.ancestor(self.base, self.y, fallback))
        self.assertFalse((cache.git_dir / 'shallow').exists())
        (cache.git_dir / 'shallow').write_text(self.base + '\n')
        with self.assertRaises(RuntimeError):
            cache.ancestor(self.w, self.y, fallback)

    def test_pending_fetch_is_a_deferral_and_failed_fetch_uses_authoritative_fallback(self):
        cache = git_ancestry.Cache(self.root / 'ancestry', 'owner/repo', lambda: 'private-test-token')
        with mock.patch.object(git_ancestry, 'launch_worker') as spawn:
            self.assertFalse(cache.prepare([self.x, self.y]))
        job = json.loads((cache.directory / 'fetch.json').read_text())
        request = cache.directory / (job['id'] + '.request.json')
        self.assertNotIn('private-test-token', request.read_text())
        self.assertNotIn('private-test-token', (cache.git_dir / 'config').read_text())
        self.assertNotIn('private-test-token', str(spawn.call_args.args[0]))
        fallback = mock.Mock(return_value=False)
        with self.assertRaises(git_ancestry.Deferred):
            cache.ancestor(self.x, self.y, fallback)
        fallback.assert_not_called()
        git_ancestry.atomic_json(cache.directory / (job['id'] + '.json'), {'ok': False})
        self.assertFalse(cache.ancestor(self.x, self.y, fallback))
        fallback.assert_called_once_with(self.x, self.y)

    def test_changed_dependency_notice_links_prs_and_names_author_action_without_internal_ids(self):
        self.selected()  # Capture the dependency before its prerequisite changes.
        self.git('checkout', '-q', 'x')
        self.items[30]['head']['sha'] = self.commit('repair', 'new prerequisite commit')
        self.selected()
        item = next(item for item in automerge.DEPENDENCY_BLOCKS if item['number'] == 20)
        transport = monitor.SlackTransport('webhook', webhook='unused')
        with mock.patch.object(monitor, 'slack_send') as send:
            automerge.report_dependency_blocks(self.conn, transport)
            self.selected()
            automerge.report_dependency_blocks(self.conn, transport)
        send.assert_not_called()
        self.assertIn(automerge.DEPENDENCY_BLOCKED_LABEL, automerge._labels(self.items[20]))
        self.assertEqual(len(self.comments[20]), 1)
        message = self.comments[20][0]['body']
        self.assertIn('[PR #20](https://github.com/BrokkAi/bifrost-dev/pull/20)', message)
        self.assertIn('[PR #30](https://github.com/BrokkAi/bifrost-dev/pull/30)', message)
        self.assertIn('merge PR #30\'s latest branch into PR #20', message)
        self.assertIn('push the update', message)
        self.assertIn('removed automatically', message)
        self.assertNotIn(item['head_sha'], message)
        self.assertNotIn('dependency_blocked-', message)
        self.assertNotIn('automerge batch', message)
        self.assertNotIn('work continues', message)
        # Lost acceptance replies retry the same comment, and missing labels
        # are restored independently without posting another comment.
        self.items[20]['labels'] = []
        self.selected()
        writer = automerge.github_api_write
        def lost_ack(endpoint, method, payload=None):
            result = writer(endpoint, method, payload)
            raise monitor.CommandError('reply lost after write')
        with mock.patch.object(automerge, 'github_api_write', side_effect=lost_ack):
            automerge.report_dependency_blocks(self.conn, transport)
        pending = self.conn.execute("SELECT * FROM automerge_github_outbox WHERE kind='dependency_notice' "
                                    "AND number=20 AND delivered_at IS NULL").fetchone()
        self.assertEqual(pending['attempts'], 1)
        self.selected()
        automerge.report_dependency_blocks(self.conn, transport)
        self.assertEqual(self.conn.execute('SELECT attempts FROM automerge_github_outbox WHERE intent_id=?',
                                          (pending['intent_id'],)).fetchone()[0], 1)
        with self.conn:
            self.conn.execute('UPDATE automerge_github_outbox SET next_attempt_at=NULL WHERE intent_id=?',
                              (pending['intent_id'],))
        automerge.report_dependency_blocks(self.conn, transport)
        self.assertEqual(len(self.comments[20]), 1)
        # A replaced or closed head clears the informational label, even when
        # an older add is replayed. It must never comment on the old block.
        self.items[20]['head']['sha'] = self.w
        automerge.deliver_github_write(pending, conn=self.conn)
        self.assertNotIn(automerge.DEPENDENCY_BLOCKED_LABEL, automerge._labels(self.items[20]))
        self.assertEqual(len(self.comments[20]), 1)
        self.items[20]['head']['sha'] = self.y
        self.items[20]['state'] = 'closed'
        self.items[20]['labels'] = [{'name': automerge.DEPENDENCY_BLOCKED_LABEL}]
        self.selected()
        automerge.report_dependency_blocks(self.conn, transport)
        self.assertNotIn(automerge.DEPENDENCY_BLOCKED_LABEL, automerge._labels(self.items[20]))

    def test_pending_legacy_dependency_notice_is_retired_without_slack_on_retry(self):
        identifier = 'dependency-20-' + self.y
        reason = 'dependency_blocked-legacy'
        transport = monitor.SlackTransport('webhook', webhook='unused')
        with self.conn:
            self.conn.execute('INSERT INTO automerge_blocked_notifications '
                '(batch_id,reason,created_at,details,slack_notification_attempted) VALUES (?,?,?,?,0)',
                (identifier, reason, automerge.utc_now(),
                'PR #20 is waiting: PR #30 changed; its current head is absent from this PR. '
                'Unrelated eligible work continues.'))
        with mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')) as send:
            automerge.deliver_blocked_notice(self.conn, transport, identifier, reason)
            automerge.deliver_blocked_notice(self.conn, transport, identifier, reason)
        send.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT slack_notification_attempted FROM automerge_blocked_notifications '
                                          'WHERE batch_id=?', (identifier,)).fetchone()[0], 1)

    def assert_closed_cleanup_does_not_starve_notices(self, *, already_absent):
        label = automerge.DEPENDENCY_BLOCKED_LABEL
        self.selected()  # Retain both PRs when they disappear from the listing.
        for number in (30, 40):
            # Merged elsewhere, so not absorbed: its head is still not in master.
            self.items[number].update(state='closed', merged_at='2020-01-01T00:00:00Z')
            self.items[number]['labels'] = [{'name': label}, {'name': 'keep-me', 'color': '123456'}]
        self.selected()
        before = pr_dependencies.inventory(self.conn)
        # Both stale cleanups precede the new blocked-head notices. The normal
        # two-delivery limit must make progress across polls without rearming.
        with mock.patch.object(automerge, 'utc_now', return_value='2020-01-01T00:00:00+00:00'), self.conn:
            for number in (30, 40):
                automerge.enqueue_github_write(self.conn, '__dependencies__', 'dependency_notice',
                    number, self.items[number]['head']['sha'], {'reason': None})
        if already_absent:
            for number in (30, 40):
                self.items[number]['labels'] = [{'name': 'keep-me', 'color': '123456'}]
        transport = monitor.SlackTransport('webhook', webhook='unused')
        with mock.patch.object(automerge, 'deliver_github_write', wraps=automerge.deliver_github_write) as deliver:
            for _ in range(3):
                self.selected()
                automerge.report_dependency_blocks(self.conn, transport)
        for number in (30, 40):
            self.assertEqual(sum(call.args[0]['number'] == number for call in deliver.call_args_list), 1)
            cached, history = pr_dependencies.inventory(self.conn)[number]
            expected = copy.deepcopy(before[number][0])
            expected['labels'] = [{'name': 'keep-me', 'color': '123456'}]
            self.assertEqual(cached, expected)
            self.assertEqual(history, before[number][1])
        self.assertEqual(len(self.comments[20]), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM automerge_github_outbox "
            "WHERE kind='dependency_notice' AND delivered_at IS NULL").fetchone()[0], 0)
        for number in (10, 20):
            self.assertIn(label, automerge._labels(pr_dependencies.inventory(self.conn)[number][0]))

    def test_closed_dependency_label_cleanup_does_not_starve_new_notices(self):
        self.assert_closed_cleanup_does_not_starve_notices(already_absent=False)

    def test_already_absent_closed_dependency_labels_repair_stale_inventory(self):
        self.assert_closed_cleanup_does_not_starve_notices(already_absent=True)

    def test_lost_dependency_label_cleanup_reply_reconciles_cache_on_retry(self):
        label = automerge.DEPENDENCY_BLOCKED_LABEL
        self.items[30]['state'] = 'closed'
        self.items[30]['labels'] = [{'name': label}]
        self.items[10]['draft'] = self.items[20]['draft'] = True
        self.selected()
        transport = monitor.SlackTransport('webhook', webhook='unused')
        writer = automerge.github_api_write
        def lost_reply(endpoint, method, payload=None):
            writer(endpoint, method, payload)
            raise OSError('cleanup accepted; reply lost')
        with mock.patch.object(automerge, 'github_api_write', side_effect=lost_reply):
            automerge.report_dependency_blocks(self.conn, transport)
        pending = self.conn.execute("SELECT * FROM automerge_github_outbox "
            "WHERE kind='dependency_notice' AND number=30").fetchone()
        self.assertIsNone(pending['delivered_at'])
        self.assertEqual(pending['attempts'], 1)
        self.assertIsNotNone(pending['next_attempt_at'])
        self.assertIn(label, automerge._labels(pr_dependencies.inventory(self.conn)[30][0]))
        self.selected()
        automerge.report_dependency_blocks(self.conn, transport)
        self.assertEqual(self.conn.execute('SELECT attempts FROM automerge_github_outbox WHERE intent_id=?',
            (pending['intent_id'],)).fetchone()[0], 1)
        with self.conn:
            self.conn.execute('UPDATE automerge_github_outbox SET next_attempt_at=NULL WHERE intent_id=?',
                (pending['intent_id'],))
        for _ in range(3):
            self.selected()
            automerge.report_dependency_blocks(self.conn, transport)
        self.assertNotIn(label, automerge._labels(pr_dependencies.inventory(self.conn)[30][0]))
        self.assertIsNotNone(self.conn.execute('SELECT delivered_at FROM automerge_github_outbox WHERE intent_id=?',
            (pending['intent_id'],)).fetchone()[0])
        self.assertEqual(len(self.writes), 1)

    def test_dependency_label_cache_and_delivery_receipt_commit_together(self):
        label = automerge.DEPENDENCY_BLOCKED_LABEL
        self.items[30]['state'] = 'closed'
        self.items[30]['labels'] = [{'name': label}]
        self.items[10]['draft'] = self.items[20]['draft'] = True
        self.selected()
        transport = monitor.SlackTransport('webhook', webhook='unused')
        with self.conn:
            self.conn.execute("CREATE TEMP TRIGGER fail_delivery BEFORE UPDATE OF delivered_at "
                "ON automerge_github_outbox WHEN NEW.delivered_at IS NOT NULL "
                "BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'receipt unavailable'):
            automerge.report_dependency_blocks(self.conn, transport)
        # GitHub accepted cleanup, but neither local checkpoint may advance.
        self.assertNotIn(label, automerge._labels(self.items[30]))
        self.assertIn(label, automerge._labels(pr_dependencies.inventory(self.conn)[30][0]))
        self.assertIsNone(self.conn.execute("SELECT delivered_at FROM automerge_github_outbox "
            "WHERE kind='dependency_notice' AND number=30").fetchone()[0])
        with self.conn:
            self.conn.execute('DROP TRIGGER fail_delivery')
        self.selected()
        automerge.report_dependency_blocks(self.conn, transport)
        self.assertNotIn(label, automerge._labels(pr_dependencies.inventory(self.conn)[30][0]))
        self.assertIsNotNone(self.conn.execute("SELECT delivered_at FROM automerge_github_outbox "
            "WHERE kind='dependency_notice' AND number=30").fetchone()[0])
        self.assertEqual(len(self.writes), 1)

    def test_notice_explains_rejected_prerequisite_and_shared_head(self):
        rejected, child = 'a' * 40, 'b' * 40
        with self.conn:
            self.conn.execute('INSERT INTO automerge_pr_dependencies '
                              '(number,head_sha,prerequisite_number,prerequisite_head) VALUES (?,?,?,?)',
                              (20, child, 30, rejected))
            automerge.enqueue_github_write(self.conn, 'batch', 'reject_head', 30, rejected,
                                           {'reason': 'regression', 'evidence': 'tests fail'})
        reason = 'blocked by prerequisite PR #30'
        detail = automerge._prerequisite_block_detail(self.conn, 20, child, reason)
        self.assertEqual(detail, {'rejected_head': rejected})
        message = automerge.dependency_block_comment(20, reason, detail)
        self.assertIn('rejected its head `aaaaaaaaaaaa`', message)
        self.assertIn('if this PR replaces PR #30, close PR #30', message)
        self.assertIn('git commit --allow-empty', message)
        self.assertEqual(automerge._prerequisite_block_detail(self.conn, 20, child, 'cycle'), {})
        shared = automerge.dependency_block_comment(20, 'ambiguous shared head with PR #30')
        self.assertIn('point at the same commit', shared)
        self.assertIn('close whichever of PR #20 and PR #30', shared)

    def test_ineligible_prerequisite_notice_points_action_at_the_prerequisite(self):
        message = automerge.dependency_block_comment(20, 'blocked by prerequisite PR #30')
        self.assertIn('/pull/30)', message)
        self.assertIn('Action: check PR #30', message)
        self.assertIn('reconsider this PR automatically', message)
        self.assertNotIn('merge PR #30', message)
        self.items[30]['draft'] = True
        self.selected()
        transport = monitor.SlackTransport('webhook', webhook='unused')
        with mock.patch.object(monitor, 'slack_send') as send:
            writer = automerge.github_api_write
            def lost_comment_ack(endpoint, method, payload=None):
                result = writer(endpoint, method, payload)
                if endpoint == 'issues/20/comments':
                    raise monitor.CommandError('comment accepted; reply lost')
                return result
            with mock.patch.object(automerge, 'github_api_write', side_effect=lost_comment_ack):
                automerge.report_dependency_blocks(self.conn, transport)
            self.assertEqual(len(self.comments[20]), 1)
            pending = self.conn.execute("SELECT * FROM automerge_github_outbox "
                                        "WHERE kind='dependency_notice' AND number=20").fetchone()
            self.assertEqual(pending['attempts'], 1)
            # Normal outbox retries wait for fresh discovery before processing
            # dependency notices. A dependency retry reconciles the accepted
            # comment instead of posting it again.
            with self.conn:
                self.conn.execute('UPDATE automerge_github_outbox SET next_attempt_at=NULL WHERE intent_id=?',
                                  (pending['intent_id'],))
            with mock.patch.object(automerge, 'deliver_github_write') as deliver:
                automerge.retry_github_outbox(self.conn, transport)
            deliver.assert_not_called()
            self.selected()
            automerge.report_dependency_blocks(self.conn, transport)
            self.assertEqual(len(self.comments[20]), 1)
            # Make the prerequisite eligible again without changing either
            # head. A late old intent uses the refreshed graph, not its payload.
            self.items[30]['draft'] = False
            self.selected()
            automerge.deliver_github_write(pending, conn=self.conn)
            automerge.report_dependency_blocks(self.conn, transport)
            self.assertNotIn(automerge.DEPENDENCY_BLOCKED_LABEL, automerge._labels(self.items[20]))
            self.assertEqual(len(self.comments[20]), 1)
            # Reblocking the same head restores the label, not the comment.
            self.items[30]['draft'] = True
            self.selected()
            automerge.report_dependency_blocks(self.conn, transport)
            self.assertIn(automerge.DEPENDENCY_BLOCKED_LABEL, automerge._labels(self.items[20]))
            self.assertEqual(len(self.comments[20]), 1)
        send.assert_not_called()
        for reason in ('unresolved prerequisite PR #30', 'cyclic prerequisites: #20 -> #30 -> #20'):
            self.assertIn('/pull/30)', automerge.dependency_block_comment(20, reason))

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
        self.assertEqual([p.number for p in automerge.row_pulls(child)], [40])
        self.assertEqual(child['base_sha'], self.base)
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

    def test_draft_prerequisite_blocks_only_descendants_and_closed_one_is_absorbed(self):
        self.items[30].update(state='open', draft=True)
        self.assertEqual([p.number for p in self.selected()], [40])
        self.items[30].update(state='closed', draft=False)
        self.assertEqual(sorted(p.number for p in self.selected()), [10, 20, 40])

    def test_closed_prerequisite_is_discovered_from_non_master_base_without_prior_state(self):
        self.items[30]['state'] = 'closed'
        graph = automerge.dependency_graph(self.conn, promote=False)
        # Found through the stacked base branch, then absorbed by the PR carrying it.
        self.assertEqual(graph.absorbed[20], (pr_dependencies.Dependency(30, self.x),))
        self.assertIn(20, [p.number for p in self.selected()])

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

    def consolidation_fixture(self):
        """PR #5980/#6017/#5986 on 2026-10-20: a rejected PR consolidated into another."""
        self.patch(automerge, 'SUPERVISOR_RUNNING', False)
        self.items = {}
        rejected = self.add_pr(5980, 'provider-inventories', self.base)
        self.reject(5980)
        self.comments[5980] = [{'user': {'login': automerge.TRUSTED_REJECTION_LOGIN},
                                'body': f'mergemarshall:rejected-head: {rejected}\nisolated failure'}]
        self.items[5980]['labels'] = [{'name': automerge.REJECTED_LABEL}]
        consolidated = self.add_pr(6017, 'flip-batch', rejected)
        child = self.add_pr(5986, 'prefix-gap', consolidated, base='flip-batch')
        self.git('checkout', '-q', 'master')
        self.commit('unrelated-master', 'unrelated')
        return rejected, consolidated, child

    def test_closed_rejected_prerequisite_is_absorbed_by_a_master_targeted_descendant(self):
        rejected, consolidated, _ = self.consolidation_fixture()
        self.items[5980]['state'] = 'closed'
        graph = automerge.dependency_graph(self.conn, promote=False)
        self.assertEqual(graph.absorbed[6017], (pr_dependencies.Dependency(5980, rejected),))
        self.assertEqual(graph.dependencies[6017], ())
        self.assertEqual(graph.dependencies[5986], (pr_dependencies.Dependency(6017, consolidated),))
        self.assertEqual([p.number for p in self.selected()], [6017, 5986])

    def test_scoped_discovery_refreshes_a_reopened_absorbed_prerequisite(self):
        rejected, consolidated, _ = self.consolidation_fixture()
        self.items[5980]['state'] = 'closed'
        self.assertIn(6017, [p.number for p in self.selected()])
        # The cached inventory still says closed; scoped discovery must load the
        # absorbed parent's fresh state instead of trusting the cached one.
        self.items[5980]['state'] = 'open'
        graph = automerge.dependency_graph(self.conn, promote=False, selection_target='master')
        self.assertNotIn(6017, graph.absorbed)
        self.assertIn(5980, {dep.number for dep in graph.dependencies[6017]})
        self.assertFalse(graph.eligible(6017))

    # Hard-won: independent review of absorption, 2026-10-20.
    def test_absorbed_rejection_can_pass_final_gate(self):
        rejected, consolidated, child = self.consolidation_fixture()
        self.items[5980]['state'] = 'closed'
        selected = self.selected()
        self.assertEqual([p.number for p in selected], [6017, 5986])
        batch = automerge.create_batch(self.conn, selected, self.git('rev-parse', 'master'))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        ok, reason = automerge.verify_source_ancestry(row, child, conn=self.conn)
        self.assertTrue(ok, reason)

    def test_absorbed_parent_keeps_unincorporated_external_prerequisite(self):
        # P targets Q but omits Q's commit; that explicit prerequisite must
        # survive closing P and absorbing its head into master-targeting N.
        self.items = {}
        q = self.add_pr(100, 'q', self.base)
        p = self.add_pr(200, 'p', self.base, base='q')
        n = self.add_pr(300, 'n', p)
        graph = automerge.dependency_graph(self.conn, promote=False)
        self.assertFalse(graph.eligible(300))
        self.items[200]['state'] = 'closed'
        graph = automerge.dependency_graph(self.conn, promote=False)
        self.assertIn(100, {d.number for d in graph.dependencies[300]})
        self.assertFalse(graph.eligible(300))

    def test_reopened_absorbed_parent_blocks_recheck(self):
        rejected, consolidated, child = self.consolidation_fixture()
        self.items[5980]['state'] = 'closed'
        selected = self.selected()
        self.items[5980]['state'] = 'open'
        graph = automerge.dependency_graph(self.conn, roots=[p.number for p in selected], promote=False)
        self.assertIn(6017, graph.validate(selected))

    def test_cached_closed_parent_merged_to_campaign_is_refreshed(self):
        self.items = {}
        p = self.add_pr(200, 'campaign-parent', self.base, base='stabilize/test')
        n = self.add_pr(300, 'master-child', p)
        self.campaign(tip=p)
        self.items[200]['state'] = 'closed'
        pr_dependencies.save_observation(self.conn, 200, p, self.items[200], [p], False)
        graph = automerge.dependency_graph(self.conn, selection_target='master', promote=False)
        self.assertTrue(graph.eligible(300))
        self.items[200]['merged_at'] = 'now'
        stale = automerge.dependency_graph(self.conn, selection_target='master', promote=False)
        # A fresh record demonstrates the campaign fence required here.
        pr_dependencies.save_observation(self.conn, 200, p, self.items[200], [p], False)
        fresh = automerge.dependency_graph(self.conn, selection_target='master', promote=False)
        self.assertFalse(fresh.eligible(300))
        self.assertFalse(stale.eligible(300), 'cached closed-unmerged record bypasses campaign dependency fence')

    def test_scoped_cached_open_now_closed_and_reopened_are_rechecked(self):
        rejected, consolidated, child = self.consolidation_fixture()
        self.campaign()
        automerge.dependency_graph(self.conn, selection_target='master', promote=False)
        self.items[5980]['state'] = 'closed'
        graph = automerge.dependency_graph(self.conn, roots=[6017], selection_target='master', promote=False)
        self.assertTrue(graph.eligible(6017))
        selected = [automerge.dependency_pull(graph, 6017)]
        self.items[5980]['state'] = 'open'
        graph = automerge.dependency_graph(self.conn, roots=[6017], selection_target='master', promote=False)
        self.assertFalse(graph.eligible(6017))
        self.assertIn(6017, graph.validate(selected))

    def test_open_prs_at_one_head_are_ambiguous_rather_than_cyclic(self):
        rejected, consolidated, _ = self.consolidation_fixture()
        self.items[5980]['head']['sha'] = consolidated
        graph = automerge.dependency_graph(self.conn, promote=False)
        self.assertEqual(graph.problems[5980], 'ambiguous shared head with PR #6017')
        self.assertEqual(graph.problems[6017], 'ambiguous shared head with PR #5980')
        self.assertFalse(any('cyclic' in problem for problem in graph.problems.values()))
        self.assertEqual(self.selected(), [])

    def test_closing_one_shared_head_pr_admits_the_other_and_its_stacked_child(self):
        rejected, consolidated, child = self.consolidation_fixture()
        self.items[5980]['head']['sha'] = consolidated
        automerge.dependency_graph(self.conn, promote=False)  # records the ambiguous period
        self.items[6017]['state'] = 'closed'
        graph = automerge.dependency_graph(self.conn, promote=False)
        self.assertEqual(graph.dependencies[5980], ())
        self.assertIn(6017, {dep.number for dep in graph.absorbed[5980]})
        # The child stays stacked on the closed branch and still carries its work.
        self.assertIn(6017, {dep.number for dep in graph.absorbed[5986]})
        self.assertEqual(graph.dependencies[5986], (pr_dependencies.Dependency(5980, consolidated),))
        self.assertEqual([p.number for p in self.selected()], [5980, 5986])
        self.assertEqual(graph.validate([automerge.dependency_pull(graph, n) for n in (5980, 5986)]), {})

    def test_stacked_pr_is_retargeted_only_after_its_own_head_lands(self):
        rejected, consolidated, child = self.consolidation_fixture()
        self.items[6017]['state'] = 'closed'
        self.items[5980]['state'] = 'closed'
        self.assertEqual([p.number for p in self.selected()], [5986])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM automerge_github_outbox "
                                            "WHERE kind='promote_dependency' AND number=5986").fetchone())
        self.git('checkout', '-q', 'master')
        self.git('merge', '-q', '--no-ff', '-m', 'land 5986', child)
        self.selected()
        row = self.conn.execute("SELECT * FROM automerge_github_outbox "
                                "WHERE kind='promote_dependency' AND number=5986").fetchone()
        payload = json.loads(row['payload_json'])
        self.assertEqual((payload['base_ref'], payload['target_ref']), ('flip-batch', 'master'))
        master = self.git('rev-parse', 'master')
        self.assertTrue(all(self.ancestor(dep['head_sha'], master) for dep in payload['dependencies']))

    def test_landed_inherited_prerequisites_do_not_retarget_unlanded_absorbed_work(self):
        """PR #5110 on 2026-10-20: retargeted while closed base #4775 was unlanded."""
        self.patch(automerge, 'SUPERVISOR_RUNNING', False)
        self.items = {}
        inherited = self.add_pr(4676, 'pack-contract', self.base)
        self.add_pr(4775, 'preparation', inherited, base='pack-contract')
        stacked = self.add_pr(5110, 'activation-receipts', self.items[4775]['head']['sha'],
                              base='preparation')
        automerge.dependency_graph(self.conn, promote=False)  # captures 4775's edge to 4676
        self.git('checkout', '-q', 'master')
        self.git('merge', '-q', '--no-ff', '-m', 'land 4676', inherited)
        self.items[4676].update(state='closed', merged_at='now')
        self.items[4775]['state'] = 'closed'
        graph = automerge.dependency_graph(self.conn)
        self.assertEqual({dep.number for dep in graph.absorbed[5110]}, {4775})
        self.assertTrue(all(graph.landed(dep.head_sha, 5110) for dep in graph.dependencies[5110]))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM automerge_github_outbox "
                                            "WHERE kind='promote_dependency' AND number=5110").fetchone())
        self.git('merge', '-q', '--no-ff', '-m', 'land 5110', stacked)
        automerge.dependency_graph(self.conn)
        row = self.conn.execute("SELECT payload_json FROM automerge_github_outbox "
                                "WHERE kind='promote_dependency' AND number=5110").fetchone()
        self.assertEqual(json.loads(row['payload_json'])['base_ref'], 'preparation')

    def test_reviewed_supersession_replaces_exact_retired_head_and_keeps_external_edges(self):
        record = self.reviewed_pair()
        graph = self.reviewed_graph(record)
        dependencies = {dep.number: dep.head_sha for dep in graph.dependencies[20]}
        self.assertNotIn(4788, dependencies)
        self.assertEqual(dependencies[4785], record.replacement_head)
        self.assertEqual(dependencies[40], self.w)
        self.assertNotIn(20, graph.problems)
        self.assertNotIn(4788, {dep.number for dep in graph.dependencies[4785]})

    def test_shared_supersession_is_verified_once_per_discovery_and_refreshed_next_time(self):
        record = self.reviewed_pair()
        for number in range(60, 72):
            self.add_pr(number, f'dependent-{number}', record.replacement_head,
                        base='replacement')
        with (mock.patch.object(pr_dependencies, 'REVIEWED_SUPERSESSIONS', (record,)),
              mock.patch.object(automerge, 'list_pull_comments',
                                side_effect=lambda number: self.comments.get(number, [])) as reads):
            graph = automerge.dependency_graph(self.conn)
            self.assertTrue(all(graph.eligible(number) for number in range(60, 72)))
            self.assertEqual(reads.call_args_list,
                             [mock.call(record.retired_number), mock.call(record.replacement_number)])

            # A subsequent discovery must see newly posted authenticated
            # rejection evidence, even though every Git head is unchanged.
            self.comments[record.retired_number] = [{
                'user': {'login': automerge.TRUSTED_REJECTION_LOGIN},
                'body': f'mergemarshall:rejected-head: {record.retired_head}\nnew rejection',
            }]
            reads.reset_mock()
            graph = automerge.dependency_graph(self.conn)
            self.assertTrue(all(not graph.eligible(number) for number in range(60, 72)))
            self.assertTrue(graph.eligible(40))
            reads.assert_called_once_with(record.retired_number)

    def test_failed_supersession_read_defers_and_is_retried_by_next_discovery(self):
        record = self.reviewed_pair()
        with (mock.patch.object(pr_dependencies, 'REVIEWED_SUPERSESSIONS', (record,)),
              mock.patch.object(automerge, 'list_pull_comments',
                                side_effect=git_ancestry.Deferred('comments unavailable'))):
            with self.assertRaisesRegex(git_ancestry.Deferred, 'comments unavailable'):
                automerge.dependency_graph(self.conn)
        with (mock.patch.object(pr_dependencies, 'REVIEWED_SUPERSESSIONS', (record,)),
              mock.patch.object(automerge, 'list_pull_comments', return_value=[]) as reads):
            graph = automerge.dependency_graph(self.conn)
            self.assertTrue(graph.eligible(20))
            self.assertEqual(reads.call_count, 2)

    def test_reviewed_supersession_keeps_captured_external_closure(self):
        record = self.reviewed_pair()
        graph = self.reviewed_graph(record)
        graph.captured[(record.retired_number, record.retired_head)] = [
            pr_dependencies.Dependency(40, self.w)]
        normalized = graph._normalize_dependencies(
            20, [pr_dependencies.Dependency(record.retired_number, record.retired_head)])
        self.assertEqual(
            normalized,
            (pr_dependencies.Dependency(record.replacement_number, record.replacement_head),
             pr_dependencies.Dependency(40, self.w)))

    def test_audited_expansion_consumes_only_its_proven_owner_history(self):
        bold = self.add_pr(4785, 'replacement', self.base)
        chead = self.add_pr(4788, 'retired-c', bold, base='replacement')
        self.items[4788]['state'] = 'closed'
        self.git('checkout', '-q', 'replacement')
        self.git('merge', '--no-ff', '-qm', 'retired C', 'retired-c')
        self.git('merge', '--no-ff', '-qm', 'external prerequisite', 'w')
        bcurrent = self.git('rev-parse', 'HEAD')
        self.items[4785]['head']['sha'] = bcurrent
        record = pr_dependencies.ReviewedSupersession(
            pr_dependencies.CANONICAL_REPOSITORY, 4788, chead, 4785, bcurrent)
        graph = self.reviewed_graph(record)
        graph.histories[4785] = [bold, bcurrent]
        graph.captured[(4788, chead)] = [pr_dependencies.Dependency(4785, bold)]
        graph.captured[(4785, bcurrent)] = [
            pr_dependencies.Dependency(4788, chead),
            pr_dependencies.Dependency(40, self.w)]
        graph.discover(roots=[4785])
        dependencies = {dep.number: dep.head_sha for dep in graph.dependencies[4785]}
        self.assertNotIn(4785, dependencies)
        self.assertEqual(dependencies[40], self.w)
        self.assertNotIn(4785, graph.problems)
        self.assertTrue(graph.eligible(4785))

    def test_unverified_reviewed_supersession_does_not_bypass_closed_prerequisite(self):
        record = self.reviewed_pair()
        record = pr_dependencies.ReviewedSupersession(
            record.repository, record.retired_number, record.retired_head,
            record.replacement_number, self.w)
        graph = self.reviewed_graph(record)
        self.assertIn(20, graph.problems)
        self.assertIn('supersession', graph.problems[20])
        self.assertFalse(graph.eligible(20))

    def test_reviewed_supersession_does_not_hide_an_unrelated_true_cycle(self):
        record = self.reviewed_pair()
        self.add_pr(60, 'cycle-a', self.base, base='cycle-b')
        self.add_pr(70, 'cycle-b', self.base, base='cycle-a')
        graph = self.reviewed_graph(record)
        self.assertIn('cyclic prerequisites', graph.problems[60])
        self.assertIn('cyclic prerequisites', graph.problems[70])
        self.assertFalse(graph.eligible(60))
        self.assertFalse(graph.eligible(70))

    def test_ordinary_self_dependency_is_retained_without_a_reviewed_record(self):
        graph = self.reviewed_graph(pr_dependencies.ReviewedSupersession(
            pr_dependencies.CANONICAL_REPOSITORY, 4788, self.x, 4785, self.y), verify=True)
        dependency = pr_dependencies.Dependency(20, self.items[20]['head']['sha'])
        graph = pr_dependencies.Graph(
            graph.nodes, graph.histories, self.base, self.ancestor, supersessions=())
        self.assertEqual(graph._normalize_dependencies(20, [dependency]), (dependency,))

    def test_reviewed_record_validation_rejects_bad_heads_numbers_repositories_and_conflicts(self):
        valid = pr_dependencies.ReviewedSupersession(
            pr_dependencies.CANONICAL_REPOSITORY, 4788, self.x, 4785, self.y)
        cases = (
            pr_dependencies.ReviewedSupersession(valid.repository, 0, valid.retired_head,
                                                  valid.replacement_number, valid.replacement_head),
            pr_dependencies.ReviewedSupersession(valid.repository, valid.retired_number, 'x',
                                                  valid.replacement_number, valid.replacement_head),
            pr_dependencies.ReviewedSupersession('other/repo', valid.retired_number, valid.retired_head,
                                                  valid.replacement_number, valid.replacement_head),
            (valid, pr_dependencies.ReviewedSupersession(valid.repository, valid.retired_number,
                                                         valid.retired_head, 4786, self.w)),
            (valid, pr_dependencies.ReviewedSupersession(valid.repository, 4787, self.w,
                                                         valid.replacement_number, self.w)),
        )
        for records in cases:
            with self.subTest(records=records):
                with self.assertRaises(ValueError):
                    pr_dependencies.Graph({}, {}, self.base, self.ancestor, supersessions=(records if isinstance(records, tuple) else (records,)))

        shared = pr_dependencies.ReviewedSupersession(
            valid.repository, 4787, self.w, valid.replacement_number, valid.replacement_head)
        pr_dependencies.Graph({}, {}, self.base, self.ancestor, supersessions=(valid, shared))

    def test_replacement_current_head_is_required_in_the_dependent(self):
        record = self.reviewed_pair()
        self.add_pr(10, 'near-miss', record.retired_head, base='x')
        graph = self.reviewed_graph(record)
        self.assertFalse(graph.eligible(10))
        self.assertIn(10, graph.problems)
        self.assertIn('replacement head is absent', graph.problems[10])
        self.assertEqual(
            {dep.number: dep.head_sha for dep in graph.dependencies[10]}[4785],
            record.replacement_head)

    def test_wrong_repository_replacement_remains_blocked(self):
        record = self.reviewed_pair()
        self.items[4785]['head']['repo']['full_name'] = 'other/repo'
        graph = self.reviewed_graph(record)
        self.assertFalse(graph.eligible(20))
        self.assertIn(20, graph.problems)
        self.assertIn(4788, {dep.number for dep in graph.dependencies[20]})

    def test_open_retired_pr_remains_blocked(self):
        record = self.reviewed_pair()
        self.items[4788]['state'] = 'open'
        graph = self.reviewed_graph(record)
        self.assertFalse(graph.eligible(20))
        self.assertIn(20, graph.problems)
        self.assertIn(4788, {dep.number for dep in graph.dependencies[20]})

    def test_draft_replacement_remains_blocked(self):
        record = self.reviewed_pair()
        self.items[4785]['draft'] = True
        graph = self.reviewed_graph(record)
        self.assertFalse(graph.eligible(20))
        self.assertIn(20, graph.problems)
        self.assertIn(4788, {dep.number for dep in graph.dependencies[20]})

    def test_retired_head_mismatch_is_not_an_alias_and_falls_back_to_absorption(self):
        record = self.reviewed_pair()
        mismatched = pr_dependencies.ReviewedSupersession(
            record.repository, record.retired_number, self.y,
            record.replacement_number, record.replacement_head)
        graph = self.reviewed_graph(mismatched)
        # The record does not cover the actual closed head, so it authorizes no
        # alias; the dependent carries that closed work through absorption.
        self.assertFalse(any('audited supersession' in problem for problem in graph.problems.values()))
        self.assertIn(4788, {dep.number for dep in graph.absorbed[20]})

    def test_active_rejection_on_retired_head_remains_blocked(self):
        record = self.reviewed_pair()
        self.items[4788]['_mm_rejection'] = {'active': True, 'head_sha': self.x}
        graph = self.reviewed_graph(record)
        self.assertFalse(graph.eligible(20))
        self.assertIn(20, graph.problems)

    def test_supersession_requires_a_true_callback_and_does_not_catch_its_failure(self):
        record = self.reviewed_pair()
        nodes = {}
        histories = {}
        for number, item in self.items.items():
            head = item['head']['sha']
            nodes[number] = pr_dependencies.Node(
                number, head, item['base']['ref'], item['head']['ref'],
                item['head']['repo']['full_name'], item['base']['repo']['full_name'],
                copy.deepcopy(item), item['state'] == 'open' and not item['draft'])
            histories[number] = [head]
        graph = pr_dependencies.Graph(nodes, histories, self.base, self.ancestor,
                                      supersessions=(record,))
        dependency = pr_dependencies.Dependency(record.retired_number, record.retired_head)
        self.assertEqual(graph._normalize_dependencies(20, [dependency]), (dependency,))
        self.assertIn(20, graph.problems)
        graph.discover()
        self.assertFalse(graph.eligible(20))

        graph = pr_dependencies.Graph(nodes, histories, self.base, self.ancestor,
                                      supersessions=(record,),
                                      verify_supersession=lambda *_args: False)
        self.assertEqual(graph._normalize_dependencies(20, [dependency]), (dependency,))

        def deferred(*_args):
            raise RuntimeError('deferred')

        graph = pr_dependencies.Graph(nodes, histories, self.base, self.ancestor,
                                      supersessions=(record,), verify_supersession=deferred)
        with self.assertRaisesRegex(RuntimeError, 'deferred'):
            graph._normalize_dependencies(20, [dependency])

    def test_landed_retired_head_does_not_require_an_open_replacement(self):
        record = self.reviewed_pair()
        self.land(record.replacement_head)
        self.items[record.replacement_number]['state'] = 'closed'
        self.items[record.replacement_number]['merged_at'] = '2026-10-11T00:00:00Z'
        graph = self.reviewed_graph(record, verify=False)
        self.assertNotIn(20, graph.problems)
        self.assertTrue(graph.eligible(20))

    def test_supersession_uses_dependent_campaign_tip_for_already_landed_preparation(self):
        self.campaign(tip=self.x)
        self.items[20]['base']['ref'] = 'stabilize/test'
        self.items[30]['state'] = self.items[40]['state'] = 'closed'
        record = pr_dependencies.ReviewedSupersession(
            automerge.REPO_NAME, 30, self.x, 40, self.w)
        with self.conn:
            self.conn.execute('INSERT INTO automerge_pr_dependencies VALUES (20,?,30,?)', (self.y, self.x))
        nodes = {number: automerge._dependency_node(self.items[number], conn=self.conn)
                 for number in (20, 30, 40)}
        graph = pr_dependencies.Graph(nodes, {n: [node.head] for n, node in nodes.items()},
            self.base, self.ancestor, conn=self.conn, targets={20: ('stabilize/test', self.x)},
            supersessions=(record,), verify_supersession=mock.Mock(side_effect=AssertionError('already landed'))).discover([20])
        self.assertNotIn(20, graph.problems)
        self.assertTrue(graph.eligible(20))
        self.assertEqual(graph.dependencies[20], (pr_dependencies.Dependency(30, self.x),))

    def test_gate_satisfied_repair_is_not_reinterpreted_as_unmerged_supersession(self):
        row, rejected, _, landed = self.gate_repair_fixture()
        record = pr_dependencies.ReviewedSupersession(automerge.REPO_NAME, 50, rejected, 60, landed)
        with self.conn:
            self.conn.execute('INSERT INTO automerge_pr_dependencies VALUES (60,?,50,?)', (landed, rejected))
        with (mock.patch.object(pr_dependencies, 'REVIEWED_SUPERSESSIONS', (record,)),
              mock.patch.object(automerge, '_verify_supersession_rejections', side_effect=AssertionError('already satisfied gate'))):
            selected = self.selected()
        self.assertEqual([p.number for p in selected], [60])
        self.assertEqual(selected[0].dependencies, ())
        self.assertTrue(automerge.verify_source_ancestry(row, landed, conn=self.conn)[0])

    def test_reviewed_supersession_campaign_closure_stays_in_one_target_and_keeps_rejections(self):
        record = self.reviewed_pair()
        self.campaign(tip=self.w)
        for number in (20, record.retired_number, record.replacement_number):
            self.items[number]['base']['ref'] = 'stabilize/test'
        with mock.patch.object(pr_dependencies, 'REVIEWED_SUPERSESSIONS', (record,)):
            selected = self.selected('stabilize/test')
            self.assertEqual([p.number for p in selected], [record.replacement_number, 20])
            self.assertTrue(all(p.target_ref == 'stabilize/test' for p in selected))
            self.reject(record.retired_number)
            self.assertNotIn(record.replacement_number, [p.number for p in self.selected('stabilize/test')])
            self.assertNotIn(20, [p.number for p in self.selected('stabilize/test')])

    def test_reviewed_supersession_cannot_import_unlanded_master_replacement_into_campaign(self):
        record = self.reviewed_pair()
        self.campaign(tip=self.w)
        self.items[20]['base']['ref'] = 'stabilize/test'
        with mock.patch.object(pr_dependencies, 'REVIEWED_SUPERSESSIONS', (record,)):
            graph = automerge.dependency_graph(self.conn)
        self.assertFalse(graph.eligible(20))
        selected = [automerge.dependency_pull(graph, n) for n in graph.select()]
        self.assertTrue(selected)
        self.assertTrue(all(p.target_ref == 'master' for p in selected))

    def test_lookahead_preserves_authenticated_reviewed_supersession(self):
        record = self.reviewed_pair()
        identifier = automerge.create_batch(self.conn, [automerge.PullRequest(40, 'w', self.w, 'url')],
                                           self.base, ci_mode='async')
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()
        checkpoint = dict(id='checkpoint', head=self.w, tree=self.git('rev-parse', self.w + '^{tree}'),
                          source_revision=speculation.source_revision(automerge, row))
        with self.conn:
            self.conn.execute('UPDATE automerge_batches SET candidate_json=? WHERE batch_id=?',
                              (json.dumps(checkpoint), identifier))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()
        with (mock.patch.object(pr_dependencies, 'REVIEWED_SUPERSESSIONS', (record,)),
              mock.patch.object(automerge, 'list_pull_comments', return_value=[]) as reads):
            selected = speculation.selection(automerge, self.conn, row)
        self.assertEqual(selected, [])
        # Both otherwise eligible heads require reserved PR #40 to land.
        # Supersession is still authenticated once within actual-master discovery.
        self.assertEqual(reads.call_args_list,
                         [mock.call(record.retired_number), mock.call(record.replacement_number)])

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

    def test_rebuild_does_not_admit_priority_arrivals_or_orphaned_dependencies(self):
        batch = automerge.create_batch(self.conn, [automerge.PullRequest(40, 'w', self.w, 'url', priority=True)],
                                      self.base, batch_id='expansion')
        self.items[20]['labels'] = [{'name': 'mergemarshall:high'}]
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertTrue(automerge._request_rebuild(self.conn, row, automerge.row_pulls(row), 'new arrivals'))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertEqual([p.number for p in automerge.row_pulls(row)], [40])
        self.assertEqual(row['expansion_count'], 0)
        automerge._persist_excluded_source_heads(self.conn, row, [{'number': 30, 'head_sha': self.x, 'kind': 'removed'}])
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.items[10]['labels'] = [{'name': 'mergemarshall:high'}]
        self.assertTrue(automerge._request_rebuild(self.conn, row, automerge.row_pulls(row), 'removed prerequisite'))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (batch,)).fetchone()
        self.assertEqual([p.number for p in automerge.row_pulls(row)], [40])
        self.assertEqual(row['expansion_count'], 0)

    def test_rebuild_does_not_admit_arrivals_for_historical_expansion_count(self):
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
