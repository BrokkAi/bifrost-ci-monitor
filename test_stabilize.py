"""Campaign state-machine tests. Every fixture uses local SQLite and mocked external I/O."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import automerge
import git_ancestry
import monitor
import stabilize as engine
import stabilize_state as state

REAL_ANCESTOR = engine.ancestor
A, B, C = 'a' * 40, 'b' * 40, 'c' * 40


def run(number=1, sha=A, stage='push', conclusion='failure', status='completed'):
    return dict(databaseId=number, headSha=sha, headBranch='stabilize/test', event='workflow_dispatch',
                status=status, conclusion=conclusion, url=f'https://example/runs/{number}',
                createdAt=monitor.utc_now(), workflowName=state.STAGE_WORKFLOWS[stage])


def report(tests=('test::old',), *, successful=False, incomplete=False, step='Run tests', jobs=('CI/unit',)):
    return automerge.FailureReport(frozenset() if successful else frozenset(jobs),
        {} if successful else {j: automerge.FailedJobDetails(frozenset({step}), frozenset(tests)) for j in jobs},
        '', frozenset(jobs) if successful else frozenset(),
        {j: frozenset({step}) for j in jobs} if successful else {},
        frozenset(jobs) if incomplete else frozenset())


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'activity.db'
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA synchronous=OFF')
        self.addCleanup(self.conn.close)
        engine.ensure_schema(self.conn)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(monitor, 'DB_PATH', self.db).start()
        mock.patch.object(engine, 'LOCK_PATH', Path(self.temp.name) / 'stabilize.lock').start()
        mock.patch.object(monitor, 'CHANNEL_PATH', Path(self.temp.name) / 'channel').start()
        self.gh = mock.patch.object(monitor, 'run_gh', return_value='[]').start()
        self.master = mock.patch.object(automerge, 'current_master_sha', return_value=A).start()
        self.ancestry = mock.patch.object(engine, 'ancestor', side_effect=lambda a, b: a == b or (a, b) in {(A, B), (A, C), (B, C)}).start()
        mock.patch.object(monitor, 'slack_send', return_value=(True, '123.456')).start()
        mock.patch.object(monitor, 'load_slack_transport', return_value=monitor.SlackTransport('chat', token='test', channel='C1')).start()

    def campaign(self, stage='push', sha=A, status='running', seeded=1):
        now = monitor.utc_now()
        with self.conn:
            self.conn.execute('INSERT INTO stabilize_campaigns(id,branch,anchor_sha,tip_sha,stage,status,round,created_at,updated_at) '
                              'VALUES (?,?,?,?,?,?,1,?,?)', ('test', 'stabilize/test', A, sha, stage, status, now, now))
            self.conn.execute('INSERT INTO stabilize_runtime(campaign_id,seeded,stage_rounds) VALUES (?, ?,1)', ('test', seeded))
        return engine.campaign(self.conn, 'test')

    def current(self):
        return engine.campaign(self.conn, 'test')

    def seed_row(self, number=7, first=A, outcome=None, workflow='CI'):
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()):
            monitor._process_known_failure_run(self.conn, workflow, run(number, first))
        with self.conn:
            self.conn.execute('UPDATE known_failures SET triage_issue_url=?,triage_outcome=? WHERE workflow=?',
                              (f'https://github.com/BrokkAi/bifrost-dev/issues/{number}', outcome, workflow))
        return self.conn.execute('SELECT * FROM known_failures WHERE workflow=?', (workflow,)).fetchone()

    def issue(self, c, number=7, status='open', stage='push', evidence=None):
        evidence = evidence or dict(self.seed_row(number))
        engine.add_issue(self.conn, c, number, stage, 'seeded', [evidence])
        with self.conn:
            self.conn.execute('UPDATE stabilize_issues SET state=? WHERE campaign_id=? AND issue_number=?',
                              (status, c.id, number))

    def issue_state(self, number=7):
        return self.conn.execute('SELECT state FROM stabilize_issues WHERE issue_number=?', (number,)).fetchone()[0]

    def pr(self, number=22, status='OPEN', sha=B, draft=False):
        return dict(number=number, state=status, headRefOid=sha, headRefName='repair', baseRefName='stabilize/test',
                    body='CI-Repair-Issue: 7\nFixes #7', isDraft=draft, mergeCommit={'oid': sha}, labels=[])

    def issue_reads(self, prs=None, status='OPEN', labels=None, assignees=None):
        def read(args, **kwargs):
            if args[:2] == ['issue', 'view']:
                return json.dumps({'state': status, 'labels': [{'name': l} for l in (labels or ['buildfailure'])],
                                   'assignees': [{'login': login} for login in (assignees or [])]})
            if args[:2] == ['pr', 'list']:
                return json.dumps(prs or [])
            return '[]'
        self.gh.side_effect = read

    def guidance_writes(self):
        return self.conn.execute("SELECT * FROM stabilize_writes WHERE kind='assignee-guidance'").fetchall()

    def test_assignee_guidance_once_per_set_with_branch_trailers(self):
        c = self.campaign()
        self.issue(c)
        self.issue_reads(assignees=['DavidBakerEffendi', 'Alice', 'brokk-service', 'mergemarshall[bot]'])
        engine.assignee_guidance(self.conn, c.id)
        self.issue_reads(assignees=['alice', 'DAVIDBAKEREFFENDI', 'BROKK-SERVICE'])
        engine.assignee_guidance(self.conn, c.id)
        rows = self.guidance_writes()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]['payload'])
        self.assertEqual(payload['assignees'], ['alice', 'davidbakereffendi'])
        for text in ('@alice', '@davidbakereffendi', '`stabilize/test`', 'Fixes #7',
                     'CI-Repair-Issue: 7', 'unassign yourself', "MergeMarshall's fixer"):
            self.assertIn(text, payload['comment'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_messages').fetchone()[0], 0)
        self.issue_reads(assignees=['Bob'])
        engine.assignee_guidance(self.conn, c.id)
        self.assertEqual(len(self.guidance_writes()), 2)

    def test_assignee_guidance_ignores_service_bot_and_unassigned(self):
        c = self.campaign()
        self.issue(c)
        for owners in ([], ['brokk-service'], ['mergemarshall[bot]'], ['BROKK-SERVICE', 'mergemarshall[bot]']):
            self.issue_reads(assignees=owners)
            engine.assignee_guidance(self.conn, c.id)
        self.assertFalse(self.guidance_writes())

    def test_assignee_guidance_checkpoint_scoped_to_campaign(self):
        c = self.campaign()
        self.issue(c)
        self.issue_reads(assignees=['David'])
        engine.assignee_guidance(self.conn, c.id)
        with self.conn:
            self.conn.execute("UPDATE stabilize_campaigns SET status='completed'")
            self.conn.execute("INSERT INTO stabilize_campaigns(id,branch,anchor_sha,tip_sha,status,created_at,updated_at) "
                              "SELECT 'next','stabilize/next',anchor_sha,tip_sha,'running',created_at,updated_at "
                              "FROM stabilize_campaigns WHERE id='test'")
        next_campaign = engine.campaign(self.conn, 'next')
        self.issue(next_campaign)
        engine.assignee_guidance(self.conn, next_campaign.id)
        self.assertEqual(len(self.guidance_writes()), 2)
        self.assertIn('`stabilize/next`', json.loads(self.guidance_writes()[1]['payload'])['comment'])

    def test_assignee_guidance_delivery_fences_campaign_and_member_state(self):
        c = self.campaign()
        self.issue(c)
        self.issue_reads(assignees=['David'])
        engine.assignee_guidance(self.conn, c.id)
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='pr_ready'")
        engine.deliver_writes(self.conn)
        self.assertIsNotNone(self.guidance_writes()[0]['cancelled_at'])
        self.assertFalse(any('/comments' in str(call.args[0]) for call in self.gh.call_args_list))

    def test_assignee_guidance_asks_escalation_owner_for_campaign_branch_pr(self):
        c = self.campaign()
        self.issue(c, status='escalated')
        self.issue_reads(assignees=['DavidBakerEffendi'])
        engine.assignee_guidance(self.conn, c.id)
        rows = self.guidance_writes()
        self.assertEqual(len(rows), 1)
        comment = json.loads(rows[0]['payload'])['comment']
        for text in ('@davidbakereffendi', '`stabilize/test`', 'Fixes #7', 'CI-Repair-Issue: 7', 'escalated this issue'):
            self.assertIn(text, comment)
        self.assertNotIn('unassign yourself', comment)
        engine.deliver_writes(self.conn)
        self.assertIsNone(self.guidance_writes()[0]['cancelled_at'])
        self.assertTrue(any('/comments' in str(call.args[0]) for call in self.gh.call_args_list))

    def test_assignee_guidance_requires_live_open_member_and_github_issue(self):
        c = self.campaign()
        self.issue(c)
        self.issue_reads(assignees=['David'], status='CLOSED')
        engine.assignee_guidance(self.conn, c.id)
        self.issue_reads(assignees=['David'])
        for status in ('pr_ready', 'landed', 'closed', 'dropped'):
            with self.conn:
                self.conn.execute('UPDATE stabilize_issues SET state=?', (status,))
            engine.assignee_guidance(self.conn, c.id)
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='open'")
            self.conn.execute("UPDATE stabilize_campaigns SET status='completed'")
        engine.assignee_guidance(self.conn, c.id)
        self.assertFalse(self.guidance_writes())

    def test_assignee_guidance_delivered_once_and_retry_adopts_bot_comment(self):
        c = self.campaign(status='paused')
        self.issue(c)
        self.issue_reads(assignees=['David'])
        engine.assignee_guidance(self.conn, c.id)
        read = self.gh.side_effect
        comments = []
        posted = []
        def external(args, **kwargs):
            if args[0] == 'api' and '/comments' in args[1]:
                if '--method' not in args:
                    return json.dumps([comments])
                body = next(arg[5:] for arg in args if arg.startswith('body='))
                comments.append({'body': body, 'user': {'login': 'mergemarshall[bot]'}})
                posted.append(body)
                raise monitor.CommandError('accepted but response lost')
            return read(args, **kwargs)
        self.gh.side_effect = external
        engine.deliver_writes(self.conn)
        self.assertIsNone(self.guidance_writes()[0]['done_at'])
        engine.deliver_writes(self.conn)
        engine.assignee_guidance(self.conn, c.id)
        engine.deliver_writes(self.conn)
        self.assertEqual(len(posted), 1)
        self.assertIsNotNone(self.guidance_writes()[0]['done_at'])

    def test_assignee_guidance_cancels_pending_comment_after_unassignment(self):
        c = self.campaign()
        self.issue(c)
        self.issue_reads(assignees=['David'])
        engine.assignee_guidance(self.conn, c.id)
        self.issue_reads(assignees=['brokk-service'])
        engine.deliver_writes(self.conn)
        self.assertIsNotNone(self.guidance_writes()[0]['cancelled_at'])
        self.assertFalse(any('/comments' in str(call.args[0]) for call in self.gh.call_args_list))
        engine.assignee_guidance(self.conn, c.id)
        self.assertEqual(len(self.guidance_writes()), 1)

    def test_assignee_guidance_tick_queues_and_delivers_without_slack_notice(self):
        c = self.campaign(status='paused')
        self.issue(c)
        self.issue_reads(assignees=['David'])
        with mock.patch.object(engine, 'deliver_slack'), mock.patch.object(engine, 'tick_campaign'), \
             mock.patch.object(engine, 'notify') as notify:
            engine.run_campaign_tick(self.conn, c.id)
            engine.run_campaign_tick(self.conn, c.id)
        self.assertEqual(len(self.guidance_writes()), 1)
        self.assertIsNotNone(self.guidance_writes()[0]['done_at'])
        posts = [call for call in self.gh.call_args_list
                 if '/comments' in str(call.args[0]) and 'POST' in call.args[0]]
        self.assertEqual(len(posts), 1)
        notify.assert_not_called()

    def test_assignee_guidance_read_failure_does_not_block_other_tick_work(self):
        c = self.campaign()
        self.issue(c)
        self.gh.side_effect = monitor.CommandError('read unavailable')
        with mock.patch.object(engine, 'deliver_slack'), mock.patch.object(engine, 'tick_campaign'), \
             mock.patch.object(engine, 'deliver_writes') as deliver:
            engine.run_campaign_tick(self.conn, c.id)
        deliver.assert_called_once_with(self.conn, campaign_id=c.id)
        self.assertFalse(self.guidance_writes())

    def test_anchor_newest_completed_non_cancelled_push(self):
        items = [dict(run(1), headBranch='master', event='push', createdAt='2026-01-01'),
                 dict(run(2, B), headBranch='master', event='push', createdAt='2026-01-02'),
                 dict(run(3, C, conclusion='cancelled'), headBranch='master', event='push', createdAt='2026-01-03'),
                 dict(run(4, C), headBranch='other', event='push')]
        with mock.patch.object(monitor, '_known_failure_runs', return_value=items):
            self.assertEqual(engine.resolve_anchor(), B)
        self.gh.return_value = json.dumps({'sha': C})
        self.assertEqual(engine.resolve_anchor('v1'), C)
        self.assertIn('commits/v1', self.gh.call_args.args[0][1])

    def test_resolve_read_only_details_and_unknown_ref(self):
        initial = self.db.read_bytes()
        def read(args, **kwargs):
            self.assertNotIn('--method', args)
            if args[0] == 'api':
                if 'compare/' in args[1]:
                    return json.dumps({'status': 'ahead'})
                return json.dumps({'sha': B if args[1].endswith('master') else A,
                    'commit': {'message': 'Fix the tests\nDetails', 'committer': {'date': '2026-10-01'}}})
            return json.dumps([dict(run(9), headBranch='master', event='push')])
        self.gh.side_effect = read
        with mock.patch.object(monitor, 'git_cache') as cache:
            result = engine.resolve('tag')
        cache.assert_not_called()
        self.assertEqual(result['subject'], 'Fix the tests')
        self.assertTrue(result['on_master'])
        self.assertEqual(result['push_ci']['run_id'], 9)
        self.assertEqual(self.db.read_bytes(), initial)
        self.gh.side_effect = monitor.CommandError('HTTP 404 Not Found')
        with self.assertRaises(engine.UnknownRef):
            engine.resolve('missing')

    def test_create_idempotency_and_second_live_request(self):
        with mock.patch.object(engine, 'ensure_ref', return_value=True) as create_ref:
            first = engine.create(A, 'operator', 'request-1')
            again = engine.create(B, 'someone else', 'request-1')
            self.assertEqual(first, again)
            self.assertEqual(first['status'], 'running')
            with self.assertRaises(engine.LiveCampaignError):
                engine.create(B, 'operator', 'request-2')
            create_ref.assert_called_once()
            with self.conn:
                self.conn.execute("UPDATE stabilize_campaigns SET status='completed'")
            terminal = engine.create(A, 'operator', 'request-1')
            self.assertEqual(terminal['status'], 'completed')
        self.assertEqual(engine.get(first['id'])['id'], first['id'])
        self.assertEqual(len(engine.list_campaigns()), 1)
        self.assertIsNone(engine.get('missing'))
        json.dumps(engine.get(first['id']))

    def test_create_ambiguous_failure_keeps_starting_intent(self):
        def failing(branch, sha):
            row = self.conn.execute('SELECT * FROM stabilize_campaigns').fetchone()
            self.assertEqual(row['branch'], branch)
            raise monitor.CommandError('lost response')
        with mock.patch.object(engine, 'ensure_ref', side_effect=failing):
            result = engine.create(A, 'operator', 'request')
        self.assertEqual(result['status'], 'starting')
        self.assertIn('lost response', result['start_error'])

    def test_ambiguous_ref_adopts_only_exact_anchor(self):
        self.gh.side_effect = monitor.CommandError('lost create response')
        with mock.patch.object(engine, 'ref_sha', side_effect=[None, A]):
            self.assertTrue(engine.ensure_ref('stabilize/test', A))
        with mock.patch.object(engine, 'ref_sha', side_effect=[None, B]):
            self.assertFalse(engine.ensure_ref('stabilize/test', A))
        with mock.patch.object(engine, 'ref_sha', return_value=B):
            with self.assertRaises(ValueError):
                engine.ensure_ref('stabilize/test', A)

    def test_lock_wait_and_nonblocking_tick(self):
        with mock.patch.object(engine.fcntl, 'flock', side_effect=[BlockingIOError, None, None]), \
             mock.patch.object(engine.time, 'sleep') as sleep:
            with engine.lock(wait_seconds=120) as acquired:
                self.assertTrue(acquired)
            sleep.assert_called_once()
        with mock.patch.object(engine.fcntl, 'flock', side_effect=BlockingIOError):
            with engine.lock() as acquired:
                self.assertFalse(acquired)
        with mock.patch.object(engine, 'lock', wraps=engine.lock) as lock, \
             mock.patch.object(engine, 'ensure_ref', return_value=True):
            engine.create(A, 'operator', 'key')
        lock.assert_called_once_with(wait_seconds=120)

    def test_seeding_ancestry_and_deferred_retry(self):
        c = self.campaign(sha=B, seeded=0)
        self.seed_row(first=A)
        self.issue_reads()
        self.ancestry.side_effect = git_ancestry.Deferred('history pending')
        self.assertFalse(engine.seed_issues(self.conn, c))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_issues').fetchone()[0], 0)
        self.ancestry.side_effect = None
        self.ancestry.return_value = True
        self.assertTrue(engine.seed_issues(self.conn, c))
        self.assertEqual(self.issue_state(), 'open')
        self.assertEqual(self.conn.execute('SELECT stage FROM stabilize_issues').fetchone()[0], 'push')
        self.assertIn('red-since', self.conn.execute("SELECT text FROM stabilize_messages WHERE checkpoint='seeded:0'").fetchone()[0])

    def test_seed_excludes_non_ancestor(self):
        c = self.campaign()
        self.seed_row(first=B)
        self.issue_reads()
        self.assertTrue(engine.seed_issues(self.conn, c))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_issues').fetchone()[0], 0)

    def test_seed_excludes_infrastructure_and_closed_or_non_buildfailure(self):
        c = self.campaign()
        self.seed_row(outcome='infrastructure')
        self.issue_reads()
        engine.seed_issues(self.conn, c)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_issues').fetchone()[0], 0)
        with self.conn:
            self.conn.execute('UPDATE known_failures SET triage_outcome=NULL')
        for status, labels in [('CLOSED', ['buildfailure']), ('OPEN', [])]:
            self.issue_reads(status=status, labels=labels or ['other'])
            engine.seed_issues(self.conn, c)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_issues').fetchone()[0], 0)

    def test_dispatch_intent_discovery_and_no_double_hourly(self):
        c = self.campaign(stage='hourly')
        def dispatch(args, **kwargs):
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 1)
            raise monitor.CommandError('ambiguous dispatch')
        self.gh.side_effect = dispatch
        with mock.patch.object(engine, 'ref_sha', return_value=A):
            engine.ensure_dispatch(self.conn, c, 'hourly', [])
            engine.ensure_dispatch(self.conn, c, 'hourly', [])
            item = run(10, stage='hourly', status='in_progress')
            engine.ensure_dispatch(self.conn, c, 'hourly', [item])
        self.gh.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT run_id FROM stabilize_dispatches').fetchone()[0], 10)

    def test_undiscovered_dispatch_grace_never_duplicates(self):
        c = self.campaign(stage='hourly')
        with mock.patch.object(engine, 'ref_sha', return_value=A):
            engine.ensure_dispatch(self.conn, c, 'hourly', [])
            with self.conn:
                self.conn.execute("UPDATE stabilize_dispatches SET intent_at='2026-01-01T00:00:00Z'")
            engine.ensure_dispatch(self.conn, c, 'hourly', [])
        self.gh.assert_called_once()
        self.assertEqual(self.current().status, 'paused')

    def test_ingestion_scoped_once_and_retirement_proof(self):
        c = self.campaign(sha=B)
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()) as collect:
            engine.ingest_run(self.conn, c, 'push', run(1, B))
            engine.ingest_run(self.conn, c, 'push', run(1, B))
        collect.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM known_failures').fetchone()[0], 0)
        for number, value in [(2, report((), step='Checkout')), (3, report(('test::other',), incomplete=True))]:
            with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=value):
                engine.ingest_run(self.conn, c, 'push', run(number, B))
            self.assertEqual(self.conn.execute("SELECT status FROM stabilize_failures WHERE identity='test::old'").fetchone()[0], 'open')
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(('test::new',))):
            engine.ingest_run(self.conn, c, 'push', run(4, B))
        self.assertEqual(self.conn.execute("SELECT status FROM stabilize_failures WHERE identity='test::old'").fetchone()[0], 'fixed')
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(successful=True)):
            engine.ingest_run(self.conn, c, 'push', run(5, B, conclusion='success'))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stabilize_failures WHERE status='open'").fetchone()[0], 0)

    def test_gate_only_run_not_green_and_cancelled_run_teaches_nothing(self):
        c = self.campaign()
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(successful=True, jobs=('CI/Gate',))):
            engine.ingest_run(self.conn, c, 'push', run(1, conclusion='success'))
        self.assertFalse(self.conn.execute('SELECT green FROM stabilize_runs').fetchone()[0])
        with mock.patch.object(automerge, 'collect_failure_report_for_run') as collect:
            engine.ingest_run(self.conn, c, 'push', run(2, conclusion='cancelled'))
        collect.assert_not_called()
        with mock.patch.object(engine, 'ref_sha', return_value=A):
            engine.ensure_dispatch(self.conn, c, 'push', [run(2, conclusion='cancelled')])
        self.assertEqual(self.conn.execute('SELECT reason FROM stabilize_dispatches').fetchone()[0], 'cancelled')

    def test_issue_ready_landed_recurrence_retry_and_gate_open(self):
        c = self.campaign(sha=B)
        self.issue(c)
        self.issue_reads([self.pr()])
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'pr_ready')
        self.assertTrue(state.merge_gate_open(self.conn, c.id))
        self.issue_reads([self.pr(status='MERGED')])
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'landed')
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()):
            engine.ingest_run(self.conn, c, 'push', run(99, B))
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'open')
        self.issue_reads([self.pr(status='MERGED'), self.pr(number=23, sha=C)])
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'pr_ready')

    def test_issue_escalated_closed_dropped(self):
        c = self.campaign(sha=B)
        self.issue(c)
        self.issue_reads(labels=['Escalated'])
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'escalated')
        self.issue_reads()
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'open')
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(successful=True)):
            engine.ingest_run(self.conn, c, 'push', run(10, B, conclusion='success'))
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'dropped')
        self.issue_reads(status='CLOSED')
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'closed')

    def test_observed_issue_membership(self):
        c = self.campaign(stage='hourly', sha=B)
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(jobs=('Hourly CI/unit',))):
            engine.ingest_run(self.conn, c, 'hourly', run(11, B, stage='hourly'))
        with self.conn:
            self.conn.execute("UPDATE stabilize_failures SET linked_issue_url='https://github.com/BrokkAi/bifrost-dev/issues/7'")
        self.issue_reads()
        engine.refresh_issues(self.conn, c)
        membership = self.conn.execute('SELECT * FROM stabilize_issues').fetchone()
        self.assertEqual((membership['stage'], membership['source'], membership['round']), ('hourly', 'observed', 1))

    def test_rejected_exact_head_does_not_release_round_gate(self):
        c = self.campaign(sha=B)
        self.issue(c)
        rejected = self.pr()
        rejected['labels'] = [{'name': 'mergemarshall:rejected'}]
        self.issue_reads([rejected])
        original = self.gh.side_effect
        def read(args, **kwargs):
            if args[0] == 'api' and any('comments' in arg for arg in args):
                return json.dumps([[{'user': {'login': 'mergemarshall[bot]'},
                    'body': f'mergemarshall:rejected-head: {B}', 'created_at': monitor.utc_now()}]])
            return original(args, **kwargs)
        self.gh.side_effect = read
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'open')
        self.assertFalse(state.merge_gate_open(self.conn, c.id))
        self.issue_reads([self.pr(sha=C)])
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'pr_ready')

    def test_recorded_rejection_is_also_a_blocker(self):
        c = self.campaign(sha=B)
        self.issue(c)
        automerge.ensure_github_outbox_schema(self.conn)
        with self.conn:
            automerge.enqueue_github_write(self.conn, 'batch', 'reject_head', 22, B, {'reason': 'broken head'})
        self.issue_reads([self.pr()])
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'open')

    def test_new_tip_dispatches_every_earlier_stage(self):
        self.campaign(stage='nightly')
        with mock.patch.object(engine, 'ref_sha', return_value=B), \
             mock.patch.object(engine, 'branch_runs', return_value=[]), \
             mock.patch.object(engine, 'refresh_issues'), mock.patch.object(engine, 'observe_gates'):
            engine.tick_campaign(self.conn, 'test')
        dispatches = self.conn.execute('SELECT stage,tip_sha FROM stabilize_dispatches').fetchall()
        self.assertEqual({(r['stage'],r['tip_sha']) for r in dispatches}, {(s,B) for s in state.STAGES})
        self.assertEqual(self.current().round, 2)

    def test_incomplete_infrastructure_redispatch_cap(self):
        c = self.campaign()
        with mock.patch.object(engine, 'ref_sha', return_value=A):
            engine.ensure_dispatch(self.conn, c, 'push', [])
            for number in (1, 2, 3):
                item = run(number)
                with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(incomplete=True)):
                    engine.ingest_run(self.conn, c, 'push', item)
                engine.ensure_dispatch(self.conn, c, 'push', [item])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 3)
        self.assertEqual(self.current().status, 'paused')
        self.assertIn('two redispatches', self.conn.execute('SELECT pause_reason FROM stabilize_campaigns').fetchone()[0])

    def test_classified_infrastructure_redispatches(self):
        c = self.campaign()
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()):
            engine.ingest_run(self.conn, c, 'push', run(1))
        state.classify(self.conn, c.id, ('CI','unit','test','test::old'), 'infrastructure', 'provider failed')
        self.conn.commit()
        with mock.patch.object(engine, 'ref_sha', return_value=A):
            engine.ensure_dispatch(self.conn, c, 'push', [run(1)])
        self.assertEqual(self.conn.execute('SELECT reason FROM stabilize_dispatches').fetchone()[0], 'infrastructure')

    def test_gate_pr_creation_reconciles_ambiguous_response_and_one_open_rule(self):
        c = self.campaign(sha=B)
        self.gh.side_effect = lambda args, **kwargs: '[]' if args[:2] == ['pr', 'list'] else ''
        with mock.patch.object(engine, 'ref_sha', return_value=None), mock.patch.object(engine, 'ensure_ref', return_value=True):
            self.assertFalse(engine.open_gate(self.conn, c))
            self.assertFalse(engine.open_gate(self.conn, c))
        created = [x for x in self.gh.call_args_list if x.args[0][:2] == ['pr', 'create']]
        self.assertEqual(len(created), 1)
        self.gh.return_value = json.dumps([dict(number=40, state='OPEN', headRefOid=B, baseRefName='master', isDraft=False)])
        self.gh.side_effect = None
        with mock.patch.object(engine, 'ref_sha', return_value=B), mock.patch.object(engine, 'ensure_ref', return_value=True):
            self.assertTrue(engine.open_gate(self.conn, c))
            self.assertEqual(self.current().stage, 'hourly')
            self.assertFalse(engine.open_gate(self.conn, self.current()))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_gate_merges').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT pr_number FROM stabilize_gate_merges').fetchone()[0], 40)

    def gate(self, c, stage='push', tip=B, number=40):
        with self.conn:
            self.conn.execute('INSERT INTO stabilize_gate_merges(campaign_id,stage,tip_sha,gate_branch,pr_number,state,created_at,updated_at) '
                'VALUES (?,?,?,?,?,\'open\',?,?)', (c.id, stage, tip, c.branch + '-gate-' + stage, number, monitor.utc_now(), monitor.utc_now()))

    def test_gate_merge_closes_only_included_landed_issues(self):
        c = self.campaign(sha=C)
        self.issue(c, status='landed')
        with self.conn:
            self.conn.execute('INSERT INTO stabilize_fix_commits VALUES (?,?,?)', (c.id,7,B))
        self.gate(c)
        self.gh.return_value = json.dumps({'state':'MERGED','labels':[], 'headRefOid': B})
        engine.observe_gates(self.conn, c)
        close = self.conn.execute("SELECT * FROM stabilize_writes WHERE kind='close'").fetchone()
        self.assertIn('gate PR #40', json.loads(close['payload'])['comment'])
        self.gh.side_effect = lambda args, **kwargs: '[]' if 'comments' in args[1] else '{}'
        engine.deliver_writes(self.conn)
        self.assertEqual(self.issue_state(), 'closed')
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_gate_merges').fetchone()[0], 'merged')

    def test_nightly_merge_completes_and_reports_remaining_branch_prs(self):
        c = self.campaign(stage='nightly', sha=B)
        self.gate(c, stage='nightly')
        self.gh.side_effect = lambda args, **kwargs: json.dumps({'state':'MERGED', 'labels':[]}) if args[:2] == ['pr','view'] else json.dumps([{'number':99,'url':'https://example/pr/99'}])
        engine.observe_gates(self.conn, c)
        self.assertEqual(self.current().status, 'completed')
        self.assertIn('https://example/pr/99', self.conn.execute("SELECT text FROM stabilize_messages WHERE checkpoint='completed:0'").fetchone()[0])

    def test_gate_rejection_pauses_and_abort_keeps_branches_and_prs(self):
        c = self.campaign(sha=B)
        self.gate(c)
        self.gh.return_value = json.dumps({'state':'OPEN', 'labels':[{'name':'mergemarshall:rejected'}]})
        engine.observe_gates(self.conn, c)
        self.assertEqual(self.current().status, 'paused')
        self.gh.reset_mock()
        engine.abort_campaign(self.conn, c.id)
        engine.abort_campaign(self.conn, c.id)
        self.assertEqual(self.current().status, 'aborted')
        self.gh.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_gate_merges').fetchone()[0], 1)

    def test_eight_round_limit(self):
        self.campaign()
        with self.conn:
            self.conn.execute('UPDATE stabilize_runtime SET stage_rounds=8')
        with mock.patch.object(engine, 'ref_sha', return_value=B), mock.patch.object(engine, 'observe_gates'):
            engine.tick_campaign(self.conn, 'test')
        self.assertEqual(self.current().status, 'paused')

    def test_only_escalated_blockers_pause(self):
        c = self.campaign()
        self.issue(c, status='escalated')
        self.issue_reads(labels=['Escalated'])
        item = run(1)
        with mock.patch.object(engine, 'ref_sha', return_value=A), mock.patch.object(engine, 'branch_runs', return_value=[item]), \
             mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()), mock.patch.object(engine, 'observe_gates'):
            engine.tick_campaign(self.conn, c.id)
        self.assertEqual(self.current().status, 'paused')

    def test_slack_outage_does_not_block_campaign_and_retries_checkpoint(self):
        c = self.campaign()
        engine.notify(self.conn, c.id, 'message', 'Some progress')
        with mock.patch.object(monitor, 'slack_send', return_value=(False,None)):
            engine.deliver_slack(self.conn)
        self.assertIsNone(self.current().thread_ts)
        engine.deliver_slack(self.conn)
        self.assertEqual(self.current().thread_ts, '123.456')
        self.assertIsNotNone(self.conn.execute('SELECT sent_at FROM stabilize_messages').fetchone()[0])
        with mock.patch.object(monitor, 'slack_send') as send:
            engine.deliver_slack(self.conn)
        send.assert_not_called()

    def test_invalid_input_and_unknown_abort_use_public_errors(self):
        for ref in ('', '   '):
            with self.assertRaises(engine.InvalidRef):
                engine.resolve(ref)
        with self.assertRaises(engine.InvalidRef):
            engine.create('short', 'operator', 'key')
        with self.assertRaises(engine.CampaignNotFound):
            engine.abort('missing', 'operator')
        self.gh.assert_not_called()

    def test_api_abort_finished_timestamp_and_read_only_views(self):
        c = self.campaign()
        result = engine.abort(c.id, 'operator')
        self.assertEqual(result['status'], 'aborted')
        self.assertIsNotNone(result['finished_at'])
        again = engine.abort(c.id, 'operator')
        self.assertEqual(result, again)
        self.gh.reset_mock()
        with mock.patch.object(monitor, 'connect_db') as connect:
            self.assertEqual(engine.get(c.id)['finished_at'], result['finished_at'])
            self.assertEqual(engine.list_campaigns()[0]['id'], c.id)
        connect.assert_not_called()
        self.gh.assert_not_called()

    def test_incomplete_job_successful_step_does_not_retire_campaign_identity(self):
        c = self.campaign(sha=B)
        self.issue(c)
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()):
            engine.ingest_run(self.conn, c, 'push', run(1, B))
        interrupted = automerge.FailureReport(frozenset({'CI/unit'}), {
            'CI/unit': automerge.FailedJobDetails(frozenset({'Upload'}), frozenset())}, '',
            successful_steps={'CI/unit': frozenset({'Run tests'})}, incomplete_jobs=frozenset({'CI/unit'}))
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=interrupted):
            engine.ingest_run(self.conn, c, 'push', run(2, B))
        self.assertEqual(self.conn.execute("SELECT status FROM stabilize_failures WHERE identity='test::old'").fetchone()[0], 'open')
        self.assertIsNone(self.conn.execute('SELECT absent_sha FROM stabilize_issue_identities').fetchone()[0])

    def test_late_older_run_does_not_reopen_newer_fixed_observation(self):
        c = self.campaign(sha=B)
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()):
            engine.ingest_run(self.conn, c, 'push', run(2, B))
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(successful=True)):
            engine.ingest_run(self.conn, c, 'push', run(3, B, conclusion='success'))
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()):
            engine.ingest_run(self.conn, c, 'push', run(1, A))
        self.assertEqual(self.conn.execute('SELECT status FROM stabilize_failures').fetchone()[0], 'fixed')

    def test_dispatch_discovery_ignores_old_and_other_tip_runs(self):
        c = self.campaign(stage='hourly')
        with mock.patch.object(engine, 'ref_sha', return_value=A):
            engine.ensure_dispatch(self.conn, c, 'hourly', [])
            old = dict(run(9), createdAt='2026-01-01T00:00:00Z')
            engine.ensure_dispatch(self.conn, c, 'hourly', [old, run(10, B)])
        self.assertIsNone(self.conn.execute('SELECT run_id FROM stabilize_dispatches').fetchone()[0])
        self.gh.assert_called_once()

    def test_gate_snapshot_collision_uses_persisted_suffix(self):
        c = self.campaign(sha=B)
        refs = []
        def create_ref(branch, sha):
            row = self.conn.execute('SELECT gate_branch FROM stabilize_gate_merges').fetchone()
            self.assertEqual(row['gate_branch'], branch)
            refs.append(branch)
            return True
        with mock.patch.object(engine, 'ref_sha', return_value=A), mock.patch.object(engine, 'ensure_ref', side_effect=create_ref):
            engine.open_gate(self.conn, c)
        self.assertEqual(refs, ['stabilize/test-gate-push-2'])

    def test_nightly_reporting_read_failure_retries_after_terminal_checkpoint(self):
        c = self.campaign(stage='nightly', sha=B)
        self.gate(c, stage='nightly')
        self.gh.return_value = json.dumps({'state': 'MERGED', 'labels': [], 'headRefOid': B})
        with mock.patch.object(engine, 'pull_requests', side_effect=monitor.CommandError('inventory unavailable')):
            with self.assertRaises(monitor.CommandError):
                engine.observe_gates(self.conn, c)
        self.assertEqual(self.current().status, 'completed')
        self.assertIsNotNone(self.conn.execute('SELECT finished_at FROM stabilize_campaigns').fetchone()[0])
        with mock.patch.object(engine, 'pull_requests', return_value=[{'number':99}]), \
             mock.patch.object(engine, 'deliver_writes'), mock.patch.object(engine, 'deliver_slack'):
            engine.once(self.conn)
        self.assertIn('#99', self.conn.execute("SELECT text FROM stabilize_messages WHERE checkpoint='completed:0'").fetchone()[0])

    def test_durable_nightly_gate_repairs_terminal_checkpoint_gap(self):
        c = self.campaign(stage='nightly', sha=B)
        self.gate(c, stage='nightly')
        with self.conn:
            self.conn.execute("UPDATE stabilize_gate_merges SET state='merged'")
        with mock.patch.object(engine, 'pull_requests', return_value=[]), mock.patch.object(engine, 'branch_runs') as runs:
            engine.tick_campaign(self.conn, c.id)
        self.assertEqual(self.current().status, 'completed')
        runs.assert_not_called()

    def test_lock_timeout_has_a_public_error(self):
        with mock.patch.object(engine.fcntl, 'flock', side_effect=BlockingIOError), \
             mock.patch.object(engine.time, 'monotonic', side_effect=[0, 121]):
            with self.assertRaises(engine.LockTimeout):
                with engine.lock(wait_seconds=120):
                    self.fail('busy lock must not be acquired')

    def test_draft_or_withdrawn_repair_returns_issue_to_open(self):
        c = self.campaign(sha=B)
        self.issue(c, status='pr_ready')
        for prs in ([self.pr(draft=True)], [self.pr(status='CLOSED')], []):
            self.issue_reads(prs)
            engine.refresh_issues(self.conn, c)
            self.assertEqual(self.issue_state(), 'open')
            self.assertFalse(state.merge_gate_open(self.conn, c.id))

    def test_issue_can_be_dropped_after_escalation_or_reopened_by_a_new_observation(self):
        c = self.campaign(sha=B)
        self.issue(c, status='escalated')
        self.issue_reads(labels=['Escalated'])
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(successful=True)):
            engine.ingest_run(self.conn, c, 'push', run(1, B, conclusion='success'))
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'dropped')
        self.issue_reads()
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()):
            engine.ingest_run(self.conn, c, 'push', run(2, B))
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'open')

    def test_shared_git_cache_deferral_does_not_fall_back_to_rest(self):
        # Exercise the real adapter, rather than the state-machine ancestry mock.
        with mock.patch.object(monitor, 'git_cache') as cache:
            cache.return_value.ancestor.side_effect = git_ancestry.Deferred('fetch pending')
            # Read the implementation's globals through a saved reference below.
            real_ancestor = REAL_ANCESTOR
            with self.assertRaises(git_ancestry.Deferred):
                real_ancestor(A, B)
        self.gh.assert_not_called()

    def test_cancelled_rejection_and_trusted_comment_match_merger_release(self):
        c = self.campaign(sha=B)
        self.issue(c)
        automerge.ensure_github_outbox_schema(self.conn)
        with self.conn:
            automerge.enqueue_github_write(self.conn, 'batch', 'reject_head', 22, B, {'reason': 'released'})
            self.conn.execute('UPDATE automerge_github_outbox SET cancelled_at=?', (monitor.utc_now(),))
        pr = self.pr()
        pr['labels'] = [{'name': 'mergemarshall:rejected'}]
        self.issue_reads([pr])
        comments = [{'user': {'login': 'mergemarshall[bot]'}, 'body': f'mergemarshall:rejected-head: {B}'}]
        with mock.patch.object(automerge, 'list_pull_comments', return_value=comments):
            engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'pr_ready')
        self.assertTrue(state.merge_gate_open(self.conn, c.id))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM automerge_github_outbox').fetchone()[0], 1)

    def test_pending_draft_intent_readiness_matches_merger(self):
        c = self.campaign(sha=B)
        self.issue(c)
        automerge.ensure_github_outbox_schema(self.conn)
        with self.conn:
            # The pending draft operation fences selection even if it names an older head.
            automerge.enqueue_github_write(self.conn, 'batch', 'draft_changed_head', 22, A, {})
        self.issue_reads([self.pr()])
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'open')
        self.assertFalse(state.merge_gate_open(self.conn, c.id))
        with self.conn:
            self.conn.execute('UPDATE automerge_github_outbox SET cancelled_at=?', (monitor.utc_now(),))
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'pr_ready')

    def test_approved_policy_readiness_is_refreshed_by_engine_tick(self):
        c = self.campaign(sha=B)
        self.issue(c)
        self.issue_reads([self.pr()])
        engine.refresh_issues(self.conn, c)
        self.assertEqual(self.issue_state(), 'pr_ready')
        with mock.patch.object(automerge, 'READY_POLICY', 'approved'), \
             mock.patch.object(automerge, 'gh_json', return_value={'reviewDecision': 'REVIEW_REQUIRED'}):
            engine.refresh_issues(self.conn, c)
            self.assertEqual(self.issue_state(), 'open')
            self.assertFalse(state.merge_gate_open(self.conn, c.id))
        with mock.patch.object(automerge, 'READY_POLICY', 'approved'), \
             mock.patch.object(automerge, 'gh_json', return_value={'reviewDecision': 'APPROVED'}):
            engine.refresh_issues(self.conn, c)
            self.assertEqual(self.issue_state(), 'pr_ready')
            self.assertTrue(state.merge_gate_open(self.conn, c.id))

    def queue_gate_close(self, c, stage='push', tip=B, number=40):
        self.gate(c, stage=stage, tip=tip, number=number)
        self.gh.side_effect = None
        self.gh.return_value = json.dumps({'state': 'MERGED', 'labels': [], 'headRefOid': tip})
        engine.observe_gates(self.conn, c)

    def test_close_is_obsoleted_by_new_recurrence_before_issue_refresh(self):
        c = self.campaign(sha=B)
        self.issue(c, status='landed')
        with self.conn:
            self.conn.execute('INSERT INTO stabilize_fix_commits VALUES (?,?,?)', (c.id, 7, B))
        self.queue_gate_close(c)
        with self.conn:
            self.conn.execute('UPDATE stabilize_campaigns SET tip_sha=?,round=2', (C,))
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report()):
            engine.ingest_run(self.conn, self.current(), 'push', run(90, C))
        with mock.patch.object(engine, 'api', return_value=[]) as api:
            engine.deliver_writes(self.conn)
        self.assertFalse(any('state=closed' in call.args for call in api.call_args_list))
        close = self.conn.execute("SELECT * FROM stabilize_writes WHERE kind='close'").fetchone()
        self.assertIsNotNone(close['cancelled_at'])
        self.assertIsNone(close['done_at'])
        self.assertEqual(self.issue_state(), 'landed')  # refresh, not delivery, owns recurrence transition.

    def test_close_is_obsoleted_when_current_fix_generation_changes(self):
        c = self.campaign(sha=B)
        self.issue(c, status='landed')
        with self.conn:
            self.conn.execute('INSERT INTO stabilize_fix_commits VALUES (?,?,?)', (c.id, 7, B))
        self.queue_gate_close(c)
        with self.conn:
            self.conn.execute('UPDATE stabilize_fix_commits SET fix_sha=?', (C,))
        with mock.patch.object(engine, 'api', return_value=[]) as api:
            engine.deliver_writes(self.conn)
        self.assertFalse(any('state=closed' in call.args for call in api.call_args_list))
        self.assertEqual(self.issue_state(), 'landed')

    def test_same_issue_closes_and_comments_once_for_each_gate(self):
        c = self.campaign(sha=B)
        self.issue(c, status='landed')
        with self.conn:
            self.conn.execute('INSERT INTO stabilize_fix_commits VALUES (?,?,?)', (c.id, 7, B))
        self.queue_gate_close(c)
        with mock.patch.object(engine, 'api', return_value=[]) as first:
            engine.deliver_writes(self.conn)
        first_comments = [call.args for call in first.call_args_list if call.args[0] == 'issues/7/comments']
        self.assertEqual(len(first_comments), 1)
        self.assertEqual(self.issue_state(), 'closed')
        with self.conn:
            self.conn.execute("UPDATE stabilize_issues SET state='landed',pr_number=23,pr_head_sha=?", (C,))
            self.conn.execute('UPDATE stabilize_fix_commits SET fix_sha=?', (C,))
            self.conn.execute("UPDATE stabilize_campaigns SET stage='hourly',tip_sha=?", (C,))
        self.queue_gate_close(self.current(), stage='hourly', tip=C, number=41)
        with mock.patch.object(engine, 'api', return_value=[]) as second:
            engine.deliver_writes(self.conn)
            engine.deliver_writes(self.conn)
        self.assertEqual(self.issue_state(), 'closed')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stabilize_writes WHERE kind='close' AND done_at IS NOT NULL").fetchone()[0], 2)
        second_comments = [call.args for call in second.call_args_list if call.args[0] == 'issues/7/comments']
        self.assertEqual(len(second_comments), 1)
        self.assertNotEqual(first_comments[0][-1], second_comments[0][-1])

    def test_github_refusal_keeps_status_and_message_without_url(self):
        live = monitor.CommandError(
            'gh workflow run hourly.yml exited 1: could not create workflow dispatch event: HTTP 403: '
            'Resource not accessible by integration '
            '(https://api.github.com/repos/BrokkAi/bifrost-dev/actions/workflows/337188008/dispatches)')
        self.assertEqual(engine.github_refusal(live), 'HTTP 403: Resource not accessible by integration')
        self.assertEqual(engine.github_refusal(monitor.CommandError('insufficient permission')),
                         'GitHub refused the request')

    def test_dispatch_permission_denial_pauses_once_and_explicit_resume_retries(self):
        c = self.campaign()
        dispatches = []
        def external(args, **kwargs):
            if args[:2] == ['workflow', 'run']:
                dispatches.append(args)
                if len(dispatches) == 1:
                    raise monitor.CommandError('HTTP 403 Resource not accessible by integration')
            return '[]'
        self.gh.side_effect = external
        with mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]):
            engine.once(self.conn)
            engine.once(self.conn)
            self.assertEqual(len(dispatches), 1)
            paused = engine.get(c.id)
            self.assertEqual(paused['status'], 'paused')
            self.assertIn('push workflow_dispatch refused (attempt 1): HTTP 403: Resource not accessible by integration.',
                          paused['pause_reason'])
            self.assertIn('token_permissions', paused['pause_reason'])
            self.assertIn(f'resume {c.id}', paused['pause_reason'])
            notices = self.conn.execute("SELECT * FROM stabilize_messages WHERE checkpoint LIKE 'paused:%'").fetchall()
            self.assertEqual(len(notices), 1)
            self.assertIsNotNone(notices[0]['sent_at'])
            resumed = engine.resume(c.id, 'admin')
            self.assertEqual(resumed['status'], 'running')
            self.assertIsNone(resumed['pause_reason'])
            engine.once(self.conn)
            engine.once(self.conn)
        self.assertEqual(len(dispatches), 2)
        attempts = self.conn.execute('SELECT * FROM stabilize_dispatches ORDER BY attempt').fetchall()
        self.assertEqual([r['attempt'] for r in attempts], [1, 2])
        self.assertTrue(attempts[0]['permission_denied'])
        self.assertIsNotNone(attempts[0]['cancelled_at'])
        self.assertIsNone(attempts[1]['cancelled_at'])

    def test_resume_retains_ambiguous_dispatch_intent(self):
        c = self.campaign()
        self.gh.side_effect = monitor.CommandError('lost dispatch response')
        with mock.patch.object(engine, 'ref_sha', return_value=A):
            engine.ensure_dispatch(self.conn, c, 'push', [])
            engine.pause(self.conn, c, 'operator investigation')
            engine.resume_campaign(self.conn, c.id, 'admin')
            engine.ensure_dispatch(self.conn, self.current(), 'push', [])
        self.gh.assert_called_once()
        self.assertIsNone(self.conn.execute('SELECT cancelled_at FROM stabilize_dispatches').fetchone()[0])

    def test_permission_matching_is_not_triggered_by_ordinary_transport_error(self):
        c = self.campaign()
        self.gh.side_effect = monitor.CommandError('connection timed out')
        with mock.patch.object(engine, 'ref_sha', return_value=A):
            engine.ensure_dispatch(self.conn, c, 'push', [])
        self.assertEqual(self.current().status, 'running')
        self.assertFalse(self.conn.execute('SELECT permission_denied FROM stabilize_dispatches').fetchone()[0])

    def test_once_records_tick_heartbeats_and_busy_lock_skips(self):
        def heartbeat():
            return self.conn.execute("SELECT * FROM entrypoint_heartbeats "
                                     "WHERE entry_point='stabilize'").fetchone()

        def run_once():
            with mock.patch('sys.argv', ['stabilize.py', 'once']), \
                    mock.patch.object(monitor, 'connect_db', side_effect=lambda: sqlite3.connect(self.db)):
                return engine.main()

        with mock.patch.object(engine, 'once') as once:
            self.assertEqual(run_once(), 0)
        once.assert_called_once()
        row = heartbeat()
        self.assertEqual((row['last_outcome'], row['last_error']), ('ok', None))
        self.assertIsNotNone(row['last_started_at'])

        with mock.patch.object(engine, 'once', side_effect=RuntimeError('secret detail')):
            with self.assertRaises(RuntimeError):
                run_once()
        self.assertEqual((heartbeat()['last_outcome'], heartbeat()['last_error']), ('error', 'RuntimeError'))

        with engine.lock() as acquired, mock.patch.object(engine, 'once') as once:
            self.assertTrue(acquired)
            self.assertEqual(run_once(), 0)
        once.assert_not_called()
        self.assertIsNotNone(heartbeat()['last_skipped_at'])

    def test_create_begins_inline_and_cron_does_not_duplicate_start_or_dispatch(self):
        self.seed_row()
        self.issue_reads()
        monitor.CHANNEL_PATH.write_text('C1')
        real_tick = engine.tick_campaign
        def locked_tick(conn, identifier):
            with engine.lock() as acquired:
                self.assertFalse(acquired, 'inline tick must retain the operator lock')
            return real_tick(conn, identifier)
        with mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]), \
             mock.patch.object(engine, 'tick_campaign', side_effect=locked_tick) as tick, \
             mock.patch.object(monitor, 'record_heartbeat') as heartbeat, \
             mock.patch.object(monitor, 'slack_send', return_value=(True, '123.456')) as slack:
            created = engine.create(A, 'alice', 'begin-now')
            tick.assert_called_once()
            self.assertEqual(created['thread_ts'], '123.456')
            self.assertEqual(created['slack_thread_url'], 'https://app.slack.com/archives/C1/p123456')
            self.assertEqual(created['issues'][0]['number'], 7)
            intent = self.conn.execute('SELECT * FROM stabilize_dispatches').fetchone()
            self.assertEqual((intent['campaign_id'], intent['stage'], intent['tip_sha'], intent['attempt']),
                             (created['id'], 'push', A, 1))
            first_messages = [call.args[1] for call in slack.call_args_list]
            self.assertEqual(first_messages[0], f'{monitor.slack_project_prefix()} stable release branch started from <https://github.com/{monitor.REPO_NAME}/commit/{A}|{A[:7]}>.')
            self.assertIn(f'<https://github.com/{monitor.REPO_NAME}/tree/{created["branch"]}|{created["branch"]}>', first_messages[1])
            self.assertIn(f'campaign `{created["id"]}`', first_messages[1])
            self.assertIn('requested by alice', first_messages[1])
            self.assertTrue(first_messages[2].startswith('Seeded issues'))
            self.assertIn(engine.slack_commit(A), first_messages[2])
            # Reusing the create key is a read of the original result, not another tick.
            self.assertEqual(engine.create(A, 'alice', 'begin-now'), created)
            tick.assert_called_once()
            engine.once(self.conn)
            self.assertEqual(tick.call_count, 2)
            self.assertEqual([call.args[1] for call in slack.call_args_list], first_messages)
            heartbeat.assert_not_called()
        calls = [call.args[0] for call in self.gh.call_args_list]
        self.assertEqual(sum(args[:2] == ['workflow', 'run'] for args in calls), 1)
        self.assertEqual(sum(args[:2] == ['label', 'create'] for args in calls), 1)
        self.assertEqual(sum(args[:2] == ['api', f'repos/{monitor.REPO_NAME}/issues/7/labels'] for args in calls), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stabilize_messages WHERE checkpoint='started:0'").fetchone()[0], 1)

    def test_inline_tick_exception_returns_created_campaign_and_cron_can_continue(self):
        def fail_after_uncommitted_change(conn, identifier):
            conn.execute("UPDATE stabilize_campaigns SET stage='nightly' WHERE id=?", (identifier,))
            raise RuntimeError('temporary read failure')
        with mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(engine, 'tick_campaign', side_effect=fail_after_uncommitted_change), \
             mock.patch.object(monitor, 'log') as log:
            created = engine.create(A, 'alice', 'failed-inline-tick')
        self.assertEqual(created['status'], 'running')
        self.assertEqual(created['stage'], 'push')
        self.assertIsNone(created['start_error'])
        self.assertEqual(created['thread_ts'], '123.456')
        self.assertTrue(any('advance deferred: temporary read failure' in call.args[0] for call in log.call_args_list))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 0)
        with mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]):
            engine.once(self.conn)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 1)

    def test_inline_seed_deferral_returns_campaign_with_visible_placeholder(self):
        self.seed_row(first=B)
        self.ancestry.side_effect = git_ancestry.Deferred('history pending')
        with mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(monitor, 'log') as log:
            created = engine.create(A, 'alice', 'deferred-inline')
        self.assertEqual((created['status'], created['thread_ts']), ('running', '123.456'))
        self.assertIsNone(created['start_error'])
        self.assertTrue(any('seeding deferred' in call.args[0] for call in log.call_args_list))
        self.assertEqual(self.conn.execute('SELECT seeded FROM stabilize_runtime').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 0)

    def test_ambiguous_create_posts_root_without_inline_ref_reconciliation(self):
        with mock.patch.object(engine, 'ensure_ref', return_value=False) as ref, \
             mock.patch.object(engine, 'tick_campaign') as tick, \
             mock.patch.object(engine, 'seed_issues') as seed, \
             mock.patch.object(monitor, 'slack_send', return_value=(True, '123.456')) as slack:
            created = engine.create(A, 'alice', 'ambiguous-root')
            ref.assert_called_once()
            tick.assert_not_called()
            seed.assert_not_called()
            self.assertEqual((created['status'], created['thread_ts']), ('starting', '123.456'))
            self.assertIn('unconfirmed', created['start_error'])
            self.assertEqual(len(slack.call_args_list), 2)  # root and first details reply
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 0)
            self.assertEqual(engine.create(A, 'alice', 'ambiguous-root'), created)
            self.assertEqual(len(slack.call_args_list), 2)
        with mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]), \
             mock.patch.object(monitor, 'slack_send', return_value=(True, 'later')) as slack:
            engine.once(self.conn)
        self.assertFalse(any(not call.kwargs.get('thread_ts') for call in slack.call_args_list))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 1)

    def test_inline_slack_failure_does_not_block_dispatch_and_cron_retries(self):
        with mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]), \
             mock.patch.object(monitor, 'slack_send', return_value=(False, None)):
            created = engine.create(A, 'alice', 'slack-down')
        self.assertEqual(created['status'], 'running')
        self.assertIsNone(created['thread_ts'])
        self.assertIsNone(created['start_error'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 1)
        with mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]), \
             mock.patch.object(monitor, 'slack_send', return_value=(True, '123.456')) as slack:
            engine.once(self.conn)
            engine.once(self.conn)
        messages = [call.args[1] for call in slack.call_args_list]
        self.assertEqual(sum(not call.kwargs.get('thread_ts') for call in slack.call_args_list), 1)
        self.assertTrue(messages[1].startswith('Branch:'))
        self.assertTrue(messages[2].startswith('Seeded issues'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 1)

    def test_resume_runs_inline_and_keeps_next_cron_dispatch_idempotent(self):
        c = self.campaign(status='paused')
        monitor.CHANNEL_PATH.write_text('C1')
        with mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]), \
             mock.patch.object(engine, 'tick_campaign', wraps=engine.tick_campaign) as tick:
            resumed = engine.resume(c.id, 'admin')
            tick.assert_called_once()
            self.assertEqual((resumed['status'], resumed['thread_ts']), ('running', '123.456'))
            self.assertEqual(resumed['slack_thread_url'], 'https://app.slack.com/archives/C1/p123456')
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 1)
            engine.once(self.conn)
        self.assertEqual(sum(call.args[0][:2] == ['workflow', 'run'] for call in self.gh.call_args_list), 1)

    def test_resume_inline_exception_keeps_resume_successful(self):
        c = self.campaign(status='paused')
        with mock.patch.object(engine, 'tick_campaign', side_effect=git_ancestry.Deferred('fetching history')), \
             mock.patch.object(monitor, 'log') as log:
            resumed = engine.resume(c.id, 'admin')
        self.assertEqual(resumed['status'], 'running')
        self.assertIsNone(resumed['pause_reason'])
        self.assertTrue(any('advance deferred: fetching history' in call.args[0] for call in log.call_args_list))

    def test_cli_start_uses_one_inline_tick_without_operator_heartbeat(self):
        with mock.patch('sys.argv', ['stabilize.py', 'start', '--ref', 'master', '--requested-by', 'alice']), \
             mock.patch.object(engine, 'resolve', return_value={'sha': A}), \
             mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]), \
             mock.patch.object(engine, 'tick_campaign', wraps=engine.tick_campaign) as tick, \
             mock.patch.object(monitor, 'record_heartbeat') as heartbeat, mock.patch('builtins.print'):
            self.assertEqual(engine.main(), 0)
        tick.assert_called_once()
        heartbeat.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 1)

    def test_cli_resume_uses_one_inline_tick_without_operator_heartbeat(self):
        c = self.campaign(status='paused')
        with mock.patch('sys.argv', ['stabilize.py', 'resume', c.id, '--requested-by', 'alice']), \
             mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]), \
             mock.patch.object(engine, 'tick_campaign', wraps=engine.tick_campaign) as tick, \
             mock.patch.object(monitor, 'record_heartbeat') as heartbeat, mock.patch('builtins.print'):
            self.assertEqual(engine.main(), 0)
        tick.assert_called_once()
        heartbeat.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM stabilize_dispatches').fetchone()[0], 1)

    def test_inline_delivery_is_scoped_to_its_campaign(self):
        old = self.campaign(status='completed')
        engine.notify(self.conn, old.id, 'old-pending', 'Old outcome')
        engine.queue_write(self.conn, old.id, 'old-label', 'label', {'number': 99, 'label': state.LABEL})
        with mock.patch.object(engine, 'ensure_ref', return_value=True), \
             mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', return_value=[]):
            created = engine.create(A, 'alice', 'scoped-inline')
        self.assertEqual(created['thread_ts'], '123.456')
        self.assertIsNone(engine.get(old.id)['thread_ts'])
        self.assertIsNone(self.conn.execute("SELECT sent_at FROM stabilize_messages WHERE campaign_id='test'").fetchone()[0])
        self.assertIsNone(self.conn.execute("SELECT done_at FROM stabilize_writes WHERE campaign_id='test'").fetchone()[0])

    def test_accepted_live_root_is_not_reposted_or_given_a_retroactive_start_reply(self):
        c = self.campaign()
        with self.conn:
            self.conn.execute('UPDATE stabilize_campaigns SET thread_ts=? WHERE id=?', ('live-thread', c.id))
        engine.notify(self.conn, c.id, 'new-progress', f'Checking {engine.slack_commit(B)}.')
        with mock.patch.object(monitor, 'slack_send', return_value=(True, 'reply')) as slack:
            engine.deliver_slack(self.conn)
            engine.deliver_slack(self.conn)
        slack.assert_called_once()
        self.assertEqual(slack.call_args.kwargs['thread_ts'], 'live-thread')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stabilize_messages WHERE checkpoint='started:0'").fetchone()[0], 0)

    def test_first_reply_retries_in_place_before_prepared_seed_reply(self):
        c = self.campaign()
        engine.notify(self.conn, c.id, 'seeded', 'Seeded issues: none.')
        with mock.patch.object(monitor, 'slack_send', side_effect=[(True, 'root'), (False, None)]) as slack:
            engine.deliver_slack(self.conn)
        self.assertTrue(slack.call_args.args[1].startswith('Branch:'))
        self.assertEqual(self.current().thread_ts, 'root')
        with mock.patch.object(monitor, 'slack_send', return_value=(True, 'reply')) as slack:
            engine.deliver_slack(self.conn)
            engine.deliver_slack(self.conn)
        self.assertEqual(len(slack.call_args_list), 2)
        self.assertTrue(slack.call_args_list[0].args[1].startswith('Branch:'))
        self.assertEqual(slack.call_args_list[1].args[1], 'Seeded issues: none.')
        self.assertTrue(all(call.kwargs['thread_ts'] == 'root' for call in slack.call_args_list))

    def test_thread_commit_messages_use_short_linked_shas(self):
        self.assertEqual(engine.slack_commit(B), f'<https://github.com/{monitor.REPO_NAME}/commit/{B}|{B[:7]}>')
        c = self.campaign(sha=B, seeded=0)
        self.seed_row()
        self.issue_reads()
        engine.seed_issues(self.conn, c)
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(successful=True)):
            engine.ingest_run(self.conn, c, 'push', run(80, B, conclusion='success'))
        with mock.patch.object(engine, 'ref_sha', return_value=B):
            engine.ensure_dispatch(self.conn, c, 'push', [])
        self.issue_reads()
        with mock.patch.object(engine, 'ref_sha', return_value=B), mock.patch.object(engine, 'ensure_ref', return_value=True):
            engine.open_gate(self.conn, c)
        engine.pause(self.conn, c, f'Checks deferred at {B[:7]}', commit_sha=B)
        rows = self.conn.execute('SELECT text FROM stabilize_messages').fetchall()
        commit_messages = [r['text'] for r in rows if '/commit/' in r['text']]
        self.assertEqual(len(commit_messages), 5)  # seed, result, dispatch, green gate, pause
        for message in commit_messages:
            self.assertNotIn(f'`{A}`', message)
            self.assertNotIn(f'`{B}`', message)
            self.assertNotIn(f'`{B[:12]}`', message)
            self.assertTrue(engine.slack_commit(A) in message or engine.slack_commit(B) in message)
        self.assertEqual(engine.get(c.id)['pause_reason'], f'Checks deferred at {B[:7]}')

    def test_zero_fix_push_gate_lands_without_pr_and_advances(self):
        c = self.campaign()
        self.master.return_value = C  # Historical anchor is behind, not equal to, master.
        with mock.patch.object(engine, 'ensure_ref') as create_ref:
            self.assertTrue(engine.open_gate(self.conn, c))
        create_ref.assert_not_called()
        self.gh.assert_not_called()
        gate = self.conn.execute('SELECT * FROM stabilize_gate_merges').fetchone()
        self.assertEqual((gate['stage'], gate['state'], gate['pr_number']), ('push', 'merged', None))
        self.assertEqual(self.current().stage, 'hourly')
        self.assertEqual(engine.get(c.id)['gate_merges'][0]['pr_number'], None)
        message = self.conn.execute("SELECT text FROM stabilize_messages WHERE checkpoint LIKE 'gate-in-master:%'").fetchone()[0]
        self.assertEqual(message, f'push gate needs no PR: {engine.slack_commit(A)} is already in master.')
        engine.observe_gates(self.conn, self.current())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stabilize_messages WHERE checkpoint LIKE 'gate-in-master:%'").fetchone()[0], 1)

    def test_live_pending_zero_fix_gate_recovers_and_dispatches_hourly_next_tick(self):
        c = self.campaign()
        self.master.return_value = C
        self.gate(c, tip=A, number=None)
        now = monitor.utc_now()
        with self.conn:
            self.conn.execute("UPDATE stabilize_gate_merges SET state='pending'")
            self.conn.execute('INSERT INTO stabilize_gate_intents(campaign_id,stage,intent_at) VALUES (?,?,?)', (c.id, 'push', now))
        passing = run(1, conclusion='success')
        with mock.patch.object(automerge, 'collect_failure_report_for_run', return_value=report(successful=True)):
            engine.ingest_run(self.conn, c, 'push', passing)
        self.gh.reset_mock()
        with mock.patch.object(engine, 'ref_sha', return_value=A), \
             mock.patch.object(engine, 'branch_runs', side_effect=lambda c, stage: [passing] if stage == 'push' else []), \
             mock.patch.object(engine, 'ensure_ref') as refs:
            engine.once(self.conn)
            engine.once(self.conn)
        refs.assert_not_called()  # Existing snapshot is left in place, not deleted or rewritten.
        self.assertEqual(self.current().stage, 'hourly')
        gate = self.conn.execute('SELECT * FROM stabilize_gate_merges').fetchone()
        self.assertEqual((gate['state'], gate['pr_number']), ('merged', None))
        self.assertEqual(self.conn.execute('SELECT intent_at FROM stabilize_gate_intents').fetchone()[0], now)
        writes = [call.args[0] for call in self.gh.call_args_list]
        self.assertFalse(any(args[:2] == ['pr', 'create'] for args in writes))
        dispatches = self.conn.execute('SELECT stage,attempt FROM stabilize_dispatches').fetchall()
        self.assertEqual([(r['stage'],r['attempt']) for r in dispatches], [('hourly',1)])

    def test_nightly_zero_fix_gate_completes_without_pr(self):
        c = self.campaign(stage='nightly')
        with mock.patch.object(engine, 'pull_requests', return_value=[{'number': 99}]), \
             mock.patch.object(engine, 'ensure_ref') as refs:
            self.assertTrue(engine.open_gate(self.conn, c))
        refs.assert_not_called()
        self.assertEqual(self.current().status, 'completed')
        self.assertIsNotNone(engine.get(c.id)['finished_at'])
        gate = self.conn.execute('SELECT * FROM stabilize_gate_merges').fetchone()
        self.assertEqual((gate['state'], gate['pr_number']), ('merged', None))
        completion = self.conn.execute("SELECT text FROM stabilize_messages WHERE checkpoint='completed:0'").fetchone()[0]
        self.assertIn('#99', completion)
        self.assertIn('nightly gate', completion)
        self.gh.assert_not_called()

    def test_absorbed_gate_with_existing_pr_lands_without_closed_or_rejected_pause(self):
        c = self.campaign(sha=B, stage='hourly')
        self.gate(c)
        self.master.return_value = C
        # Inclusion is sufficient even if PR bookkeeping is closed or unavailable.
        with mock.patch.object(engine, 'gh', side_effect=AssertionError('PR view not needed for a proven landing')):
            engine.observe_gates(self.conn, c)
        self.assertEqual((self.current().status, self.current().stage), ('running', 'hourly'))
        gate = self.conn.execute('SELECT * FROM stabilize_gate_merges').fetchone()
        self.assertEqual((gate['state'], gate['pr_number']), ('merged', 40))
        self.assertIsNone(engine.get(c.id)['pause_reason'])
        notice = self.conn.execute("SELECT text FROM stabilize_messages WHERE checkpoint LIKE 'gate-in-master:%'").fetchone()[0]
        self.assertIn('Gate PR #40 is no longer needed', notice)

    def test_closed_gate_pr_rechecks_master_advanced_during_read(self):
        c = self.campaign(sha=B)
        self.gate(c)
        self.master.side_effect = [A, C]
        self.gh.return_value = json.dumps({'state': 'CLOSED', 'labels': [], 'headRefOid': B})
        engine.observe_gates(self.conn, c)
        self.assertEqual((self.current().status, self.current().stage), ('running', 'hourly'))
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_gate_merges').fetchone()[0], 'merged')

    def test_already_in_master_gate_closes_included_landed_issues_with_null_pr_fence(self):
        c = self.campaign(sha=B)
        self.issue(c, status='landed')
        with self.conn:
            self.conn.execute('INSERT INTO stabilize_fix_commits VALUES (?,?,?)', (c.id, 7, B))
        self.master.return_value = C
        self.assertTrue(engine.open_gate(self.conn, c))
        close = self.conn.execute("SELECT payload FROM stabilize_writes WHERE kind='close'").fetchone()
        payload = json.loads(close[0])
        self.assertIsNone(payload['gate_pr_number'])
        self.assertIn('no gate PR was needed', payload['comment'])
        with mock.patch.object(engine, 'api', return_value=[]) as api:
            engine.deliver_writes(self.conn)
        self.assertEqual(self.issue_state(), 'closed')
        self.assertTrue(any('state=closed' in call.args for call in api.call_args_list))

    def test_ancestry_unavailable_defers_gate_without_publication_or_advancement(self):
        c = self.campaign(sha=B)
        self.master.return_value = C
        self.ancestry.side_effect = git_ancestry.Deferred('secret unprintable details')
        with mock.patch.object(monitor, 'log') as log, mock.patch.object(engine, 'ensure_ref') as refs:
            self.assertFalse(engine.open_gate(self.conn, c))
        self.assertEqual(self.current().stage, 'push')
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_gate_merges').fetchone()[0], 'pending')
        refs.assert_not_called()
        self.gh.assert_not_called()
        self.assertTrue(any('deferred: Deferred' in call.args[0] for call in log.call_args_list))
        self.assertFalse(any('secret' in call.args[0] for call in log.call_args_list))

    def test_unavailable_master_read_defers_pending_gate_reconciliation(self):
        c = self.campaign()
        self.gate(c, tip=A, number=None)
        with self.conn:
            self.conn.execute("UPDATE stabilize_gate_merges SET state='pending'")
        self.master.side_effect = monitor.CommandError('secret auth details')
        with mock.patch.object(monitor, 'log') as log:
            engine.observe_gates(self.conn, c)
        self.assertEqual(self.current().stage, 'push')
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_gate_merges').fetchone()[0], 'pending')
        self.assertFalse(any('secret' in call.args[0] for call in log.call_args_list))

    def test_gate_publication_error_logs_exception_class_without_message_body(self):
        c = self.campaign(sha=B)
        def external(args, **kwargs):
            if args[:2] == ['pr', 'list']:
                return '[]'
            if args[:2] == ['pr', 'create']:
                raise monitor.CommandError('secret PR message/token')
            return ''
        self.gh.side_effect = external
        with mock.patch.object(engine, 'ref_sha', return_value=B), \
             mock.patch.object(engine, 'ensure_ref', return_value=True), mock.patch.object(monitor, 'log') as log:
            self.assertFalse(engine.open_gate(self.conn, c))
        self.assertTrue(any('publication failed: CommandError' in call.args[0] for call in log.call_args_list))
        self.assertFalse(any('secret' in call.args[0] for call in log.call_args_list))
        self.assertEqual(self.current().stage, 'push')

    def test_master_absorbs_tip_while_gate_publication_fails(self):
        c = self.campaign(sha=B)
        self.master.side_effect = [A, C]
        def external(args, **kwargs):
            if args[:2] == ['pr', 'list']:
                return '[]'
            if args[:2] == ['pr', 'create']:
                raise monitor.CommandError('No commits between branches')
            return ''
        self.gh.side_effect = external
        with mock.patch.object(engine, 'ref_sha', return_value=B), mock.patch.object(engine, 'ensure_ref', return_value=True):
            self.assertTrue(engine.open_gate(self.conn, c))
        self.assertEqual(self.current().stage, 'hourly')
        self.assertEqual(self.conn.execute('SELECT state FROM stabilize_gate_merges').fetchone()[0], 'merged')


if __name__ == '__main__':
    unittest.main()
