"""Repair context includes the full issue/PR inventory and sourced ledger evidence."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import monitor
from test_monitor import make_run


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
