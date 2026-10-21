"""Benchmark evidence, provenance and durable triage lifecycle contracts."""
import copy
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from unittest import TestCase, mock
import zipfile

import monitor
import performance_findings as p
import triage
from test_triage import FakeGitHub


def run(identifier=10, sha='a', repository='BrokkAi/bifrost-dev'):
    return dict(id=identifier, run_attempt=1, head_sha=sha * 40,
        head_repository={'full_name': repository}, head_branch='master' if repository.endswith('bifrost-dev') else 'main',
        path='.github/workflows/benchmark.yml', event='schedule', status='completed', conclusion='success',
        created_at=f'2026-10-16T0{identifier % 8}:00:00+00:00', run_started_at='2026-10-16T00:00:00Z',
        html_url=f'https://github.com/{repository}/actions/runs/{identifier}')


def bifrost_members(r, regression=True, baseline='b'):
    report = dict(bifrost_commit='c' * 40, selected_repo=None, max_files=None,
                  repos=[dict(name='fixture', commit='d' * 40)])
    row = dict(repo_name='fixture', scenario='query_code', case_id='one', transport='mcp',
        baseline_success=True, candidate_success=True, baseline_median_ms=100,
        candidate_median_ms=200 if regression else 100, delta_ms=100 if regression else 0,
        delta_pct=100 if regression else 0, is_regression=regression,
        outcome='regression' if regression else 'unchanged')
    compare = dict(thresholds={'relative_pct': 20, 'absolute_ms': 50}, compared_scenarios_count=1,
        regression_count=int(regression), actionable_regression_count=int(regression), scenarios=[row])
    raw, comp = json.dumps(report).encode(), json.dumps(compare).encode()
    proof = dict(source_sha=r['head_sha'], run_id=r['id'], run_attempt=r['run_attempt'],
        build_identity='c' * 40, report_sha256=hashlib.sha256(raw).hexdigest(),
        comparison_sha256=hashlib.sha256(comp).hexdigest(), baseline_sha256=baseline * 64,
        manifest_sha256='e' * 64, bundle_profile='release', bundle_features='default')
    return {'run-timestamp.json': raw, f'compare-{r["id"]}.json': comp,
            'full-performance-provenance.json': json.dumps(proof).encode()}


def usage_members(r, status='failed', complete=True):
    report = dict(usagebenchRevision=r['head_sha'], bifrostResolvedCommit='f' * 40,
        invocation={'includeUnsupported': False}, completed=complete,
        totals={'failed': int(status == 'failed'), 'cases': 1},
        documents=[dict(caseFile='cases/fixture.yaml', language='cpp',
            cases=[dict(id='reference', status=status,
                        locationMetrics={'falseNegatives': 1, 'falsePositives': 0})])])
    return {f'run-{r["id"]}.json': json.dumps(report).encode()}


class Reports(TestCase):
    def test_real_comparator_flags_and_semantic_baseline_are_preserved(self):
        r = run(); a = p.bifrost_report(bifrost_members(r), r)
        self.assertEqual(a[1][0][2]['candidate_median_ms'], 200)
        self.assertEqual(a[1][0][2]['thresholds'], {'relative_pct': 20, 'absolute_ms': 50})
        self.assertNotEqual(a[0], p.bifrost_report(bifrost_members(r, baseline='f'), r)[0])
        passing = p.bifrost_report(bifrost_members(r, regression=False), r)
        self.assertEqual(passing[0], a[0]); self.assertEqual(passing[1], [])
        self.assertEqual(passing[2], [a[1][0][0]])

    def test_cross_head_or_mixed_attempt_proof_cannot_produce_regression(self):
        for field, value in [('source_sha', 'f' * 40), ('run_attempt', 2), ('build_identity', 'f' * 40)]:
            r = run(); members = bifrost_members(r); proof = json.loads(members['full-performance-provenance.json'])
            proof[field] = value; members['full-performance-provenance.json'] = json.dumps(proof).encode()
            with self.assertRaisesRegex(ValueError, 'provenance'):
                p.bifrost_report(members, r)

    def test_usagebench_keeps_engine_revision_distinct_and_ignores_expected_failure(self):
        r = run(repository='BrokkAi/usagebench')
        result = p.usagebench_report(usage_members(r), r)
        self.assertEqual(result[1][0][2]['engine_sha'], 'f' * 40)
        self.assertEqual(result[1][0][2]['harness_sha'], 'a' * 40)
        for status in ('expected_failure', 'unsupported', 'not_planned', 'skipped'):
            self.assertEqual(p.usagebench_report(usage_members(r, status), r)[1], [])

    def test_partial_usagebench_keeps_observed_failures_without_inventing_complete_coverage(self):
        r = run(repository='BrokkAi/usagebench'); members = usage_members(r, complete=False)
        members[f'run-{r["id"]}.partial.json'] = members.pop(f'run-{r["id"]}.json')
        result = p.usagebench_report(members, r)
        self.assertFalse(result[3]['complete'])
        self.assertEqual([v[1] for v in result[1]], ['case_failure', 'coverage'])

    def test_untrusted_zip_paths_and_duplicate_members_are_rejected_without_extraction(self):
        for names in (['../report.json'], ['/report.json'], ['same.json', 'same.json']):
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w') as z:
                for name in names:z.writestr(name, '{}')
            with self.assertRaises(ValueError):p.read_archive(out.getvalue())

    def test_binary_github_download_uses_shared_token_and_preserves_exact_bytes(self):
        payload = b'PK\x03\x04\xff\x00\r\n '
        with (mock.patch.object(monitor, 'github_app_token', side_effect=['expired', 'fresh']) as token,
              mock.patch.object(monitor.subprocess, 'run', side_effect=[
                  subprocess.CompletedProcess([], 1, b'', b'HTTP 401: Bad credentials'),
                  subprocess.CompletedProcess([], 0, payload, b'')]) as invoke):
            self.assertEqual(monitor.run_gh(['api', 'artifact/zip'], binary=True), payload)
        self.assertEqual(token.call_count, 2)
        self.assertEqual(invoke.call_args.kwargs['env']['GH_TOKEN'], 'fresh')
        self.assertFalse(invoke.call_args.kwargs['text'])


