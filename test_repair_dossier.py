"""Repair context includes the full issue/PR inventory and sourced ledger evidence."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import monitor
import issue_fixer as fixer
import local_findings
from test_monitor import make_run
from test_issue_fixer import issue, pr


class RepairDossierTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(monitor, "DB_PATH", Path(directory.name) / "activity.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = monitor.connect_db()
        self.addCleanup(self.conn.close)

    def dossier(self, github):
        with mock.patch.object(monitor, "run_gh", side_effect=github):
            text = monitor.repair_dossier(make_run(), "b" * 40)
        return text, json.loads(text.strip().splitlines()[-1])

    def test_all_pages_of_open_issues_and_prs_are_included_without_relevance_filter(self):
        issue = lambda n, labels=[]: {"number": n, "title": f"Ticket {n}", "html_url": f"https://github.test/issues/{n}",
                                     "labels": [{"name": label} for label in labels], "body": "x" * 5000}
        pull = lambda n, draft=False: dict(issue(n), draft=draft, head={"sha": str(n) * 40, "ref": f"feature-{n}"})
        calls = []
        def github(args, **kwargs):
            calls.append(args)
            self.assertIn("--paginate", args)
            self.assertIn("--slurp", args)
            if "/issues?" in args[-1]:
                return json.dumps([[issue(1, ["buildfailure"]), dict(issue(2), pull_request={})], [issue(3)]])
            self.assertIn("base=master", args[-1])
            return json.dumps([[pull(4, True)], [pull(5)]])
        text, data = self.dossier(github)
        self.assertEqual([i["number"] for i in data["open_issues"]], [1, 3])
        self.assertEqual([p["number"] for p in data["open_prs"]], [4, 5])
        self.assertTrue(data["open_prs"][0]["draft"])
        self.assertEqual(data["open_prs"][0]["labels"], [])
        self.assertEqual(len(data["open_issues"][0]["body_excerpt"]), 4000)
        self.assertTrue(data["open_issues"][0]["body_truncated"])
        self.assertEqual(len(data["open_prs"][0]["body_excerpt"]), 2000)
        self.assertEqual(data["unavailable"], [])
        self.assertIn("untrusted evidence", text)

    def test_ledger_keeps_observed_sha_and_diagnosis_provenance(self):
        with self.conn:
            self.conn.execute("INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
                              "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
                              "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,"
                              "diagnosis,diagnosis_source,updated_at) VALUES "
                              "('CI','linux','step','Cargo nextest','old-sha',1,'url','then',"
                              "'failing-sha',2,'run-url','now','compile failure','old merge agent','now')")
        text, data = self.dossier(lambda *args, **kwargs: "[[]]")
        row = data["known_failures"][0]
        self.assertEqual(row["last_seen_sha"], "failing-sha")
        self.assertEqual(row["diagnosis_source"], "old merge agent")
        self.assertEqual(data["checkout_base_sha"], "b" * 40)
        self.assertIn("Old compile diagnoses can be stale", text)

    def test_inventory_outage_is_explicit_and_does_not_prevent_launch(self):
        def offline(*args, **kwargs):
            raise monitor.CommandError("GitHub unavailable")
        text, data = self.dossier(offline)
        self.assertEqual(len(data["unavailable"]), 2)
        self.assertIn("Open issue inventory", data["unavailable"][0])
        self.assertIn("Refresh unavailable inventory with gh", text)

    def test_invalid_paginated_response_is_reported_as_unavailable(self):
        _, data = self.dossier(lambda *args, **kwargs: '{"unexpected":"object"}')
        self.assertEqual(len(data["unavailable"]), 2)
        self.assertEqual(data["open_issues"], [])


class CampaignRepairDossierTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(monitor, 'DB_PATH', Path(directory.name) / 'activity.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = monitor.connect_db()
        self.addCleanup(self.conn.close)
        fixer.ensure_schema(self.conn)
        with self.conn:
            self.conn.execute("INSERT INTO stabilize_campaigns "
                              "(id,branch,anchor_sha,tip_sha,stage,status,created_at,updated_at) "
                              "VALUES ('campaign','stabilize/campaign','anchor','tip','hourly','running','now','now')")

    def failure(self, *, branch=False, identity='regression', diagnosis='product regression'):
        columns = ("workflow,job_name,identity_kind,identity,first_seen_sha,first_seen_run_id,"
                   "first_seen_run_url,first_seen_at,last_seen_sha,last_seen_run_id,"
                   "last_seen_run_url,last_seen_at,diagnosis,diagnosis_source,updated_at,")
        values = ['CI', 'linux', 'test', identity, 'first-sha', 1, 'first-run-url', '2026-10-10',
                  'latest-sha', 2, 'latest-run-url', '2026-10-13', diagnosis, 'triage session', 'now',
                  issue()['html_url']]
        if branch:
            table = 'stabilize_failures'
            columns += 'linked_issue_url,campaign_id,stage'
            values += ['campaign', 'hourly']
        else:
            table = 'known_failures'
            columns += 'triage_issue_url'
        with self.conn:
            self.conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})", values)

    def context(self, *, campaign=True, pulls=None, comments=None, target=None):
        with mock.patch.object(fixer, 'api', return_value=comments or []):
            return fixer.dossier(self.conn, target or issue(), pulls or [],
                                 target_branch='stabilize/campaign' if campaign else None)

    def test_campaign_context_includes_master_branch_and_local_evidence_with_red_since(self):
        self.failure(identity='master-regression')
        self.failure(branch=True, identity='branch-regression')
        with self.conn:
            finding = local_findings.record(self.conn, 'batch', 'session', 'flaky', 'tested-sha',
                                            'local-regression', 'check tests', 'failure log', 'now')
            local_findings.classify(self.conn, finding['id'], 'product', 'flake', issue()['html_url'])
        data = self.context()
        self.assertEqual(data['stabilize_campaign'], dict(branch='stabilize/campaign', anchor_sha='anchor',
                                                        tip_sha='tip', stage='hourly'))
        observed = {row['identity']: row for row in data['observed_failures']}
        self.assertEqual(set(observed), {'master-regression', 'branch-regression', 'local-regression'})
        for identity in ['master-regression', 'branch-regression']:
            row = observed[identity]
            self.assertEqual(row['red_since'], 'Red since first-sha at 2026-10-10; run first-run-url')
            self.assertEqual(row['first_seen_run_id'], 1)
            self.assertEqual(row['diagnosis_source'], 'triage session')
            self.assertEqual(row['last_seen_sha'], 'latest-sha')
        self.assertEqual(observed['branch-regression']['stabilize_branch'], 'stabilize/campaign')
        self.assertNotIn('red_since', observed['local-regression'])

    def test_master_dossier_has_red_since_without_campaign_observations(self):
        self.failure(identity='master-regression')
        self.failure(branch=True, identity='branch-regression')
        data = self.context(campaign=False)
        self.assertNotIn('stabilize_campaign', data)
        self.assertEqual([row['identity'] for row in data['observed_failures']], ['master-regression'])
        self.assertIn('first-sha', data['observed_failures'][0]['red_since'])

    def test_pr_inventory_includes_campaign_and_master_bases_for_campaign_job(self):
        pulls = [dict(pr(n), base={'ref': base}) for n, base in
                 [(30, 'master'), (31, 'stabilize/campaign'), (32, 'another-branch')]]
        self.assertEqual([p['number'] for p in self.context(pulls=pulls)['open_pr_inventory']], [30, 31])
        self.assertEqual([p['number'] for p in self.context(campaign=False, pulls=pulls)['open_pr_inventory']], [30])

    def test_campaign_prompt_bounds_preserve_branch_observation_and_red_since(self):
        for number in range(24):
            self.failure(branch=number % 2 == 0, identity=f'test-{number}', diagnosis='界' * 5000)
        target = dict(issue(), body='🧪' * 16000)
        pulls = [dict(pr(n), body='🧪' * 10000, base={'ref': 'stabilize/campaign'}) for n in range(100)]
        comments = [dict(html_url='comment-url', user={'login': 'user'}, body='界' * 1500) for _ in range(8)]
        data = self.context(pulls=pulls, comments=comments, target=target)
        job = dict(id='job', issue_number=12, issue_url=target['html_url'], branch='repair',
                   base_sha='tip', target_branch='stabilize/campaign')
        prompt = fixer.build_prompt(job, data)
        self.assertLessEqual(len(prompt), 65536)
        self.assertLessEqual(len(json.dumps({'prompt': prompt}).encode()), 96 * 1024)
        self.assertEqual(len(data['observed_failures']), 24)
        self.assertEqual(data['stabilize_campaign']['branch'], 'stabilize/campaign')
        self.assertEqual(data['open_pr_inventory'], [])
        self.assertEqual(data['prs_omitted'], 100)
        self.assertEqual(data['comments_omitted'], 8)
        self.assertTrue(data['target_issue']['body']['truncated'])
        self.assertTrue(any(row.get('diagnosis_truncated') for row in data['observed_failures']))
        for row in data['observed_failures']:
            self.assertEqual(row['first_seen_sha'], 'first-sha')
            self.assertEqual(row['first_seen_at'], '2026-10-10')
            self.assertEqual(row['first_seen_run_url'], 'first-run-url')
            self.assertEqual(row['red_since'], 'Red since first-sha at 2026-10-10; run first-run-url')
            self.assertEqual(row['last_seen_sha'], 'latest-sha')
