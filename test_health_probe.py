"""Isolated real-schema tests; no daemon, GitHub or Slack connections."""
import contextlib
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import automerge
import health_probe as hp
import issue_fixer
import monitor
import stabilize
import triage


NOW = hp.parsed('2026-10-27T12:00:00Z')


class HealthProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'activity.db'
        for module, attribute, value in [(monitor, 'DB_PATH', self.path),
                (automerge, 'DB_PATH', self.path), (monitor, 'CONFIG_DIR', self.root / 'config')]:
            patch = mock.patch.object(module, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {'BIFROST_FIXER_CONCURRENCY': '1'})
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = monitor.connect_db()
        issue_fixer.ensure_schema(self.conn)
        triage.ensure_schema(self.conn)
        other = automerge.connect_db()
        stabilize.ensure_schema(other)
        other.commit()
        other.close()
        self.addCleanup(self.conn.close)
        for name in ('monitor', 'merger', 'triage', 'stabilize'):
            self.insert('entrypoint_heartbeats', entry_point=name, last_finished_at=NOW.isoformat(), last_outcome='ok')
        self.sessions_file = self.root / 'sessions.json'
        self.sessions_file.write_text('{"sessions": []}')
        patch = mock.patch.object(hp.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='{}'))
        self.mj = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(hp.os, 'statvfs', return_value=mock.Mock(f_blocks=100, f_bfree=80, f_bavail=80))
        patch.start()
        self.addCleanup(patch.stop)

    def insert(self, table, **values):
        # Fill unrelated required fields using the repository's actual schema.
        for row in self.conn.execute(f'PRAGMA table_info({table})'):
            name, datatype, required, default = row[1:5]
            if name not in values and required and default is None:
                values[name] = 1 if 'INT' in datatype else (NOW.isoformat() if name.endswith('_at') else name)
        columns = ','.join(values)
        self.conn.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))
        self.conn.commit()

    def batch(self, ident='batch', **values):
        row = dict(batch_id=ident, status='running', phase='building', pull_requests_json='[]',
                   title=ident, branch=ident, target_ref='master')
        row.update(values)
        self.insert('automerge_batches', **row)

    def repair(self, ident='repair', **values):
        row = dict(id=ident, work_key=ident, issue_number=12, status='running')
        row.update(values)
        self.insert('issue_repairs', **row)

    def job(self, **values):
        self.insert('triage_jobs', id='triage', status='running', observations_json='[]', **values)

    def result(self):
        return hp.Probe(self.path, now=NOW, sessions_json=self.sessions_file).run()

    def kinds(self):
        result = self.result()
        self.assertEqual(result['check_errors'], [])
        return {r['kind'] for r in result['findings']}

    def update(self, table, **values):
        self.conn.execute(f'UPDATE {table} SET ' + ','.join(f'{k}=?' for k in values), tuple(values.values()))
        self.conn.commit()

    def test_baseline_and_readonly(self):
        self.assertEqual(self.kinds(), set())
        before = self.path.read_bytes()
        conn = hp.open_readonly(self.path)
        self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute('CREATE TABLE forbidden (id)')
        conn.close()
        self.result()
        self.assertEqual(before, self.path.read_bytes())

    def test_daemon_unavailable(self):
        self.mj.return_value.stdout = 'token=never-echo invalid'
        self.assertIn('mj_daemon_unavailable', self.kinds())
        self.assertNotIn('never-echo', json.dumps(self.result()))
        self.mj.return_value.stdout = '{}'
        self.assertNotIn('mj_daemon_unavailable', self.kinds())
        self.mj.return_value.returncode = 1
        self.assertIn('mj_daemon_unavailable', self.kinds())

    def test_heartbeats(self):
        self.update('entrypoint_heartbeats', last_finished_at=(NOW - timedelta(minutes=16)).isoformat(),
                    last_skipped_at=NOW.isoformat(), last_outcome='error', last_error='ValueError')
        self.assertTrue({'heartbeat_stale', 'heartbeat_failing'} <= self.kinds())
        self.update('entrypoint_heartbeats', last_finished_at=NOW.isoformat(), last_outcome='ok', last_error=None)
        self.assertFalse({'heartbeat_stale', 'heartbeat_failing'} & self.kinds())
        self.update('entrypoint_heartbeats', last_finished_at=None)
        self.assertIn('heartbeat_stale', self.kinds())

    def test_sessions_all_supervisors(self):
        self.batch(session_id='native')
        self.repair(session_id='native')
        self.job(session_id='native')
        self.assertIn('session_not_running', self.kinds())
        self.sessions_file.write_text(json.dumps({'sessions': [dict(id='native', state='suspended', lifecycle='suspended', has_error=True)]}))
        self.assertTrue({'session_error', 'session_not_running'} <= self.kinds())
        self.sessions_file.write_text(json.dumps({'sessions': [dict(id='native', state='running', lifecycle='live', has_error=False)]}))
        self.assertFalse({'session_error', 'session_not_running'} & self.kinds())
        self.sessions_file.write_text('{"sessions": []}')
        self.update('automerge_batches', status='completed', terminal_status='merged')
        self.update('issue_repairs', status='completed')
        self.update('triage_jobs', status='completed')
        self.assertNotIn('session_not_running', self.kinds())

    def test_recovery_and_job_errors(self):
        self.repair(last_error='auth failed', recovery_json=json.dumps(dict(stage='blocked', outcome='quota_limit', prompt='PRIVATE')))
        self.job(last_error='delivery failed', recovery_json=json.dumps(dict(stage='clear', failed_step='clear', failure_notified=True)))
        self.assertTrue({'repair_error', 'triage_error', 'recovery_blocked'} <= self.kinds())
        self.assertNotIn('PRIVATE', json.dumps(self.result()))
        self.update('issue_repairs', last_error=None, recovery_json='{}')
        self.update('triage_jobs', last_error=None, recovery_json='{}')
        self.assertFalse({'repair_error', 'triage_error', 'recovery_blocked'} & self.kinds())
        self.update('triage_jobs', status='publishing', last_error='failure')
        self.assertIn('triage_error', self.kinds())
        self.update('triage_jobs', status='completed')
        self.assertNotIn('triage_error', self.kinds())

    def test_blocked(self):
        self.batch()
        self.insert('automerge_blocked_notifications', batch_id='batch', reason='ref unavailable', details='PRIVATE')
        self.insert('blocked_notifications', workflow_run_id=44, reason='auth', details='PRIVATE')
        self.assertTrue({'merge_blocked', 'ci_blocked'} <= self.kinds())
        self.update('automerge_batches', status='completed', terminal_status='merged')
        self.update('blocked_notifications', created_at=(NOW - timedelta(days=2)).isoformat())
        self.assertFalse({'merge_blocked', 'ci_blocked'} & self.kinds())

    def test_legacy_ci_blocked(self):
        self.conn.execute('DROP TABLE blocked_notifications')
        self.conn.execute('CREATE TABLE blocked_notifications(workflow_run_id INTEGER,reason TEXT)')
        self.insert('blocked_notifications', workflow_run_id=12, reason='old run')
        self.assertNotIn('ci_blocked', self.kinds())
        self.insert('blocked_notifications', workflow_run_id=-1, reason='host failure')
        self.assertIn('ci_blocked', self.kinds())

    def test_outbox_includes_silent_kinds(self):
        for kind in ('membership_label', 'dependency_notice'):
            self.insert('automerge_github_outbox', intent_id=kind, kind=kind, attempts=3, last_error='refused')
        self.assertIn('outbox_failing', self.kinds())
        self.update('automerge_github_outbox', attempts=2)
        self.assertNotIn('outbox_failing', self.kinds())
        self.update('automerge_github_outbox', attempts=3, delivered_at=NOW.isoformat())
        self.assertNotIn('outbox_failing', self.kinds())
        self.update('automerge_github_outbox', delivered_at=None, cancelled_at=NOW.isoformat())
        self.assertNotIn('outbox_failing', self.kinds())

    def test_outbox_hash_ids_remain_distinct(self):
        for ident in ('a' * 64, 'b' * 64):
            self.insert('automerge_github_outbox', intent_id=ident, kind='membership_label', attempts=3)
        findings = [r for r in self.result()['findings'] if r['kind'] == 'outbox_failing']
        self.assertEqual({r['entity'] for r in findings}, {'a' * 64, 'b' * 64})

    def test_supervisor_actions(self):
        self.insert('automerge_supervisor_actions', action_id='action', attempts=3, last_error='auth')
        self.assertIn('supervisor_action_failing', self.kinds())
        self.update('automerge_supervisor_actions', attempts=0, lock_failures=3)
        self.assertIn('supervisor_action_failing', self.kinds())
        self.update('automerge_supervisor_actions', lock_failures=2)
        self.assertNotIn('supervisor_action_failing', self.kinds())
        self.update('automerge_supervisor_actions', attempts=3, status='complete')
        self.assertNotIn('supervisor_action_failing', self.kinds())

    def test_ledger(self):
        self.insert('known_failure_errors', reason='upkeep', failure_count=3, last_error='ValueError')
        self.insert('known_failure_state', key='issue_pending_body', value='PRIVATE')
        self.assertTrue({'ledger_upkeep_failing', 'ledger_view_pending'} <= self.kinds())
        self.update('known_failure_errors', updated_at=(NOW - timedelta(hours=2)).isoformat())
        self.conn.execute('DELETE FROM known_failure_state')
        self.conn.commit()
        self.assertFalse({'ledger_upkeep_failing', 'ledger_view_pending'} & self.kinds())

    def test_performance(self):
        self.insert('performance_runs', repository='repo', run_id=1, attempt=1, status='pending', error_class='PermissionError')
        self.assertIn('performance_intake_failing', self.kinds())
        self.insert('performance_runs', repository='repo', run_id=1, attempt=2, status='done')
        self.assertNotIn('performance_intake_failing', self.kinds())
        self.update('performance_runs', status='done', error_class=None)
        self.assertNotIn('performance_intake_failing', self.kinds())

    def test_suspend(self):
        self.batch(status='completed', phase='terminal', terminal_status='merged', suspend_pending=1, suspend_verify_failures=3)
        self.assertIn('suspend_failing', self.kinds())
        before = [r['fingerprint'] for r in self.result()['findings']]
        self.update('automerge_batches', suspend_verify_failures=40, abort_reason='preempted by immediate PR #1')
        self.assertEqual([r['fingerprint'] for r in self.result()['findings']], before)
        self.update('automerge_batches', suspend_verify_failures=2)
        self.assertNotIn('suspend_failing', self.kinds())
        self.update('automerge_batches', status='running', phase='building', terminal_status=None, suspend_verify_failures=3)
        self.assertNotIn('suspend_failing', self.kinds())

    def test_campaign(self):
        self.insert('stabilize_campaigns', id='campaign', status='paused', pause_reason='dispatch refused')
        self.assertIn('campaign_paused', self.kinds())
        self.update('stabilize_campaigns', pause_reason='Only escalated issues remain in push')
        self.assertNotIn('campaign_paused', self.kinds())
        self.update('stabilize_campaigns', pause_reason='dispatch refused')
        self.insert('stabilize_runtime', campaign_id='campaign', pause_kind='escalated_only')
        self.assertNotIn('campaign_paused', self.kinds())
        self.update('stabilize_campaigns', status='running')
        self.assertNotIn('campaign_paused', self.kinds())

    def test_disk(self):
        with mock.patch.object(hp.os, 'statvfs', return_value=mock.Mock(f_blocks=100, f_bfree=9, f_bavail=9)):
            self.assertIn('disk_high', self.kinds())
            # One finding per actual device, even with several paths on that device.
            devices = {Path('/').stat().st_dev, self.path.stat().st_dev, Path.home().stat().st_dev}
            self.assertLessEqual(sum(r['kind'] == 'disk_high' for r in self.result()['findings']), len(devices))
        self.assertNotIn('disk_high', self.kinds())

    def test_repeats(self):
        for i in range(4):
            self.repair(str(i), status='completed')
            self.insert('issue_repair_deferrals', job_id=str(i), issue_number=12)
        self.assertTrue({'fixer_repeat', 'fixer_deferral_repeat'} <= self.kinds())
        for table in ('issue_repairs', 'issue_repair_deferrals'):
            self.update(table, created_at=(NOW - timedelta(hours=7)).isoformat())
        self.assertFalse({'fixer_repeat', 'fixer_deferral_repeat'} & self.kinds())

    def inventory(self, number=1, **values):
        item = dict(number=number, state='open', draft=False, baseRefName='master', labels=[])
        blocked = values.pop('blocked_reason', None)
        item.update(values)
        self.insert('automerge_pr_inventory', number=number, head_sha='a' * 40, data_json=json.dumps(item), blocked_reason=blocked)

    def test_merger_admission(self):
        self.inventory()
        self.inventory(2, labels=[{'name': 'mergemarshall:immediate'}])
        self.assertEqual({r['entity'] for r in self.result()['findings'] if r['kind'] == 'merge_admission_stall'}, {'ordinary', 'immediate'})
        self.batch()
        self.batch('immediate', lane='immediate')
        self.assertNotIn('merge_admission_stall', self.kinds())

    def test_merger_obvious_ineligibility(self):
        for i, values in enumerate([dict(draft=True), dict(state='closed'), dict(baseRefName='release'),
                dict(blocked_reason='dependency'), dict(labels=[{'name': 'mergemarshall:batch'}]),
                dict(labels=[{'name': 'mergemarshall:rejected'}])]):
            self.inventory(i + 1, **values)
        self.assertNotIn('merge_admission_stall', self.kinds())
        self.inventory(9)
        self.batch(lane='immediate', pull_requests_json=json.dumps([{'number': 9}]))
        self.assertNotIn('merge_admission_stall', self.kinds())
        self.conn.execute('DELETE FROM automerge_batches')
        self.insert('automerge_github_outbox', intent_id='reject', kind='reject_head', number=9, head_sha='a' * 40)
        self.assertNotIn('merge_admission_stall', self.kinds())

    def test_merger_admission_ignores_landed_stale_inventory(self):
        # PR 6329 kept its open inventory record after an immediate batch landed it.
        self.inventory(5, labels=[{'name': 'mergemarshall:immediate'}])
        self.batch('landed', status='completed', phase='terminal', terminal_status='merged', lane='immediate',
                   pull_requests_json=json.dumps([{'number': 5, 'head_sha': 'a' * 40}]))
        self.assertNotIn('merge_admission_stall', self.kinds())
        self.update('automerge_batches', pull_requests_json=json.dumps([{'number': 5, 'head_sha': 'b' * 40}]))
        self.assertIn('merge_admission_stall', self.kinds())

    def test_fixer_admission(self):
        self.insert('known_failures', identity='failure', first_seen_at=(NOW - timedelta(hours=3)).isoformat(), linked_issue_url='https://github.com/x/y/issues/12')
        self.assertIn('fixer_admission_stall', self.kinds())
        self.repair()
        self.assertNotIn('fixer_admission_stall', self.kinds())
        self.update('issue_repairs', status='completed', report_json='{"outcome":"submitted"}')
        self.assertNotIn('fixer_admission_stall', self.kinds())
        self.update('issue_repairs', report_json='{}')
        self.update('known_failures', triage_outcome='infrastructure')
        self.assertNotIn('fixer_admission_stall', self.kinds())
        self.update('known_failures', triage_outcome=None, first_seen_at=NOW.isoformat())
        self.assertNotIn('fixer_admission_stall', self.kinds())

    def test_merge_handoff(self):
        self.batch(ready_json=json.dumps(dict(id='receipt', head='a' * 40)))
        self.assertIn('merge_handoff_unconsumed', self.kinds())
        first = next(r for r in self.result()['findings'] if r['kind'] == 'merge_handoff_unconsumed')
        self.update('automerge_batches', ready_json=json.dumps(dict(id='receipt', head='b' * 40)))
        second = next(r for r in self.result()['findings'] if r['kind'] == 'merge_handoff_unconsumed')
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])
        self.batch('parent')
        self.conn.execute("UPDATE automerge_batches SET predecessor_id='parent' WHERE batch_id='batch'")
        self.conn.commit()
        self.assertNotIn('merge_handoff_unconsumed', self.kinds())
        self.update('automerge_batches', predecessor_id=None, github_write_retry_after=(NOW + timedelta(minutes=5)).isoformat())
        self.assertNotIn('merge_handoff_unconsumed', self.kinds())
        self.update('automerge_batches', github_write_retry_after=None, ready_json='{}')
        self.assertNotIn('merge_handoff_unconsumed', self.kinds())

    def test_handoff_phase_and_receipt_identity(self):
        self.batch(ready_json=json.dumps(dict(id='a' * 32, head='b' * 40)))
        def fingerprint():
            row = next(r for r in self.result()['findings'] if r['kind'] == 'merge_handoff_unconsumed')
            self.assertEqual(row['fingerprint'], f"{row['kind']}:{row['entity']}:" + hp.hashlib.sha256(hp.normalized_cause(row['cause']).encode()).hexdigest()[:12])
            return row['fingerprint']
        first = fingerprint()
        self.update('automerge_batches', phase='fixing')
        self.assertNotEqual(first, fingerprint())
        second = fingerprint()
        self.update('automerge_batches', ready_json=json.dumps(dict(id='c' * 32, head='b' * 40)))
        self.assertNotEqual(second, fingerprint())

    def test_malformed_handoff_does_not_expose_body(self):
        self.batch(ready_json=json.dumps(dict(id='receipt', head={'body': 'PRIVATE'})))
        result = self.result()
        self.assertIn('merge_handoff', {r['check'] for r in result['check_errors']})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_fixer_handoff(self):
        self.repair(status='finishing', report_json='{"outcome":"submitted"}')
        self.assertIn('fixer_handoff_unconsumed', self.kinds())
        self.update('issue_repairs', status='running', report_json=None)
        self.assertNotIn('fixer_handoff_unconsumed', self.kinds())
        self.insert('issue_repair_messages', job_id='repair', stable_id='report-handoff:abc')
        self.assertIn('fixer_handoff_unconsumed', self.kinds())
        self.update('issue_repairs', recovery_json='{"stage":"running"}')
        self.assertNotIn('fixer_handoff_unconsumed', self.kinds())
        self.update('issue_repairs', status='completed')
        self.assertNotIn('fixer_handoff_unconsumed', self.kinds())

    def test_concurrency_config_and_environment(self):
        os.environ.pop('BIFROST_FIXER_CONCURRENCY')
        self.assertEqual(issue_fixer.concurrency_limit(), 1)
        config = self.root / 'config'
        config.mkdir()
        (config / 'fixer-concurrency').write_text('5')
        self.assertEqual(issue_fixer.concurrency_limit(), 5)
        os.environ['BIFROST_FIXER_CONCURRENCY'] = '2'
        self.assertEqual(issue_fixer.concurrency_limit(), 2)
        os.environ['BIFROST_FIXER_CONCURRENCY'] = 'bad'
        self.assertIn('fixer_admission', {r['check'] for r in self.result()['check_errors']})

    def test_missing_schema(self):
        self.conn.execute('DROP TABLE known_failure_errors')
        self.conn.execute('ALTER TABLE issue_repairs DROP COLUMN last_error')
        self.conn.commit()
        result = self.result()
        self.assertTrue({'ledger_errors', 'repair_error'} <= {r['check'] for r in result['check_errors']})
        self.assertEqual(result['format'], 1)
        self.update('entrypoint_heartbeats', last_finished_at=None)
        self.assertIn('heartbeat_stale', {r['kind'] for r in self.result()['findings']})

    def test_secret_scrub_fingerprints_and_order(self):
        self.repair(last_error='attempt 3 timeout after 30 seconds at 2026-10-27T01:02:03Z abcdef123456 token=PRIVATE ghp_SECRET')
        result = self.result()
        output = json.dumps(result)
        self.assertNotIn('PRIVATE', output)
        self.assertNotIn('ghp_SECRET', output)
        first = next(r for r in result['findings'] if r['kind'] == 'repair_error')
        self.update('issue_repairs', last_error='attempt 9 timeout after 50 seconds at 2026-10-27T05:06:07Z 123456abcdef token=OTHER ghp_OTHER')
        second = next(r for r in self.result()['findings'] if r['kind'] == 'repair_error')
        self.assertEqual(first['fingerprint'], second['fingerprint'])
        self.update('issue_repairs', last_error='authentication refused')
        third = next(r for r in self.result()['findings'] if r['kind'] == 'repair_error')
        self.assertNotEqual(first['fingerprint'], third['fingerprint'])
        self.assertEqual(self.result(), self.result())
        findings = self.result()['findings']
        self.assertEqual(findings, sorted(findings, key=lambda r: (r['kind'], r['entity'], r['fingerprint'])))

    def test_cli_and_subprocess_allowlist(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = hp.main(['--db', str(self.path), '--sessions-json', str(self.sessions_file), '--now', NOW.isoformat()])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {'format', 'probed_at', 'findings', 'check_errors'})
        self.assertEqual(result['format'], 1)
        self.assertEqual(result['probed_at'], NOW.isoformat())
        self.mj.return_value.stdout = '{"sessions": []}'
        hp.Probe(self.path, now=NOW).run()
        for call in self.mj.call_args_list:
            self.assertIn(call.args[0][1:], [['api-info', '--json'], ['sessions', '--workspace', 'CI', '--json']])
            self.assertLessEqual(call.kwargs['timeout'], 15)

    def test_missing_database_and_bad_sessions_still_json(self):
        self.sessions_file.write_text('null')
        result = hp.Probe(self.root / 'absent.db', now=NOW, sessions_json=self.sessions_file).run()
        self.assertEqual({r['check'] for r in result['check_errors']}, {'database', 'native_sessions'})
        self.assertFalse((self.root / 'absent.db').exists())


if __name__ == '__main__':
    unittest.main()
