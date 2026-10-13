"""Independent cross-slice checks; every service boundary is mocked.

BIFROST_INTEGRATION_NO_LIVE=1 installs a process audit guard before the other
unittest modules run. Only loopback sockets and local subprocesses are allowed.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


def _no_live(event, args):
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError(f'forbidden nonlocal socket: {address[0]}')
    if event == 'subprocess.Popen':
        executable, command = args[:2]
        name = Path(str(executable)).name
        if name in ('mj', 'gh', 'aws', 'ssh', 'curl', 'wget'):
            raise AssertionError(f'forbidden live executable: {name}')
        words = command if isinstance(command, (list, tuple)) else [command]
        if name == 'git' and any(str(x).startswith(('http:', 'https:', 'ssh:', 'git@')) for x in words):
            raise AssertionError('forbidden network Git command')


if os.environ.get('BIFROST_INTEGRATION_NO_LIVE') == '1':
    sys.addaudithook(_no_live)
    _suite_root = tempfile.TemporaryDirectory(prefix='stabilize-review-')
    for _variable, _leaf in [('BIFROST_CI_DB', 'activity.db'), ('BIFROST_CI_MONITOR_STATE', 'monitor'),
                             ('BIFROST_CI_AUTOMERGE_STATE', 'merger'), ('BIFROST_CI_CONFIG_DIR', 'config')]:
        os.environ[_variable] = str(Path(_suite_root.name) / _leaf)

import automerge
import issue_fixer as fixer
import mm_service
import monitor
import stabilize as engine
import stabilize_state as state
import triage


SCHEMA_BASELINE_COMMIT = 'adcb4d5583759e16790d534579d386d5c2edbc1e'


def _baseline_module(name, commit=SCHEMA_BASELINE_COMMIT):
    source = subprocess.check_output(['git', 'show', f'{commit}:{name}.py'], text=True)
    module = types.ModuleType('integration_baseline_' + name)
    module.__file__ = str(Path(name + '.py').resolve())
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


class _IntegrationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch(monitor, 'DB_PATH', self.root / 'db')
        self.patch(automerge, 'DB_PATH', self.root / 'db')
        self.patch(monitor, 'LOCK_PATH', self.root / 'fixer.lock')
        self.patch(monitor, 'run_gh', side_effect=AssertionError('unexpected gh boundary'))
        self.patch(monitor, 'mj_command', side_effect=AssertionError('unexpected mj boundary'))
        self.patch(monitor, 'slack_send', return_value=(True, 'thread'))
        self.patch(automerge, 'run_gh', side_effect=AssertionError('unexpected merger gh boundary'))
        self.patch(automerge, 'SUPERVISOR_RUNNING', False)
        self.conn = monitor.connect_db()
        self.addCleanup(self.conn.close)
        other = automerge.connect_db()
        other.close()
        fixer.ensure_schema(self.conn)
        triage.ensure_schema(self.conn)
        engine.ensure_schema(self.conn)
        self.repo = self.root / 'git'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'master')
        self.git('config', 'user.name', 'Integration')
        self.git('config', 'user.email', 'integration@example.invalid')
        self.a = self.commit('base')
        self.b = self.commit('repair')
        self.git('update-ref', 'refs/heads/master', self.a)
        self.git('checkout', '-q', '--detach', self.b)
        self.patch(engine, 'ancestor', side_effect=self.ancestor)
        self.patch(automerge, 'compare_commit_ancestry', side_effect=self.ancestor)
        self.patch(automerge, 'current_master_sha', return_value=self.a)
        self.patch(automerge, 'list_pull_comments', return_value=[])
        self.patch(automerge, 'run_ci_impact', return_value={'mode': 'impact'})
        self.patch(automerge, '_known_failures_prompt', return_value='')
        self.items = {}
        self.patch(automerge, 'list_open_pull_requests', side_effect=lambda: [copy.deepcopy(p) for p in self.items.values() if p['state'] == 'open'])
        self.patch(automerge, 'gh_json', side_effect=lambda args, **kw: copy.deepcopy(self.items[int(args[-1].split('/')[-1])]))

    def patch(self, obj, name, *args, **kw):
        patcher = mock.patch.object(obj, name, *args, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, name):
        (self.repo / name).write_text(name)
        self.git('add', name)
        self.git('commit', '-qm', name)
        return self.git('rev-parse', 'HEAD')

    def ancestor(self, a, b):
        return subprocess.run(['git', '-C', str(self.repo), 'merge-base', '--is-ancestor', a, b], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def report(self, green=False):
        return automerge.FailureReport(frozenset() if green else frozenset({'CI/unit'}),
            {} if green else {'CI/unit': automerge.FailedJobDetails(frozenset({'Run tests'}), frozenset({'test::failure'}))},
            '', frozenset({'CI/unit'}) if green else frozenset(),
            {'CI/unit': frozenset({'Run tests'})} if green else {})

    def ci_run(self, number=1, head=None, green=False):
        return dict(databaseId=number, headSha=head or self.a, status='completed', conclusion='success' if green else 'failure',
                    url=f'https://example.invalid/runs/{number}', createdAt=monitor.utc_now())

    def issue(self, number):
        return dict(number=number, html_url=f'https://github.com/{monitor.REPO_NAME}/issues/{number}',
                    title=f'Failure {number}', body='Product test failure', state='open', assignees=[], labels=[{'name': 'buildfailure'}])

    def pr(self, number, branch, base, head=None, body='CI-Repair-Issue: 7\nFixes #7', labels=None):
        return dict(number=number, title=branch, body=body, state='open', draft=False,
                    html_url=f'https://github.com/{monitor.REPO_NAME}/pull/{number}',
                    head={'ref': branch, 'sha': head or self.b, 'repo': {'full_name': monitor.REPO_NAME}},
                    base={'ref': base, 'repo': {'full_name': monitor.REPO_NAME}}, labels=labels or [])

    def engine_pr(self, p):
        return dict(number=p['number'], state='MERGED' if p.get('merged') else p['state'].upper(),
                    headRefOid=p['head']['sha'], headRefName=p['head']['ref'], baseRefName=p['base']['ref'],
                    body=p['body'], isDraft=p['draft'], labels=p['labels'], mergeCommit={'oid': p['head']['sha']})

    def start(self, seed=False):
        if seed:
            monitor._process_known_failure_run(self.conn, 'CI', self.ci_run(), report=self.report())
            with self.conn:
                self.conn.execute("UPDATE known_failures SET triage_issue_url=?", (self.issue(7)['html_url'],))
        with mock.patch.object(engine, 'deliver_writes'), \
             mock.patch.object(engine, 'deliver_slack'), \
             mock.patch.object(engine, 'resolve_anchor', return_value=self.a), \
             mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(engine, 'gh', return_value={'state': 'OPEN', 'labels': [{'name': 'buildfailure'}]}):
            c = engine.start_campaign(self.conn)
            engine.advance_start(self.conn, c.id)
        self.branch = c.branch
        self.tip = self.a
        self.patch(automerge, 'remote_branch_sha', side_effect=lambda branch: self.tip if branch == self.branch else None)
        return engine.campaign(self.conn, c.id)

    def ready(self):
        c = self.start(seed=True)
        p = self.pr(22, 'repair-7', c.branch)
        self.items[22] = p
        with mock.patch.object(engine, 'pull_requests', return_value=[self.engine_pr(p)]), \
             mock.patch.object(engine, 'gh', return_value={'state': 'OPEN', 'labels': []}), \
             mock.patch.object(engine, 'api', return_value=[]):
            engine.refresh_issues(self.conn, c)
        return c, p


class IntegrationTests(_IntegrationFixture):
    def test_campaign_cross_slice_path(self):
        c = self.start(seed=True)
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=self.report()):
            engine.ingest_run(self.conn, c, 'push', self.ci_run(2))
        from test_triage import FakeGitHub
        observation = next(o for o in triage.pending(self.conn) if o.get('stabilize_campaign_id'))
        report = {'findings': [{'failure_ids': [observation['failure_id']], 'outcome': 'product',
                  'diagnosis': 'Shared cause', 'evidence': 'run 2: test::failure',
                  'issue': {'title': 'Failure 7', 'body': 'Known product cause', 'existing_number': 7}}]}
        with self.conn:
            self.conn.execute('INSERT INTO triage_jobs(id,title,base_sha,created_at,status,observations_json,session_id,report_json) VALUES (?,?,?,?,?,?,?,?)',
                              ('triage', 'integration triage', self.a, monitor.utc_now(), 'publishing',
                               json.dumps([observation]), 'fake-session', json.dumps(report)))
        github = FakeGitHub()
        github.issues[7] = self.issue(7)
        with mock.patch.object(triage, 'gh_api', side_effect=github), \
             mock.patch.object(monitor, '_sync_known_failure_issue'), \
             mock.patch.object(monitor, 'load_slack_transport', return_value=monitor.SlackTransport('chat', token='test', channel='test')):
            triage.publish(self.conn, self.conn.execute("SELECT * FROM triage_jobs WHERE id='triage'").fetchone())
        self.assertEqual(set(github.issues), {7})
        self.assertIn({'name': state.LABEL}, github.issues[7]['labels'])
        self.assertEqual(triage.branch_observations(self.conn)[0]['linked_issue_url'], self.issue(7)['html_url'])
        selected = fixer.select_work(self.conn, [self.issue(1), self.issue(7)], [])
        self.assertEqual(selected[0]['number'], 7)
        with mock.patch.object(fixer, 'api', side_effect=lambda endpoint, **kw: {'sha': self.a} if endpoint.startswith('commits/') else []):
            job = fixer.create_job(self.conn, *selected, [], None)
        self.assertEqual(job['target_branch'], c.branch)
        self.assertIn('CI-Repair-Issue: 7', job['prompt'])
        p = self.pr(22, job['branch'], c.branch)
        self.items[22] = p
        with mock.patch.object(engine, 'pull_requests', return_value=[self.engine_pr(p)]), \
             mock.patch.object(engine, 'gh', return_value={'state': 'OPEN', 'labels': []}), \
             mock.patch.object(engine, 'api', return_value=[]):
            engine.refresh_issues(self.conn, c)
        self.assertTrue(state.merge_gate_open(self.conn, c.id))
        pulls = automerge.select_eligible_pull_requests(conn=self.conn)
        self.assertEqual([p.number for p in pulls], [22])
        identifier = automerge.create_batch(self.conn, pulls, self.a, ci_mode='sync')
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()
        self.assertEqual(row['target_ref'], c.branch)
        self.assertEqual(row['ci_mode'], 'async')
        self.assertEqual(mm_service.state(self.conn, identifier)['target_ref'], c.branch)
        self.items[22].update(state='closed', merged=True, merged_at=monitor.utc_now())
        self.tip = self.b
        with mock.patch.object(monitor, 'run_gh', return_value=''), \
             mock.patch.object(engine, 'ref_sha', return_value=self.b), \
             mock.patch.object(engine, 'branch_runs', return_value=[]) as runs, \
             mock.patch.object(engine, 'gh', return_value={'state': 'OPEN', 'labels': []}), \
             mock.patch.object(engine, 'pull_requests', return_value=[self.engine_pr(self.items[22])]):
            engine.tick_campaign(self.conn, c.id)
        current = engine.campaign(self.conn, c.id)
        self.assertEqual((current.tip_sha, current.round), (self.b, 2))
        runs.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT tip_sha FROM stabilize_dispatches').fetchone()[0], self.b)
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_issues').fetchone()[0], 'landed')
        green_run = self.ci_run(3, self.b, True)
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=self.report(True)):
            engine.ingest_run(self.conn, current, 'push', green_run)
        published = []
        def gate_gh(args, **kw):
            if args[:2] == ['pr', 'list']:
                return published
            return []
        with mock.patch.object(engine, 'ref_sha', return_value=self.b), \
             mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(engine, 'gh', side_effect=gate_gh), \
             mock.patch.object(monitor, 'run_gh', return_value='https://example.invalid/pull/90'):
            engine.open_gate(self.conn, current)
            gate = self.conn.execute('SELECT * FROM stabilize_gate_merges').fetchone()
            published.append(dict(number=90, state='OPEN', headRefOid=self.b, isDraft=False, baseRefName='master'))
            engine.open_gate(self.conn, current)
        self.assertEqual(state.gate_for_branch(self.conn, gate['gate_branch'])[0]['tip_sha'], self.b)
        self.items[90] = self.pr(90, gate['gate_branch'], 'master', labels=[{'name': 'mergemarshall:high'}])
        self.items[90]['body'] = 'green gate'
        pulls = automerge.select_eligible_pull_requests(conn=self.conn)
        self.assertEqual([p.number for p in pulls], [90])
        self.assertEqual(pulls[0].dependencies, ())
        gate_id = automerge.create_batch(self.conn, pulls, self.a)
        gate_row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (gate_id,)).fetchone()
        self.assertTrue(automerge.verify_source_ancestry(gate_row, self.b, conn=self.conn)[0])


    def test_campaign_single_up_to_date_pr_allows_gated_async_direct_landing(self):
        c, p = self.ready()
        base = self.tip
        self.git('update-ref', 'refs/heads/' + c.branch, base)
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='open' WHERE campaign_id=?", (c.id,))
        self.assertFalse(state.merge_gate_open(self.conn, c.id))
        self.assertEqual(automerge.select_eligible_pull_requests(conn=self.conn), [])
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='pr_ready' WHERE campaign_id=?", (c.id,))
        self.assertTrue(state.merge_gate_open(self.conn, c.id))

        # Master advances independently; this source is current on the campaign.
        self.git('checkout', '-q', '--detach', self.a)
        master = self.commit('master-later')
        self.git('update-ref', 'refs/heads/master', master)
        cache = mock.Mock()
        cache.behind.side_effect = lambda head, target: int(self.git('rev-list', '--count', head + '..' + target))
        def merge(args, **kwargs):
            self.assertEqual(args[:3], ['pr', 'merge', '22'])
            self.assertEqual(args[-2:], ['--match-head-commit', self.b])
            self.assertEqual(p['base']['ref'], c.branch)
            self.tip = self.b
            self.git('update-ref', 'refs/heads/' + c.branch, self.tip)
            p.update(state='closed', merged=True, merged_at='now', merge_commit_sha=self.b)
        with (mock.patch.object(automerge, 'current_master_sha', return_value=master),
              mock.patch.object(automerge, '_ancestry_cache', return_value=cache),
              mock.patch.object(automerge, 'remote_branch_sha', side_effect=lambda branch: self.tip if branch == c.branch else None) as remote,
              mock.patch.object(automerge, 'direct_pull_request_view', side_effect=lambda number: self.engine_pr(p)),
              mock.patch.object(automerge, '_try_post_verdict_status', return_value=True) as verdict,
              mock.patch.object(automerge, '_direct_sync_gate', side_effect=AssertionError('campaign direct path waited for CI')),
              mock.patch.object(automerge, 'finish_batch'),
              mock.patch.object(automerge, 'run_gh', side_effect=merge) as write):
            pulls = automerge.select_eligible_pull_requests(conn=self.conn)
            self.assertEqual([pull.number for pull in pulls], [22])
            identifier = automerge.create_selected_batch(self.conn, pulls, base, ci_mode='sync')
            row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()
            self.assertEqual(row['kind'], 'direct')
            self.assertEqual(row['ci_mode'], 'async')
            self.assertEqual(row['target_ref'], c.branch)
            self.assertEqual(row['base_sha'], base)
            self.assertIsNone(row['session_id'])
            automerge._process_direct_batch(self.conn, None, row)
            write.assert_called_once()
            self.assertEqual(verdict.call_args.args[3:5], (self.b, 'success'))
            self.assertTrue(all(call.args == (c.branch,) for call in remote.call_args_list))
        self.assertTrue(cache.behind.called)
        self.assertTrue(all(call.args == (self.b, base) for call in cache.behind.call_args_list))
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()
        self.assertEqual(row['terminal_status'], 'merged')
        self.assertEqual(row['target_ref'], c.branch)
        self.assertEqual(self.git('rev-parse', c.branch), self.b)
        self.assertEqual(self.git('rev-parse', 'master'), master)


    def test_cancelled_rejection_does_not_hold_round_gate(self):
        c, p = self.ready()
        with self.conn:
            automerge.enqueue_github_write(self.conn, 'cancelled', 'reject_head', 22, self.b, {'reason': 'old rejection'})
            self.conn.execute("UPDATE automerge_github_outbox SET cancelled_at='cancelled' WHERE kind='reject_head'")
        self.assertTrue(automerge._queue_ready(copy.deepcopy(p), conn=self.conn, dry_run=False))
        with mock.patch.object(engine, 'pull_requests', return_value=[self.engine_pr(p)]), \
             mock.patch.object(engine, 'gh', return_value={'state': 'OPEN', 'labels': []}), \
             mock.patch.object(engine, 'api', return_value=[]):
            engine.refresh_issues(self.conn, c)
        self.assertTrue(state.merge_gate_open(self.conn, c.id), 'engine counts a cancelled rejection that the merger explicitly releases')


    def test_approved_policy_engine_readiness_matches_merger(self):
        c, p = self.ready()
        with mock.patch.object(automerge, 'READY_POLICY', 'approved'), \
             mock.patch.object(automerge, 'gh_json', return_value={'reviewDecision': 'REVIEW_REQUIRED'}), \
             mock.patch.object(engine, 'pull_requests', return_value=[self.engine_pr(p)]), \
             mock.patch.object(engine, 'gh', return_value={'state': 'OPEN', 'labels': []}):
            engine.refresh_issues(self.conn, c)
            pulls = automerge.select_eligible_pull_requests(conn=self.conn)
            self.assertEqual(pulls, [])
            self.assertFalse(state.merge_gate_open(self.conn, c.id),
                             'unapproved repair must not release the round gate under approved policy')
            with mock.patch.object(automerge, 'gh_json', return_value={'reviewDecision': 'APPROVED'}):
                engine.refresh_issues(self.conn, c)
                self.assertTrue(state.merge_gate_open(self.conn, c.id))
                self.assertEqual([pull.number for pull in automerge.select_eligible_pull_requests(conn=self.conn)], [22])


    def test_linked_branch_issue_has_campaign_priority_before_engine_membership(self):
        c = self.start()
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=self.report()):
            engine.ingest_run(self.conn, c, 'push', self.ci_run(2))
        o = triage.branch_observations(self.conn)[0]
        state.classify(self.conn, c.id, tuple(o[k] for k in triage.KEY), 'product', 'cause', self.issue(7)['html_url'])
        self.conn.commit()
        self.assertEqual(state.campaign_issue(self.conn, 7, self.issue(7)['html_url'])[1], 'open')
        selected = fixer.select_work(self.conn, [self.issue(1), self.issue(7)], [])
        self.assertEqual(selected[0]['number'], 7, 'triage-linked campaign issue is targeted to campaign but not prioritized until engine refresh')


    def test_recurrent_issue_is_closed_again_by_later_gate(self):
        c, p = self.ready()
        self.tip = self.b
        now = monitor.utc_now()
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='landed' WHERE issue_number=7")
            self.conn.execute('INSERT INTO stabilize_fix_commits VALUES (?,?,?)', (c.id, 7, self.b))
            self.conn.execute("INSERT INTO stabilize_gate_merges(campaign_id,stage,tip_sha,gate_branch,pr_number,state,created_at,updated_at) VALUES (?,?,?,?,90,'open',?,?)",
                              (c.id, 'push', self.b, c.branch + '-gate-push', now, now))
        with mock.patch.object(monitor, 'run_gh', return_value=''), \
             mock.patch.object(engine, 'gh', return_value={'state': 'MERGED', 'labels': [], 'headRefOid': self.b}), \
             mock.patch.object(engine, 'api', return_value=[]):
            engine.observe_gates(self.conn, c)
            engine.deliver_writes(self.conn)
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_issues').fetchone()[0], 'closed')
        # Same cause recurs on hourly, is reopened, and receives a second branch repair.
        later = self.commit('hourly-repair')
        with self.conn:
            self.conn.execute("UPDATE stabilize_campaigns SET stage='hourly',tip_sha=?", (later,))
            self.conn.execute("UPDATE stabilize_issues SET state='landed',pr_head_sha=?", (later,))
            self.conn.execute('UPDATE stabilize_fix_commits SET fix_sha=?', (later,))
            self.conn.execute("INSERT INTO stabilize_gate_merges(campaign_id,stage,tip_sha,gate_branch,pr_number,state,created_at,updated_at) VALUES (?,?,?,?,91,'open',?,?)",
                              (c.id, 'hourly', later, c.branch + '-gate-hourly', now, now))
        with mock.patch.object(engine, 'gh', return_value={'state': 'MERGED', 'labels': [], 'headRefOid': later}), \
             mock.patch.object(engine, 'api', return_value=[]) as api:
            engine.observe_gates(self.conn, engine.campaign(self.conn, c.id))
            engine.deliver_writes(self.conn)
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_issues').fetchone()[0], 'closed', 'per-issue close checkpoint suppresses a later gate close after recurrence')


    def test_baseline_schema_upgrades_through_all_entry_points(self):
        old_monitor = _baseline_module('monitor')
        old_merger = _baseline_module('automerge')
        old_fixer = _baseline_module('issue_fixer')
        old_triage = _baseline_module('triage')
        old_monitor.DB_PATH = self.root / 'migration.db'
        old_merger.DB_PATH = old_monitor.DB_PATH
        conn = old_monitor.connect_db()
        old_fixer.ensure_schema(conn)
        old_triage.ensure_schema(conn)
        conn.close()
        conn = old_merger.connect_db()
        self.assertNotIn('target_ref', {r[1] for r in conn.execute('PRAGMA table_info(automerge_batches)')})
        self.assertNotIn('target_branch', {r[1] for r in conn.execute('PRAGMA table_info(issue_repairs)')})
        with conn:
            conn.execute("INSERT INTO known_failure_state(key,value) VALUES ('migration-sentinel','kept')")
            conn.execute("INSERT INTO issue_repairs(id,work_key,issue_number,issue_url,title,branch,base_sha,created_at,prompt) VALUES ('legacy','legacy',7,'issue','legacy','legacy',?,'now','private prompt')", (self.a,))
        old_merger.create_batch(conn, [old_merger.PullRequest(99, 'legacy', self.b, 'url')], self.a, batch_id='legacy')
        conn.close()
        with mock.patch.object(monitor, 'DB_PATH', old_monitor.DB_PATH), \
             mock.patch.object(automerge, 'DB_PATH', old_monitor.DB_PATH):
            conn = monitor.connect_db()
            conn.close()
            conn = automerge.connect_db()
            fixer.ensure_schema(conn)
            triage.ensure_schema(conn)
            mm_service.ensure_schema(conn)
            engine.ensure_schema(conn)
        self.addCleanup(conn.close)
        self.assertIn('target_ref', {r[1] for r in conn.execute('PRAGMA table_info(automerge_batches)')})
        self.assertIn('target_branch', {r[1] for r in conn.execute('PRAGMA table_info(issue_repairs)')})
        self.assertEqual(conn.execute("SELECT value FROM known_failure_state WHERE key='migration-sentinel'").fetchone()[0], 'kept')
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertIsNone(conn.execute("SELECT target_ref FROM automerge_batches WHERE batch_id='legacy'").fetchone()[0])
        self.assertIsNone(conn.execute("SELECT target_branch FROM issue_repairs WHERE id='legacy'").fetchone()[0])


    def test_gate_accepts_proven_merged_repair_of_rejected_ancestor(self):
        c, p = self.ready()
        self.assertEqual([p.number for p in automerge.select_eligible_pull_requests(conn=self.conn)], [22])
        repaired = self.commit('repaired-rejected-head')
        with self.conn:
            automerge.enqueue_github_write(self.conn, 'rejection', 'reject_head', 22, self.b, {'reason': 'bad old head'})
            self.conn.execute("UPDATE stabilize_campaigns SET tip_sha=? WHERE id=?", (repaired, c.id))
            now = monitor.utc_now()
            self.conn.execute("INSERT INTO stabilize_gate_merges(campaign_id,stage,tip_sha,gate_branch,pr_number,state,created_at,updated_at) VALUES (?,?,?,?,90,'open',?,?)",
                              (c.id, 'push', repaired, c.branch + '-gate-push', now, now))
        self.items[22]['head']['sha'] = repaired
        self.items[22].update(state='closed', merged=True, merged_at=now, merge_commit_sha=repaired)
        self.tip = repaired
        self.items[90] = self.pr(90, c.branch + '-gate-push', 'master', head=repaired, body='green gate', labels=[{'name': 'mergemarshall:high'}])
        pulls = automerge.select_eligible_pull_requests(conn=self.conn)
        self.assertEqual([p.number for p in pulls], [90])
        self.assertEqual(pulls[0].dependencies, ())
        identifier = automerge.create_batch(self.conn, pulls, self.a)
        row = self.conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (identifier,)).fetchone()
        self.assertTrue(automerge.verify_source_ancestry(row, repaired, conn=self.conn)[0])


    def test_pending_draft_intent_is_not_engine_ready(self):
        c, p = self.ready()
        with self.conn:
            automerge.enqueue_github_write(self.conn, 'draft', 'draft_changed_head', 22, self.b, {})
        self.assertFalse(automerge._queue_ready(copy.deepcopy(p), conn=self.conn, dry_run=False))
        with mock.patch.object(engine, 'pull_requests', return_value=[self.engine_pr(p)]), \
             mock.patch.object(engine, 'gh', return_value={'state': 'OPEN', 'labels': []}), \
             mock.patch.object(engine, 'api', return_value=[]):
            engine.refresh_issues(self.conn, c)
        self.assertFalse(state.merge_gate_open(self.conn, c.id), 'pending draft prevents merger eligibility but not engine readiness')


    def test_delayed_gate_close_preserves_new_recurrence(self):
        c, p = self.ready()
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='landed'")
            self.conn.execute('INSERT INTO stabilize_fix_commits VALUES (?,?,?)', (c.id, 7, self.b))
            now = monitor.utc_now()
            self.conn.execute("INSERT INTO stabilize_gate_merges(campaign_id,stage,tip_sha,gate_branch,pr_number,state,created_at,updated_at) VALUES (?,?,?,?,90,'open',?,?)",
                              (c.id, 'push', self.b, c.branch + '-gate-push', now, now))
        with mock.patch.object(engine, 'gh', return_value={'state': 'MERGED', 'labels': [], 'headRefOid': self.b}):
            engine.observe_gates(self.conn, c)
        later = self.commit('new-failure')
        with self.conn:
            self.conn.execute("UPDATE stabilize_campaigns SET tip_sha=?,round=2 WHERE id=?", (later, c.id))
        current = engine.campaign(self.conn, c.id)
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=self.report()):
            engine.ingest_run(self.conn, current, 'push', self.ci_run(3, later))
        p.update(state='closed', merged=True)
        with mock.patch.object(engine, 'pull_requests', return_value=[self.engine_pr(p)]), \
             mock.patch.object(engine, 'gh', return_value={'state': 'OPEN', 'labels': []}):
            engine.refresh_issues(self.conn, current)
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_issues').fetchone()[0], 'open')
        with mock.patch.object(monitor, 'run_gh', return_value=''), \
             mock.patch.object(engine, 'api', return_value=[]):
            engine.deliver_writes(self.conn)
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_issues').fetchone()[0], 'open',
                         'queued close for an older gate must not close a reopened failure at a later tip')
