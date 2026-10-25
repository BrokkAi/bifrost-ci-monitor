import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase, mock

import issue_fixer as fixer
import automerge
import monitor
import local_findings
import stabilize_state


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
        config = mock.patch.object(monitor, 'CONFIG_DIR', Path(temp.name) / 'config')
        config.start()
        self.addCleanup(config.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('BIFROST_FIXER_CONCURRENCY', None)
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
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1,repair_pr_number=30 WHERE id=?", (job["id"],))
        return job

    def escalated_return(self):
        job = self.job()
        report = dict(issue=12, outcome='escalated', pr=None, summary='Need compilation decision')
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',session_id='original',"
                              "created_at='2026-10-16T01:00:00+00:00',"
                              "finished_at='2026-10-16T02:00:00+00:00',cleanup_done=1,outcome_sent=1,"
                              "report_json=? WHERE id=?", (json.dumps(report), job['id']))
        return self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (job['id'],)).fetchone()

    def return_github(self, *, target=None, decision=None, permission='write', prs=None,
                      changed_issue=False, changed_decision=False):
        target = target if target is not None else issue(assignees=(fixer.ESCALATION_ASSIGNEE,))
        decision = decision if decision is not None else dict(
            id=77, user=dict(login=fixer.ESCALATION_ASSIGNEE, type='User'),
            issue_url=f'https://api.github.com/repos/{monitor.REPO_NAME}/issues/12',
            html_url=target['html_url']+'#issuecomment-77', created_at='2026-10-16T03:00:00Z',
            body='Implement explicit compilation membership; retain assignment and priority.')
        calls = {}

        def github(endpoint, **kwargs):
            calls[endpoint] = calls.get(endpoint, 0) + 1
            if endpoint == 'issues/12':
                if changed_issue and calls[endpoint] > 1:
                    return dict(target, assignees=[dict(login='another-person')])
                return target
            if endpoint == 'issues/comments/77':
                if changed_decision and calls[endpoint] > 1:
                    return dict(decision, body='Withdraw the implementation return')
                return decision
            if endpoint == f'collaborators/{fixer.ESCALATION_ASSIGNEE}/permission':
                return dict(permission=permission)
            if endpoint == 'pulls?state=open&base=master&per_page=100':
                return prs or []
            if endpoint == 'issues/12/comments?per_page=100':
                return [decision]
            self.fail(f'Unexpected GitHub endpoint: {endpoint}')
        return github

    def test_operator_return_archives_and_reuses_session_before_native_recovery(self):
        job = self.escalated_return()
        target = issue(assignees=(fixer.ESCALATION_ASSIGNEE,))
        with mock.patch.object(fixer, 'api', side_effect=self.return_github(target=target)), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}') as mj:
            resumed, queued = fixer.resume_escalated_repair(self.conn, 12, 77)
        self.assertTrue(queued)
        self.assertEqual(mj.call_args_list, [mock.call(['sessions', '--session', 'original', '--json'])]*2)
        for key in ('id', 'session_id', 'branch', 'work_key', 'base_sha'):
            self.assertEqual(resumed[key], job[key])
        self.assertEqual(resumed['status'], 'running')
        self.assertIsNone(resumed['report_json'])
        self.assertIsNone(resumed['finished_at'])
        self.assertEqual(resumed['cleanup_done'], 0)
        self.assertEqual(resumed['outcome_sent'], 0)
        self.assertIn('explicit compilation membership', resumed['prompt'])
        self.assertIn('Retain the human assignment exactly', resumed['prompt'])
        self.assertNotIn('If anybody else has assigned it', resumed['prompt'])
        self.assertTrue(fixer.prompt_fits(resumed['prompt']))
        recovery = json.loads(resumed['recovery_json'])
        self.assertEqual(recovery['stage'], 'stop')
        self.assertEqual(recovery['decision_comment_id'], 77)
        saved = self.conn.execute('SELECT * FROM issue_repair_operator_returns').fetchone()
        self.assertEqual(json.loads(saved['prior_state_json']), dict(job))
        self.assertEqual(saved['decision_author'], fixer.ESCALATION_ASSIGNEE)
        self.assertFalse(fixer.available(target))  # Default human ownership rules still apply.
        with mock.patch.object(fixer, 'api') as api:
            self.assertIsNone(fixer.select_work(self.conn, [target], []))
            api.assert_not_called()  # No automatic comment/decision admission.
        with mock.patch.object(fixer, 'recover_session') as recover, \
             mock.patch.object(monitor, 'wait_once') as wait:
            fixer.collect_step(self.conn, self.transport, resumed)
        recover.assert_called_once()
        wait.assert_not_called()  # The old escalated report is fenced by recovery.

    def test_operator_return_retry_never_resets_resumed_work_again(self):
        self.escalated_return()
        with mock.patch.object(fixer, 'api', side_effect=self.return_github()), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'):
            first, _ = fixer.resume_escalated_repair(self.conn, 12, 77)
        for status in ('running', 'completed'):
            with self.conn:
                self.conn.execute('UPDATE issue_repairs SET status=? WHERE id=?', (status, first['id']))
            with mock.patch.object(fixer, 'api') as api, \
                 mock.patch.object(monitor, 'require_mj_success') as mj:
                again, queued = fixer.resume_escalated_repair(self.conn, 12, 77)
            self.assertFalse(queued)
            self.assertEqual(again['recovery_json'], first['recovery_json'])
            api.assert_not_called()
            mj.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'another issue'):
            fixer.resume_escalated_repair(self.conn, 13, 77)

    def test_operator_return_rejects_untrusted_or_changed_handoffs(self):
        job = self.escalated_return()
        for options in (
            dict(target=issue(assignees=('someone-else',))),
            dict(target=issue(assignees=(fixer.ESCALATION_ASSIGNEE, 'someone-else'))),
            dict(target=issue(assignees=(fixer.ESCALATION_ASSIGNEE,), progress=True)),
            dict(target=dict(issue(assignees=(fixer.ESCALATION_ASSIGNEE,)), state='closed')),
            dict(target=dict(issue(assignees=(fixer.ESCALATION_ASSIGNEE,)), labels=[])),
            dict(target=dict(issue(assignees=(fixer.ESCALATION_ASSIGNEE,)),
                             labels=[dict(name='buildfailure'), dict(name='eScAlAtEd')])),
            dict(permission='read'),
            dict(changed_issue=True),
            dict(changed_decision=True),
        ):
            with self.subTest(options=options), \
                 mock.patch.object(fixer, 'api', side_effect=self.return_github(**options)), \
                 mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'):
                with self.assertRaises(ValueError):
                    fixer.resume_escalated_repair(self.conn, 12, 77)
            current = self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(dict(current), dict(job))
            self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_operator_returns').fetchone()[0], 0)

    def test_operator_return_rejects_foreign_bot_empty_and_old_decisions(self):
        job = self.escalated_return()
        decision = dict(id=77, user=dict(login=fixer.ESCALATION_ASSIGNEE, type='User'),
                        issue_url=f'https://api.github.com/repos/{monitor.REPO_NAME}/issues/12',
                        html_url=job['issue_url']+'#issuecomment-77',
                        created_at='2026-10-16T03:00:00Z', body='Approved implementation')
        for fields in (
            dict(id=78), dict(issue_url='https://api.github.com/repos/other/repo/issues/12'),
            dict(user=dict(login=fixer.ESCALATION_ASSIGNEE, type='Bot')),
            dict(user=dict(login=None, type='User')), dict(body=' '), dict(body=None),
            dict(created_at='2026-10-15T03:00:00Z'), dict(created_at='2026-10-16T03:00:00'),
        ):
            with self.subTest(fields=fields), \
                 mock.patch.object(fixer, 'api', side_effect=self.return_github(decision=dict(decision, **fields))), \
                 mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'):
                with self.assertRaises(ValueError):
                    fixer.resume_escalated_repair(self.conn, 12, 77)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_operator_returns').fetchone()[0], 0)

    def test_operator_return_preserves_active_sessions_and_open_branches(self):
        job = self.escalated_return()
        for states in (['running'], ['suspended', 'running']):
            with self.subTest(states=states), \
                 mock.patch.object(fixer, 'api', side_effect=self.return_github()), \
                 mock.patch.object(monitor, 'require_mj_success',
                                   side_effect=[json.dumps(dict(state=s)) for s in states]) as mj:
                with self.assertRaisesRegex(ValueError, 'session'):
                    fixer.resume_escalated_repair(self.conn, 12, 77)
                self.assertTrue(all(c.args[0][0] == 'sessions' for c in mj.call_args_list))
        with mock.patch.object(fixer, 'api', side_effect=self.return_github(
                prs=[dict(head=dict(ref=job['branch']))])), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'):
            with self.assertRaisesRegex(ValueError, 'open PR'):
                fixer.resume_escalated_repair(self.conn, 12, 77)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_operator_returns').fetchone()[0], 0)

    def test_operator_return_respects_terminal_state_and_capacity(self):
        job = self.escalated_return()
        for update in ("status='running'", 'cleanup_done=0', 'session_id=NULL',
                       "target_branch='stabilize/old'", 'repair_pr_number=30', 'retry_pr_number=30',
                       "report_json='{}'"):
            self.conn.execute('SAVEPOINT return_case')
            self.conn.execute('UPDATE issue_repairs SET '+update+' WHERE id=?', (job['id'],))
            with self.subTest(update=update), mock.patch.object(fixer, 'api') as api:
                with self.assertRaises(ValueError):
                    fixer.resume_escalated_repair(self.conn, 12, 77)
                api.assert_not_called()
            self.conn.execute('ROLLBACK TO return_case')
            self.conn.execute('RELEASE return_case')
        with mock.patch.object(fixer, 'occupied_slots', return_value=1), \
             mock.patch.object(fixer, 'concurrency_limit', return_value=1), \
             mock.patch.object(fixer, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'concurrency'):
                fixer.resume_escalated_repair(self.conn, 12, 77)
            api.assert_not_called()

    def test_operator_return_is_atomic_and_operator_cli_omits_private_state(self):
        import triage
        job = self.escalated_return()
        self.conn.execute("CREATE TEMP TRIGGER refuse_return BEFORE UPDATE ON issue_repairs "
                          "WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'test refusal'); END")
        with mock.patch.object(fixer, 'api', side_effect=self.return_github()), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'):
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                fixer.resume_escalated_repair(self.conn, 12, 77)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_operator_returns').fetchone()[0], 0)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (job['id'],)).fetchone()), dict(job))
        self.conn.execute('DROP TRIGGER refuse_return')
        with mock.patch.object(triage, 'lock') as lock, \
             mock.patch.object(fixer, 'api', side_effect=self.return_github()), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'), \
             mock.patch.object(monitor, 'record_heartbeat') as heartbeat, \
             mock.patch('builtins.print') as output:
            lock.return_value.__enter__.return_value = True
            self.assertEqual(monitor.resume_escalated_issue(12, 77), 0)
        lock.assert_called_once_with(monitor.LOCK_PATH)
        heartbeat.assert_not_called()
        receipt = json.loads(output.call_args.args[0])
        self.assertEqual(set(receipt), {'issue','job_id','session_id','branch','status','decision_comment_id','recovery_queued'})
        self.assertEqual(receipt['session_id'], 'original')
        self.assertTrue(receipt['recovery_queued'])

    def test_operator_return_cli_requires_explicit_paired_identifiers(self):
        from contextlib import redirect_stderr
        from io import StringIO
        with mock.patch('sys.argv', ['monitor.py','--resume-escalated-issue','12','--decision-comment','77']):
            args = monitor.parse_args()
        self.assertEqual((args.resume_escalated_issue, args.decision_comment), (12,77))
        for argv in (['--resume-escalated-issue','12'], ['--decision-comment','77'],
                     ['--resume-escalated-issue','0','--decision-comment','77'],
                     ['--check','--resume-escalated-issue','12','--decision-comment','77']):
            with self.subTest(argv=argv), mock.patch('sys.argv',['monitor.py',*argv]), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    monitor.parse_args()

    def test_operator_return_bounds_decision_excerpt_without_losing_claim_instructions(self):
        import hashlib
        job = self.escalated_return()
        body = 'a' * 64000 + '\nImplementation contract at the end'
        decision = dict(id=77, user=dict(login=fixer.ESCALATION_ASSIGNEE, type='User'),
                        issue_url=f'https://api.github.com/repos/{monitor.REPO_NAME}/issues/12',
                        html_url=job['issue_url']+'#issuecomment-77',
                        created_at='2026-10-16T03:00:00Z', body=body)
        with mock.patch.object(fixer, 'api', side_effect=self.return_github(decision=decision)), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'):
            resumed, _ = fixer.resume_escalated_repair(self.conn, 12, 77)
        prompt = resumed['prompt']
        self.assertTrue(fixer.prompt_fits(prompt))
        self.assertIn('Retain the human assignment exactly', prompt)
        self.assertIn(job['branch'], prompt)
        self.assertIn('Read the selected decision in full on GitHub', prompt)
        selected, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"url":'):])
        self.assertTrue(selected['implementation_decision']['truncated'])
        self.assertEqual(len(selected['implementation_decision']['text']), 6000)
        digest = self.conn.execute('SELECT decision_body_sha256 FROM issue_repair_operator_returns').fetchone()[0]
        self.assertEqual(digest, hashlib.sha256(body.encode()).hexdigest())

    def test_operator_return_busy_lock_does_not_open_or_migrate_database(self):
        import triage
        with mock.patch.object(triage, 'lock') as lock, \
             mock.patch.object(monitor, 'connect_db') as connect, \
             mock.patch.object(monitor, 'record_heartbeat') as heartbeat:
            lock.return_value.__enter__.return_value = False
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                monitor.resume_escalated_issue(12, 77)
        connect.assert_not_called()
        heartbeat.assert_not_called()

    def triaged_repair(self):
        import triage
        triage.ensure_schema(self.conn)
        job = self.job()
        report = dict(issue=12, outcome='submitted', pr=30, summary='Diagnostic blocker only')
        finding = dict(outcome='product', diagnosis='Intermittent pipe wait remains',
                       evidence='Symbolized stacks from the later run', issue=dict(existing_number=12))
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',session_id='original',"
                              "finished_at='2026-10-16T01:00:00+00:00',cleanup_done=1,outcome_sent=1,"
                              "start_notified=1,repair_pr_number=30,report_json=? WHERE id=?",
                              (json.dumps(report), job['id']))
            self.conn.execute("INSERT INTO triage_jobs (id,title,base_sha,created_at,status,"
                              "observations_json,report_json,finished_at,cleanup_done) VALUES "
                              "('later-triage','triage','base','2026-10-16T02:00:00+00:00','completed',"
                              "'[]',?,'2026-10-16T03:00:00+00:00',1)",
                              (json.dumps(dict(findings=[finding])),))
        return self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (job['id'],)).fetchone()

    def followup_github(self, job, *, target=None, merged=True, claim_login=None):
        def github(endpoint, **kwargs):
            if endpoint == 'issues/12':
                return target if target is not None else issue(progress=True)
            if endpoint == 'pulls/30':
                return dict(pr(), merged=merged, head=dict(pr()['head'], ref=job['branch']))
            if endpoint.startswith('issues/12/comments'):
                return [dict(user=dict(login=claim_login or automerge.TRUSTED_REJECTION_LOGIN),
                             body=f"<!-- mergemarshall-fixer:{job['id']} -->")]
            self.fail(f'Unexpected GitHub endpoint: {endpoint}')
        return github

    def test_fresh_triage_resumes_merged_repair_in_original_session_and_archives_completion(self):
        job = self.triaged_repair()
        candidates = fixer.triaged_followups(self.conn, [issue(progress=True)])
        self.assertEqual(len(candidates), 1)
        # No open ledger row is needed: a later pass may retire an intermittent observation.
        self.assertFalse(fixer.observations(self.conn, issue()['html_url']))
        with mock.patch.object(fixer, 'api', side_effect=self.followup_github(job)), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}') as mj:
            resumed = fixer.resume_triaged_repair(self.conn, candidates[0], [])
        mj.assert_called_once_with(['sessions', '--session', 'original', '--json'])
        # Native mutation waits for the committed recovery intent.
        for key in ('id', 'session_id', 'branch', 'base_sha', 'work_key'):
            self.assertEqual(resumed[key], job[key])
        self.assertEqual(resumed['status'], 'running')
        self.assertIsNone(resumed['report_json'])
        self.assertIsNone(resumed['finished_at'])
        self.assertEqual(resumed['cleanup_done'], 0)
        self.assertEqual(resumed['outcome_sent'], 0)
        recovery = json.loads(resumed['recovery_json'])
        self.assertEqual(recovery['stage'], 'stop')
        self.assertEqual(recovery['triage_job_id'], 'later-triage')
        self.assertIn('Intermittent pipe wait remains', recovery['prompt'])
        self.assertIn('own exact claim', recovery['prompt'])
        self.assertTrue(fixer.prompt_fits(resumed['prompt']))
        prior = json.loads(self.conn.execute('SELECT prior_state_json FROM issue_repair_followups').fetchone()[0])
        self.assertEqual(prior['report_json'], job['report_json'])
        self.assertEqual(prior['prompt'], job['prompt'])
        self.assertEqual(prior['finished_at'], job['finished_at'])
        self.assertEqual(fixer.occupied_slots(self.conn), 1)
        self.assertFalse(fixer.triaged_followups(self.conn, [issue(progress=True)]))
        self.assertIsNone(fixer.resume_triaged_repair(self.conn, candidates[0], []))
        with mock.patch.object(fixer, 'recover_session') as recover, \
             mock.patch.object(monitor, 'wait_once') as wait:
            fixer.collect_step(self.conn, self.transport, resumed)
        recover.assert_called_once()
        wait.assert_not_called()  # An old completed report cannot bypass the clear boundary.

    def test_followup_requires_new_completed_product_triage_after_previous_completion(self):
        self.triaged_repair()
        for update in (
            "status='running'", "created_at='2026-10-16T00:00:00+00:00'",
            "report_json='{}'",
            "report_json='{\"findings\":[{\"outcome\":\"resolved\",\"issue\":{\"existing_number\":12}}]}'",
            "report_json='{\"findings\":[{\"outcome\":\"product\",\"issue\":{\"existing_number\":13}}]}'",
        ):
            with self.subTest(update=update):
                self.conn.execute('SAVEPOINT followup_case')
                self.conn.execute('UPDATE triage_jobs SET '+update)
                self.assertFalse(fixer.triaged_followups(self.conn, [issue(progress=True)]))
                self.conn.execute('ROLLBACK TO followup_case')
                self.conn.execute('RELEASE followup_case')

    def resumed_retry(self):
        job = self.triaged_repair()
        with self.conn:
            self.conn.execute('UPDATE issue_repairs SET retry_pr_number=30,rejected_head_sha=? WHERE id=?',
                              ('a' * 40, job['id']))
        candidate = fixer.triaged_followups(self.conn, [issue(progress=True)])[0]
        with mock.patch.object(fixer, 'api', side_effect=self.followup_github(job)), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'):
            return fixer.resume_triaged_repair(self.conn, candidate, [])

    def test_followup_archives_and_retires_the_merged_rejection_retry(self):
        resumed = self.resumed_retry()
        self.assertIsNone(resumed['retry_pr_number'])
        self.assertIsNone(resumed['rejected_head_sha'])
        self.assertNotIn('top-priority repair of merger-rejected PR', resumed['prompt'])
        prior = json.loads(self.conn.execute('SELECT prior_state_json FROM issue_repair_followups').fetchone()[0])
        self.assertEqual(prior['retry_pr_number'], 30)
        self.assertEqual(prior['rejected_head_sha'], 'a' * 40)

    def test_old_resumed_retry_finishes_new_pr_without_replacing_its_session(self):
        resumed = self.resumed_retry()
        recovery = json.loads(resumed['recovery_json'])
        recovery['stage'] = 'running'
        prior = json.loads(self.conn.execute('SELECT prior_state_json FROM issue_repair_followups').fetchone()[0])
        prior.pop('retry_pr_number')
        prior.pop('rejected_head_sha')
        with self.conn:
            # Simulate a continuation checkpointed by the older supervisor.
            self.conn.execute('UPDATE issue_repair_followups SET prior_state_json=?', (json.dumps(prior),))
            self.conn.execute("UPDATE issue_repairs SET status='finishing',retry_pr_number=30,"
                              "rejected_head_sha=?,recovery_json=?,report_json=? WHERE id=?",
                              ('a' * 40, json.dumps(recovery), json.dumps(dict(issue=12, outcome='submitted',
                               pr=31, summary='Follow-up repair')), resumed['id']))
        submitted = pr(31, sha='c' * 40)
        submitted['head']['ref'] = resumed['branch']
        job = self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (resumed['id'],)).fetchone()
        with mock.patch.object(fixer, 'api', return_value=submitted) as github:
            fixer.finish(self.conn, job)
        github.assert_called_once_with('pulls/31')
        saved = self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (resumed['id'],)).fetchone()
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['repair_pr_number'], 31)
        for key in ('id', 'session_id', 'branch', 'base_sha', 'work_key'):
            self.assertEqual(saved[key], resumed[key])
        self.assertIsNone(saved['retry_pr_number'])
        self.assertIsNone(saved['rejected_head_sha'])
        prior = json.loads(self.conn.execute('SELECT prior_state_json FROM issue_repair_followups').fetchone()[0])
        self.assertEqual(prior['retry_pr_number'], 30)
        self.assertEqual(prior['rejected_head_sha'], 'a' * 40)

    def test_retry_fence_requires_matching_authenticated_followup_archive(self):
        resumed = self.resumed_retry()
        archived = self.conn.execute('SELECT prior_state_json FROM issue_repair_followups').fetchone()[0]
        recovery = json.loads(resumed['recovery_json']);recovery['stage'] = 'running'
        variants = [dict(recovery={}), dict(recovery=dict(recovery, triage_job_id='another')),
                    dict(prior=dict(json.loads(archived), repair_pr_number=99)),
                    dict(prior=dict(json.loads(archived), cleanup_done=0)),
                    dict(prior=dict(json.loads(archived), report_json='{}'))]
        for variant in variants:
            with self.subTest(variant=variant):
                self.conn.execute('SAVEPOINT retry_fence')
                self.conn.execute("UPDATE issue_repairs SET status='finishing',retry_pr_number=30,"
                                  "rejected_head_sha=?,recovery_json=?,report_json=? WHERE id=?",
                                  ('a' * 40, json.dumps(variant.get('recovery', recovery)),
                                   json.dumps(dict(issue=12, outcome='submitted', pr=31, summary='Other PR')), resumed['id']))
                self.conn.execute('UPDATE issue_repair_followups SET prior_state_json=?',
                                  (json.dumps(variant['prior']) if 'prior' in variant else archived,))
                job = self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (resumed['id'],)).fetchone()
                submitted = pr(31);submitted['head']['ref'] = resumed['branch']
                with mock.patch.object(fixer, 'api', return_value=submitted):
                    with self.assertRaisesRegex(ValueError, 'submitted PR does not match'):
                        fixer.finish(self.conn, job)
                self.assertEqual(self.conn.execute('SELECT retry_pr_number FROM issue_repairs WHERE id=?',
                                                 (resumed['id'],)).fetchone()[0], 30)
                self.conn.execute('ROLLBACK TO retry_fence')
                self.conn.execute('RELEASE retry_fence')

    def test_followup_never_takes_closed_unclaimed_or_human_owned_issue(self):
        job = self.triaged_repair()
        candidate = fixer.triaged_followups(self.conn, [issue(progress=True)])[0]
        for target in (dict(issue(progress=True), state='closed'), issue(),
                       issue(progress=True, assignees=['someone']),
                       dict(issue(progress=True), labels=issue(progress=True)['labels']+[dict(name='escalated')]),
                       dict(issue(progress=True), labels=[dict(name='agent-in-progress')])):
            with self.subTest(target=target):
                self.assertFalse(fixer.triaged_followups(self.conn, [target]))
                with mock.patch.object(fixer, 'api', side_effect=self.followup_github(job, target=target)) as github:
                    self.assertIsNone(fixer.resume_triaged_repair(self.conn, candidate, []))
                self.assertEqual(github.call_count, 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_followups').fetchone()[0], 0)

    def test_followup_waits_for_merge_and_authenticates_exact_claim(self):
        job = self.triaged_repair()
        candidate = fixer.triaged_followups(self.conn, [issue(progress=True)])[0]
        for kwargs in (dict(merged=False), dict(claim_login='someone')):
            with self.subTest(kwargs=kwargs), \
                 mock.patch.object(fixer, 'api', side_effect=self.followup_github(job, **kwargs)):
                self.assertIsNone(fixer.resume_triaged_repair(self.conn, candidate, []))
        github = self.followup_github(job)
        for bad in (dict(pr(), merged=True),  # Original branch does not match the recorded job.
                    dict(pr(), merged=True, head=dict(pr()['head'], ref=job['branch'],
                                                     repo=dict(full_name='someone/fork')))):
            def changed(endpoint, **kwargs):
                return bad if endpoint == 'pulls/30' else github(endpoint, **kwargs)
            with self.subTest(bad=bad), mock.patch.object(fixer, 'api', side_effect=changed):
                self.assertIsNone(fixer.resume_triaged_repair(self.conn, candidate, []))
        def changed_claim(endpoint, **kwargs):
            result = github(endpoint, **kwargs)
            if 'comments' in endpoint:
                result.append(dict(user=dict(login=automerge.TRUSTED_REJECTION_LOGIN),
                                   body='<!-- mergemarshall-fixer:another-repair -->'))
            return result
        with mock.patch.object(fixer, 'api', side_effect=changed_claim):
            self.assertIsNone(fixer.resume_triaged_repair(self.conn, candidate, []))

    def test_followup_never_supersedes_newer_repair_or_campaign_target(self):
        job = self.triaged_repair()
        for update in ("status='running'", "cleanup_done=0", "target_branch='stabilize/old'",
                       "report_json='{\"outcome\":\"escalated\"}'", "session_id=NULL"):
            with self.subTest(update=update):
                self.conn.execute('SAVEPOINT followup_case')
                self.conn.execute('UPDATE issue_repairs SET '+update)
                self.assertFalse(fixer.triaged_followups(self.conn, [issue(progress=True)]))
                self.conn.execute('ROLLBACK TO followup_case')
                self.conn.execute('RELEASE followup_case')
        with mock.patch.object(fixer, 'api', return_value=[]):
            newer = fixer.create_job(self.conn, issue(), None, None, 'new-observation', [], 'b'*40)
        # Timestamps have second precision and UUIDs do not encode insertion order.
        with self.conn:
            self.conn.execute('UPDATE issue_repairs SET created_at=?,id=? WHERE id=?',
                              (job['created_at'], '000-newer', newer['id']))
        self.assertFalse(fixer.triaged_followups(self.conn, [issue(progress=True)]))
        self.assertEqual(fixer.occupied_slots(self.conn), 1)
        self.assertEqual(newer['status'], 'selected')

    def test_followup_preserves_a_session_already_resumed_by_an_operator(self):
        job = self.triaged_repair()
        candidate = fixer.triaged_followups(self.conn, [issue(progress=True)])[0]
        with mock.patch.object(fixer, 'api', side_effect=self.followup_github(job)), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"running"}') as mj:
            self.assertIsNone(fixer.resume_triaged_repair(self.conn, candidate, []))
        mj.assert_called_once_with(['sessions', '--session', 'original', '--json'])
        self.assertEqual(self.conn.execute('SELECT status FROM issue_repairs').fetchone()[0], 'completed')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_followups').fetchone()[0], 0)

    def test_followup_failure_does_not_starve_unrelated_admission(self):
        self.triaged_repair()
        def github(endpoint, **kwargs):
            if endpoint.startswith('issues?'):
                return [issue(progress=True), issue(13)]
            if endpoint == 'issues/12':
                raise RuntimeError('unavailable repair metadata')
            if endpoint == 'commits/master':
                return dict(sha='b'*40)
            return []
        with mock.patch.object(fixer, 'api', side_effect=github), \
             mock.patch.object(fixer, 'launch') as launch, \
             mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')):
            with self.assertRaisesRegex(RuntimeError, 'unavailable repair metadata'):
                fixer.tick(self.conn, self.transport)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.args[1]['issue_number'], 13)

    def test_followup_intent_is_not_replayed_after_completion_or_schema_upgrade(self):
        job = self.triaged_repair()
        candidate = fixer.triaged_followups(self.conn, [issue(progress=True)])[0]
        with mock.patch.object(fixer, 'api', side_effect=self.followup_github(job)), \
             mock.patch.object(monitor, 'require_mj_success', return_value='{"state":"suspended"}'):
            resumed = fixer.resume_triaged_repair(self.conn, candidate, [])
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1,"
                              "finished_at='2026-10-16T04:00:00+00:00',report_json=? WHERE id=?",
                              (job['report_json'], resumed['id']))
        fixer.ensure_schema(self.conn)
        self.assertFalse(fixer.triaged_followups(self.conn, [issue(progress=True)]))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM issue_repair_followups').fetchone()[0], 1)

    def campaign(self, members):
        with self.conn:
            self.conn.execute("INSERT INTO stabilize_campaigns "
                              "(id,branch,anchor_sha,tip_sha,stage,status,created_at,updated_at) "
                              "VALUES ('campaign','stabilize/campaign',?,?, 'hourly','running','now','now')",
                              ('c' * 40, 'd' * 40))
            self.conn.executemany("INSERT INTO stabilize_issues "
                                  "(campaign_id,issue_number,stage,state,source,round,added_at,updated_at) "
                                  "VALUES ('campaign',?,?,?,'seed',0,'now','now')", members)

    def campaign_job(self, number=12, *, retry=None):
        def github(endpoint, **kwargs):
            if endpoint == 'commits/stabilize%2Fcampaign':
                return {'sha': 'e' * 40}
            self.assertNotEqual(endpoint, 'commits/master')
            return []
        with mock.patch.object(fixer, 'api', side_effect=github):
            return fixer.create_job(self.conn, issue(number), retry, 'bad test' if retry else None,
                                    f'campaign:{number}:{retry["head"]["sha"] if retry else "initial"}',
                                    [], 'b' * 40)

    def test_campaign_priority_precedes_rejected_master_repairs_and_orders_by_stage(self):
        self.owned_pr()
        self.campaign([(90, 'nightly', 'open'), (60, 'hourly', 'open'),
                       (80, 'push', 'open'), (70, 'push', 'open')])
        pending = [issue(n) for n in [90, 60, 80, 70, 1, 12]]
        with mock.patch.object(fixer, 'api', return_value=[rejection()]):
            for expected in [70, 80, 60, 90, 12, 1]:
                selected = fixer.select_work(self.conn, pending, [pr(rejected=True)])
                self.assertEqual(selected[0]['number'], expected)
                pending = [i for i in pending if i['number'] != expected]

    def test_triage_linked_campaign_issues_have_priority_before_membership_is_recorded(self):
        self.campaign([(90, 'nightly', 'open')])
        with self.conn:
            for number, stage in [(80, 'hourly'), (70, 'push')]:
                self.conn.execute("INSERT INTO stabilize_failures "
                                  "(campaign_id,stage,workflow,job_name,identity_kind,identity,"
                                  "first_seen_sha,first_seen_run_id,first_seen_run_url,first_seen_at,"
                                  "last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,"
                                  "linked_issue_url,updated_at) "
                                  "VALUES ('campaign',?,'CI','linux','test',?,'first',1,'url','then',"
                                  "'last',2,'url','now',?,'now')",
                                  (stage, f'failure-{number}', issue(number)['html_url']))
        pending = [issue(n) for n in [1, 90, 80, 70]]
        for expected in [70, 80, 90, 1]:
            selected = fixer.select_work(self.conn, pending, [])
            self.assertEqual(selected[0]['number'], expected)
            pending = [i for i in pending if i['number'] != expected]
        job = self.campaign_job(70)
        self.assertEqual(job['target_branch'], 'stabilize/campaign')

    def test_campaign_ready_and_landed_issues_are_not_admitted_even_with_new_evidence(self):
        self.campaign([(12, 'push', 'pr_ready'), (13, 'hourly', 'landed')])
        with mock.patch.object(fixer, 'create_job') as admission, \
             mock.patch.object(fixer, 'cleanup'), \
             mock.patch.object(fixer, 'api', side_effect=lambda endpoint, **kw:
                               [dict(issue(12), body='new evidence'), issue(13)]
                               if endpoint.startswith('issues?') else []):
            fixer.tick(self.conn, self.transport)
        admission.assert_not_called()

    def test_inactive_campaign_issue_states_follow_master_behavior(self):
        self.campaign([(12, 'push', 'escalated'), (13, 'hourly', 'closed'),
                       (14, 'nightly', 'dropped')])
        for number in [12, 13, 14]:
            self.assertEqual(fixer.select_work(self.conn, [issue(number)], [])[0]['number'], number)
            self.assertIsNone(self.job(number)['target_branch'])

    def test_campaign_owned_exact_head_rejection_is_admitted_when_pr_ready(self):
        self.campaign([(12, 'push', 'open')])
        job = self.campaign_job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1,"
                              "repair_pr_number=30 WHERE id=?", (job['id'],))
            self.conn.execute("UPDATE stabilize_issues SET state='pr_ready'")
        repair = dict(pr(rejected=True), base={'ref': 'stabilize/campaign'})
        with mock.patch.object(fixer, 'api', return_value=[rejection()]):
            selected = fixer.select_work(self.conn, [issue(12, assignees=[fixer.ASSIGNEE], progress=True)], [repair])
        self.assertEqual(selected[1]['number'], 30)
        retry = self.campaign_job(retry=repair)
        self.assertEqual(retry['target_branch'], 'stabilize/campaign')
        self.assertEqual(retry['base_sha'], repair['head']['sha'])
        self.assertIn('Merge current origin/stabilize/campaign', retry['prompt'])
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1")
            self.conn.execute("UPDATE stabilize_issues SET state='landed'")
        with mock.patch.object(fixer, 'api') as api:
            self.assertIsNone(fixer.select_work(self.conn, [issue()], [repair]))
        api.assert_not_called()

    def test_campaign_job_uses_fresh_target_tip_and_keeps_target_after_campaign_ends(self):
        self.campaign([(12, 'push', 'open')])
        job = self.campaign_job()
        self.assertEqual(job['target_branch'], 'stabilize/campaign')
        self.assertEqual(job['base_sha'], 'e' * 40)
        for text in ['MERGE origin/stabilize/campaign', 'PR against stabilize/campaign',
                     '--base stabilize/campaign', 'target stabilize/campaign',
                     'Never merge master', 'Fixes #12', 'CI-Repair-Issue: 12']:
            self.assertIn(text, job['prompt'])
        self.assertNotIn('origin/master', job['prompt'])
        with self.conn:
            self.conn.execute("UPDATE stabilize_campaigns SET status='completed'")
        sent = []
        def mj(args, **kwargs):
            if args[0] == 'sessions':
                return '{"sessions":[]}'
            sent.append(args)
            prompt = Path(args[args.index('--prompt-file') + 1]).read_text()
            self.assertTrue(prompt.endswith(job['prompt']))
            self.assertIn('There are no subtle subagents', prompt)
            return '{"session_id":"session"}'
        with mock.patch.object(monitor, 'require_mj_success', side_effect=mj), \
             mock.patch.object(fixer, 'api', return_value=issue()):
            fixer.launch(self.conn, job)
        self.assertEqual(sent[0][sent[0].index('--at') + 1], 'e' * 40)
        self.assertEqual(sent[0][sent[0].index('--branch') + 1], job['branch'])
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1,repair_pr_number=30")
        retry = self.campaign_job(retry=pr(rejected=True))
        self.assertEqual(retry['target_branch'], 'stabilize/campaign')
        context = json.loads(retry['prompt'].split('## Issue dossier\n', 1)[1])
        self.assertEqual(context['stabilize_campaign']['branch'], 'stabilize/campaign')

    def test_completed_master_work_does_not_hide_new_campaign_work(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1")
        self.campaign([(12, 'push', 'open')])
        selected = fixer.select_work(self.conn, [issue()], [])
        self.assertIsNotNone(selected)
        self.assertNotEqual(selected[3], job['work_key'])

    def test_master_rejection_retry_retains_master_target_when_issue_joins_campaign(self):
        self.owned_pr()
        self.campaign([(12, 'push', 'open')])
        retry = self.campaign_job(retry=pr(rejected=True))
        self.assertIsNone(retry['target_branch'])
        self.assertIn('MERGE origin/master', retry['prompt'])
        self.assertNotIn('--base master', retry['prompt'])
        self.assertNotIn('target master', retry['prompt'])

    def test_tick_reads_campaign_prs_and_fresh_campaign_tip_without_reading_master(self):
        self.campaign([(12, 'push', 'open')])
        repair = dict(pr(), base={'ref': 'stabilize/campaign'})
        calls = []
        def github(endpoint, **kwargs):
            calls.append(endpoint)
            if endpoint.startswith('issues?'):
                return [issue()]
            if endpoint.startswith('pulls?'):
                self.assertNotIn('base=master', endpoint)
                return [repair]
            if endpoint == 'commits/stabilize%2Fcampaign':
                return {'sha': 'e' * 40}
            self.assertNotEqual(endpoint, 'commits/master')
            return []
        with mock.patch.object(fixer, 'api', side_effect=github), \
             mock.patch.object(fixer, 'process_job'), mock.patch.object(fixer, 'cleanup'):
            fixer.tick(self.conn, self.transport)
        saved = self.conn.execute('SELECT * FROM issue_repairs').fetchone()
        self.assertEqual(saved['target_branch'], 'stabilize/campaign')
        self.assertEqual(saved['base_sha'], 'e' * 40)
        context = json.loads(saved['prompt'].split('## Issue dossier\n', 1)[1])
        self.assertEqual(context['open_pr_inventory'][0]['number'], repair['number'])
        self.assertEqual(context['open_pr_inventory'][0]['base_branch'], 'stabilize/campaign')

    def test_tick_discovers_owned_campaign_rejection_after_campaign_ends(self):
        self.campaign([(12, 'push', 'open')])
        job = self.campaign_job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1,"
                              "outcome_sent=1,repair_pr_number=30 WHERE id=?", (job['id'],))
            self.conn.execute("UPDATE stabilize_campaigns SET status='completed'")
        repair = dict(pr(rejected=True), base={'ref': 'stabilize/campaign'})
        def github(endpoint, **kwargs):
            if endpoint.startswith('issues?'):
                return [issue(assignees=[fixer.ASSIGNEE], progress=True)]
            if endpoint.startswith('pulls?'):
                self.assertNotIn('base=master', endpoint)
                return [repair]
            if endpoint.startswith('issues/30/comments'):
                return [rejection()]
            self.assertFalse(endpoint.startswith('commits/'))
            return []
        with mock.patch.object(fixer, 'api', side_effect=github), \
             mock.patch.object(fixer, 'process_job'), mock.patch.object(fixer, 'cleanup'):
            fixer.tick(self.conn, self.transport)
        saved = self.conn.execute("SELECT * FROM issue_repairs WHERE retry_pr_number=30").fetchone()
        self.assertEqual(saved['target_branch'], 'stabilize/campaign')

    def test_failed_campaign_tip_read_does_not_starve_master_admission(self):
        self.campaign([(12, 'push', 'open')])
        def github(endpoint, **kwargs):
            if endpoint.startswith('issues?'):
                return [issue(12), issue(1)]
            if endpoint == 'commits/stabilize%2Fcampaign':
                raise monitor.CommandError('campaign tip unavailable')
            if endpoint == 'commits/master':
                return {'sha': 'b' * 40}
            return []
        with mock.patch.object(fixer, 'api', side_effect=github), \
             mock.patch.object(fixer, 'process_job') as process, mock.patch.object(fixer, 'cleanup'), \
             mock.patch.object(monitor, 'log'):
            with self.assertRaisesRegex(monitor.CommandError, 'campaign tip unavailable'):
                fixer.tick(self.conn, self.transport)
        saved = self.conn.execute('SELECT * FROM issue_repairs').fetchone()
        self.assertEqual(saved['issue_number'], 1)
        self.assertIsNone(saved['target_branch'])
        self.assertEqual(process.call_args.args[2]['issue_number'], 1)

    def test_existing_database_additively_migrates_target_branch(self):
        job = self.job()
        self.conn.execute('ALTER TABLE issue_repairs DROP COLUMN target_branch')
        fixer.ensure_schema(self.conn)
        migrated = self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (job['id'],)).fetchone()
        self.assertIsNone(migrated['target_branch'])
        self.assertEqual(migrated['prompt'], job['prompt'])

    def test_new_work_requires_no_assignee_and_no_progress_label(self):
        self.assertIsNone(fixer.select_work(self.conn, [issue(1, assignees=["dave"]), issue(2, progress=True),
                                                       issue(3, assignees=[fixer.ASSIGNEE])], []))
        selected = fixer.select_work(self.conn, [issue(2), issue(1)], [])
        self.assertEqual(selected[0]["number"], 1)

    def test_local_finding_repair_link_state_is_metadata(self):
        target = issue()
        with self.conn:
            finding = local_findings.record(self.conn, 'batch', 'session', 'baseline', 'b' * 40,
                                            'policy_cli', 'check policy_cli', 'expected 2, got 1', 'now')
            local_findings.classify(self.conn, finding['id'], 'product', 'exit-code regression',
                                    target['html_url'])
            self.conn.execute('UPDATE local_findings SET linked_pr_url=?', (pr()['html_url'],))
        self.assertIsNotNone(fixer.select_work(self.conn, [target], []))
        with mock.patch.object(monitor, 'run_gh', return_value='{"state":"CLOSED"}'):
            monitor.refresh_known_failure_link_states(self.conn)
        self.assertIsNotNone(fixer.select_work(self.conn, [target], []))
        self.assertEqual(fixer.observations(self.conn, target['html_url'])[0]['linked_pr_state'], 'CLOSED')

    def test_large_ci_diagnoses_fit_without_losing_observation_provenance(self):
        for text in ['failure details\n' * 400, '\u754c' * 5000]:
            with self.subTest(unicode=text.startswith('\u754c')):
                rows = [dict(workflow='CI', job_name='linux', identity_kind='test',
                             identity=f'test_{number}', last_seen_sha='a' * 40,
                             last_seen_run_id=42, last_seen_run_url='run-url',
                             diagnosis=text, diagnosis_source='triage session',
                             triage_issue_url=issue()['html_url'], linked_pr_url=pr()['html_url'],
                             linked_pr_state='OPEN', linked_issue_url=None, linked_issue_state='OPEN')
                        for number in range(29)]
                with mock.patch.object(fixer, 'observations', return_value=rows), \
                     mock.patch.object(fixer, 'api', return_value=[]):
                    job = dict(id='diagnosis-budget', issue_number=12, issue_url=issue()['html_url'],
                               branch='repair-12', base_sha='b' * 40)
                    prompt = fixer.build_prompt(job, fixer.dossier(self.conn, issue(), []))
                self.assertTrue(fixer.prompt_fits(prompt))
                context = json.loads(prompt.split('## Issue dossier\n', 1)[1])
                observed = context['observed_failures']
                self.assertEqual(len(observed), 29)
                for number, row in enumerate(observed):
                    self.assertEqual(row['identity'], f'test_{number}')
                    self.assertEqual(row['last_seen_sha'], 'a' * 40)
                    self.assertEqual(row['last_seen_run_id'], 42)
                    self.assertEqual(row['last_seen_run_url'], 'run-url')
                    self.assertEqual(row['diagnosis_source'], 'triage session')
                    self.assertEqual(row['linked_pr_url'], pr()['html_url'])
                trimmed = [row for row in observed if row.get('diagnosis_truncated')]
                self.assertTrue(trimmed)
                for row in trimmed:
                    self.assertEqual(row['diagnosis_original_characters'], len(text))
                    self.assertTrue(text.startswith(row['diagnosis']))

    def test_combined_local_evidence_and_diagnoses_fit_with_all_provenance(self):
        command = 'cargo nextest run --workspace --all-features -E "' + 'test(golden) | ' * 12 + 'test(policy)"'
        with self.conn:
            for number in range(29):
                finding = local_findings.record(self.conn, 'b' * 32, 's' * 32, 'baseline', 'a' * 40,
                                                f'golden_suite::failure_{number}', command, 'e' * 12000, 'now')
                local_findings.classify(self.conn, finding['id'], 'product', 'd' * 6000,
                                        issue()['html_url'])
        job = self.job()
        self.assertTrue(fixer.prompt_fits(job['prompt']))
        observed = json.loads(job['prompt'].split('## Issue dossier\n', 1)[1])['observed_failures']
        self.assertEqual(len(observed), 29)
        self.assertEqual({row['identity'] for row in observed},
                         {f'golden_suite::failure_{number}' for number in range(29)})
        for row in observed:
            self.assertEqual(row['last_seen_sha'], 'a' * 40)
            self.assertEqual(row['command'], command)
            self.assertEqual(row['session_id'], 's' * 32)
            self.assertEqual(row['batch_id'], 'b' * 32)
            self.assertEqual(row['local_finding_id'], row['id'])
        self.assertTrue(any(row.get('diagnosis_truncated') for row in observed))
        self.assertTrue(any(row['evidence']['truncated'] for row in observed))

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

    def test_open_pr_and_issue_links_do_not_claim_or_cover_the_issue(self):
        row = dict(workflow="CI", job_name="linux", identity_kind="step", identity="test",
                   last_seen_sha="a" * 40, last_seen_run_id=42,
                   linked_pr_url=None, linked_pr_state="OPEN", linked_issue_url=None, linked_issue_state="OPEN")
        with mock.patch.object(fixer, "observations", return_value=[row]):
            self.assertIsNotNone(fixer.select_work(self.conn, [issue()], []))
        row["linked_pr_url"] = pr()["html_url"]
        with mock.patch.object(fixer, "observations", return_value=[row, dict(row, identity='another_failure', linked_pr_url=None)]):
            self.assertIsNotNone(fixer.select_work(self.conn, [issue()], []))
        row['linked_issue_url'] = issue(99)['html_url']
        with mock.patch.object(fixer, 'observations', return_value=[row]):
            self.assertIsNotNone(fixer.select_work(self.conn, [issue()], []))

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
            self.conn.execute("UPDATE issue_repairs SET work_key=?,status='completed',cleanup_done=1 WHERE id=?",
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
            self.conn.execute("UPDATE issue_repairs SET status='completed',cleanup_done=1 WHERE id=?", (job["id"],))
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
        self.assertIn('committed Git changes and applicable local test evidence', prompt)
        self.assertIn('a link alone never justifies deferral', prompt)
        self.assertIn(f"repair `{job['id']}` and this branch", prompt)
        self.assertIn(f"<!-- mergemarshall-fixer:{job['id']} -->", prompt)
        self.assertIn('repair record, not a Mjolnir session', prompt)
        self.assertIn('only when verified from session metadata', prompt)

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
        # #5679: the CLI uses a space; not_planned is GitHub's API spelling.
        gh.assert_called_once_with(['issue', 'close', '12', '--repo', monitor.REPO_NAME, '--reason', 'not planned'])
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

    def test_dossier_retains_scoped_completed_reports_after_comments_roll_off(self):
        for number, index in [(12, i) for i in range(5)] + [(13, 5)]:
            with mock.patch.object(fixer, 'api', return_value=[]):
                job = fixer.create_job(self.conn, issue(number), None, None,
                                       f'history:{index}', [], 'b' * 40)
            report = dict(issue=number, outcome='deferred', pr=30,
                          summary=f'Native validation {index}: ' + 'evidence ' * 500)
            with self.conn:
                self.conn.execute(
                    "UPDATE issue_repairs SET status=?,session_id=?,finished_at=?,report_json=?,"
                    "prompt='PRIVATE STORED PROMPT',cleanup_done=1 WHERE id=?",
                    ('running' if index == 4 else 'completed', f'session-{index}',
                     f'2026-10-18T12:0{index}:00Z', json.dumps(report), job['id']))
        comments = [dict(html_url=f'comment/{i}', user=dict(login='bot'), body='Claim update')
                    for i in range(20)]
        with mock.patch.object(fixer, 'api', return_value=comments):
            data = fixer.dossier(self.conn, issue(), [pr(sha='f' * 40)])
        self.assertEqual([r['session_id'] for r in data['previous_repairs']],
                         ['session-3', 'session-2', 'session-1'])
        self.assertEqual(data['previous_repairs_omitted'], 1)
        self.assertEqual(data['comments_omitted'], 12)
        self.assertTrue(data['previous_repairs'][0]['summary']['truncated'])
        self.assertEqual(data['previous_repairs'][0]['checkout_base_sha'], 'b' * 40)
        self.assertEqual(data['previous_repairs'][0]['target_branch'], 'master')
        self.assertEqual(data['open_pr_inventory'][0]['head_sha'], 'f' * 40)
        self.assertNotIn('PRIVATE STORED PROMPT', json.dumps(data))
        self.assertNotIn('session-5', json.dumps(data))

    def test_invalid_legacy_completion_cannot_supply_another_issues_evidence(self):
        job = self.job()
        for report in ('malformed JSON', json.dumps(dict(issue=13, summary='Another issue')),
                       json.dumps(dict(issue=12, outcome='deferred', summary=None)),
                       json.dumps(dict(issue=12, outcome={'prompt': 'PRIVATE'}, summary='Invalid'))):
            with self.subTest(report=report), self.conn:
                self.conn.execute("UPDATE issue_repairs SET status='completed',report_json=? "
                                  "WHERE id=?", (report, job['id']))
            with mock.patch.object(fixer, 'api', return_value=[]):
                data = fixer.dossier(self.conn, issue(), [])
            self.assertEqual(data['previous_repairs'], [])
            self.assertEqual(data['previous_repairs_omitted'], 1)
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET report_json=? WHERE id=?",
                              (json.dumps(dict(issue=12, outcome='deferred',
                                               summary='Existing test evidence',
                                               pr={'prompt': 'PRIVATE'})), job['id']))
        with mock.patch.object(fixer, 'api', return_value=[]):
            data = fixer.dossier(self.conn, issue(), [])
        self.assertIsNone(data['previous_repairs'][0]['pr_number'])
        self.assertNotIn('PRIVATE', json.dumps(data))

    def test_prompt_compacts_prior_reports_before_target_and_rejection_evidence(self):
        with mock.patch.object(fixer, 'api', return_value=[]):
            data = fixer.dossier(self.conn, issue(), [], rejection='Exact rejected-head evidence')
        data['target_issue']['body'] = fixer.excerpt('Original target evidence ' * 80, 4000)
        original = data['target_issue']['body'].copy()
        data['previous_repairs'] = [dict(summary=fixer.excerpt('Prior evidence ' * 200, 2000))
                                    for _ in range(3)]
        with mock.patch.object(fixer, 'MAX_PROMPT_CHARS', 6000):
            prompt = fixer.render_prompt('Prefix ' * 300, data)
        self.assertLessEqual(len(prompt), 6000)
        self.assertGreater(data['previous_repairs_omitted'], 0)
        self.assertEqual(data['target_issue']['body'], original)
        self.assertEqual(data['merger_rejection']['text'], 'Exact rejected-head evidence')

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
        for flag, value in (('--target', 'bedrock-podman'), ('--model', 'opus'),
                            ('--effort', 'high'), ('--subagents', 'mjolnir'),
                            ('--subagent-straightforward', 'deepseek-flash'),
                            ('--subagent-straightforward-effort', 'high')):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertNotIn('--subagent-subtle', args)
        saved_prompt = self.conn.execute('SELECT prompt FROM issue_repairs').fetchone()[0]
        self.assertIn('There are no subtle subagents', saved_prompt)

    def test_launch_recovery_adopts_session_without_reprompt_or_recreation(self):
        job = self.job()
        with mock.patch.object(monitor, "require_mj_success", return_value=json.dumps({"sessions":[{"id":"existing","title":job["title"]}]})) as mj:
            fixer.launch(self.conn, job)
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT session_id FROM issue_repairs").fetchone()[0], "existing")
        self.assertEqual(self.conn.execute('SELECT prompt FROM issue_repairs').fetchone()[0], job['prompt'])

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

    # Hard-won: de22861: a pre-creation 422 otherwise reserves a fixer slot forever.
    def test_subagent_validation_rejection_retries_persisted_launch_after_absence_lookup(self):
        for number, task in enumerate(("straightforward", "subtle"), 14):
            with self.subTest(task=task):
                job = self.job(number=number)
                error = monitor.MjError(
                    "mj new --workspace exited 1: Error: the Mjolnir API answered "
                    f"422 Unprocessable Entity: Mjolnir requires [subagents.{task}] "
                    f"or --subagent-{task} <MODEL>")
                with mock.patch.object(monitor, "require_mj_success", side_effect=['{"sessions":[]}', error]), \
                     mock.patch.object(fixer, "api", return_value=issue(number)):
                    with self.assertRaises(monitor.MjError):
                        fixer.launch(self.conn, job)
                saved = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
                self.assertEqual(saved["status"], "selected")
                # Upgrade also recovers rows already persisted by the old supervisor.
                with self.conn:
                    self.conn.execute("UPDATE issue_repairs SET status='launching' WHERE id=?", (job["id"],))
                saved = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
                with mock.patch.object(monitor, "require_mj_success", side_effect=[
                        '{"sessions":[]}', '{"session_id":"recovered"}']) as mj, \
                     mock.patch.object(fixer, "api", return_value=issue(number)):
                    fixer.launch(self.conn, saved)
                self.assertEqual(mj.call_args_list[0].args[0][0], "sessions")
                self.assertEqual(mj.call_args_list[1].args[0][0], "new")
                restored = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
                self.assertEqual(restored["id"], job["id"])
                self.assertEqual(restored["branch"], job["branch"])
                self.assertEqual(restored["session_id"], "recovered")

    def test_other_422_launch_errors_remain_ambiguous(self):
        job = self.job()
        message = "the Mjolnir API answered 422 Unprocessable Entity: worker setup failed"
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='launching',last_error=? WHERE id=?",
                              (message, job["id"]))
        saved = self.conn.execute("SELECT * FROM issue_repairs WHERE id=?", (job["id"],)).fetchone()
        with mock.patch.object(monitor, "require_mj_success", return_value='{"sessions":[]}') as mj:
            fixer.launch(self.conn, saved)
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(mj.call_args.args[0][0], "sessions")

    def test_running_session_is_left_live(self):
        job = dict(self.job(), session_id="session")
        with mock.patch.object(monitor, "wait_once", return_value=monitor.TurnResult("running", "timeout", True)), \
             mock.patch.object(fixer, "relay"), \
             mock.patch.object(monitor, 'read_final_agent_message', return_value='Waiting on CI'), \
             mock.patch.object(monitor, "send_session_message") as prompt:
            fixer.collect(self.conn, self.transport, job)
        prompt.assert_not_called()

    def test_valid_report_with_unfinished_native_turn_requests_one_durable_handoff(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='running',session_id='original',"
                              "feedback_digest='existing-correction' WHERE id=?", (job['id'],))
        report = dict(issue=12, outcome='deferred', pr=30, summary='Existing PR verified')
        with mock.patch.object(monitor, 'wait_once', return_value=monitor.TurnResult('running', 'timeout', True)), \
             mock.patch.object(fixer, 'relay'), \
             mock.patch.object(monitor, 'read_final_agent_message', side_effect=[
                 'Evidence\nfixer-result: ' + json.dumps(report),
                 'fixer-result: ' + json.dumps(report, sort_keys=True)]), \
             mock.patch.object(monitor, 'send_session_message', return_value={'via': 'mailbox'}) as send, \
             mock.patch.object(monitor, 'interrupt_turn') as interrupt, \
             mock.patch.object(monitor, 'suspend_session') as suspend:
            for _ in range(2):
                fixer.collect(self.conn, self.transport, self.saved_job(job['id']))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 'original')
        interrupt.assert_not_called()
        suspend.assert_not_called()
        saved = self.saved_job(job['id'])
        self.assertEqual(saved['status'], 'running')
        self.assertIsNone(saved['report_json'])
        self.assertEqual(saved['feedback_digest'], 'existing-correction')

    def test_report_handoff_retries_lost_reply_with_same_request_id(self):
        job = dict(self.job(), session_id='original')
        report = 'fixer-result: ' + json.dumps(dict(issue=12, outcome='resolved', summary='Verified'))
        with mock.patch.object(monitor, 'read_final_agent_message', return_value=report), \
             mock.patch.object(monitor, 'send_session_message', side_effect=[monitor.MjError('lost reply'), {}]) as send:
            with self.assertRaises(monitor.MjError):
                fixer.request_report_handoff(self.conn, job)
            self.assertFalse(self.conn.execute('SELECT 1 FROM issue_repair_messages').fetchone())
            fixer.request_report_handoff(self.conn, job)
            fixer.request_report_handoff(self.conn, job)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].kwargs['request_id'], send.call_args_list[1].kwargs['request_id'])

    def test_report_handoff_reads_only_after_recovery_boundary_and_ignores_invalid_report(self):
        job = dict(self.job(), session_id='original', report_after_seq=84)
        with mock.patch.object(monitor, 'read_final_agent_message', return_value='Working') as read, \
             mock.patch.object(monitor, 'send_session_message') as send:
            fixer.request_report_handoff(self.conn, job)
        read.assert_called_once_with('original', after_seq=84)
        send.assert_not_called()

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
            fixer.request_correction(self.conn, latest, "claim handoff", "report")
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(prompt.call_args_list[0].kwargs['request_id'], prompt.call_args_list[1].kwargs['request_id'])
        self.assertLessEqual(len(prompt.call_args.kwargs['request_id']), 64)
        self.assertEqual(latest["status"], "running")

    def running_job(self):
        job = self.job()
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='running',session_id='session',start_notified=1 WHERE id=?", (job['id'],))
        return job['id']

    def saved_job(self, identifier):
        return self.conn.execute('SELECT * FROM issue_repairs WHERE id=?', (identifier,)).fetchone()

    def reopen(self):
        self.conn.close()
        self.conn = monitor.connect_db()
        self.addCleanup(self.conn.close)

    def test_failed_repair_recovers_same_session_with_durable_clear_and_restart(self):
        identifier = self.running_job()
        turn = monitor.TurnResult('error', 'error', turn_id=14)
        with mock.patch.object(monitor, 'wait_once', return_value=turn) as wait, \
             mock.patch.object(fixer, 'relay'), mock.patch.object(monitor, 'slack_send', return_value=(True, None)) as slack, \
             mock.patch.object(monitor, 'interrupt_turn') as interrupt:
            fixer.collect(self.conn, self.transport, self.saved_job(identifier))
            recovery = json.loads(self.saved_job(identifier)['recovery_json'])
            self.assertEqual(recovery['stage'], 'stop')
            self.assertIn('Preserve HEAD, source edits, built trees', recovery['prompt'])
            self.assertIn('Repair ONLY issue #12', recovery['prompt'])
            self.assertTrue(fixer.prompt_fits(recovery['prompt']))
            slack.assert_not_called()
            identity = recovery['id']
            def mj(args, **kwargs):
                self.assertEqual(args[args.index('--session') + 1], 'session')
                if args[0] == 'sessions':
                    return '{"state":"running","is_idle":false}'
                if args[0] == 'transcript':
                    if args[args.index('--after-seq') + 1] == '0':
                        return '{"latest_seq":41}'
                    return json.dumps(dict(items=[dict(seq=45, stable_id='context-cleared:fixer-clear-' + identity + '-0')]))
                self.assertIn(args[0], ['clear-queue', 'stop-task'])
                return '{}'
            with mock.patch.object(monitor, 'require_mj_success', side_effect=mj) as native, \
                 mock.patch.object(fixer.agent_recovery.speculation, 'send_once',
                     side_effect=[monitor.MjError('lost clear reply'), {}, monitor.MjError('lost restart reply'), {}]) as send:
                fixer.collect(self.conn, self.transport, self.saved_job(identifier))
                interrupt.assert_called_once_with('session')
                for stage in ['clear', 'restart']:
                    with self.assertRaises(monitor.MjError):
                        fixer.collect(self.conn, self.transport, self.saved_job(identifier))
                    self.assertEqual(json.loads(self.saved_job(identifier)['recovery_json'])['stage'], stage)
                    self.reopen()
                    fixer.collect(self.conn, self.transport, self.saved_job(identifier))
                    if stage == 'clear':
                        fixer.collect(self.conn, self.transport, self.saved_job(identifier))
                        self.assertEqual(self.saved_job(identifier)['report_after_seq'], 45)
                saved = self.saved_job(identifier)
                self.assertEqual(saved['status'], 'running')
                self.assertEqual(json.loads(saved['recovery_json'])['stage'], 'running')
                self.assertIsNone(saved['last_error'])
                self.assertEqual([c.args[3] for c in send.call_args_list],
                                 ['fixer-clear-' + identity + '-0'] * 2 + ['fixer-restart-' + identity] * 2)
                self.assertEqual(slack.call_count, 1)  # Only the failed recovery alert.
                fixer.collect(self.conn, self.transport, saved)  # Sticky old error is quiet.
                self.assertEqual(send.call_count, 4)
                self.assertNotIn('new', [c.args[0][0] for c in native.call_args_list])
                wait.return_value = monitor.TurnResult('completed', 'finished', turn_id=55)
                report = dict(issue=12, outcome='resolved', summary='Fixed upstream')
                with mock.patch.object(monitor, 'read_final_agent_message', return_value='fixer-result: ' + json.dumps(report)) as final:
                    fixer.collect(self.conn, self.transport, self.saved_job(identifier))
                final.assert_called_once_with('session', after_seq=45)
                self.assertEqual(self.saved_job(identifier)['status'], 'finishing')
                self.assertEqual(json.loads(self.saved_job(identifier)['report_json']), report)

    def test_fixer_quota_and_input_blocks_alert_once_without_resetting_work(self):
        identifier = self.running_job()
        with mock.patch.object(fixer, 'relay'), mock.patch.object(monitor, 'require_mj_success') as native, \
             mock.patch.object(monitor, 'slack_send', return_value=(True, None)) as slack:
            for turn_id, outcome in [(14, 'quota_limit'), (15, 'input_required')]:
                with mock.patch.object(monitor, 'wait_once', return_value=monitor.TurnResult(outcome, outcome, turn_id=turn_id)):
                    for _ in range(3):
                        fixer.collect(self.conn, self.transport, self.saved_job(identifier))
                self.assertEqual(json.loads(self.saved_job(identifier)['recovery_json'])['stage'], 'blocked')
            self.assertEqual(slack.call_count, 2)
            self.assertIn('Restore provider capacity', slack.call_args_list[0].args[1])
            self.assertIn('Respond to the session', slack.call_args_list[1].args[1])
            native.assert_not_called()

    def test_fixer_new_turn_can_receive_another_correction_but_old_turn_is_quiet(self):
        identifier = self.running_job()
        with mock.patch.object(monitor, 'wait_once', return_value=monitor.TurnResult('completed', 'finished', turn_id=14)) as wait, \
             mock.patch.object(fixer, 'relay'), mock.patch.object(monitor, 'read_final_agent_message', return_value='no report'), \
             mock.patch.object(monitor, 'send_session_message', side_effect=[monitor.MjError('lost reply'), None, None]) as send, \
             mock.patch.object(monitor, 'slack_send', return_value=(True, None)) as slack:
            with self.assertRaises(monitor.MjError):
                fixer.collect(self.conn, self.transport, self.saved_job(identifier))
            self.assertEqual(slack.call_count, 1)
            self.reopen()
            fixer.collect(self.conn, self.transport, self.saved_job(identifier))
            fixer.collect(self.conn, self.transport, self.saved_job(identifier))
            wait.return_value = monitor.TurnResult('completed', 'finished', turn_id=15)
            fixer.collect(self.conn, self.transport, self.saved_job(identifier))
            self.assertEqual(send.call_count, 3)
            self.assertEqual(send.call_args_list[0].kwargs['request_id'], send.call_args_list[1].kwargs['request_id'])
            self.assertNotEqual(send.call_args_list[1].kwargs['request_id'], send.call_args_list[2].kwargs['request_id'])
            self.assertIsNone(self.saved_job(identifier)['last_error'])

    def test_recovery_columns_upgrade_existing_live_repair_in_connect_db(self):
        identifier = self.running_job()
        prompt = self.saved_job(identifier)['prompt']
        with self.conn:
            self.conn.execute('ALTER TABLE issue_repairs DROP COLUMN recovery_json')
            self.conn.execute('ALTER TABLE issue_repairs DROP COLUMN report_after_seq')
        self.reopen()  # No call to issue_fixer.ensure_schema: the shared DB migrates it.
        saved = self.saved_job(identifier)
        self.assertEqual(saved['session_id'], 'session')
        self.assertEqual(saved['prompt'], prompt)
        self.assertEqual(saved['recovery_json'], '{}')
        self.assertEqual(saved['report_after_seq'], 0)

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

    def test_concurrency_configuration_file_and_environment(self):
        self.assertEqual(fixer.concurrency_limit(), 1)
        monitor.CONFIG_DIR.mkdir()
        (monitor.CONFIG_DIR / 'fixer-concurrency').write_text('5\n')
        self.assertEqual(fixer.concurrency_limit(), 5)
        with mock.patch.dict(os.environ, BIFROST_FIXER_CONCURRENCY='3'):
            self.assertEqual(fixer.concurrency_limit(), 3)
        for value in ['0', '-1', 'five', '', '1.5']:
            with self.subTest(value=value), mock.patch.dict(os.environ, BIFROST_FIXER_CONCURRENCY=value):
                with self.assertRaisesRegex(ValueError, 'positive integer'):
                    fixer.concurrency_limit()

    def test_changed_evidence_cannot_duplicate_live_issue(self):
        job = self.job()
        changed = dict(issue(), body='new evidence')
        for status in ['selected', 'launching', 'running', 'finishing', 'completed']:
            with self.subTest(status=status), self.conn:
                self.conn.execute('UPDATE issue_repairs SET status=? WHERE id=?', (status, job['id']))
                self.assertIsNone(fixer.select_work(self.conn, [changed], []))
        with self.conn:
            self.conn.execute('UPDATE issue_repairs SET cleanup_done=1 WHERE id=?', (job['id'],))
        self.assertIsNotNone(fixer.select_work(self.conn, [changed], []))

    def test_tick_fills_five_slots_and_preserves_oldest_first(self):
        existing = self.job(2)
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='running',session_id='live',start_notified=1 WHERE id=?", (existing['id'],))
        def github(endpoint, **kwargs):
            if endpoint.startswith('issues?'):
                return [dict(issue(2), body='changed while running')] + [issue(n) for n in range(6, 0, -1)]
            if endpoint == 'commits/master':
                return {'sha': 'b' * 40}
            return []
        def launch(conn, job):
            with conn:
                conn.execute("UPDATE issue_repairs SET status='running',session_id=? WHERE id=?", (f"session-{job['issue_number']}", job['id']))
        with mock.patch.object(fixer, 'concurrency_limit', return_value=5), \
             mock.patch.object(fixer, 'api', side_effect=github), mock.patch.object(fixer, 'cleanup'), \
             mock.patch.object(fixer, 'collect') as collect, mock.patch.object(fixer, 'launch', side_effect=launch) as create, \
             mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')):
            fixer.tick(self.conn, self.transport)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual([c.args[1]['issue_number'] for c in create.call_args_list], [1, 3, 4, 5])
        self.assertEqual(fixer.occupied_slots(self.conn), 5)

    def test_failed_job_does_not_starve_other_repairs_or_admission(self):
        jobs = [self.job(n) for n in range(1, 4)]
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='running',session_id='live',start_notified=1")
        def collect(conn, transport, job):
            if job['issue_number'] == 1:
                raise monitor.MjError('failed supervision')
        def github(endpoint, **kwargs):
            if endpoint.startswith('issues?'):
                return [issue(4)]
            if endpoint == 'commits/master':
                return {'sha': 'b' * 40}
            return []
        with mock.patch.object(fixer, 'concurrency_limit', return_value=5), \
             mock.patch.object(fixer, 'api', side_effect=github), mock.patch.object(fixer, 'cleanup'), \
             mock.patch.object(fixer, 'collect', side_effect=collect) as poll, mock.patch.object(fixer, 'launch') as launch, \
             mock.patch.object(monitor, 'slack_send', return_value=(True, 'thread')):
            with self.assertRaisesRegex(monitor.MjError, 'failed supervision'):
                fixer.tick(self.conn, self.transport)
        self.assertEqual({c.args[2]['issue_number'] for c in poll.call_args_list}, {1, 2, 3})
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(fixer.occupied_slots(self.conn), 4)

    def test_failed_admission_still_fills_all_free_slots_and_can_retry(self):
        create_job = fixer.create_job
        def create(conn, target, *args):
            if target['number'] == 1:
                raise ValueError('issue evidence alone exceeds mj prompt budget')
            return create_job(conn, target, *args)
        def github(endpoint, **kwargs):
            if endpoint.startswith('issues?'):
                return [issue(n) for n in range(7, 0, -1)]
            if endpoint == 'commits/master':
                return {'sha': 'b' * 40}
            return []
        with mock.patch.object(fixer, 'concurrency_limit', return_value=5), \
             mock.patch.object(fixer, 'api', side_effect=github), mock.patch.object(fixer, 'cleanup'), \
             mock.patch.object(fixer, 'create_job', side_effect=create) as admission, \
             mock.patch.object(fixer, 'process_job') as process, mock.patch.object(monitor, 'log') as log:
            with self.assertRaisesRegex(ValueError, 'issue evidence alone exceeds'):
                fixer.tick(self.conn, self.transport)
        self.assertEqual([call.args[1]['number'] for call in admission.call_args_list], [1, 2, 3, 4, 5, 6])
        self.assertEqual([call.args[2]['issue_number'] for call in process.call_args_list], [2, 3, 4, 5, 6])
        self.assertEqual(fixer.occupied_slots(self.conn), 5)
        log.assert_called_once()
        self.assertIn('issue #1 admission failed', log.call_args.args[0])
        self.assertEqual(fixer.select_work(self.conn, [issue(1)], [])[0]['number'], 1)

    def test_lower_limit_supervises_all_existing_jobs_without_cancelling(self):
        for n in range(1, 6):
            self.job(n)
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='running',session_id='live',start_notified=1")
        with mock.patch.object(fixer, 'concurrency_limit', return_value=2), mock.patch.object(fixer, 'cleanup'), \
             mock.patch.object(fixer, 'collect') as collect, mock.patch.object(fixer, 'api') as github:
            fixer.tick(self.conn, self.transport)
        self.assertEqual(collect.call_count, 5)
        github.assert_not_called()
        self.assertEqual(fixer.occupied_slots(self.conn), 5)

    def test_ambiguous_launch_and_pending_cleanup_reserve_slots(self):
        first, second = self.job(1), self.job(2)
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='launching',start_notified=1 WHERE id=?", (first['id'],))
            self.conn.execute("UPDATE issue_repairs SET status='completed',outcome_sent=1,report_json='{}' WHERE id=?", (second['id'],))
        with mock.patch.object(fixer, 'concurrency_limit', return_value=2), mock.patch.object(fixer, 'cleanup'), \
             mock.patch.object(fixer, 'launch') as launch, mock.patch.object(fixer, 'api') as github:
            fixer.tick(self.conn, self.transport)
        launch.assert_called_once()
        github.assert_not_called()
        self.assertEqual(fixer.occupied_slots(self.conn), 2)
        with self.conn:
            self.conn.execute('UPDATE issue_repairs SET cleanup_done=1,outcome_sent=0 WHERE id=?', (second['id'],))
        self.assertEqual(fixer.occupied_slots(self.conn), 1)

    def test_terminal_notice_failure_releases_capacity_and_other_jobs_continue(self):
        first, second = self.job(1), self.job(2)
        with self.conn:
            self.conn.execute("UPDATE issue_repairs SET status='completed',session_id=id,report_json=?",
                              (json.dumps(dict(outcome='resolved', summary='fixed')),))
        with mock.patch.object(monitor, 'slack_send', side_effect=[RuntimeError('Slack unavailable'), (True, 'thread')]), \
             mock.patch.object(monitor, 'require_mj_success') as suspend:
            fixer.cleanup(self.conn, self.transport)
        self.assertEqual(suspend.call_count, 2)
        self.assertEqual(fixer.occupied_slots(self.conn), 0)
        notices = {r['id']: r['outcome_sent'] for r in self.conn.execute('SELECT * FROM issue_repairs')}
        self.assertEqual(notices, {first['id']: 0, second['id']: 1})
