import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import issue_fixer as fixer
import monitor


def issue(number=12, *, assignees=(), progress=False):
    return dict(number=number, html_url=f"https://github.com/{monitor.REPO_NAME}/issues/{number}",
                title=f"Failure {number}", body="Failure evidence", state="open",
                assignees=[{"login": x} for x in assignees],
                labels=[{"name": "buildfailure"}] + ([{"name": "agent-in-progress"}] if progress else []))


def pr(number=30, *, rejected=False, sha="a" * 40):
    return dict(number=number, html_url=f"https://github.com/{monitor.REPO_NAME}/pull/{number}",
                title="Repair", body="Fixes #12", state="open", draft=False,
                head={"sha": sha, "ref": "ci-repair/issue-12-first", "repo": {"full_name": monitor.REPO_NAME}},
                labels=[{"name": "ci-fix"}] + ([{"name": "automerge-rejected"}] if rejected else []))


def rejection(sha="a" * 40, login=fixer.ASSIGNEE):
    return dict(user={"login": login}, created_at="2026-10-07T01:00:00Z", id=1,
                body=f"Broken test: abc\nautomerge-rejected-head: {sha}")


class IssueFixerTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(monitor, "DB_PATH", Path(temp.name) / "activity.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = monitor.connect_db()
        self.addCleanup(self.conn.close)
        fixer.ensure_schema(self.conn)
        self.transport = monitor.SlackTransport("webhook", webhook="test")

    def job(self, number=12, *, retry=None):
        with mock.patch.object(fixer, "api", return_value=[]):
            return fixer.create_job(self.conn, issue(number), retry, "bad test" if retry else None,
                                    f"retry:{retry['number']}" if retry else fixer.initial_work_key(issue(number), []), [], "b" * 40)

    def owned_pr(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',repair_pr_number=30 WHERE id=?", (job["id"],))
        return job

    def test_new_work_requires_no_assignee_and_no_progress_label(self):
        self.assertIsNone(fixer.select_work(self.conn, [issue(1, assignees=["dave"]), issue(2, progress=True),
                                                       issue(3, assignees=[fixer.ASSIGNEE])], []))
        selected = fixer.select_work(self.conn, [issue(2), issue(1)], [])
        self.assertEqual(selected[0]["number"], 1)

    def test_aggregate_and_pr_shaped_issues_are_never_work(self):
        aggregate = issue()
        aggregate["title"] = monitor.KNOWN_FAILURE_ISSUE_TITLE
        self.assertIsNone(fixer.select_work(self.conn, [aggregate, dict(issue(13), pull_request={})], []))

    def test_escalated_ticket_is_excluded_even_if_unassigned(self):
        for spelling in ["Escalated", "escalated"]:
            target = issue()
            target["labels"].append({"name": spelling})
            self.assertIsNone(fixer.select_work(self.conn, [target], []))
            self.assertFalse(fixer.available(target, own_retry=True))

    def test_rejected_owned_pr_has_priority_over_new_issue(self):
        self.owned_pr()
        with mock.patch.object(fixer, "api", return_value=[rejection()]):
            selected = fixer.select_work(self.conn, [issue(1), issue(12, assignees=[fixer.ASSIGNEE], progress=True)], [pr(rejected=True)])
        self.assertEqual(selected[0]["number"], 12)
        self.assertEqual(selected[1]["number"], 30)
        self.assertIn("Broken test", selected[2])

    def test_rejected_pr_never_takes_ticket_assigned_to_someone_else(self):
        self.owned_pr()
        with mock.patch.object(fixer, "api") as api:
            selected = fixer.select_work(self.conn, [issue(12, assignees=["dave"]), issue(13)], [pr(rejected=True)])
        api.assert_not_called()
        self.assertEqual(selected[0]["number"], 13)

    def test_stale_untrusted_and_unowned_rejections_do_not_trigger_repair(self):
        for comments in [[rejection("c" * 40)], [rejection(login="someone")]]:
            self.owned_pr() if not self.conn.execute("SELECT 1 FROM issue_repairs").fetchone() else None
            with mock.patch.object(fixer, "api", return_value=comments):
                self.assertIsNone(fixer.select_work(self.conn, [issue()], [pr(rejected=True)]))
        with mock.patch.object(fixer, "api") as api:
            self.assertIsNone(fixer.select_work(self.conn, [issue()], [pr(31, rejected=True)]))
        api.assert_not_called()

    def test_same_rejected_head_is_not_repaired_repeatedly(self):
        self.owned_pr()
        retry = self.job(retry=pr(rejected=True))
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET work_key=?,status='completed' WHERE id=?",
                              ("rejection:30:" + "a" * 40, retry["id"]))
        with mock.patch.object(fixer, "api", return_value=[rejection()]):
            self.assertIsNone(fixer.select_work(self.conn, [issue()], [pr(rejected=True)]))
        with mock.patch.object(fixer, "api", return_value=[rejection("c" * 40)]):
            self.assertIsNotNone(fixer.select_work(self.conn, [issue()], [pr(rejected=True, sha="c" * 40)]))

    def test_two_issues_can_be_worked_from_same_master_without_run_dedup(self):
        first = self.job(12)
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed' WHERE id=?", (first["id"],))
        selected = fixer.select_work(self.conn, [issue(12), issue(13)], [])
        self.assertEqual(selected[0]["number"], 13)
        second = self.job(13)
        self.assertEqual(first["base_sha"], second["base_sha"])
        self.assertNotEqual(first["branch"], second["branch"])

    def test_changed_requirements_can_reengage_but_own_comments_cannot(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed' WHERE id=?", (job["id"],))
        self.assertIsNone(fixer.select_work(self.conn, [dict(issue(), updated_at="later", comments=10)], []))
        self.assertIsNotNone(fixer.select_work(self.conn, [dict(issue(), body="Revised requirements")], []))

    def test_prompt_is_one_issue_and_claims_then_rechecks_ownership(self):
        job = self.job()
        prompt = job["prompt"]
        for text in ["Repair ONLY issue #12", "Read AGENTS.md", "mergemarshall[bot]", "--add-label agent-in-progress",
                     "Recheck ownership before publishing", "When standing down", "No hard runtime limit",
                     "Fixes #12", "Never rebase", "do not\nexpand into repairing every failure"]:
            self.assertIn(text, prompt)
        self.assertNotIn("Classify EACH failing test independently", prompt)

    def test_retry_prompt_uses_same_pr_and_draft_before_pushing(self):
        job = self.job(retry=pr(rejected=True))
        self.assertEqual(job["branch"], pr()["head"]["ref"])
        for text in ["top-priority repair", "SAME issue, branch and PR", "gh pr ready 30 --undo",
                     "Do not remove automerge-rejected", "reproduce the reported regression"]:
            self.assertIn(text, job["prompt"])

    def test_prompt_permits_tricky_or_conflicting_requirements_to_escalate(self):
        prompt = self.job()["prompt"]
        for text in ["permission to bail out", "particularly tricky", "impossible to reconcile",
                     "DavidBakerEffendi", "Escalated", "do not have to", "Verify the handoff"]:
            self.assertIn(text, prompt)

    def test_escalation_completion_verifies_david_handoff(self):
        job = dict(self.job(), report_json=json.dumps({"issue":12,"outcome":"escalated","pr":None,"summary":"conflicting requirements"}))
        with mock.patch.object(fixer, "api", return_value=issue()):
            with self.assertRaisesRegex(ValueError, "handoff is incomplete"):
                fixer.finish(self.conn, job)
        handed_off = issue(assignees=["DavidBakerEffendi"])
        handed_off["labels"].append({"name": "escalated"})
        with mock.patch.object(fixer, "api", return_value=handed_off):
            fixer.finish(self.conn, job)
        self.assertEqual(self.conn.execute("SELECT status FROM issue_repairs").fetchone()[0], "completed")

    def test_dossier_has_only_target_issue_and_related_observations(self):
        with mock.patch.object(fixer, "api", return_value=[]):
            data = fixer.dossier(self.conn, issue(), [pr()])
        self.assertEqual(data["target_issue"]["number"], 12)
        self.assertNotIn("open_issues", data)
        self.assertEqual(data["observed_failures"], [])
        self.assertEqual(data["open_pr_inventory"][0]["number"], 30)

    def test_encoded_prompt_stays_below_transport_limit_even_with_large_unicode_inventory(self):
        target = issue()
        target["body"] = "\U0001f9ea" * 65000
        pulls = [dict(pr(n), body="\U0001f9ea" * 10000) for n in range(1000)]
        job = dict(id="job", issue_number=12, issue_url=target["html_url"], branch="branch", base_sha="b" * 40)
        with mock.patch.object(fixer, "api", return_value=[]):
            data = fixer.dossier(self.conn, target, pulls)
        prompt = fixer.build_prompt(job, data)
        self.assertLessEqual(len(json.dumps({"prompt": prompt}).encode()), fixer.MAX_PROMPT_BYTES)
        self.assertGreater(data["prs_omitted"], 0)
        self.assertTrue(data["target_issue"]["body"]["truncated"])
        self.assertEqual(data["target_issue"]["number"], 12)

    def test_launch_checks_ownership_again_and_does_not_start_claimed_issue(self):
        job = self.job()
        with mock.patch.object(monitor, "require_mj_success", return_value='{"sessions":[]}') as mj, \
             mock.patch.object(fixer, "api", return_value=issue(assignees=["dave"])):
            fixer.launch(self.conn, job)
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT status FROM issue_repairs").fetchone()[0], "cancelled")

    def test_retry_launch_checks_out_existing_branch_without_at(self):
        job = self.job(retry=pr(rejected=True))
        with mock.patch.object(monitor, "require_mj_success", side_effect=['{"sessions":[]}', '{"session_id":"session"}']) as mj, \
             mock.patch.object(fixer, "api", return_value=issue()):
            fixer.launch(self.conn, job)
        args = mj.call_args_list[-1].args[0]
        self.assertNotIn("--at", args)
        self.assertEqual(args[args.index("--branch") + 1], pr()["head"]["ref"])

    def test_launch_recovery_adopts_session_without_reprompt_or_recreation(self):
        job = self.job()
        with mock.patch.object(monitor, "require_mj_success", return_value=json.dumps({"sessions":[{"id":"existing","title":job["title"]}]})) as mj:
            fixer.launch(self.conn, job)
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT session_id FROM issue_repairs").fetchone()[0], "existing")

    def test_running_session_is_left_live(self):
        job = dict(self.job(), session_id="session")
        with mock.patch.object(monitor, "wait_once", return_value=monitor.TurnResult("running", "timeout", True)), \
             mock.patch.object(fixer, "relay"), mock.patch.object(monitor, "send_session_prompt") as prompt:
            fixer.collect(self.conn, self.transport, job)
        prompt.assert_not_called()

    def test_report_must_match_target_issue(self):
        with self.assertRaises(ValueError):
            fixer.parse_report('fixer-result: {"issue":13,"outcome":"resolved","summary":"done"}', 12)

    def test_retry_submission_cannot_claim_unchanged_rejected_head(self):
        job = dict(self.job(retry=pr(rejected=True)), report_json=json.dumps({"issue":12,"outcome":"submitted","pr":30,"summary":"fixed"}))
        with mock.patch.object(fixer, "api", return_value=pr()):
            with self.assertRaises(ValueError):
                fixer.finish(self.conn, job)

    def test_schema_is_idempotent_and_keeps_history(self):
        self.job()
        fixer.ensure_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM issue_repairs").fetchone()[0], 1)

    def test_pr_links_only_observations_for_the_target_issue(self):
        job = dict(self.job(), report_json=json.dumps({"issue":12,"outcome":"submitted","pr":30,"summary":"fixed"}))
        for number in [12, 13]:
            with self.conn:
                self.conn.execute("INSERT INTO known_failures(workflow,job_name,identity_kind,identity,"
                                  "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
                                  "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,"
                                  "triage_issue_url,updated_at) VALUES "
                                  "('CI','linux','test',?,'sha',42,'run','then','sha',42,'run','now',?,'now')",
                                  (str(number), issue(number)["html_url"]))
        submitted = pr()
        submitted["head"]["ref"] = job["branch"]
        with mock.patch.object(fixer, "api", return_value=submitted):
            fixer.finish(self.conn, job)
        rows = self.conn.execute("SELECT identity,linked_pr_url FROM known_failures ORDER BY identity").fetchall()
        self.assertEqual(rows[0]["linked_pr_url"], pr()["html_url"])
        self.assertIsNone(rows[1]["linked_pr_url"])
        self.assertEqual(len(fixer.observations(self.conn, issue()["html_url"])), 1)

    def test_correction_is_sent_once_and_never_suspends(self):
        job = dict(self.job(), session_id="session")
        with mock.patch.object(monitor, "send_session_prompt") as prompt:
            fixer.request_correction(self.conn, job, "claim handoff", "report")
            latest = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
            with self.assertRaisesRegex(RuntimeError, "already requested"):
                fixer.request_correction(self.conn, latest, "claim handoff", "report")
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(latest["status"], "running")

    def test_poll_with_live_session_does_not_select_more_work(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='running',session_id='live',start_notified=1 WHERE id=?", (job["id"],))
        with mock.patch.object(fixer, "cleanup"), mock.patch.object(fixer, "collect") as collect, \
             mock.patch.object(fixer, "select_work") as select:
            fixer.tick(self.conn, self.transport)
        self.assertEqual(collect.call_count, 1)
        select.assert_not_called()

    def test_missing_escalation_handoff_gets_followup_in_same_live_session(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='finishing',session_id='live',start_notified=1,report_json=? WHERE id=?",
                              (json.dumps({"issue":12,"outcome":"escalated","pr":None,"summary":"tricky"}), job["id"]))
        with mock.patch.object(fixer, "api", return_value=issue()), mock.patch.object(fixer, "cleanup"), \
             mock.patch.object(monitor, "send_session_prompt") as prompt:
            with self.assertRaisesRegex(ValueError, "handoff is incomplete"):
                fixer.tick(self.conn, self.transport)
        self.assertEqual(prompt.call_args.args[0], "live")
        self.assertEqual(self.conn.execute("SELECT status FROM issue_repairs").fetchone()[0], "running")