class DurableFindings(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(monitor, 'DB_PATH', self.root/'state.db');patcher.start();self.addCleanup(patcher.stop)
        self.conn = monitor.connect_db();self.addCleanup(self.conn.close);triage.ensure_schema(self.conn)

    def record(self, r, failed=True, baseline='b'):
        result = p.bifrost_report(bifrost_members(r, failed, baseline), r)
        with self.conn:p.record(self.conn, p.SOURCES[0][0], 'benchmark.yml', r, *result)

    def test_repeated_run_one_investigation_recovery_and_recurrence_are_separate_episodes(self):
        r = run(8); self.record(r)
        first = triage.pending(self.conn)[0]
        job = {'id': 'triage-one'}
        finding = {'outcome': 'product', 'diagnosis': 'real regression', 'issue': {}}
        with self.conn:triage.record_observation(self.conn, job, finding, first, 'https://github.com/BrokkAi/bifrost-dev/issues/1')
        self.record(run(9));self.assertEqual(triage.pending(self.conn), [])
        self.record(run(10), False);self.assertEqual(p.observations(self.conn), [])
        self.record(run(11));again = triage.pending(self.conn)[0]
        self.assertNotEqual(first['fingerprint'], again['fingerprint'])
        self.assertEqual(again['episode'], 2)

    def test_new_baseline_or_older_pass_cannot_retire_current_failure(self):
        self.record(run(11));self.record(run(10), False)
        self.assertEqual(len(p.observations(self.conn)), 1)
        self.record(run(12), False, baseline='f')
        self.assertEqual(len(p.observations(self.conn)), 1)

    def test_triage_resolution_is_not_a_passing_measurement_or_a_new_episode(self):
        self.record(run(8))
        first = triage.pending(self.conn)[0]
        finding = {'outcome': 'resolved', 'diagnosis': 'private engine fixed; public projection stale',
                   'evidence': 'Private replay passed; observed public engine still fails', 'issue': None}
        with self.conn:
            self.assertTrue(triage.retire_resolved_observation(self.conn, {'id': 'review'}, finding, first))
            triage.record_observation(self.conn, {'id': 'review'}, finding, first, None)
        row = p.observations(self.conn)[0]
        self.assertEqual((row['status'], row['triage_outcome'], row['episode']), ('open', 'resolved', 1))
        self.assertEqual(triage.pending(self.conn), [])
        self.record(run(9))
        self.assertEqual(triage.pending(self.conn), [])
        row = p.observations(self.conn)[0]
        self.assertEqual((row['status'], row['triage_outcome'], row['episode']), ('open', 'resolved', 1))
        self.record(run(10), False)
        self.assertEqual(p.observations(self.conn), [])
        self.record(run(11))
        again = triage.pending(self.conn)[0]
        self.assertEqual(again['episode'], 2)
        self.assertNotEqual(again['fingerprint'], first['fingerprint'])

    def test_missing_measurement_is_coverage_and_never_a_measured_regression(self):
        with (mock.patch.object(p, 'api', side_effect=[{'total_count': 0, 'artifacts': []},
                {'jobs': [{'name': 'Build', 'conclusion': 'failure', 'steps': []}]}])):
            with self.conn:p.ingest(self.conn, p.SOURCES[0], run())
        row = p.observations(self.conn)[0]
        self.assertEqual(row['kind'], 'coverage')
        self.assertNotIn('candidate_median_ms', row['evidence_json'])

    def test_wrong_repository_is_rejected_before_artifact_reads(self):
        r = run(repository='evil/fork')
        with mock.patch.object(p, 'api') as api:
            with self.assertRaises(ValueError):p.ingest(self.conn, p.SOURCES[0], r)
        api.assert_not_called()

    def test_partial_bifrost_artifact_records_coverage_without_retiring_measured_failures(self):
        self.record(run(8))
        r = run(9)
        all_members = bifrost_members(r)
        for missing in ('comparison', 'provenance', 'report'):
            members = dict(all_members)
            key = {'comparison': 'compare-9.json', 'provenance': 'full-performance-provenance.json',
                   'report': 'run-timestamp.json'}[missing]
            del members[key]
            self.ingest_members(r, members)
        rows = p.observations(self.conn)
        self.assertEqual(sorted(row['kind'] for row in rows), ['coverage', 'regression'])
        coverage = next(row for row in rows if row['kind'] == 'coverage')
        evidence = json.loads(coverage['evidence_json'])
        self.assertIn('missing', evidence['reason'])
        self.assertEqual(evidence['jobs'][0]['failed_steps'], ['Native benchmark'])
        self.assertNotIn('candidate_median_ms', evidence)
        receipt = self.conn.execute('SELECT status,error_class,summary_json FROM performance_runs '
            'WHERE run_id=9').fetchone()
        self.assertEqual(receipt['status'], 'done')
        self.assertIsNone(receipt['error_class'])
        self.assertFalse(json.loads(receipt['summary_json'])['complete'])

    def ingest_members(self, r, members, current=None):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            for name, raw in members.items():
                archive.writestr(name, raw)
        raw = out.getvalue()
        artifact = {'id': 1, 'name': f'benchmark-{r["id"]}', 'expired': False,
            'size_in_bytes': len(raw), 'created_at': r['run_started_at'],
            'workflow_run': {'head_sha': r['head_sha']},
            'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
        self.conn.execute('INSERT OR IGNORE INTO performance_runs VALUES(?,?,?,?,?,?,?,?,?)',
            (r['head_repository']['full_name'], r['id'], 1, r['created_at'], r['created_at'],
             r['head_sha'], 'pending', 'ValueError', '{}'))
        def api(repo, endpoint):
            if '/artifacts?' in endpoint:
                return {'total_count': 1, 'artifacts': [artifact]}
            if '/jobs?' in endpoint:
                return {'jobs': [{'name': 'Benchmark', 'conclusion': 'failure',
                    'steps': [{'name': 'Native benchmark', 'conclusion': 'failure'}]}]}
            return current or r
        with mock.patch.object(p, 'api', side_effect=api), mock.patch.object(monitor, 'run_gh', return_value=raw):
            with self.conn:
                p.ingest(self.conn, p.SOURCES[0], r)

    def test_partial_artifact_from_superseded_attempt_is_not_coverage(self):
        r = run(9)
        with self.assertRaisesRegex(ValueError, 'attempt changed'):
            self.ingest_members(r, {'run-one.json': b'{}'}, current={**r, 'run_attempt': 2})
        self.assertEqual(p.observations(self.conn), [])

    def test_complete_authenticated_comparison_still_records_measured_regression(self):
        r = run(9)
        self.ingest_members(r, bifrost_members(r))
        row, = p.observations(self.conn)
        self.assertEqual(row['kind'], 'regression')
        self.assertEqual(json.loads(row['evidence_json'])['candidate_median_ms'], 200)

    def test_ambiguous_partial_and_bad_complete_artifacts_remain_invalid(self):
        r = run(9)
        ambiguous = {'first/run-one.json': b'{}', 'second/run-two.json': b'{}'}
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.ingest_members(r, ambiguous)
        members = bifrost_members(r)
        proof = json.loads(members['full-performance-provenance.json'])
        proof['source_sha'] = 'f' * 40
        members['full-performance-provenance.json'] = json.dumps(proof).encode()
        with self.assertRaisesRegex(ValueError, 'provenance mismatch'):
            self.ingest_members(r, members)
        self.assertEqual(p.observations(self.conn), [])

    def test_changed_engine_with_unchanged_harness_gets_fresh_review(self):
        r = run(8, repository='BrokkAi/usagebench')
        with self.conn:p.record(self.conn, p.SOURCES[1][0], 'benchmark.yml', r,
                                *p.usagebench_report(usage_members(r), r))
        first = triage.pending(self.conn)[0]
        finding = {'outcome': 'product', 'diagnosis': 'first engine failure', 'issue': {}}
        with self.conn:triage.record_observation(self.conn, {'id': 'first'}, finding, first, None)
        later = run(9, repository='BrokkAi/usagebench');members = usage_members(later)
        report = json.loads(members['run-9.json']);report['bifrostResolvedCommit'] = 'e' * 40
        members['run-9.json'] = json.dumps(report).encode()
        with self.conn:p.record(self.conn, p.SOURCES[1][0], 'benchmark.yml', later,
                                *p.usagebench_report(members, later))
        current = triage.pending(self.conn)[0]
        self.assertNotEqual(current['fingerprint'], first['fingerprint'])
        self.assertEqual(current['harness_sha'], 'a' * 40)
        self.assertEqual(current['last_seen_sha'], 'e' * 40)

    def test_one_bad_source_does_not_block_other_source_and_new_runs_can_pass_a_failed_import(self):
        first, second = run(8), run(9)
        calls = []
        def ingest(conn, source, value):
            calls.append((source[0], value['id']))
            raise ValueError('fixture invalid report')
        def api(repo, endpoint):
            return {'workflow_runs': [first, second]} if repo == p.SOURCES[0][0] else {'workflow_runs': [run(8, repository=repo)]}
        now = dt.datetime(2026, 10, 16, 9, tzinfo=dt.timezone.utc)
        with mock.patch.object(p, 'api', side_effect=api), mock.patch.object(p, 'ingest', side_effect=ingest):
            p.tick(self.conn, now=now);p.tick(self.conn, now=now+dt.timedelta(minutes=5))
        self.assertEqual(calls[:2], [(p.SOURCES[0][0], 8), (p.SOURCES[1][0], 8)])
        self.assertEqual(calls[2], (p.SOURCES[0][0], 9))

    def test_performance_ticket_uses_durable_publication_without_fixer_buildfailure_label(self):
        self.record(run());observation = triage.pending(self.conn)[0]
        job = {'id': 'publication', 'session_id': 'session', 'observations_json': json.dumps([observation])}
        finding = {'diagnosis': 'query latency doubled', 'evidence': '100ms to200ms', 'outcome': 'product',
            'issue': {'title': 'Query regression', 'body': 'Investigate query work', 'existing_number': None}}
        finding['issue_body'] = triage.issue_body(job, 0, finding, [observation])
        gh = FakeGitHub()
        with mock.patch.object(triage, 'gh_api', side_effect=gh):
            first = triage.publish_issue(self.conn, job, 0, finding, [observation])
            again = triage.publish_issue(self.conn, job, 0, finding, [observation])
        self.assertEqual(first, again);self.assertEqual(len(gh.issues), 1)
        self.assertEqual(next(iter(gh.issues.values()))['labels'], [{'name': p.LABEL}])
        self.assertIn('engine_sha', finding['issue_body'])
        self.assertIn('UsageBench', triage.build_prompt({**job, 'base_sha': 'a'*40}))

    def test_external_harness_ticket_target_is_allowlisted_and_durable(self):
        r = run(repository='BrokkAi/usagebench')
        with self.conn:p.record(self.conn, r['head_repository']['full_name'], 'benchmark.yml',
                                r, *p.usagebench_report(usage_members(r), r))
        observation = triage.pending(self.conn)[0]
        finding = {'diagnosis': 'wrong authored location', 'evidence': 'source token differs',
            'outcome': 'product', 'issue': {'repository': 'BrokkAi/usagebench', 'title': 'Wrong location',
            'body': 'Fix the authored ground truth', 'existing_number': None}, 'issue_body': 'draft'}
        job = {'id': 'external', 'session_id': 'session'};gh = FakeGitHub()
        def api(endpoint, *, repository=None, **kwargs):
            self.assertEqual(repository, 'BrokkAi/usagebench')
            return gh(endpoint, **kwargs)
        with mock.patch.object(triage, 'gh_api', side_effect=api):
            url = triage.publish_issue(self.conn, job, 0, finding, [observation])
            self.assertEqual(triage.publish_issue(self.conn, job, 0, finding, [observation]), url)
        stored = self.conn.execute('SELECT * FROM triage_publications').fetchone()
        self.assertEqual(stored['repository'], 'BrokkAi/usagebench')
        self.assertEqual(url, 'https://github.com/BrokkAi/usagebench/issues/101')
        for repository in ('evil/other', monitor.REPO_NAME):
            finding['issue']['repository'] = repository
            with self.assertRaises(ValueError):triage.publish_issue(self.conn, job, 0, finding, [observation])

    def test_ordinary_ci_observations_cannot_route_tickets_to_another_repository(self):
        finding = {'issue': {'repository': 'BrokkAi/usagebench'}}
        with self.assertRaises(ValueError):triage.issue_repository(finding, [{'workflow': 'CI'}])
