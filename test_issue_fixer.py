import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import issue_fixer as fixer
import automerge
import monitor
import local_findings


def issue(number=12, *, assignees=(), progress=False):
    return dict(number=number, html_url=f"https://github.com/{monitor.REPO_NAME}/issues/{number}",
                title=f"Failure {number}", body="Failure evidence", state="open",
                assignees=[{"login": x} for x in assignees],
                labels=[{"name": "buildfailure"}] + ([{"name": "agent-in-progress"}] if progress else []))


def pr(number=30, *, rejected=False, sha="a" * 40):
    return dict(number=number, html_url=f"https://github.com/{monitor.REPO_NAME}/pull/{number}",
                title="Repair", body="Fixes #12", state="open", draft=False,
                head={"sha": sha, "ref": "ci-repair/issue-12-first", "repo": {"full_name": monitor.REPO_NAME}},
                labels=[{"name": "ci-fix"}] + ([{"name": automerge.REJECTED_LABEL}] if rejected else []))


def rejection(sha="a" * 40, login="mergemarshall[bot]"):
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

    def test_local_finding_repair_link_closure_releases_selection(self):
        target = issue()
        with self.conn:
            finding = local_findings.record(self.conn, 'batch', 'session', 'baseline', 'b' * 40,
                                            'policy_cli', 'check policy_cli', 'expected 2, got 1', 'now')
            local_findings.classify(self.conn, finding['id'], 'product', 'exit-code regression',
                                    target['html_url'])
            self.conn.execute('UPDATE local_findings SET linked_pr_url=?', (pr()['html_url'],))
        self.assertIsNone(fixer.select_work(self.conn, [target], []))
        with mock.patch.object(monitor, 'run_gh', return_value='{"state":"CLOSED"}'):
            monitor.refresh_known_failure_link_states(self.conn)
        self.assertIsNotNone(fixer.select_work(self.conn, [target], []))

    def test_local_finding_evidence_is_bounded_in_repair_prompt(self):
        with self.conn:
            for number in range(20):
                finding = local_findings.record(self.conn, 'batch', 'session', 'baseline', 'b' * 40,
                                                f'test_{number}', 'check tests', 'x' * 12000, 'now')
                local_findings.classify(self.conn, finding['id'], 'product', 'observed failure',
                                        issue()['html_url'])
        job = self.job()
        self.assertTrue(fixer.prompt_fits(job['prompt']))
        context = json.loads(job['prompt'].split('## Issue dossier\n', 1)[1])
        self.assertEqual(len(context['observed_failures']), 20)
        self.assertTrue(any(row['evidence']['truncated'] for row in context['observed_failures']))

    def test_default_open_link_states_without_urls_do_not_claim_the_issue(self):
        row = dict(workflow="CI", job_name="linux", identity_kind="step", identity="test",
                   last_seen_sha="a" * 40, last_seen_run_id=42,
                   linked_pr_url=None, linked_pr_state="OPEN", linked_issue_url=None, linked_issue_state="OPEN")
        with mock.patch.object(fixer, "observations", return_value=[row]):
            self.assertIsNotNone(fixer.select_work(self.conn, [issue()], []))
        row["linked_pr_url"] = pr()["html_url"]
        with mock.patch.object(fixer, "observations", return_value=[row]):
            self.assertIsNone(fixer.select_work(self.conn, [issue()], []))

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

    def test_legacy_rejection_label_and_new_marker_still_select_owned_repair(self):
        self.owned_pr()
        target = pr(rejected=True)
        target['labels'] = [{'name': 'automerge-rejected'}]
        comment = rejection()
        comment['body'] = comment['body'].replace('automerge-rejected-head:', 'mergemarshall:rejected-head:')
        with mock.patch.object(fixer, 'api', return_value=[comment]):
            selected = fixer.select_work(self.conn, [issue()], [target])
        self.assertEqual(selected[1]['number'], target['number'])

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
        for text in ["Repair ONLY issue #12", "Read AGENTS.md", "brokk-service", "--add-label agent-in-progress",
                     "Recheck ownership before publishing", "When standing down", "No hard runtime limit",
                     "Fixes #12", "Never rebase", "do not\nexpand into repairing every failure"]:
            self.assertIn(text, prompt)
        self.assertNotIn("Classify EACH failing test independently", prompt)
        self.assertIn("user explicitly permits proceeding with agent-in-progress", prompt)
        self.assertIn("Do not try assigning mergemarshall[bot]", prompt)

    def test_retry_prompt_uses_same_pr_and_draft_before_pushing(self):
        job = self.job(retry=pr(rejected=True))
        self.assertEqual(job["branch"], pr()["head"]["ref"])
        for text in ["top-priority repair", "SAME issue, branch and PR", "gh pr ready 30 --undo",
                     "Do not remove mergemarshall:rejected", "reproduce the reported regression"]:
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

    def test_infrastructure_prompt_routes_to_channel_notice_instead_of_david(self):
        prompt = self.job()['prompt']
        self.assertIn('report outcome\ninfrastructure with pr:null', prompt)
        self.assertIn('Do not assign David or create another ticket', prompt)
        self.assertIn('Flaky product tests', prompt)
        self.assertNotIn('Flaky/infrastructure failures', prompt)

    def test_infrastructure_report_cannot_submit_a_product_pr(self):
        report = dict(issue=12, outcome='infrastructure', pr=None, summary='Runner not acquired')
        self.assertEqual(fixer.parse_report('fixer-result: ' + json.dumps(report), 12), report)
        report['pr'] = 30
        with self.assertRaisesRegex(ValueError, 'product PR'):
            fixer.parse_report('fixer-result: ' + json.dumps(report), 12)

    def test_infrastructure_closes_released_ticket_and_posts_at_channel_level(self):
        job = dict(self.job(), report_json=json.dumps(dict(issue=12, outcome='infrastructure', pr=None,
            summary='Runner never acquired; next hourly job passed')))
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='finishing',session_id='session',thread_ts='old-thread',report_json=? WHERE id=?",
                              (job['report_json'], job['id']))
        closed = dict(issue(), state='closed')
        with mock.patch.object(fixer, 'api', side_effect=[issue(), closed]), \
             mock.patch.object(monitor, 'run_gh') as gh:
            fixer.finish(self.conn, job)
        gh.assert_called_once_with(['issue', 'close', '12', '--repo', monitor.REPO_NAME, '--reason', 'not_planned'])
        with mock.patch.object(monitor, 'slack_send', side_effect=[(False, None), (True, 'notice')]) as slack, \
             mock.patch.object(monitor, 'require_mj_success', return_value='{}') as mj:
            fixer.cleanup(self.conn, self.transport)
            self.assertEqual(self.conn.execute('SELECT outcome_sent FROM issue_repairs').fetchone()[0], 0)
            fixer.cleanup(self.conn, self.transport)
            self.assertEqual(slack.call_count, 2)
            self.assertEqual(slack.call_args_list[0], slack.call_args_list[1])
            self.assertIsNone(slack.call_args.kwargs['thread_ts'])
            self.assertTrue(slack.call_args.args[1].startswith('*bifrost-dev* fixbot:'))
            mj.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT outcome_sent FROM issue_repairs').fetchone()[0], 1)

    def test_lost_infrastructure_close_response_retries_without_recomputing_report(self):
        job = dict(self.job(), report_json=json.dumps(dict(issue=12, outcome='infrastructure', pr=None,
                                                         summary='External capacity failure')))
        with mock.patch.object(fixer, 'api', return_value=issue()), \
             mock.patch.object(monitor, 'run_gh', side_effect=monitor.CommandError('lost response')):
            with self.assertRaises(monitor.CommandError):
                fixer.finish(self.conn, job)
        with mock.patch.object(fixer, 'api', return_value=dict(issue(), state='closed')), \
             mock.patch.object(monitor, 'run_gh') as gh:
            fixer.finish(self.conn, job)
        gh.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM issue_repairs').fetchone()[0], 'completed')

    def test_infrastructure_does_not_close_another_persons_ticket(self):
        job = dict(self.job(), report_json=json.dumps(dict(issue=12, outcome='infrastructure', pr=None,
                                                         summary='Runner failure')))
        with mock.patch.object(fixer, 'api', return_value=issue(assignees=['dave'])), \
             mock.patch.object(monitor, 'run_gh') as gh:
            with self.assertRaisesRegex(ValueError, 'another person'):
                fixer.finish(self.conn, job)
        gh.assert_not_called()

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
        self.assertLessEqual(len(prompt), fixer.MAX_PROMPT_CHARS)
        self.assertLessEqual(len(json.dumps({"prompt": prompt}).encode()), fixer.MAX_PROMPT_BYTES)
        self.assertGreater(data["prs_omitted"], 0)
        self.assertTrue(data["target_issue"]["body"]["truncated"])
        self.assertEqual(data["target_issue"]["number"], 12)

    def oversized_ascii_prompt(self, job):
        pulls = [dict(pr(n), body="x" * 600) for n in range(80)]
        with mock.patch.object(fixer, "api", return_value=[]):
            context = fixer.dossier(self.conn, issue(), pulls)
        prefix, separator, _ = job["prompt"].rpartition("\n## Issue dossier\n")
        return prefix + separator + json.dumps(context, separators=(",", ":"))

    def test_ascii_prompt_respects_character_limit_below_the_request_byte_limit(self):
        job = self.job()
        oversized = self.oversized_ascii_prompt(job)
        self.assertGreater(len(oversized), fixer.MAX_PROMPT_CHARS)
        self.assertLess(len(json.dumps({"prompt": oversized}).encode()), fixer.MAX_PROMPT_BYTES)
        context = json.loads(oversized.rpartition("\n## Issue dossier\n")[2])
        prompt = fixer.build_prompt(dict(job), context)
        self.assertLessEqual(len(prompt), fixer.MAX_PROMPT_CHARS)
        self.assertGreater(context["prs_omitted"], 0)
        self.assertEqual(context["target_issue"]["body"]["text"], "Failure evidence")

    def test_launch_compacts_and_persists_an_older_selected_prompt_before_sending(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET prompt=? WHERE id=?",
                              (self.oversized_ascii_prompt(job), job["id"]))
        job = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
        sent = []
        def mj(args, **kwargs):
            if args[0] == "sessions":
                return '{"sessions":[]}'
            sent.append(Path(args[args.index("--prompt-file") + 1]).read_text())
            self.assertLessEqual(len(sent[0]), fixer.MAX_PROMPT_CHARS)
            return '{"session_id":"session"}'
        with mock.patch.object(monitor, "require_mj_success", side_effect=mj), \
             mock.patch.object(fixer, "api", return_value=issue()):
            fixer.launch(self.conn, job)
        saved = self.conn.execute("SELECT prompt,status FROM issue_repairs").fetchone()
        self.assertEqual(saved["prompt"], sent[0])
        self.assertEqual(saved["status"], "running")

    def test_launch_checks_ownership_again_and_does_not_start_claimed_issue(self):
        job = self.job()
        with mock.patch.object(monitor, "require_mj_success", return_value='{"sessions":[]}') as mj, \
             mock.patch.object(fixer, "api", return_value=issue(assignees=["dave"])):
            fixer.launch(self.conn, job)
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT status FROM issue_repairs").fetchone()[0], "cancelled")

    def test_prompt_file_failure_does_not_leave_an_ambiguous_launch(self):
        job = self.job()
        with mock.patch.object(monitor, "require_mj_success", return_value='{"sessions":[]}') as mj, \
             mock.patch.object(fixer, "api", return_value=issue()), \
             mock.patch.object(fixer.tempfile, "NamedTemporaryFile", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                fixer.launch(self.conn, job)
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT status FROM issue_repairs").fetchone()[0], "selected")

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

    def test_explicit_prompt_rejection_is_retryable_without_waiting_for_a_session(self):
        for rejection in ["400 Bad Request: prompt must contain 1-65536 characters",
                          "413 Payload Too Large: body limit exceeded"]:
            with self.subTest(rejection=rejection):
                job = self.job(number=12 if rejection.startswith("400") else 13)
                error = monitor.MjError("mj new --workspace exited 1: Error: the Mjolnir API answered " + rejection)
                with mock.patch.object(monitor, "require_mj_success", side_effect=['{"sessions":[]}', error]), \
                     mock.patch.object(fixer, "api", return_value=issue()):
                    with self.assertRaises(monitor.MjError):
                        fixer.launch(self.conn, job)
                saved = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
                self.assertEqual(saved["status"], "selected")
                self.assertEqual(saved["last_error"], str(error))
                with mock.patch.object(monitor, "require_mj_success", side_effect=['{"sessions":[]}', '{"session_id":"session"}']), \
                     mock.patch.object(fixer, "api", return_value=issue()):
                    fixer.launch(self.conn, saved)
                self.assertEqual(self.conn.execute("SELECT status FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()[0], "running")

    def test_ambiguous_launch_keeps_original_error_and_never_recreates(self):
        job = self.job()
        error = monitor.MjError("mj new timed out while provisioning", reason="daemon_unreachable")
        with mock.patch.object(monitor, "require_mj_success", side_effect=['{"sessions":[]}', error]), \
             mock.patch.object(fixer, "api", return_value=issue()):
            with self.assertRaises(monitor.MjError):
                fixer.launch(self.conn, job)
        saved = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(saved["status"], "launching")
        with mock.patch.object(monitor, "require_mj_success", return_value='{"sessions":[]}') as mj:
            fixer.launch(self.conn, saved)
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(mj.call_args.args[0][0], "sessions")
        self.assertEqual(self.conn.execute("SELECT last_error FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()[0], str(error))

    def test_running_session_is_left_live(self):
        job = dict(self.job(), session_id="session")
        with mock.patch.object(monitor, "wait_once", return_value=monitor.TurnResult("running", "timeout", True)), \
             mock.patch.object(fixer, "relay"), mock.patch.object(monitor, "send_session_message") as prompt:
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
        with mock.patch.object(monitor, "send_session_message",
                               side_effect=[monitor.MjError('lost reply'), None]) as prompt:
            with self.assertRaises(monitor.MjError):
                fixer.request_correction(self.conn, job, 'claim handoff', 'report')
            pending = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
            self.assertIsNone(pending['feedback_digest'])
            fixer.request_correction(self.conn, job, "claim handoff", "report")
            latest = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
            with self.assertRaisesRegex(RuntimeError, "already requested"):
                fixer.request_correction(self.conn, latest, "claim handoff", "report")
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(prompt.call_args_list[0].kwargs['request_id'], prompt.call_args_list[1].kwargs['request_id'])
        self.assertEqual(latest["status"], "running")

    def test_poll_with_live_session_does_not_select_more_work(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='running',session_id='live',start_notified=0 WHERE id=?", (job["id"],))
        with mock.patch.object(fixer, "cleanup"), mock.patch.object(fixer, "collect") as collect, \
             mock.patch.object(fixer, "select_work") as select, \
             mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')) as slack, \
             mock.patch.object(monitor, 'REPO_NAME', 'Example/another-project'):
            fixer.tick(self.conn, self.transport)
        self.assertTrue(slack.call_args.args[1].startswith('*another-project* fixbot: repairing'))
        self.assertNotIn(monitor.AGENT_LABEL, slack.call_args.args[1])
        self.assertEqual(collect.call_count, 1)
        select.assert_not_called()

    def test_missing_escalation_handoff_gets_followup_in_same_live_session(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='finishing',session_id='live',start_notified=1,report_json=? WHERE id=?",
                              (json.dumps({"issue":12,"outcome":"escalated","pr":None,"summary":"tricky"}), job["id"]))
        with mock.patch.object(fixer, "api", return_value=issue()), mock.patch.object(fixer, "cleanup"), \
             mock.patch.object(monitor, "send_session_message") as prompt:
            with self.assertRaisesRegex(ValueError, "handoff is incomplete"):
                fixer.tick(self.conn, self.transport)
        self.assertEqual(prompt.call_args.args[0], "live")
        self.assertEqual(self.conn.execute("SELECT status FROM issue_repairs").fetchone()[0], "running")
