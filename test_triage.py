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

    def add_failure(self, identity="Cargo nextest", sha="a" * 40):
        with self.conn:
            self.conn.execute(
                "INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
                "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
                "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,updated_at) "
                "VALUES ('CI','linux','step',?,?,1,'https://github.test/run/1','2026-01-01',"
                "?,1,'https://github.test/run/1','2026-01-01','2026-01-01')", (identity, sha, sha))

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

    def infrastructure_job(self):
        report = self.make_job()
        finding = report['findings'][0]
        finding.update(outcome='infrastructure', issue=None, diagnosis='Runner was not acquired',
                       evidence='run 1: runner_id=0, no steps; quota/capacity cause unconfirmed')
        with self.conn:
            self.conn.execute('UPDATE triage_jobs SET report_json=?', (json.dumps(report),))
        return report

    def test_infrastructure_is_reported_once_without_a_ticket_or_false_resolution(self):
        self.add_failure()
        self.infrastructure_job()
        triage.publish(self.conn, self.job())
        self.reopen()
        triage.publish(self.conn, self.job())
        self.slack.assert_called_once()
        self.assertIsNone(self.slack.call_args.kwargs.get('thread_ts'))
        self.assertIn('Runner was not acquired', self.slack.call_args.args[1])
        self.assertEqual(self.github.calls, [])
        row = self.conn.execute('SELECT * FROM known_failures').fetchone()
        self.assertEqual(row['status'], 'open')
        self.assertIsNone(row['fixed_at'])
        self.assertIsNone(row['triage_issue_url'])
        self.assertIsNone(self.conn.execute('SELECT resolved_run_id FROM triage_observations').fetchone()[0])
        self.assertEqual(triage.reconcile_resolved(self.conn), 0)
        self.assertEqual(triage.pending(self.conn), [])

    def test_slack_retry_reuses_cached_notice_after_restart_and_recovery(self):
        self.add_failure()
        self.infrastructure_job()
        self.slack.return_value = (False, None)
        with self.assertRaisesRegex(RuntimeError, 'cached report'):
            triage.publish(self.conn, self.job())
        cached = json.loads(self.job()['report_json'])['findings'][0]['slack_text']
        self.assertEqual(self.job()['status'], 'publishing')
        self.assertEqual(triage.pending(self.conn), [])
        self.reopen()
        with self.conn:
            self.conn.execute("UPDATE known_failures SET status='fixed',diagnosis='later pass'")
        self.slack.return_value = (True, 'notice')
        triage.publish(self.conn, self.job())
        self.assertEqual([c.args[1] for c in self.slack.call_args_list], [cached, cached])
        self.assertEqual(self.job()['status'], 'completed')
        self.assertEqual(self.conn.execute('SELECT status FROM known_failures').fetchone()[0], 'fixed')
        self.assertEqual(self.github.calls, [])

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
        self.slack.assert_called_once()
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(self.job()['status'], 'completed')

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
        prompt = self.patch(monitor, 'send_session_prompt')
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
        self.conn.execute("ALTER TABLE triage_observations DROP COLUMN resolved_run_id")
        self.conn.commit()
        self.reopen()
        row = self.conn.execute("SELECT * FROM known_failures").fetchone()
        self.assertEqual(row["identity"], "Cargo nextest")
        self.assertIsNone(row["triage_issue_url"])
        self.assertEqual(row["triage_issue_state"], "OPEN")
        self.assertIn("resolved_run_id", {r["name"] for r in self.conn.execute("PRAGMA table_info(triage_observations)")})

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
        prompt = self.patch(monitor, "send_session_prompt")
        triage.collect_report(self.conn, self.job())
        self.reopen()
        with self.assertRaisesRegex(RuntimeError, "correction already submitted"):
            triage.collect_report(self.conn, self.job())
        prompt.assert_called_once()

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
