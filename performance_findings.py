"""Retryable benchmark artifact intake; observations never participate in merge gates."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
from pathlib import PurePosixPath
import re
import stat
import zipfile

import monitor

SOURCES = (
    ('BrokkAi/bifrost-dev', 'master', 'benchmark.yml', 'bifrost'),
    ('BrokkAi/usagebench', 'main', 'benchmark.yml', 'usagebench'),
)
LABEL = 'performance-regression'
MAX_ZIP_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024


def ensure_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS performance_runs (
            repository TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, head_sha TEXT NOT NULL,
            status TEXT NOT NULL, error_class TEXT, summary_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(repository,run_id,attempt)
        );
        CREATE TABLE IF NOT EXISTS performance_findings (
            id TEXT PRIMARY KEY, repository TEXT NOT NULL, workflow TEXT NOT NULL,
            comparison_key TEXT NOT NULL, identity TEXT NOT NULL, kind TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, first_seen_sha TEXT NOT NULL,
            first_seen_run_id INTEGER NOT NULL, first_seen_run_url TEXT NOT NULL,
            last_seen_at TEXT NOT NULL, last_seen_sha TEXT NOT NULL,
            last_seen_run_id INTEGER NOT NULL, last_seen_run_url TEXT NOT NULL,
            evidence_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
            diagnosis TEXT, triage_outcome TEXT, triage_issue_url TEXT,
            episode INTEGER NOT NULL DEFAULT 1, harness_sha TEXT
        );
        CREATE TABLE IF NOT EXISTS performance_poll (
            repository TEXT PRIMARY KEY, checked_at TEXT NOT NULL
        );
    ''')
    monitor.ensure_column(conn, 'performance_findings', 'episode', 'INTEGER NOT NULL DEFAULT 1')
    monitor.ensure_column(conn, 'performance_findings', 'harness_sha', 'TEXT')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def text(value, limit=500):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError('invalid benchmark text field')
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value):
        raise ValueError('missing exact benchmark source revision')
    return value


def project(value, fields):
    return {k: value[k] for k in fields if k in value}


def json_object(raw):
    obj = json.loads(raw, parse_constant=lambda _x: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    if not isinstance(obj, dict):
        raise ValueError('benchmark report must be an object')
    return obj


def read_archive(raw):
    """Read bounded JSON members without extracting or executing artifact content."""
    if len(raw) > MAX_ZIP_BYTES:
        raise ValueError('benchmark archive too large')
    members = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000:
            raise ValueError('too many benchmark archive members')
        for entry in entries:
            name = entry.filename
            if (name.startswith('/') or '\\' in name or
                    any(p in ('', '.', '..') for p in name.rstrip('/').split('/')) or
                    stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('unsafe benchmark archive member')
            if entry.is_dir() or not name.endswith('.json'):
                continue
            if name in members or entry.file_size > MAX_MEMBER_BYTES:
                raise ValueError('duplicate or oversized benchmark member')
            members[name] = archive.read(entry)
            if sum(map(len, members.values())) > 2 * MAX_MEMBER_BYTES:
                raise ValueError('benchmark JSON population too large')
    return members


def one_member(members, predicate):
    found = [(name, raw) for name, raw in members.items() if predicate(PurePosixPath(name).name)]
    if len(found) != 1:
        raise ValueError('missing or ambiguous benchmark report')
    return found[0]


def bifrost_report(members, run):
    _, raw = one_member(members, lambda n: n.startswith('run-') and n.endswith('.json'))
    _, compare_raw = one_member(members, lambda n: n == f'compare-{run["id"]}.json')
    _, proof_raw = one_member(members, lambda n: n == 'full-performance-provenance.json')
    report, compare, proof = map(json_object, (raw, compare_raw, proof_raw))
    if (proof.get('source_sha') != run['head_sha'] or str(proof.get('run_id')) != str(run['id']) or
            str(proof.get('run_attempt')) != str(run['run_attempt']) or
            proof.get('report_sha256') != hashlib.sha256(raw).hexdigest() or
            proof.get('comparison_sha256') != hashlib.sha256(compare_raw).hexdigest() or
            proof.get('build_identity') != report.get('bifrost_commit')):
        raise ValueError('benchmark provenance mismatch')
    sha(proof['build_identity'])
    for field in ('baseline_sha256', 'manifest_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', proof.get(field, '')):
            raise ValueError('benchmark comparison identity missing')
    if report.get('selected_repo') is not None or report.get('max_files') is not None:
        return 'subset', [], [], {'complete': False, 'diagnostic_subset': True}
    fixture_revisions = [(text(r['name']), sha(r['commit'])) for r in report['repos']]
    if not fixture_revisions or not compare.get('scenarios'):
        raise ValueError('empty benchmark population')
    context = digest([project(proof, ('baseline_sha256', 'manifest_sha256', 'bundle_target',
                                     'bundle_profile', 'bundle_features', 'bundle_rustflags')),
                      compare.get('thresholds'), fixture_revisions])
    variance = compare.get('environment_variance') or {}
    candidates, passing, seen = [], [], set()
    for row in compare['scenarios']:
        identity = json.dumps([text(row['repo_name']), text(row['scenario']),
                               text(row['case_id']) if row.get('case_id') is not None else None,
                               text(row['transport'])], separators=(',', ':'))
        if identity in seen:
            raise ValueError('duplicate benchmark scenario identity')
        seen.add(identity)
        evidence = project(row, ('repo_name', 'scenario', 'case_id', 'transport', 'outcome',
                                'baseline_success', 'candidate_success', 'baseline_median_ms',
                                'candidate_median_ms', 'delta_ms', 'delta_pct', 'is_regression',
                                'cold_retention_failure', 'plan_flips'))
        for key, value in evidence.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError('nonfinite benchmark measurement')
        evidence.update(engine_repository=run['head_repository']['full_name'], engine_sha=run['head_sha'],
                        harness_sha=run['head_sha'],
                        build_identity=proof['build_identity'],
                        thresholds=project(compare.get('thresholds') or {}, ('relative_pct', 'absolute_ms')),
                        baseline_sha256=proof['baseline_sha256'],
                        environment_variance=project(variance, ('covered_regression_count',
                            'median_workspace_build_delta_pct', 'median_timed_delta_pct',
                            'disproportionate_regression_count')),
                        actionable_regression_count=compare.get('actionable_regression_count'))
        if row.get('is_regression') is True or row.get('candidate_success') is False:
            candidates.append((identity, 'regression', evidence))
        elif row.get('candidate_success') is True and row.get('outcome') in ('unchanged', 'improvement'):
            passing.append(identity)
    if len(compare['scenarios']) != compare.get('compared_scenarios_count'):
        raise ValueError('incomplete benchmark comparison')
    return context, candidates, passing, {'engine_sha': run['head_sha'], 'complete': True,
        'raw_regressions': compare.get('regression_count'), 'environment_variance': bool(variance),
        'actionable_regressions': compare.get('actionable_regression_count')}


def usagebench_report(members, run):
    if any(PurePosixPath(n).name == f'run-{run["id"]}.json' for n in members):
        _, raw = one_member(members, lambda n: n == f'run-{run["id"]}.json')
        partial = False
    else:
        _, raw = one_member(members, lambda n: n == f'run-{run["id"]}.partial.json')
        partial = True
    report = json_object(raw)
    if report.get('usagebenchRevision') != run['head_sha']:
        raise ValueError('UsageBench harness revision mismatch')
    engine_sha = sha(report.get('bifrostResolvedCommit'))
    # The public engine revision is distinct from the UsageBench workflow SHA.
    context = digest([run['head_sha'], project(report.get('invocation') or {},
        ('includeUnsupported', 'includeDefinitionLookups', 'scanUsagesMaxDurationSecs'))])
    candidates, passing, seen = [], [], set()
    for document in report['documents']:
        for case in document['cases']:
            identity = json.dumps([text(document['caseFile']), text(case['id'])], separators=(',', ':'))
            if identity in seen:
                raise ValueError('duplicate UsageBench case identity')
            seen.add(identity)
            status = case.get('status')
            evidence = dict(engine_repository='BrokkAi/bifrost', engine_sha=engine_sha,
                harness_sha=run['head_sha'], case_file=document['caseFile'], case_id=case['id'],
                case_status=status, language=document.get('language'),
                corpus_partition=document.get('corpusPartition'),
                location_metrics=project(case.get('locationMetrics') or {},
                    ('cases', 'queries', 'truePositives', 'falsePositives', 'falseNegatives')))
            if status in ('failed', 'error', 'near_miss', 'nearMiss'):
                candidates.append((identity, 'case_failure', evidence))
            elif status in ('passed', 'improved'):
                passing.append(identity)
            elif status not in ('expected_failure', 'expectedFailure', 'not_planned', 'notPlanned',
                                'unsupported', 'skipped', 'position_unverified', 'positionUnverified'):
                raise ValueError('unrecognized UsageBench case status')
    complete = report.get('completed') is True and not partial
    requested = report.get('requestedCaseFiles')
    if requested is not None and set(requested) != {d['caseFile'] for d in report['documents']}:
        complete = False
    if not complete:
        candidates.append(('measurement-coverage', 'coverage', dict(engine_repository='BrokkAi/bifrost',
            engine_sha=engine_sha, reason='UsageBench report is incomplete',
            totals=project(report.get('totals') or {}, ('cases', 'failed', 'errors', 'passed')))))
    return context, candidates, passing, {'engine_sha': engine_sha, 'complete': complete,
        'totals': project(report.get('totals') or {}, ('cases', 'passed', 'failed', 'errors'))}


def observations(conn):
    result = []
    for row in conn.execute("SELECT * FROM performance_findings WHERE status='open' ORDER BY last_seen_at DESC,id"):
        result.append(dict(row, performance_finding_id=row['id'], identity_kind='performance',
            job_name='Measurement coverage' if row['kind'] == 'coverage' else 'Benchmark results',
            last_seen_failed_steps_json='[]', linked_issue_url=None,
            linked_issue_state='OPEN', linked_pr_url=None, linked_pr_state='OPEN'))
    return result


def classify(conn, identifier, outcome, diagnosis, issue_url=None):
    # Triage can establish that no further product repair is needed (for example,
    # the public projection is stale). Only comparable passing results can prove
    # that the observed benchmark failure itself recovered.
    conn.execute("UPDATE performance_findings SET diagnosis=?,triage_outcome=?,"
                 "triage_issue_url=COALESCE(?,triage_issue_url) WHERE id=? AND status='open'",
                 (diagnosis, outcome, issue_url, identifier))


def record(conn, repository, workflow, run, context, candidates, passing, summary):
    stamp = run['created_at']
    for identity, kind, evidence in candidates:
        key = digest([repository, workflow, context, identity])
        observed_sha = evidence.get('engine_sha') or run['head_sha']
        sha(observed_sha)
        conn.execute('''INSERT INTO performance_findings
            (id,repository,workflow,comparison_key,identity,kind,first_seen_at,first_seen_sha,
             first_seen_run_id,first_seen_run_url,last_seen_at,last_seen_sha,last_seen_run_id,
             last_seen_run_url,evidence_json,harness_sha) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
             last_seen_sha=excluded.last_seen_sha,last_seen_run_id=excluded.last_seen_run_id,
             last_seen_run_url=excluded.last_seen_run_url,evidence_json=excluded.evidence_json,
             harness_sha=excluded.harness_sha,
             episode=performance_findings.episode+CASE WHEN performance_findings.status='fixed' THEN 1 ELSE 0 END,
             first_seen_run_id=CASE WHEN performance_findings.status='fixed' THEN excluded.first_seen_run_id
                ELSE performance_findings.first_seen_run_id END,
             first_seen_at=CASE WHEN performance_findings.status='fixed' THEN excluded.first_seen_at
                ELSE performance_findings.first_seen_at END,
             first_seen_sha=CASE WHEN performance_findings.status='fixed' THEN excluded.first_seen_sha
                ELSE performance_findings.first_seen_sha END,
             first_seen_run_url=CASE WHEN performance_findings.status='fixed' THEN excluded.first_seen_run_url
                ELSE performance_findings.first_seen_run_url END,
             status='open',triage_outcome=CASE WHEN performance_findings.last_seen_sha!=excluded.last_seen_sha
                OR performance_findings.status='fixed' THEN NULL ELSE performance_findings.triage_outcome END
            WHERE excluded.last_seen_at>=performance_findings.last_seen_at''',
            (key, repository, workflow, context, identity, kind, stamp, observed_sha, run['id'],
             run['html_url'], stamp, observed_sha, run['id'], run['html_url'], json.dumps(evidence), run['head_sha']))
    # Explicit comparable passes are recovery. Missing rows and interrupted runs are not.
    for identity in passing:
        conn.execute("UPDATE performance_findings SET status='fixed' WHERE repository=? AND workflow=? "
                     "AND comparison_key=? AND identity=? AND last_seen_at<=?",
                     (repository, workflow, context, identity, stamp))
    if summary.get('complete'):
        conn.execute("UPDATE performance_findings SET status='fixed' WHERE repository=? AND workflow=? "
                     "AND kind='coverage' AND last_seen_at<=?", (repository, workflow, stamp))
    conn.execute("UPDATE performance_runs SET status='done',error_class=NULL,summary_json=?,updated_at=? "
                 "WHERE repository=? AND run_id=? AND attempt=?",
                 (json.dumps(summary), monitor.utc_now(), repository, run['id'], run['run_attempt']))


def api(repository, endpoint):
    return json.loads(monitor.run_gh(['api', f'repos/{repository}/{endpoint}'], timeout=30))


def record_missing_measurement(conn, repository, workflow, run, reason):
    jobs = api(repository, f'actions/runs/{run["id"]}/jobs?per_page=100')
    evidence = {'reason': reason, 'workflow_conclusion': run['conclusion'],
        'harness_sha': run['head_sha'],
        'jobs': [{'name': j['name'], 'conclusion': j['conclusion'],
                  'failed_steps': [s['name'] for s in j.get('steps', []) if s.get('conclusion') == 'failure']}
                 for j in jobs['jobs'] if j['conclusion'] not in ('success', 'skipped')][:10]}
    record(conn, repository, workflow, run, 'coverage', [('measurement-coverage', 'coverage', evidence)],
           [], {'complete': False, 'missing_measurement': True})


def ingest(conn, source, run):
    repository, branch, workflow, adapter = source
    if (run.get('head_repository', {}).get('full_name') != repository or
            run.get('head_branch') != branch or run.get('path') != '.github/workflows/' + workflow or
            run.get('event') not in ('schedule', 'workflow_dispatch') or run.get('status') != 'completed'):
        raise ValueError('untrusted benchmark run')
    sha(run['head_sha'])
    artifacts = api(repository, f'actions/runs/{run["id"]}/artifacts?per_page=100')
    if artifacts['total_count'] > len(artifacts['artifacts']):
        raise ValueError('truncated benchmark artifact listing')
    expected = f'benchmark-{run["id"]}'
    selected = [a for a in artifacts['artifacts'] if not a['expired'] and
        (a['name'] == expected if adapter == 'bifrost' else
         a['name'].startswith('usagebench-') and a['name'].endswith('-' + str(run['id'])))]
    if not selected:
        record_missing_measurement(conn, repository, workflow, run,
            'No benchmark report artifact: no performance conclusion is possible')
        return
    if len(selected) != 1:
        raise ValueError('ambiguous benchmark artifact')
    artifact = selected[0]
    if (artifact['size_in_bytes'] > MAX_ZIP_BYTES or
            (artifact.get('workflow_run') or {}).get('head_sha') != run['head_sha'] or
            artifact['created_at'] < run['run_started_at']):
        raise ValueError('benchmark artifact provenance or size mismatch')
    raw = monitor.run_gh(['api', f'repos/{repository}/actions/artifacts/{artifact["id"]}/zip'], binary=True)
    if artifact.get('digest') and artifact['digest'] != 'sha256:' + hashlib.sha256(raw).hexdigest():
        raise ValueError('benchmark artifact digest mismatch')
    members = read_archive(raw)
    if adapter == 'bifrost':
        # The workflow uploads raw results even if it fails before comparison
        # and provenance generation. That is missing measurement, not a broken
        # import to retry forever. Ambiguous members remain invalid evidence.
        names = [PurePosixPath(name).name for name in members]
        populations = [sum(n.startswith('run-') and n.endswith('.json') for n in names),
                       names.count(f'compare-{run["id"]}.json'),
                       names.count('full-performance-provenance.json')]
        if any(count > 1 for count in populations):
            raise ValueError('missing or ambiguous benchmark report')
        if 0 in populations:
            current = api(repository, f'actions/runs/{run["id"]}')
            if current['run_attempt'] != run['run_attempt'] or current['status'] != 'completed':
                raise ValueError('benchmark run attempt changed during intake')
            record_missing_measurement(conn, repository, workflow, run,
                'Benchmark report, comparison or provenance is missing: no performance conclusion is possible')
            return
    context, candidates, passing, summary = (bifrost_report if adapter == 'bifrost' else usagebench_report)(members, run)
    # A rerun can supersede artifacts while they are being read. Do not mix attempts.
    current = api(repository, f'actions/runs/{run["id"]}')
    if current['run_attempt'] != run['run_attempt'] or current['status'] != 'completed':
        raise ValueError('benchmark run attempt changed during intake')
    record(conn, repository, workflow, run, context, candidates, passing, summary)


def tick(conn, *, now=None):
    ensure_schema(conn)
    current = now or dt.datetime.now(dt.timezone.utc)
    for source in SOURCES:
        repository, branch, workflow, _adapter = source
        prior = conn.execute('SELECT checked_at FROM performance_poll WHERE repository=?', (repository,)).fetchone()
        if prior and (current - dt.datetime.fromisoformat(prior[0])).total_seconds() < 300:
            continue
        with conn:
            conn.execute('INSERT OR REPLACE INTO performance_poll VALUES (?,?)', (repository, current.isoformat()))
        try:
            runs = api(repository, f'actions/workflows/{workflow}/runs?status=completed&branch={branch}&per_page=10')['workflow_runs']
            runs = [r for r in runs if r.get('event') in ('schedule', 'workflow_dispatch') and
                    dt.datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')) >= current - dt.timedelta(days=3)]
            def order(run):
                receipt = conn.execute('SELECT status,updated_at FROM performance_runs '
                    'WHERE repository=? AND run_id=? AND attempt=?',
                    (repository, run['id'], run['run_attempt'])).fetchone()
                return (receipt['updated_at'] if receipt and receipt['status'] != 'done' else '', run['created_at'])
            for run in sorted(runs, key=order):
                receipt = conn.execute('SELECT status FROM performance_runs WHERE repository=? AND run_id=? AND attempt=?',
                                      (repository, run['id'], run['run_attempt'])).fetchone()
                if receipt and receipt[0] == 'done':
                    continue
                with conn:
                    conn.execute("INSERT OR IGNORE INTO performance_runs VALUES (?,?,?,?,?,?,'pending',NULL,'{}')",
                        (repository, run['id'], run['run_attempt'], run['created_at'], monitor.utc_now(), run['head_sha']))
                try:
                    with conn:
                        ingest(conn, source, run)
                except Exception as exc:
                    with conn:
                        conn.execute('UPDATE performance_runs SET error_class=?,updated_at=? WHERE repository=? AND run_id=? AND attempt=?',
                            (type(exc).__name__, monitor.utc_now(), repository, run['id'], run['run_attempt']))
                    raise
                break  # One new run per source/tick; retry receipts without monopolizing triage.
        except Exception as exc:
            monitor.log(f'Benchmark intake deferred for {repository}: {type(exc).__name__}')
