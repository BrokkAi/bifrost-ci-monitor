"""Triage lifecycle tests with real SQLite and fake external services."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

import monitor
import triage
import automerge
import local_findings
import issue_fixer


class FakeGitHub:
    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.calls = []
        self.lose_response_to = None

    def __call__(self, endpoint, *, method="GET", payload=None, pages=False):
        self.calls.append((method, endpoint, copy.deepcopy(payload)))
        parts = endpoint.split("?")[0].split("/")
        if len(parts) == 1 and method == "GET":
            result = list(self.issues.values())
        elif len(parts) == 1 and method == "POST":
            number = max(self.issues, default=100) + 1
            result = dict(number=number, state="open", title=payload["title"], body=payload["body"],
                          labels=[{"name": label} for label in payload["labels"]])
            self.issues[number] = result
        else:
            number = int(parts[1])
            issue = self.issues[number]
            if len(parts) == 2:
                if method == "PATCH":
                    issue.update(payload)
                result = issue
            elif parts[2] == "comments":
                result = self.comments.setdefault(number, [])
                if method == "POST":
                    result.append(dict(payload))
                    result = result[-1]
            elif parts[2] == "labels":
                issue["labels"] += [{"name": label} for label in payload["labels"]]
                result = issue["labels"]
            else:
                raise AssertionError(endpoint)
        if self.lose_response_to == (method, endpoint):
            self.lose_response_to = None
            raise RuntimeError("response lost after accepted write")
        return copy.deepcopy(result)


class TriageTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "activity.db"
        self.patch(monitor, "DB_PATH", self.path)
        self.patch(monitor, "LOCK_PATH", Path(self.directory.name) / "fixer.lock")
        self.patch(monitor, "run_gh", side_effect=AssertionError("unexpected real GitHub call"))
        self.patch(monitor, "mj_command", side_effect=AssertionError("unexpected real mj call"))
        self.patch(monitor, "_sync_known_failure_issue")
        self.patch(monitor, 'load_slack_transport', return_value=monitor.SlackTransport('chat', token='test', channel='test'))
        self.slack = self.patch(monitor, 'slack_send', return_value=(True, 'notice'))
        self.conn = monitor.connect_db()
        self.addCleanup(lambda: self.conn.close())
        triage.ensure_schema(self.conn)
        self.github = FakeGitHub()
        self.patch(triage, "gh_api", side_effect=self.github)

    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def reopen(self):
        self.conn.close()
        self.conn = monitor.connect_db()
        triage.ensure_schema(self.conn)

    def add_failure(self, identity="Cargo nextest", sha="a" * 40, job_name="linux"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
                "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
                "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,updated_at) "
                "VALUES ('CI',?,'step',?,?,1,'https://github.test/run/1','2026-01-01',"
                "?,1,'https://github.test/run/1','2026-01-01','2026-01-01')", (job_name, identity, sha, sha))

    def add_local_finding(self, kind='baseline'):
        with self.conn:
            return local_findings.record(self.conn, 'batch', 'merge-session', kind, 'a' * 40,
                                         'local_policy_test', 'eatmydata cargo nextest run local_policy_test',
                                         'expected exit 2, got 1; settings and log /tmp/base.log', '2026-01-02')

    def test_local_finding_reaches_deduplicated_issue_and_fixer_dossier(self):
        finding = self.add_local_finding()
        self.make_job()
        captured = json.loads(self.job()['observations_json'])[0]
        self.assertEqual(captured['local_finding_id'], finding['id'])
        self.assertIsNone(captured['last_seen_run_id'])
        self.assertIsNone(captured['last_seen_run_url'])
        self.assertIn('A passing rerun alone does not resolve a flaky product test',
                      triage.build_prompt(self.job()))
        triage.publish(self.conn, self.job())
        self.reopen()
        triage.publish(self.conn, self.job())
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(triage.pending(self.conn), [])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM known_failures').fetchone()[0], 0)
        row = self.conn.execute('SELECT * FROM local_findings').fetchone()
        published = dict(next(iter(self.github.issues.values())),
                         html_url=row['triage_issue_url'], assignees=[])
        self.assertIn('Local baseline', published['body'])
        self.assertIn(finding['command'], published['body'])
        self.assertIn(finding['evidence'], published['body'])
        issue_fixer.ensure_schema(self.conn)
        selected = issue_fixer.select_work(self.conn, [published], [])
        self.assertEqual(selected[0]['number'], published['number'])
        with mock.patch.object(issue_fixer, 'api', return_value=[]):
            dossier = issue_fixer.dossier(self.conn, published, [])
        self.assertEqual(dossier['observed_failures'][0]['command'], finding['command'])
        self.assertEqual(dossier['observed_failures'][0]['last_seen_sha'], finding['head_sha'])
        with self.conn:
            repeated = local_findings.record(self.conn, 'another-batch', 'another-session',
                                            finding['kind'], finding['head_sha'], finding['identity'],
                                            finding['command'], finding['evidence'], '2026-01-03')
        self.assertEqual(repeated['id'], finding['id'])
        self.assertEqual(triage.pending(self.conn), [])

    def test_local_resolution_does_not_retire_master_ci_failure(self):
        self.add_local_finding()
        self.make_job(resolved=True)
        self.add_failure()
        triage.publish(self.conn, self.job())
        self.assertEqual(self.conn.execute('SELECT status FROM local_findings').fetchone()[0], 'fixed')
        self.assertEqual(self.conn.execute('SELECT status FROM known_failures').fetchone()[0], 'open')
        self.assertEqual(len(triage.pending(self.conn)), 1)
        self.assertEqual(triage.reconcile_resolved(self.conn), 0)

    def test_large_local_findings_are_split_into_bounded_triage_prompts(self):
        with self.conn:
            for number in range(12):
                local_findings.record(self.conn, 'batch', 'session', 'baseline', 'a' * 40,
                                     f'test_{number}', 'check tests', '\u754c' * 6000, '2026-01-02')
        observations = triage.pending(self.conn)
        self.assertGreater(len(observations), 0)
        self.assertLess(len(observations), 12)
        prompt = triage.build_prompt(dict(id='job', base_sha='a' * 40,
                                         observations_json=json.dumps(observations)))
        self.assertLess(len(prompt), 65536)

    def test_local_publication_retries_lost_create_without_another_investigation(self):
        self.add_local_finding(kind='flaky')
        self.make_job()
        self.github.lose_response_to = ('POST', 'issues')
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            triage.publish(self.conn, self.job())
        self.reopen()
        self.assertEqual(triage.pending(self.conn), [])
        triage.publish(self.conn, self.job())
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(self.conn.execute('SELECT triage_outcome FROM local_findings').fetchone()[0], 'product')

    def job(self, job_id="job"):
        return self.conn.execute("SELECT * FROM triage_jobs WHERE id=?", (job_id,)).fetchone()

    def make_job(self, status="publishing", existing=None, resolved=False, job_id="job"):
        observations = triage.pending(self.conn)
        report = {"findings": [{"failure_ids": [o["failure_id"] for o in observations],
            'outcome': 'resolved' if resolved else 'product',
            "diagnosis": "Missing import", "evidence": "run 1: unresolved symbol at src/lib.rs:2",
            "issue": None if resolved else {"title": "Fix missing import", "body": "Compiler failed; restore import.",
                                          "existing_number": existing}}]}
        with self.conn:
            self.conn.execute("INSERT INTO triage_jobs(id,title,base_sha,created_at,status,"
                              "observations_json,session_id,report_json) VALUES (?,?,?,?,?,?,?,?)",
                              (job_id, f"Bifrost CI triage {job_id}", "a" * 40, "2026-01-01", status,
                               json.dumps(observations), "session", json.dumps(report)))
        return report

    def infrastructure_job(self, job_id="job", separate=False):
        report = self.make_job(job_id=job_id)
        finding = report['findings'][0]
        finding.update(outcome='infrastructure', issue=None, diagnosis='Runner was not acquired',
                       evidence='run 1: runner_id=0, no steps; quota/capacity cause unconfirmed')
        if separate:
            report['findings'] = [dict(finding, failure_ids=[number]) for number in finding['failure_ids']]
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=? WHERE id=?', (json.dumps(report), job_id))
        return report

    def test_infrastructure_is_reported_once_without_a_ticket_or_false_resolution(self):
        self.add_failure()
        self.infrastructure_job()
        triage.publish(self.conn, self.job())
        self.reopen()
        triage.publish(self.conn, self.job())
        self.assertEqual(self.slack.call_count, 2)
        summary, detail = self.slack.call_args_list
        self.assertIsNone(summary.kwargs.get('thread_ts'))
        self.assertIn('CI infrastructure incidents', summary.args[1])
        self.assertNotIn('runner_id=0', summary.args[1])
        self.assertEqual(detail.kwargs['thread_ts'], 'notice')
        self.assertIn('CI infrastructure incident: linux', detail.args[1])
        self.assertIn('Runner was not acquired', detail.args[1])
        self.assertIn('runner_id=0', detail.args[1])
        self.assertIn('https://github.test/run/1', detail.args[1])
        self.assertEqual(self.github.calls, [])
        row = self.conn.execute('SELECT * FROM known_failures').fetchone()
        self.assertEqual(row['status'], 'open')
        self.assertEqual(row['triage_outcome'], 'infrastructure')
        context = automerge._known_failures_prompt(self.conn)
        self.assertIn('infrastructure (diagnostic only', context)
        prompt = automerge.build_async_prompt('batch', [automerge.PullRequest(7, 'Change', 'b' * 40, 'url')],
                                             'a' * 40, known_failures_context=context)
        self.assertIn('Do not investigate them again', prompt)
        self.assertNotIn('triage issue (available for repair)', context)
        self.assertIsNone(row['fixed_at'])
        self.assertIsNone(row['triage_issue_url'])
        self.assertIsNone(self.conn.execute('SELECT resolved_run_id FROM triage_observations').fetchone()[0])
        self.assertEqual(triage.reconcile_resolved(self.conn), 0)
        self.assertEqual(triage.pending(self.conn), [])

    def test_infrastructure_cluster_has_one_parent_and_a_reply_for_each_job(self):
        jobs = ['os matrix / Pi package', 'os matrix / extension boundary',
                'os matrix / python (x86_64-pc-windows-msvc)',
                'os matrix / rust (aarch64-unknown-linux-gnu)']
        for name in jobs:
            self.add_failure(job_name=name)
        self.infrastructure_job(separate=True)
        triage.publish(self.conn, self.job())
        self.reopen()
        triage.publish(self.conn, self.job())
        parent, *replies = self.slack.call_args_list
        self.assertIsNone(parent.kwargs.get('thread_ts'))
        self.assertEqual(len(replies), len(jobs))
        for name, reply in zip(sorted(jobs), replies):
            self.assertEqual(reply.kwargs['thread_ts'], 'notice')
            self.assertIn(name, reply.args[1])
            self.assertIn('runner_id=0', reply.args[1])
        self.assertEqual(self.github.calls, [])
        self.assertEqual(triage.pending(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM known_failures WHERE status='open' "
                                           "AND triage_outcome='infrastructure'").fetchone()[0], len(jobs))

    def test_nearby_reports_reuse_thread_across_restart_and_extend_quiet_window(self):
        with mock.patch.object(monitor, 'utc_now', return_value='2026-10-07T17:00:00+00:00'):
            self.add_failure()
            self.infrastructure_job()
            triage.publish(self.conn, self.job())
        self.reopen()
        for minute, job_id in [(10, 'second'), (20, 'third')]:
            with mock.patch.object(monitor, 'utc_now', return_value=f'2026-10-07T17:{minute}:00+00:00'):
                self.add_failure(identity=job_id, job_name=job_id)
                self.infrastructure_job(job_id=job_id)
                triage.publish(self.conn, self.job(job_id))
        self.assertEqual([c.kwargs.get('thread_ts') for c in self.slack.call_args_list],
                         [None, 'notice', 'notice', 'notice'])
        for job_id, reply in zip(['second', 'third'], self.slack.call_args_list[2:]):
            self.assertIn(job_id, reply.args[1])

    def test_infrastructure_reports_start_new_thread_after_quiet_gap_or_in_new_channel(self):
        with mock.patch.object(monitor, 'utc_now', return_value='2026-10-07T17:00:00+00:00'):
            self.add_failure()
            self.infrastructure_job()
            triage.publish(self.conn, self.job())
        with mock.patch.object(monitor, 'utc_now', return_value='2026-10-07T17:15:00+00:00'):
            self.add_failure(identity='second')
            self.infrastructure_job(job_id='second')
            self.slack.return_value = (True, 'new-thread')
            triage.publish(self.conn, self.job('second'))
            self.add_failure(identity='third')
            self.infrastructure_job(job_id='third')
            transport = monitor.SlackTransport('chat', token='test', channel='other')
            with mock.patch.object(monitor, 'load_slack_transport', return_value=transport):
                self.slack.return_value = (True, 'other-channel-thread')
                triage.publish(self.conn, self.job('third'))
        self.assertEqual([c.kwargs.get('thread_ts') for c in self.slack.call_args_list],
                         [None, 'notice', None, 'new-thread', None, 'other-channel-thread'])

    def test_partial_cluster_retry_keeps_thread_after_window_without_repeating_accepted_reply(self):
        self.add_failure()
        self.add_failure(identity='second', job_name='windows')
        self.add_failure(identity='third', job_name='macos')
        self.infrastructure_job(separate=True)
        self.slack.side_effect = [(True, 'parent-ts'), (True, 'first-reply'), (False, None),
                                  (True, 'retry'), (True, 'last-reply')]
        with mock.patch.object(monitor, 'utc_now', return_value='2026-10-07T17:00:00+00:00'):
            with self.assertRaisesRegex(RuntimeError, 'detail pending'):
                triage.publish(self.conn, self.job())
        self.reopen()
        with mock.patch.object(monitor, 'utc_now', return_value='2026-10-07T18:00:00+00:00'), \
             mock.patch.object(triage, 'infrastructure_slack_text', side_effect=AssertionError('already cached')):
            triage.publish(self.conn, self.job())
        self.assertEqual([c.kwargs.get('thread_ts') for c in self.slack.call_args_list],
                         [None, 'parent-ts', 'parent-ts', 'parent-ts', 'parent-ts'])
        self.assertEqual(self.slack.call_args_list[2], self.slack.call_args_list[3])
        self.assertEqual(self.job()['status'], 'completed')

    def test_long_infrastructure_reply_keeps_job_and_run_link_within_slack_limit(self):
        self.add_failure()
        report = self.infrastructure_job()
        report['findings'][0]['evidence'] = 'Runner unavailable. ' * 1000
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=? WHERE id=?', (json.dumps(report), 'job'))
        triage.publish(self.conn, self.job())
        reply = self.slack.call_args.args[1]
        self.assertLessEqual(len(reply), monitor.SLACK_MESSAGE_LIMIT)
        self.assertIn('CI infrastructure incident: linux', reply)
        self.assertIn('https://github.test/run/1', reply)

    def test_webhook_fallback_preserves_infrastructure_summary_and_detail(self):
        self.add_failure()
        self.infrastructure_job()
        transport = monitor.SlackTransport('webhook', webhook='test')
        with mock.patch.object(monitor, 'load_slack_transport', return_value=transport):
            triage.publish(self.conn, self.job())
        self.slack.assert_called_once()
        self.assertIn('CI infrastructure incident: linux', self.slack.call_args.args[1])
        self.assertIn('runner_id=0', self.slack.call_args.args[1])
        self.assertEqual(self.job()['status'], 'completed')

    def test_slack_retry_reuses_cached_notice_after_restart_and_recovery(self):
        self.add_failure()
        self.infrastructure_job()
        self.slack.return_value = (False, None)
        with self.assertRaisesRegex(RuntimeError, 'cached report'):
            triage.publish(self.conn, self.job())
        cached = json.loads(self.job()['report_json'])['findings'][0]
        self.assertEqual(self.job()['status'], 'publishing')
        self.assertEqual(triage.pending(self.conn), [])
        self.reopen()
        with self.conn:
            self.conn.execute("UPDATE known_failures SET status='fixed',diagnosis='later pass'")
        self.slack.return_value = (True, 'notice')
        triage.publish(self.conn, self.job())
        self.assertEqual([c.args[1] for c in self.slack.call_args_list],
                         [triage.INFRASTRUCTURE_SLACK_SUMMARY, triage.INFRASTRUCTURE_SLACK_SUMMARY,
                          cached['slack_text'] + '\n\n' + cached['slack_detail']])
        self.assertEqual(self.slack.call_args_list[-1].kwargs['thread_ts'], 'notice')
        self.assertEqual(self.job()['status'], 'completed')
        self.assertEqual(self.conn.execute('SELECT status FROM known_failures').fetchone()[0], 'fixed')
        self.assertEqual(self.github.calls, [])

    def test_slack_detail_retry_uses_cached_thread_without_reposting_summary(self):
        self.add_failure()
        self.infrastructure_job()
        self.slack.side_effect = [(True, 'parent-ts'), (False, None), (True, 'reply-ts')]
        with self.assertRaisesRegex(RuntimeError, 'detail pending'):
            triage.publish(self.conn, self.job())
        cached = json.loads(self.job()['report_json'])['findings'][0]
        self.assertEqual(cached['slack_thread_ts'], 'parent-ts')
        self.reopen()
        triage.publish(self.conn, self.job())
        self.assertEqual(self.slack.call_count, 3)
        self.assertEqual([c.kwargs.get('thread_ts') for c in self.slack.call_args_list],
                         [None, 'parent-ts', 'parent-ts'])
        self.assertEqual(self.job()['status'], 'completed')

    def test_partial_publication_retry_does_not_repeat_an_infrastructure_notice(self):
        self.add_failure()
        self.add_failure('Other failure')
        report = self.infrastructure_job()
        observations = json.loads(self.job()['observations_json'])
        report['findings'][0]['failure_ids'] = [observations[0]['failure_id']]
        report['findings'].append(dict(failure_ids=[observations[1]['failure_id']], outcome='product',
            diagnosis='Product regression', evidence='assertion failed',
            issue=dict(title='Fix regression', body='Failure evidence', existing_number=None)))
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=?', (json.dumps(report),))
        self.github.lose_response_to = ('POST', 'issues')
        with self.assertRaises(RuntimeError):
            triage.publish(self.conn, self.job())
        self.reopen()
        triage.publish(self.conn, self.job())
        self.assertEqual(self.slack.call_count, 3)
        self.assertIn('1 product issue', self.slack.call_args_list[-1].args[1])
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(self.job()['status'], 'completed')

    def separate_product_findings(self):
        report = self.make_job()
        observations = json.loads(self.job()['observations_json'])
        report['findings'] = [dict(failure_ids=[o['failure_id']], outcome='product',
            diagnosis='Assertion failed', evidence='Failure log',
            issue=dict(title='Fix ' + o['identity'], body='Product defect evidence', existing_number=None))
            for o in observations]
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=? WHERE id=?', (json.dumps(report), 'job'))
        return report

    def test_product_report_announces_created_and_reopened_issues_once(self):
        self.add_failure()
        self.add_failure('Second failure')
        report = self.separate_product_findings()
        report['findings'][1]['issue']['existing_number'] = 77
        self.github.issues[77] = dict(number=77, title='Existing <defect> & regression | fix', body='Original body',
                                     state='closed', labels=[])
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=? WHERE id=?', (json.dumps(report), 'job'))
        triage.publish(self.conn, self.job())
        self.reopen()
        triage.publish(self.conn, self.job())
        triage.deliver_product_announcements(self.conn)
        self.assertEqual(self.slack.call_count, 1)
        text = self.slack.call_args.args[1]
        self.assertIn('2 product issues', text)
        self.assertIn(f'<https://github.com/{monitor.REPO_NAME}/issues/78|#78: Fix Cargo nextest>', text)
        self.assertIn(f'<https://github.com/{monitor.REPO_NAME}/issues/77|#77: Existing &lt;defect&gt; &amp; regression ¦ fix>', text)
        self.assertNotIn('job', text)
        self.assertNotIn('session', text)
        self.assertIsNone(self.slack.call_args.kwargs['thread_ts'])
        self.assertEqual(self.github.issues[77]['state'], 'open')

    def test_product_announcement_deduplicates_a_reused_ticket(self):
        self.add_failure()
        self.add_failure('Second failure')
        report = self.separate_product_findings()
        for finding in report['findings']:
            finding['issue']['existing_number'] = 77
        self.github.issues[77] = dict(number=77, title='Shared cause', body='Original body', state='open', labels=[])
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=?', (json.dumps(report),))
        triage.publish(self.conn, self.job())
        self.assertEqual(self.slack.call_count, 1)
        text = self.slack.call_args.args[1]
        self.assertIn('1 product issue', text)
        self.assertEqual(text.count('/issues/77'), 1)

    def test_partial_product_publication_waits_for_the_complete_report(self):
        self.add_failure()
        self.add_failure('Second failure')
        self.separate_product_findings()
        original = triage.publish_issue
        def publish_first(conn, job, index, *args):
            if index == 1:
                raise RuntimeError('GitHub unavailable')
            return original(conn, job, index, *args)
        with mock.patch.object(triage, 'publish_issue', side_effect=publish_first):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                triage.publish(self.conn, self.job())
        self.slack.assert_not_called()
        self.assertEqual(len(self.github.issues), 1)
        # A report partly published by the previous code has no cached issue title.
        report = json.loads(self.job()['report_json'])
        report['findings'][0].pop('published_issue')
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=?', (json.dumps(report),))
        self.reopen()
        triage.publish(self.conn, self.job())
        self.assertEqual(len(self.github.issues), 2)
        self.assertEqual(self.slack.call_count, 1)
        self.assertIn('/issues/101', self.slack.call_args.args[1])
        self.assertIn('/issues/102', self.slack.call_args.args[1])

    def test_product_slack_outage_does_not_block_selection_or_repeat_github_writes(self):
        self.add_failure()
        self.make_job()
        self.slack.return_value = (False, None)
        triage.publish(self.conn, self.job())
        self.assertEqual(self.job()['status'], 'completed')
        self.assertEqual(triage.pending(self.conn), [])
        cached = self.slack.call_args.args[1]
        announcement = self.conn.execute('SELECT * FROM triage_product_announcements').fetchone()
        self.assertIsNone(announcement['completed_at'])
        self.assertEqual(announcement['posted_count'], 0)
        self.assertIsNotNone(announcement['last_error'])
        self.reopen()
        self.add_failure('New failure')
        self.patch(monitor, 'update_known_failures')
        self.patch(triage.uuid, 'uuid4', return_value=SimpleNamespace(hex='next'))
        self.patch(triage, 'gh_api', side_effect=lambda endpoint, **kwargs:
                   {'sha': 'a' * 40} if endpoint == 'commits/master' else self.github(endpoint, **kwargs))
        self.patch(monitor, 'require_mj_success', side_effect=lambda args, **kwargs:
                   '{"sessions":[]}' if args[0] == 'sessions' else '{"session_id":"next-session"}')
        triage.tick(self.conn)
        self.assertEqual(self.job('next')['status'], 'running')
        calls = copy.deepcopy(self.github.calls)
        self.github.issues[101]['title'] = 'Later title edit'
        self.slack.return_value = (True, 'notice')
        triage.deliver_product_announcements(self.conn)
        triage.deliver_product_announcements(self.conn)
        self.assertEqual(self.github.calls, calls)
        self.assertEqual(self.slack.call_args.args[1], cached)
        self.assertEqual(self.slack.call_count, 3)
        self.assertIsNotNone(self.conn.execute('SELECT completed_at FROM triage_product_announcements').fetchone()[0])

    def test_large_issue_list_retries_only_remaining_thread_replies(self):
        for number in range(20):
            self.add_failure('Failure ' + str(number) + ' ' + 'x' * 210)
        self.separate_product_findings()
        self.slack.side_effect = [(True, 'parent'), (False, None)]
        triage.publish(self.conn, self.job())
        self.assertEqual(self.job()['status'], 'completed')
        announcement = self.conn.execute('SELECT * FROM triage_product_announcements').fetchone()
        self.assertEqual(announcement['posted_count'], 1)
        self.assertEqual(announcement['thread_ts'], 'parent')
        messages = json.loads(announcement['messages_json'])
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= monitor.SLACK_MESSAGE_LIMIT for message in messages))
        for number in self.github.issues:
            self.assertEqual('\n'.join(messages).count(f'/issues/{number}|'), 1)
        self.reopen()
        self.slack.side_effect = None
        self.slack.return_value = (True, 'reply')
        triage.deliver_product_announcements(self.conn)
        triage.deliver_product_announcements(self.conn)
        self.assertEqual(self.slack.call_count, len(messages) + 1)
        self.assertEqual(sum(call.kwargs['thread_ts'] is None for call in self.slack.call_args_list), 1)
        self.assertTrue(all(call.kwargs['thread_ts'] == 'parent' for call in self.slack.call_args_list[1:]))

    def test_product_announcement_supports_webhook_without_timestamp(self):
        self.add_failure()
        self.make_job()
        self.patch(monitor, 'load_slack_transport', return_value=monitor.SlackTransport('webhook', webhook='test'))
        self.slack.return_value = (True, None)
        triage.publish(self.conn, self.job())
        self.assertEqual(self.slack.call_count, 1)
        self.assertIsNotNone(self.conn.execute('SELECT completed_at FROM triage_product_announcements').fetchone()[0])

    def test_upgrade_does_not_announce_completed_history(self):
        self.add_failure()
        self.make_job(status='completed')
        with self.conn:
            self.conn.execute('DROP TABLE triage_product_announcements')
        self.reopen()
        triage.deliver_product_announcements(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM triage_product_announcements').fetchone()[0], 0)
        self.slack.assert_not_called()

    def test_issue_retry_uses_prepared_body_even_if_a_later_run_arrives(self):
        self.add_failure()
        self.make_job()
        with mock.patch.object(triage, 'publish_issue', side_effect=RuntimeError('GitHub unavailable')):
            with self.assertRaises(RuntimeError):
                triage.publish(self.conn, self.job())
        cached = json.loads(self.job()['report_json'])['findings'][0]['issue_body']
        self.reopen()
        with self.conn:
            self.conn.execute("UPDATE known_failures SET last_seen_run_id=2,last_seen_run_url='https://github.test/run/2'")
        with mock.patch.object(triage, 'issue_body', side_effect=AssertionError('must not render on retry')):
            triage.publish(self.conn, self.job())
        self.assertEqual(self.github.issues[101]['body'], cached)
        self.assertIn('https://github.test/run/1', cached)
        self.assertNotIn('https://github.test/run/2', cached)

    def test_publication_outage_does_not_reinvestigate_or_block_other_observations(self):
        self.add_failure()
        self.infrastructure_job()
        self.add_failure('New product failure')
        self.slack.return_value = (False, None)
        self.patch(monitor, 'update_known_failures')
        self.patch(triage.uuid, 'uuid4', return_value=SimpleNamespace(hex='next'))
        self.patch(triage, 'gh_api', side_effect=lambda endpoint, **kwargs:
                   {'sha': 'a' * 40} if endpoint == 'commits/master' else self.github(endpoint, **kwargs))
        mj = self.patch(monitor, 'require_mj_success', side_effect=lambda args, **kwargs:
                        '{"sessions":[]}' if args[0] == 'sessions' else '{"session_id":"next-session"}')
        triage.tick(self.conn)
        self.assertEqual(self.job()['status'], 'publishing')
        self.assertEqual(self.job('next')['status'], 'running')
        self.assertEqual([o['identity'] for o in json.loads(self.job('next')['observations_json'])], ['New product failure'])
        self.assertEqual([c.args[0][0] for c in mj.call_args_list], ['sessions', 'new'])

    def test_old_unclassified_report_is_corrected_before_any_issue_write(self):
        self.add_failure()
        report = self.make_job()
        del report['findings'][0]['outcome']
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=?', (json.dumps(report),))
        prompt = self.patch(monitor, 'send_session_message')
        triage.publish(self.conn, self.job())
        prompt.assert_called_once()
        self.assertIn('do not repeat the', prompt.call_args.args[1])
        self.assertEqual(self.job()['status'], 'running')
        self.assertEqual(self.github.calls, [])

    def test_report_enforces_routing_instead_of_accepting_infrastructure_issue_drafts(self):
        self.add_failure()
        report = self.make_job()
        observations = json.loads(self.job()['observations_json'])
        for outcome, ticket in [('infrastructure', report['findings'][0]['issue']),
                                ('resolved', report['findings'][0]['issue']), ('product', None), ('unknown', None)]:
            invalid = copy.deepcopy(report)
            invalid['findings'][0].update(outcome=outcome, issue=ticket)
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                triage.parse_report('triage-result: ' + json.dumps(invalid), observations)

    def test_schema_migrates_existing_ledger_additively(self):
        self.add_failure()
        self.conn.execute("ALTER TABLE known_failures DROP COLUMN triage_issue_url")
        self.conn.execute("ALTER TABLE known_failures DROP COLUMN triage_issue_state")
        self.conn.execute("ALTER TABLE known_failures DROP COLUMN triage_outcome")
        self.conn.execute("ALTER TABLE triage_observations DROP COLUMN resolved_run_id")
        self.conn.execute("DROP TABLE triage_infrastructure_threads")
        self.conn.commit()
        self.reopen()
        row = self.conn.execute("SELECT * FROM known_failures").fetchone()
        self.assertEqual(row["identity"], "Cargo nextest")
        self.assertIsNone(row["triage_issue_url"])
        self.assertEqual(row["triage_issue_state"], "OPEN")
        self.assertIsNone(row["triage_outcome"])
        self.assertIn("resolved_run_id", {r["name"] for r in self.conn.execute("PRAGMA table_info(triage_observations)")})
        self.assertEqual(self.conn.execute('SELECT count(*) FROM triage_infrastructure_threads').fetchone()[0], 0)

    def test_upgrade_reuses_cached_infrastructure_classification_without_publication(self):
        self.add_failure()
        self.infrastructure_job()
        triage.publish(self.conn, self.job())
        with self.conn:
            self.conn.execute('ALTER TABLE known_failures DROP COLUMN triage_outcome')
            self.conn.execute("DELETE FROM known_failure_state WHERE key='triage_outcomes_v1'")
        self.reopen()
        self.assertEqual(self.conn.execute('SELECT triage_outcome FROM known_failures').fetchone()[0], 'infrastructure')
        triage.ensure_schema(self.conn)
        self.assertEqual(self.slack.call_count, 2)
        self.assertEqual(self.github.calls, [])

    def test_cached_classification_does_not_apply_to_changed_observations(self):
        self.add_failure()
        self.infrastructure_job()
        triage.publish(self.conn, self.job())
        for changed in ["last_seen_sha='b'", "last_seen_failed_steps_json='[\"New step\"]'"]:
            with self.subTest(changed=changed), self.conn:
                self.conn.execute("UPDATE known_failures SET last_seen_sha=?,last_seen_failed_steps_json='[]',triage_outcome=NULL",
                                  ('a' * 40,))
                self.conn.execute('UPDATE known_failures SET ' + changed)
                self.conn.execute("DELETE FROM known_failure_state WHERE key='triage_outcomes_v1'")
            triage.backfill_outcomes(self.conn)
            self.assertIsNone(self.conn.execute('SELECT triage_outcome FROM known_failures').fetchone()[0])

    def test_groups_shared_cause_into_one_issue_and_preserves_repair_eligibility(self):
        self.add_failure()
        self.add_failure("Cargo clippy")
        self.make_job()
        triage.publish(self.conn, self.job())
        self.assertEqual(len(self.github.issues), 1)
        rows, _ = monitor._failure_rows_for_prompt(self.conn, omit_linked=True)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["triage_issue_url"].endswith("/101") for r in rows))
        self.assertTrue(all(r["status"] == "open" and r["diagnosis_source"] == "triage session session" for r in rows))
        self.assertTrue(all(r['triage_outcome'] == 'product' for r in rows))
        with self.conn:
            self.conn.execute("UPDATE known_failures SET linked_issue_url='human-owned'")
        self.assertEqual(monitor._failure_rows_for_prompt(self.conn, omit_linked=True)[0], [])

    def test_repeated_run_same_commit_is_deduplicated_but_new_commit_is_pending(self):
        self.add_failure()
        self.make_job()
        triage.publish(self.conn, self.job())
        with self.conn:
            self.conn.execute("UPDATE known_failures SET last_seen_run_id=2,last_seen_at='2026-01-02'")
        self.reopen()
        self.assertEqual(triage.pending(self.conn), [])
        with self.conn:
            self.conn.execute("UPDATE known_failures SET last_seen_sha=?", ("b" * 40,))
        self.assertEqual(len(triage.pending(self.conn)), 1)

    def test_fixed_and_superseded_observations_are_not_published_or_diagnosed(self):
        for status, sha in (("fixed", "a" * 40), ("open", "b" * 40)):
            with self.subTest(status=status):
                self.conn.execute("DELETE FROM known_failures")
                self.conn.execute("DELETE FROM triage_jobs")
                self.conn.execute("DELETE FROM triage_observations")
                self.add_failure()
                self.make_job()
                with self.conn:
                    self.conn.execute("UPDATE known_failures SET status=?,last_seen_sha=?", (status, sha))
                triage.publish(self.conn, self.job())
                self.assertEqual(self.github.calls, [])
                self.slack.assert_not_called()
                self.assertIsNone(self.conn.execute("SELECT diagnosis FROM known_failures").fetchone()[0])

    def test_observation_changed_during_publication_is_not_overwritten(self):
        self.add_failure()
        self.make_job()
        original = triage.publish_issue
        def changed(*args):
            result = original(*args)
            with self.conn:
                self.conn.execute("UPDATE known_failures SET last_seen_sha=?,diagnosis='newer diagnosis'", ("b" * 40,))
            return result
        with mock.patch.object(triage, "publish_issue", side_effect=changed):
            triage.publish(self.conn, self.job())
        row = self.conn.execute("SELECT * FROM known_failures").fetchone()
        self.assertEqual(row["diagnosis"], "newer diagnosis")
        self.assertIsNone(row["triage_issue_url"])
        self.assertEqual(len(triage.pending(self.conn)), 1)

    def test_lost_issue_create_response_is_recovered_after_restart(self):
        self.add_failure()
        self.make_job()
        self.github.lose_response_to = ("POST", "issues")
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            triage.publish(self.conn, self.job())
        self.reopen()
        triage.publish(self.conn, self.job())
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(sum(method == "POST" and endpoint == "issues" for method, endpoint, _ in self.github.calls), 1)
        self.assertEqual(self.job()["status"], "completed")

    def test_lost_existing_issue_comment_response_is_not_posted_twice(self):
        self.add_failure()
        self.make_job(existing=77)
        self.github.issues[77] = dict(number=77, title="Original title", body="Original body", state="closed", labels=[])
        self.github.lose_response_to = ("POST", "issues/77/comments")
        with self.assertRaises(RuntimeError):
            triage.publish(self.conn, self.job())
        self.reopen()
        triage.publish(self.conn, self.job())
        self.assertEqual(len(self.github.comments[77]), 1)
        self.assertEqual(self.github.issues[77]["state"], "open")
        self.assertEqual(self.github.issues[77]["body"], "Original body")
        self.assertEqual(self.github.issues[77]["labels"], [{"name": "buildfailure"}])

    def test_fixer_escalation_created_during_investigation_is_reused(self):
        self.add_failure()
        self.make_job()
        self.github.issues[77] = dict(number=77, title="Fixer escalation", body="Human-owned", state="open", labels=[])
        with self.conn:
            self.conn.execute("UPDATE known_failures SET linked_issue_url=?",
                              (f"https://github.com/{monitor.REPO_NAME}/issues/77",))
        triage.publish(self.conn, self.job())
        self.assertEqual(set(self.github.issues), {77})
        self.assertEqual(len(self.github.comments[77]), 1)

    def test_fixer_lock_defers_publication_without_changing_job(self):
        self.add_failure()
        self.make_job()
        with triage.lock(monitor.LOCK_PATH) as acquired:
            self.assertTrue(acquired)
            triage.publish(self.conn, self.job())
        self.assertEqual(self.job()["status"], "publishing")
        self.assertEqual(self.github.calls, [])

    def test_active_fixer_invocation_defers_publication_after_crash(self):
        self.add_failure()
        self.make_job()
        with self.conn:
            self.conn.execute("INSERT INTO invocations(workflow_run_id,sha,workflow_run_url,conclusion,observed_at,started_at,status) "
                              "VALUES (1,'sha','url','failure','now','now','running')")
        triage.publish(self.conn, self.job())
        self.assertEqual(self.job()["status"], "publishing")
        self.assertEqual(self.github.calls, [])

    def test_resolved_finding_retires_the_row_without_deleting_history_or_creating_ticket(self):
        self.add_failure()
        self.make_job(resolved=True)
        triage.publish(self.conn, self.job())
        self.assertEqual(self.github.calls, [])
        self.slack.assert_not_called()
        row = self.conn.execute("SELECT * FROM known_failures").fetchone()
        self.assertEqual(row["status"], "fixed")
        self.assertIsNotNone(row["fixed_at"])
        self.assertIsNone(row["fixed_by_sha"])
        self.assertEqual(row["diagnosis_source"], "triage session session")
        self.assertIn("Master has no known failures.", monitor._known_failure_issue_body(self.conn))
        self.assertEqual(monitor.render_known_failures_prompt(self.conn), "")
        self.assertEqual(triage.pending(self.conn), [])

    def test_newer_failure_run_on_same_commit_is_not_retired_by_an_old_report(self):
        self.add_failure()
        self.make_job(resolved=True)
        with self.conn:
            self.conn.execute("UPDATE known_failures SET last_seen_run_id=2")
        triage.publish(self.conn, self.job())
        row = self.conn.execute("SELECT * FROM known_failures").fetchone()
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["diagnosis"])
        self.assertEqual(len(triage.pending(self.conn)), 1)

    def test_ci_recurrence_reopens_and_retriages_even_with_identical_fingerprint(self):
        self.add_failure()
        with self.conn:
            self.conn.execute("UPDATE known_failures SET last_seen_failed_steps_json=?", (json.dumps(["Cargo nextest"]),))
        self.make_job(resolved=True)
        before = triage.fingerprint(self.conn.execute("SELECT * FROM known_failures").fetchone())
        triage.publish(self.conn, self.job())
        report = automerge.FailureReport(frozenset({"CI/linux"}), {
            "CI/linux": automerge.FailedJobDetails(frozenset({"Cargo nextest"}), frozenset())}, "failure log")
        with mock.patch.object(automerge, "collect_failure_report_for_run", return_value=report):
            monitor._process_known_failure_run(self.conn, "CI", {
                "databaseId": 2, "headSha": "a" * 40, "url": "https://github.test/run/2", "conclusion": "failure"})
        row = self.conn.execute("SELECT * FROM known_failures").fetchone()
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["diagnosis"])
        self.assertEqual(triage.fingerprint(row), before)
        self.assertEqual(len(triage.pending(self.conn)), 1)
        self.make_job(job_id="recurrence")
        triage.publish(self.conn, self.job("recurrence"))
        cached = self.conn.execute("SELECT * FROM triage_observations").fetchone()
        self.assertIsNone(cached["resolved_run_id"])
        self.assertIsNotNone(cached["issue_url"])
        self.assertEqual(triage.pending(self.conn), [])

    def legacy_resolved_job(self):
        self.add_failure()
        report = self.make_job(status="completed", resolved=True)
        o = json.loads(self.job()["observations_json"])[0]
        with self.conn:
            self.conn.execute("INSERT INTO triage_observations "
                              "(fingerprint,job_id,diagnosis,issue_url,completed_at) VALUES (?,'job',?,NULL,'2026-01-01')",
                              (o["fingerprint"], report["findings"][0]["diagnosis"]))

    def test_completed_legacy_resolutions_are_reconciled_once(self):
        self.legacy_resolved_job()
        self.assertEqual(triage.reconcile_resolved(self.conn), 1)
        self.assertEqual(triage.reconcile_resolved(self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT status FROM known_failures").fetchone()[0], "fixed")
        self.assertEqual(self.conn.execute("SELECT resolved_run_id FROM triage_observations").fetchone()[0], 1)

    def test_legacy_resolution_cannot_retire_a_newer_run(self):
        self.legacy_resolved_job()
        with self.conn:
            self.conn.execute("UPDATE known_failures SET last_seen_run_id=2")
        self.assertEqual(triage.reconcile_resolved(self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT status FROM known_failures").fetchone()[0], "open")
        self.assertEqual(len(triage.pending(self.conn)), 1)

    def test_legacy_resolution_cannot_replace_a_newer_diagnosis(self):
        self.legacy_resolved_job()
        with self.conn:
            self.conn.execute("UPDATE triage_observations SET job_id='newer-job'")
            self.conn.execute("UPDATE known_failures SET diagnosis='new failure'")
        self.assertEqual(triage.reconcile_resolved(self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT status,diagnosis FROM known_failures").fetchone()[:],
                         ("open", "new failure"))

    def test_aggregate_issue_cannot_be_used_as_individual_failure_ticket(self):
        self.add_failure()
        self.make_job(existing=77)
        self.github.issues[77] = dict(number=77, title=monitor.KNOWN_FAILURE_ISSUE_TITLE, body="index", state="open", labels=[])
        with self.assertRaisesRegex(ValueError, "not an individual"):
            triage.publish(self.conn, self.job())
        self.assertEqual([method for method, _, _ in self.github.calls], ["GET"])

    def test_report_rejects_duplicate_missing_or_invented_ids(self):
        self.add_failure()
        valid = self.make_job()
        observations = json.loads(self.job()["observations_json"])
        for ids in ([1, 1], [2], [], [True]):
            report = copy.deepcopy(valid)
            report["findings"][0]["failure_ids"] = ids
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                triage.parse_report("triage-result: " + json.dumps(report), observations)
        with self.assertRaisesRegex(ValueError, "missing failure_ids"):
            triage.parse_report('triage-result: {"findings":[]}', observations)

    def test_launch_persists_intent_and_uses_small_flash_session(self):
        self.add_failure()
        self.make_job(status="queued")
        def mj(args, **kwargs):
            if args[0] == "sessions":
                return '{"sessions":[]}'
            self.assertEqual(self.job()["status"], "launching")
            for flag, value in (("--cpus", "2"), ("--memory-gib", "4"), ("--model", "deepseek-flash"),
                                ("--subagents", "none"), ("--workspace", "CI")):
                self.assertEqual(args[args.index(flag) + 1], value)
            prompt = Path(args[args.index("--prompt-file") + 1]).read_text()
            self.assertIn("Cargo nextest", prompt)
            self.assertNotIn("--profile", args)
            return '{"session_id":"small-session"}'
        self.patch(monitor, "require_mj_success", side_effect=mj)
        triage.launch(self.conn, self.job())
        self.assertEqual(self.job()["session_id"], "small-session")

    def test_ambiguous_launch_is_adopted_after_restart_without_duplicate(self):
        self.add_failure()
        self.make_job(status="queued")
        mj = self.patch(monitor, "require_mj_success", side_effect=['{"sessions":[]}', RuntimeError("lost response")])
        with self.assertRaises(RuntimeError):
            triage.launch(self.conn, self.job())
        self.reopen()
        mj.side_effect = None
        mj.return_value = json.dumps({"sessions": [{"id": "adopted", "title": self.job()["title"]}]})
        triage.launch(self.conn, self.job())
        self.assertEqual(self.job()["session_id"], "adopted")
        self.assertEqual(sum(call.args[0][0] == "new" for call in mj.call_args_list), 1)

    def test_ambiguous_launch_with_no_session_does_not_relaunch(self):
        self.add_failure()
        self.make_job(status="launching")
        mj = self.patch(monitor, "require_mj_success", return_value='{"sessions":[]}')
        with self.assertRaisesRegex(RuntimeError, "launch outcome unknown"):
            triage.launch(self.conn, self.job())
        self.assertEqual([call.args[0][0] for call in mj.call_args_list], ["sessions"])

    def test_running_session_has_no_deadline_and_is_not_suspended(self):
        self.add_failure()
        self.make_job(status="running")
        self.patch(monitor, "wait_once", return_value=monitor.TurnResult("running", "timeout", timed_out=True))
        mj = self.patch(monitor, "require_mj_success")
        triage.collect_report(self.conn, self.job())
        triage.cleanup(self.conn)
        self.assertEqual(self.job()["status"], "running")
        mj.assert_not_called()

    def test_report_collected_before_publication_and_cleanup_only_after_completion(self):
        self.add_failure()
        report = self.make_job(status="running")
        self.patch(monitor, "wait_once", return_value=monitor.TurnResult("completed", "finished"))
        self.patch(monitor, "read_final_agent_message", return_value="triage-result: " + json.dumps(report))
        mj = self.patch(monitor, "require_mj_success", return_value="{}")
        triage.collect_report(self.conn, self.job())
        self.reopen()
        triage.cleanup(self.conn)
        mj.assert_not_called()
        triage.publish(self.conn, self.job())
        triage.cleanup(self.conn)
        triage.cleanup(self.conn)
        self.assertEqual([call.args[0][0] for call in mj.call_args_list], ["suspend"])

    def test_malformed_report_correction_does_not_duplicate_prompts(self):
        self.add_failure()
        self.make_job(status="running")
        self.patch(monitor, "wait_once", return_value=monitor.TurnResult("completed", "finished"))
        self.patch(monitor, "read_final_agent_message", return_value="Here are some thoughts without a report.")
        prompt = self.patch(monitor, "send_session_message",
                            side_effect=[monitor.MjError('lost reply'), None])
        with self.assertRaises(monitor.MjError):
            triage.collect_report(self.conn, self.job())
        self.assertIsNone(self.job()['feedback_digest'])
        triage.collect_report(self.conn, self.job())
        self.reopen()
        triage.collect_report(self.conn, self.job())
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(prompt.call_args_list[0].kwargs['request_id'], prompt.call_args_list[1].kwargs['request_id'])

    def recovery(self):
        return json.loads(self.job()['recovery_json'])

    def start_recovery(self):
        self.add_failure()
        report = self.make_job(status='running')
        self.wait = self.patch(monitor, 'wait_once', return_value=monitor.TurnResult('error', 'error', turn_id=14))
        triage.collect_report(self.conn, self.job())
        return report

    def test_recovery_survives_lost_clear_and_restart_replies_in_same_session(self):
        report = self.start_recovery()
        identity = self.recovery()['id']
        self.assertIn('Cargo nextest', self.recovery()['prompt'])
        self.assertIn('reuse collected evidence', self.recovery()['prompt'])
        self.slack.assert_not_called()

        def mj(args, **kwargs):
            self.assertEqual(args[args.index('--session') + 1], 'session')
            if args[0] == 'sessions':
                return json.dumps(dict(state='running', is_idle=False, chat_phase='working'))
            if args[0] == 'transcript':
                if args[args.index('--after-seq') + 1] == '0':
                    return '{"latest_seq":2869}'
                return json.dumps(dict(items=[dict(seq=2870, stable_id='context-cleared:triage-clear-' + identity + '-0')]))
            self.assertIn(args[0], ['clear-queue', 'stop-task'])
            return '{}'

        native = self.patch(monitor, 'require_mj_success', side_effect=mj)
        interrupt = self.patch(monitor, 'interrupt_turn')
        send = self.patch(triage.speculation, 'send_once', side_effect=[monitor.MjError('lost clear reply'), {},
                                                                    monitor.MjError('lost restart reply'), {}])
        self.reopen()
        triage.collect_report(self.conn, self.job())
        self.assertEqual(self.recovery()['stage'], 'clear')
        interrupt.assert_called_once_with('session')
        for stage in ('clear', 'restart'):
            with self.assertRaises(monitor.MjError):
                triage.collect_report(self.conn, self.job())
            self.assertEqual(self.recovery()['stage'], stage)
            self.reopen()
            triage.collect_report(self.conn, self.job())
            if stage == 'clear':
                self.reopen()
                triage.collect_report(self.conn, self.job())
                self.assertEqual(self.job()['report_after_seq'], 2870)
        self.assertEqual(self.recovery()['stage'], 'running')
        self.assertIsNone(self.job()['last_error'])
        self.assertEqual([call.args[3] for call in send.call_args_list],
                         ['triage-clear-' + identity + '-0'] * 2 + ['triage-restart-' + identity] * 2)
        self.assertEqual(self.slack.call_count, 2)  # Only the failed-recovery alert and its session detail.
        self.assertNotIn('`session`', self.slack.call_args_list[0].args[1])
        self.assertIn('`session`', self.slack.call_args_list[1].args[1])
        triage.collect_report(self.conn, self.job())  # Sticky old failed turn cannot reset again.
        self.assertEqual(send.call_count, 4)
        self.wait.return_value = monitor.TurnResult('completed', 'finished', turn_id=55)
        final = self.patch(monitor, 'read_final_agent_message', return_value='triage-result: ' + json.dumps(report))
        triage.collect_report(self.conn, self.job())
        final.assert_called_once_with('session', after_seq=2870)
        self.assertEqual(self.job()['status'], 'publishing')
        self.assertNotIn('new', [call.args[0][0] for call in native.call_args_list])

    def test_clear_boundary_must_match_and_confirmed_failure_gets_new_command(self):
        self.start_recovery()
        recovery = self.recovery()
        recovery.update(stage='cleared', cursor=2869)
        triage.save_recovery(self.conn, self.job(), recovery)
        native = self.patch(monitor, 'require_mj_success', return_value=json.dumps(dict(
            items=[dict(seq=2870, stable_id='unrelated:triage-clear-' + recovery['id'] + '-0')],
            next_after_seq=2870)))
        outcome = self.patch(triage.speculation, 'command_outcome', return_value=(None, 80))
        triage.collect_report(self.conn, self.job())
        self.assertEqual(self.recovery()['stage'], 'cleared')
        self.assertEqual(self.recovery()['cursor'], 2870)
        self.assertEqual(self.job()['report_after_seq'], 0)
        self.reopen()
        outcome.return_value = (dict(outcome='failed', message='clear rejected'), 81)
        with self.assertRaisesRegex(RuntimeError, 'context clear failed'):
            triage.collect_report(self.conn, self.job())
        self.assertEqual(self.recovery()['stage'], 'stop')
        self.assertEqual(self.recovery()['clear_retry'], 1)
        self.assertIn('clear step failed', self.slack.call_args_list[-2].args[1])
        recovery = self.recovery()
        recovery['stage'] = 'clear'
        triage.save_recovery(self.conn, self.job(), recovery)
        send = self.patch(triage.speculation, 'send_once')
        triage.collect_report(self.conn, self.job())
        self.assertEqual(send.call_args.args[3], 'triage-clear-' + recovery['id'] + '-1')

    def test_recovery_resumes_a_stopped_session_without_creating_another(self):
        self.start_recovery()
        native = self.patch(monitor, 'require_mj_success', side_effect=['{"state":"stopped"}', '{}'])
        triage.collect_report(self.conn, self.job())
        self.assertEqual(self.recovery()['stage'], 'stop')
        self.assertEqual([call.args[0] for call in native.call_args_list],
                         [['sessions', '--session', 'session', '--json'],
                          ['resume', '--session', 'session', '--queue', 'discard', '--json']])

    def test_quota_and_input_require_action_without_resetting_or_repeated_notices(self):
        self.add_failure()
        native = self.patch(monitor, 'require_mj_success')
        for outcome, action in [('quota_limit', 'Restore provider capacity'),
                                ('input_required', 'Respond to the session')]:
            with self.subTest(outcome=outcome):
                self.make_job(status='running', job_id=outcome)
                self.patch(monitor, 'wait_once', return_value=monitor.TurnResult(outcome, outcome, turn_id=14))
                self.slack.reset_mock()
                for _ in range(3):
                    triage.collect_report(self.conn, self.job(outcome))
                    self.reopen()
                recovery = json.loads(self.job(outcome)['recovery_json'])
                self.assertEqual(recovery['stage'], 'blocked')
                self.assertEqual(self.slack.call_count, 2)
                self.assertIn(action, self.slack.call_args_list[0].args[1])
        native.assert_not_called()

    def test_slack_outage_does_not_block_recovery_and_notice_retries(self):
        self.slack.return_value = (False, None)
        self.start_recovery()
        self.slack.assert_not_called()
        self.patch(monitor, 'require_mj_success', side_effect=[monitor.MjError('daemon unreachable'),
            monitor.MjError('daemon unreachable'), '{"state":"running"}', '{}', '{}', '{"latest_seq":2869}'])
        self.patch(monitor, 'interrupt_turn')
        with self.assertRaises(monitor.MjError):
            triage.collect_report(self.conn, self.job())
        self.assertNotIn('failure_notified', self.recovery())
        self.slack.return_value = (True, 'notice')
        with self.assertRaises(monitor.MjError):
            triage.collect_report(self.conn, self.job())
        self.assertTrue(self.recovery()['failure_notified'])
        self.assertEqual(self.slack.call_count, 3)
        triage.collect_report(self.conn, self.job())
        self.assertEqual(self.recovery()['stage'], 'clear')
        self.patch(triage.speculation, 'send_once')
        triage.collect_report(self.conn, self.job())
        self.assertEqual(self.recovery()['stage'], 'cleared')
        self.assertEqual(self.slack.call_count, 3)

    def test_new_completed_turn_can_correct_the_same_bad_report_again(self):
        self.add_failure()
        self.make_job(status='running')
        turn = self.patch(monitor, 'wait_once', return_value=monitor.TurnResult('completed', 'finished', turn_id=14))
        self.patch(monitor, 'read_final_agent_message', return_value='missing report')
        send = self.patch(monitor, 'send_session_message')
        triage.collect_report(self.conn, self.job())
        self.reopen()
        triage.collect_report(self.conn, self.job())
        turn.return_value = monitor.TurnResult('completed', 'finished', turn_id=15)
        triage.collect_report(self.conn, self.job())
        self.assertEqual(send.call_count, 2)
        self.assertNotEqual(send.call_args_list[0].kwargs['request_id'], send.call_args_list[1].kwargs['request_id'])

    def test_existing_job_survives_additive_recovery_migration(self):
        self.add_failure()
        self.make_job(status='running')
        with self.conn:
            self.conn.execute('ALTER TABLE triage_jobs DROP COLUMN recovery_json')
            self.conn.execute('ALTER TABLE triage_jobs DROP COLUMN report_after_seq')
        self.reopen()
        self.assertEqual(self.job()['session_id'], 'session')
        self.assertEqual(self.job()['status'], 'running')
        self.assertEqual(self.job()['recovery_json'], '{}')
        self.assertEqual(self.job()['report_after_seq'], 0)

    def test_poll_cycle_restarts_without_duplicate_session_or_ticket(self):
        self.add_failure()
        self.patch(triage.uuid, "uuid4", return_value=SimpleNamespace(hex="job"))
        self.patch(monitor, "load_slack_transport", return_value=monitor.SlackTransport("webhook", webhook="unused"))
        self.patch(monitor, "update_known_failures")
        self.patch(triage, "gh_api", side_effect=lambda endpoint, **kwargs:
                   {"sha": "a" * 40} if endpoint == "commits/master" else self.github(endpoint, **kwargs))
        mj = self.patch(monitor, "require_mj_success", side_effect=lambda args, **kwargs:
                         '{"sessions":[]}' if args[0] == "sessions" else '{"session_id":"session"}')
        triage.tick(self.conn)
        self.assertEqual(self.job()["status"], "running")
        report = {"findings": [{"failure_ids": [1], 'outcome': 'product', "diagnosis": "Missing import",
            "evidence": "Compiler log at run 1", "issue": {"title": "Fix import",
            "body": "Restore the import.", "existing_number": None}}]}
        self.patch(monitor, "wait_once", return_value=monitor.TurnResult("completed", "finished"))
        self.patch(monitor, "read_final_agent_message", return_value="triage-result: " + json.dumps(report))
        for expected in ("publishing", "completed", "completed"):
            self.reopen()
            triage.tick(self.conn)
            self.assertEqual(self.job()["status"], expected)
        self.assertEqual([call.args[0][0] for call in mj.call_args_list], ["sessions", "new", "suspend"])
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(triage.pending(self.conn), [])


class GitHubCommandTests(TestCase):
    def test_json_file_preserves_issue_text_without_shell_interpolation(self):
        payload = {"title": "Failure", "body": "line 1\n`code` and $(literal)\nline 3"}
        def gh(args):
            self.assertIn("POST", args)
            self.assertEqual(args[1], f"repos/{monitor.REPO_NAME}/issues")
            self.assertEqual(json.loads(Path(args[args.index("--input") + 1]).read_text()), payload)
            return '{"number": 123}'
        with mock.patch.object(monitor, "run_gh", side_effect=gh):
            self.assertEqual(triage.gh_api("issues", method="POST", payload=payload), {"number": 123})

    def test_paginated_enumeration_flattens_all_pages(self):
        with mock.patch.object(monitor, "run_gh", return_value='[[{"number":1}],[{"number":2}]]') as gh:
            self.assertEqual(triage.gh_api("issues?state=all", pages=True), [{"number": 1}, {"number": 2}])
        self.assertIn("--paginate", gh.call_args.args[0])
        self.assertIn("--slurp", gh.call_args.args[0])
