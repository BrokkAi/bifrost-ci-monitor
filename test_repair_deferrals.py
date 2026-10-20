import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import issue_fixer as fixer
import monitor
from test_issue_fixer import issue, pr


class RepairDeferralTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(monitor, 'DB_PATH', Path(temp.name) / 'activity.db')
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = monitor.connect_db()
        self.addCleanup(self.conn.close)
        fixer.ensure_schema(self.conn)
        self.source = dict(workflow='Hourly CI', job_name='windows', identity_kind='step',
                           identity='Cargo nextest', last_seen_sha='b' * 40, last_seen_run_id=42)

    def completed_deferral(self, *, rows=None, target=None, captured_pr=None):
        rows = [self.source] if rows is None else rows
        pull = captured_pr or pr()
        ticket = target or issue()
        with mock.patch.object(fixer, 'api', return_value=[]), \
             mock.patch.object(fixer, 'observations', return_value=rows):
            job = fixer.create_job(self.conn, ticket, None, None,
                                   fixer.initial_work_key(ticket, rows), [pull], 'b' * 40)
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1,"
                              "finished_at='then',report_json=? WHERE id=?",
                              (json.dumps(dict(issue=ticket['number'], outcome='deferred',
                                               pr=pull['number'], summary='Verified exact repair coverage')),
                               job['id']))
        return self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (job['id'],)).fetchone()

    def receipt(self, job, *, rows=None, target=None, pull=None):
        with mock.patch.object(fixer, 'api', return_value=target or issue()), \
             mock.patch.object(fixer, 'observations', return_value=[self.source] if rows is None else rows):
            return fixer.record_verified_deferral(self.conn, job, pull or pr())

    def select(self, rows, *, target=None, pulls=None):
        with mock.patch.object(fixer, 'observations', return_value=rows):
            return fixer.select_work(self.conn, [target or issue()], [pr()] if pulls is None else pulls)

    def test_repeat_ci_run_and_retired_rows_wait_without_relaunch_or_external_reads(self):
        extra = dict(self.source, job_name='macos')
        job = self.completed_deferral(rows=[self.source, extra])
        self.assertTrue(self.receipt(job, rows=[self.source, extra]))
        # A new CI run and disappearance/reappearance of already reviewed rows
        # formerly created three distinct work_keys and three new sessions.
        with mock.patch.object(fixer, 'api') as github:
            for rows in [[dict(self.source, last_seen_run_id=43)], [],
                         [extra, dict(self.source, last_seen_run_id=44), extra]]:
                self.assertIsNone(self.select(rows))
        github.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repairs').fetchone()[0], 1)

    def test_changed_source_identity_platform_command_or_requirements_reengages(self):
        job = self.completed_deferral()
        self.assertTrue(self.receipt(job))
        for changed in [dict(self.source, last_seen_sha='c' * 40),
                        dict(self.source, identity='different test'),
                        dict(self.source, job_name='linux'),
                        dict(self.source, command='different native inputs')]:
            with self.subTest(changed=changed):
                self.assertIsNotNone(self.select([changed]))
        self.assertIsNotNone(self.select([self.source], target=dict(issue(), body='new requirement')))
        self.assertIsNotNone(self.select([self.source], target=dict(issue(), title='Changed cause')))

    def test_changed_pr_head_draft_and_rejection_admit_even_without_new_ci_run(self):
        job = self.completed_deferral()
        self.assertTrue(self.receipt(job))
        for changed in [pr(sha='c' * 40), dict(pr(), draft=True), pr(rejected=True),
                        dict(pr(), labels=[{'name': 'automerge-rejected'}]),
                        dict(pr(), base={'ref': 'stabilize/campaign'})]:
            with self.subTest(changed=changed):
                selected = self.select([self.source], pulls=[changed])
                self.assertIsNotNone(selected)
                self.assertNotEqual(selected[3], job['work_key'])
                # Deferral is not ownership of someone else's PR/rejection.
                self.assertIsNone(selected[1])
        self.assertIsNone(self.select([self.source]))

    def test_merged_or_closed_pr_gets_one_new_lifecycle_review(self):
        job = self.completed_deferral()
        self.assertTrue(self.receipt(job))
        for merged in [True, False]:
            with self.subTest(merged=merged), \
                 mock.patch.object(fixer, 'api', return_value=dict(pr(), state='closed', merged=merged)) as github:
                selected = self.select([self.source], pulls=[])
                self.assertIsNotNone(selected)
                self.assertNotEqual(selected[3], job['work_key'])
                github.assert_called_once_with('pulls/30')
                self.assertEqual(selected[3], self.select([self.source], pulls=[])[3])

    def test_unavailable_pr_read_is_not_treated_as_unchanged_or_closed(self):
        job = self.completed_deferral()
        self.assertTrue(self.receipt(job))
        with mock.patch.object(fixer, 'api', side_effect=monitor.CommandError('unavailable')):
            with self.assertRaises(monitor.CommandError):
                self.select([self.source], pulls=[])

    def test_unverified_links_and_legacy_deferrals_do_not_hide_work(self):
        self.assertIsNotNone(self.select([dict(self.source, linked_pr_url=pr()['html_url'])]))
        job = self.completed_deferral()
        # No receipt: retain historical admission semantics for genuinely new evidence.
        self.assertIsNotNone(self.select([dict(self.source, last_seen_run_id=43)]))
        for pull in [pr(sha='c' * 40), dict(pr(), draft=True), pr(rejected=True),
                     dict(pr(), state='closed', merged=True)]:
            with self.subTest(pull=pull):
                self.assertFalse(self.receipt(job, pull=pull))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_deferrals').fetchone()[0], 0)

    def test_changed_requirements_or_sources_during_investigation_cannot_acquire_receipt(self):
        job = self.completed_deferral()
        self.assertFalse(self.receipt(job, target=dict(issue(), body='new requirements')))
        self.assertFalse(self.receipt(job, rows=[dict(self.source, last_seen_sha='c' * 40)]))
        prompt = json.loads(job['prompt'].rpartition('\n## Issue dossier\n')[2])
        prompt['target_issue']['body']['truncated'] = True
        self.assertFalse(self.receipt(dict(job, prompt='\n## Issue dossier\n' + json.dumps(prompt))))
        prompt['target_issue']['body']['truncated'] = False
        prompt['observed_failures'][0]['last_seen_sha'] = None
        self.assertFalse(self.receipt(dict(job, prompt='\n## Issue dossier\n' + json.dumps(prompt))))

    def test_receipt_is_idempotent_private_and_uses_latest_completed_report_only(self):
        secret = 'sensitive-native-command'
        source = dict(self.source, command=secret)
        job = self.completed_deferral(rows=[source])
        self.assertTrue(self.receipt(job, rows=[source]))
        self.assertTrue(self.receipt(job, rows=[source]))
        rows = self.conn.execute('SELECT * FROM issue_repair_deferrals').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(secret, rows[0]['inputs_json'])
        with mock.patch.object(fixer, 'api', return_value=[]):
            newer = fixer.create_job(self.conn, issue(), None, None, 'newer', [], 'b' * 40)
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1,"
                              "report_json=? WHERE id=?",
                              (json.dumps(dict(issue=12, outcome='resolved', pr=None, summary='resolved')), newer['id']))
        self.assertIsNotNone(self.select([dict(source, last_seen_run_id=43)]))

    def test_finish_records_deferral_without_claiming_deferred_pr_ownership(self):
        job = self.completed_deferral()
        with mock.patch.object(fixer, 'api', side_effect=lambda endpoint: pr() if endpoint == 'pulls/30' else issue()), \
             mock.patch.object(fixer, 'observations', return_value=[self.source]):
            fixer.finish(self.conn, job)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_deferrals').fetchone()[0], 1)
        saved = self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (job['id'],)).fetchone()
        self.assertIsNone(saved['repair_pr_number'])
        self.assertIsNone(saved['repair_pr_url'])
