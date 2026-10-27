#!/usr/bin/env python3
"""Read-only, stdlib health observations. Findings never authorize remediation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import quote

import automerge
import issue_fixer
import monitor
from secret_filter import diagnostic

BATCH_ACTIVE = {'launching', 'running', 'finishing'}
REPAIR_ACTIVE = {'selected', 'launching', 'running', 'finishing'}
TRIAGE_ACTIVE = {'queued', 'launching', 'running', 'publishing'}
UTC = timezone.utc


def safe(value):
    return ' '.join(str(diagnostic(str(value or ''))).split())[:200]


def normalized_cause(value):
    value = safe(value).lower()
    value = re.sub(r'\b\d{4}-\d\d-\d\d[t ]\d\d:\d\d:\d\d(?:\.\d+)?(?:z|[+-]\d\d:\d\d)?', '<time>', value)
    value = re.sub(r'(?:batch|tested_head|receipt)=[0-9a-f]{8,}|\b[0-9a-f]{8,}\b',
                   lambda m: m[0] if '=' in m[0] else '<id>', value)
    # Keep error/status codes (401/503 etc); replace retry counts and durations.
    value = re.sub(r'\b(attempts?|retries|retry|count|counter)\s*[=: #]*\d+\b', r'\1 <n>', value)
    value = re.sub(r'\b\d+\s+(attempts?|retries)\b', r'<n> \1', value)
    value = re.sub(r'\b\d+(?:\.\d+)?\s*(?:ms|s|m|h|seconds?|secs?|minutes?|mins?|hours?|hrs?)\b', '<duration>', value)
    return ' '.join(value.split())


def parsed(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def open_readonly(path):
    conn = sqlite3.connect(f'file:{quote(str(Path(path).absolute()), safe="/")}?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


class Probe:
    def __init__(self, db, *, now=None, sessions_json=None):
        self.db = Path(db)
        self.now = now or datetime.now(UTC)
        self.sessions_json = sessions_json
        self.findings = []
        self.errors = []
        self.conn = None
        self.sessions = None

    def add(self, kind, entity, cause, persist=0, summary=None, *, entity_is_hash=False):
        # Reviewed IDs can be 64 hex characters (outbox intent hashes); these
        # are identities, not the HMAC credential values the free-text scrub removes.
        entity = str(entity)
        if not (entity_is_hash and re.fullmatch(r'[0-9a-fA-F]{64}', entity)):
            entity = safe(entity)
        cause = safe(cause)
        digest = hashlib.sha256(normalized_cause(cause).encode()).hexdigest()[:12]
        self.findings.append(dict(kind=kind, entity=entity, cause=cause,
            fingerprint=f'{kind}:{entity}:{digest}', persist_seconds=persist,
            summary=safe(summary or f'{entity}: {cause}')))

    def check(self, name, fn):
        try:
            fn()
        except Exception as exc:
            self.errors.append(dict(check=name, error=f'{type(exc).__name__}: {safe(exc)}'))

    def rows(self, table, columns):
        return [dict(r) for r in self.conn.execute(f'SELECT {columns} FROM {table}')]

    def age(self, timestamp):
        return (self.now - parsed(timestamp)).total_seconds()

    def mj(self, args):
        binary = str(monitor.MJ_BIN)
        if not Path(binary).exists() and not os.environ.get('BIFROST_MJ_BIN'):
            binary = 'mj'
        result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=12, check=False)
        # Never echo stdout/stderr: either can contain connection metadata.
        if result.returncode:
            raise RuntimeError(f'mj command exited {result.returncode}')
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise ValueError('invalid mj JSON') from None
        if not isinstance(value, dict):
            raise ValueError('invalid mj response object')
        return value

    def daemon(self):
        try:
            self.mj(['api-info', '--json'])
        except Exception as exc:
            self.add('mj_daemon_unavailable', 'CI', f'{type(exc).__name__}: {safe(exc)}')

    def native(self):
        value = json.loads(Path(self.sessions_json).read_text()) if self.sessions_json else self.mj(
            ['sessions', '--workspace', 'CI', '--json'])
        if not isinstance(value, dict) or not isinstance(value.get('sessions'), list):
            raise ValueError('invalid sessions list')
        if any(not isinstance(s, dict) or not isinstance(s.get('id'), str) for s in value['sessions']):
            raise ValueError('invalid session identity')
        self.sessions = {s['id']: s for s in value['sessions']}

    def heartbeats(self):
        rows = {r['entry_point']: r for r in self.rows('entrypoint_heartbeats',
            'entry_point,last_finished_at,last_outcome,last_error')}
        for name in ('monitor', 'merger', 'triage', 'stabilize'):
            row = rows.get(name, {})
            if not row.get('last_finished_at') or self.age(row['last_finished_at']) > 900:
                self.add('heartbeat_stale', name, 'no finish within 15 minutes')
            if row.get('last_error') or row.get('last_outcome') in {'error', 'failed', 'failure', 'failing'}:
                self.add('heartbeat_failing', name, row.get('last_error') or row['last_outcome'])

    def batches(self):
        return self.rows('automerge_batches', 'batch_id,status,terminal_status,phase,session_id')

    @staticmethod
    def batch_live(row):
        return row['status'] in BATCH_ACTIVE and not row['terminal_status'] and row['phase'] != 'terminal'

    def session_work(self, table, active, key):
        columns = f'{key},status,session_id'
        if table == 'automerge_batches':
            columns += ',terminal_status,phase'
        for row in self.rows(table, columns):
            live = self.batch_live(row) if table == 'automerge_batches' else row['status'] in active
            if not live or not row['session_id']:
                continue  # Launch reservations need not yet have a native session.
            if self.sessions is None:
                continue  # An unavailable list is a check error, not proof of missing sessions.
            sid = row['session_id']
            session = self.sessions.get(sid)
            if session and session.get('has_error'):
                self.add('session_error', sid, 'native session has_error')
            if not session or session.get('state') != 'running' or session.get('lifecycle') != 'live':
                self.add('session_not_running', sid, 'native session missing or not running/live', 1800)

    def jobs(self, table, active, kind):
        for row in self.rows(table, 'id,status,last_error,recovery_json'):
            if row['status'] not in active:
                continue
            if row['last_error']:
                self.add(kind, row['id'], row['last_error'])
            recovery = json.loads(row['recovery_json'] or '{}')
            stage = recovery.get('stage')
            if (stage in {'blocked', 'failed'} or recovery.get('failed_step')
                    or recovery.get('failure_notified')):
                # These fields have a fixed vocabulary; never emit recovery prose.
                stage = stage if stage in {'blocked', 'failed', 'stop', 'clear', 'cleared', 'restart', 'running'} else 'unknown'
                self.add('recovery_blocked', f'{table}:{row["id"]}', f'recovery stage {stage}')

    def merge_blocked(self):
        live = {r['batch_id'] for r in self.batches() if self.batch_live(r)}
        for row in self.rows('automerge_blocked_notifications', 'batch_id,reason'):
            if row['batch_id'] in live:
                self.add('merge_blocked', row['batch_id'], row['reason'])

    def ci_blocked(self):
        columns = {r[1] for r in self.conn.execute('PRAGMA table_info(blocked_notifications)')}
        stamp = next((c for c in ('created_at', 'updated_at') if c in columns), None)
        for row in self.rows('blocked_notifications', 'workflow_run_id,reason' + (',' + stamp if stamp else '')):
            if (stamp and row[stamp] and self.age(row[stamp]) <= 86400) or (not stamp and row['workflow_run_id'] == -1):
                self.add('ci_blocked', row['workflow_run_id'], row['reason'])

    def outbox(self):
        for row in self.rows('automerge_github_outbox', 'intent_id,kind,attempts,delivered_at,cancelled_at,last_error'):
            if not row['delivered_at'] and not row['cancelled_at'] and row['attempts'] >= 3:
                self.add('outbox_failing', row['intent_id'], f'{row["kind"]}: {row["last_error"] or "delivery failed"}', entity_is_hash=True)

    def actions(self):
        for row in self.rows('automerge_supervisor_actions', 'action_id,status,attempts,lock_failures,last_error'):
            if row['status'] in {'pending', 'running'} and row['last_error'] and max(row['attempts'], row['lock_failures']) >= 3:
                self.add('supervisor_action_failing', row['action_id'], row['last_error'])

    def ledger_errors(self):
        for row in self.rows('known_failure_errors', 'reason,failure_count,last_error,updated_at'):
            if row['failure_count'] >= 3 and self.age(row['updated_at']) <= 3600:
                self.add('ledger_upkeep_failing', row['reason'], row['last_error'])

    def ledger_pending(self):
        if self.conn.execute("SELECT 1 FROM known_failure_state WHERE key='issue_pending_body' AND value IS NOT NULL").fetchone():
            self.add('ledger_view_pending', 'ledger', 'issue view publication pending', 1800)

    def performance(self):
        rows = self.rows('performance_runs', 'repository,run_id,attempt,status,error_class')
        done = {}
        for row in rows:
            if row['status'] == 'done':
                key = (row['repository'], row['run_id'])
                done[key] = max(done.get(key, -1), row['attempt'])
        for row in rows:
            if row['status'] != 'done' and (row['error_class'] or row['status'] in {'error', 'failed'}):
                if done.get((row['repository'], row['run_id']), -1) >= row['attempt']:
                    continue
                self.add('performance_intake_failing', f'{row["repository"]}:{row["run_id"]}:{row["attempt"]}',
                         row['error_class'] or row['status'])

    def suspension(self):
        for row in self.rows('automerge_batches', 'batch_id,status,terminal_status,phase,suspend_pending,suspend_verify_failures'):
            if not self.batch_live(row) and row['suspend_pending'] and row['suspend_verify_failures'] >= 3:
                # abort_reason explains why the batch ended, not why suspension fails.
                self.add('suspend_failing', row['batch_id'], 'terminal suspension verification failed',
                         summary=f'{row["batch_id"]}: terminal suspension unverified after '
                                 f'{row["suspend_verify_failures"]} attempts')

    def campaigns(self):
        rows = self.rows('stabilize_campaigns', 'id,status,pause_reason')
        # Legacy campaigns predate runtime.pause_kind. The reason prefix was the old contract.
        kinds = {}
        columns = {r[1] for r in self.conn.execute('PRAGMA table_info(stabilize_runtime)')}
        if 'pause_kind' in columns:
            kinds = {r['campaign_id']: r['pause_kind'] for r in self.rows('stabilize_runtime', 'campaign_id,pause_kind')}
        for row in rows:
            if row['status'] == 'paused' and kinds.get(row['id']) != 'escalated_only' and not (
                    not kinds.get(row['id']) and (row['pause_reason'] or '').startswith('Only escalated issues remain')):
                self.add('campaign_paused', row['id'], row['pause_reason'] or 'campaign paused')

    def disks(self):
        seen = set()
        # mj's default data root; no credentials or token files are read.
        data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
        paths = ['/', self.db.resolve(), Path(os.environ.get('MJ_DATA_DIR', data_home / 'mjolnir'))]
        for original in paths:
            path = Path(original)
            while not path.exists() and path != path.parent:
                path = path.parent
            device = path.stat().st_dev
            if device in seen:
                continue
            seen.add(device)
            stat = os.statvfs(path)
            used = stat.f_blocks - stat.f_bfree
            total = used + stat.f_bavail
            if total and used / total > .90:
                self.add('disk_high', f'device:{device}', 'filesystem usage exceeds 90 percent',
                         summary=f'{path}: {used / total:.1%} disk usage')

    def repeats(self, table, kind):
        counts = {}
        for row in self.rows(table, 'issue_number,created_at'):
            if self.age(row['created_at']) <= 21600:
                counts[row['issue_number']] = counts.get(row['issue_number'], 0) + 1
        for issue, count in sorted(counts.items()):
            if count > 3:
                self.add(kind, issue, 'more than three records in six hours',
                         summary=f'issue #{issue}: {count} records in six hours')

    def admission(self):
        live = self.rows('automerge_batches', 'batch_id,status,terminal_status,phase,lane,target_ref,predecessor_id,role_promoted,active_pull_requests_json,pull_requests_json,excluded_source_heads_json')
        live = [r for r in live if self.batch_live(r)]
        occupied, reserved, rejected = set(), set(), set()
        for row in live:
            sources = json.loads(row['active_pull_requests_json'] or row['pull_requests_json'])
            reserved.update(p['number'] for p in sources)
            if (row['target_ref'] or 'master') == 'master' and (not row['predecessor_id'] or row['role_promoted']):
                occupied.add(row['lane'] or 'ordinary')
        landed = set()
        for row in self.rows('automerge_batches', 'terminal_status,excluded_source_heads_json,active_pull_requests_json,pull_requests_json'):
            if row['terminal_status'] == 'merged':
                # The inventory can keep a landed PR's last open record after it
                # leaves GitHub's open list; a landed exact head is not queued work.
                for source in json.loads(row['active_pull_requests_json'] or row['pull_requests_json'] or '[]'):
                    landed.add((source.get('number'), source.get('head_sha')))
            for exclusion in json.loads(row['excluded_source_heads_json'] or '[]'):
                if exclusion.get('kind') == 'rejected':
                    rejected.add((exclusion['number'], exclusion.get('head_sha') or exclusion.get('head')))
        for row in self.rows('automerge_github_outbox', 'number,head_sha,kind,cancelled_at'):
            if not row['cancelled_at'] and row['kind'] in {'reject_head', 'draft_changed_head'}:
                rejected.add((row['number'], row['head_sha']))
        eligible = {'ordinary': [], 'immediate': []}
        for row in self.rows('automerge_pr_inventory', 'number,head_sha,data_json,blocked_reason'):
            item = json.loads(row['data_json'])
            labels = {str(label['name'] if isinstance(label, dict) else label).casefold()
                      for label in item.get('labels', [])}
            base = (item.get('base') or {}).get('ref') or item.get('baseRefName')
            if (str(item.get('state', '')).lower() != 'open' or base != 'master'
                    or item.get('draft', item.get('isDraft', False)) or row['blocked_reason']
                    or row['number'] in reserved or (row['number'], row['head_sha']) in rejected
                    or (row['number'], row['head_sha']) in landed
                    or labels & {'mergemarshall:dependency-blocked', 'mergemarshall:rejected', 'automerge-rejected'}
                    or labels & automerge.INTEGRATION_LABELS or automerge._is_integration_pull(item)):
                continue
            lane = 'immediate' if labels & automerge.IMMEDIATE_LABELS else 'ordinary'
            eligible[lane].append(row['number'])
        for lane, numbers in eligible.items():
            if numbers and lane not in occupied:
                self.add('merge_admission_stall', lane, 'lane free with eligible work', 600 if lane == 'immediate' else 900,
                         f'{lane} lane free; cached eligible PRs: {", ".join(map(str, sorted(numbers)[:10]))}')

    def fixer_admission(self):
        limit = issue_fixer.concurrency_limit()  # Reads env/config only; no writes or service calls.
        repairs = self.rows('issue_repairs', 'id,issue_number,status,created_at,json_extract(report_json,\'$.outcome\') AS outcome')
        active = {r['issue_number'] for r in repairs if r['status'] in REPAIR_ACTIVE}
        if sum(r['status'] in REPAIR_ACTIVE for r in repairs) >= limit:
            return
        latest = {}
        for row in sorted(repairs, key=lambda r: (r['created_at'], r['id'])):
            latest[row['issue_number']] = row['outcome']
        issues = set()
        for row in self.rows('known_failures', 'status,triage_outcome,first_seen_at,triage_issue_url,linked_issue_url,linked_pr_state'):
            url = row['triage_issue_url'] or row['linked_issue_url'] or ''
            match = re.search(r'/issues/(\d+)/?$', url)
            if not match or row['status'] != 'open' or row['triage_outcome'] == 'infrastructure':
                continue
            issue = int(match[1])
            if (issue not in active and latest.get(issue) not in {'submitted', 'deferred', 'resolved', 'infrastructure', 'claimed_elsewhere', 'escalated'}
                    and row['linked_pr_state'] != 'MERGED' and row['first_seen_at'] and self.age(row['first_seen_at']) > 7200):
                issues.add(issue)
        for issue in sorted(issues):
            self.add('fixer_admission_stall', issue, 'free fixer capacity with unhandled issue', 900)

    def merge_handoff(self):
        rows = self.rows('automerge_batches', 'batch_id,status,terminal_status,phase,ready_json,predecessor_id,role_promoted,github_write_retry_after')
        live_ids = {r['batch_id'] for r in rows if self.batch_live(r)}
        for row in rows:
            if not self.batch_live(row) or row['phase'] in {'aborting', 'resetting', 'restarting', 'waiting_ci', 'waiting_base_ci', 'direct_waiting_ci', 'direct_rejecting'}:
                continue
            if row['predecessor_id'] in live_ids and not row['role_promoted']:
                continue
            if row['github_write_retry_after'] and self.age(row['github_write_retry_after']) < 0:
                continue
            receipt = json.loads(row['ready_json'] or '{}')
            if not receipt:
                continue
            if (not isinstance(receipt, dict) or not isinstance(receipt.get('head'), str)
                    or not re.fullmatch(r'[0-9a-f]{40}', receipt['head'])
                    or not isinstance(receipt.get('id'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', receipt['id'])):
                raise ValueError('invalid readiness receipt identity')
            cause = (f'batch={row["batch_id"]} tested_head={receipt["head"]} '
                     f'phase={row["phase"]} receipt={receipt["id"]} readiness receipt unconsumed')
            self.add('merge_handoff_unconsumed', row['batch_id'], cause, 1800,
                     f'{row["batch_id"]}: readiness receipt in phase {row["phase"]}')

    def fixer_handoff(self):
        reminders = {r['job_id'] for r in self.rows('issue_repair_messages', 'job_id,stable_id')
                     if r['stable_id'].startswith('report-handoff:')}
        for row in self.rows('issue_repairs', 'id,status,report_json IS NOT NULL AS has_report,recovery_json'):
            if row['status'] not in REPAIR_ACTIVE:
                continue
            # finishing means the parsed report was durably accepted by collect_step.
            # Reminder receipts record parsed reports while native completion is pending.
            # Old reminders after a reset cannot establish a current report.
            recovery = json.loads(row['recovery_json'] or '{}')
            if (row['status'] == 'finishing' and row['has_report']) or (row['id'] in reminders and not recovery):
                self.add('fixer_handoff_unconsumed', row['id'], 'parsed report awaiting completion', 900)

    def run(self):
        self.check('mj_daemon', self.daemon)
        self.check('native_sessions', self.native)
        self.check('database', lambda: setattr(self, 'conn', open_readonly(self.db)))
        if self.conn is not None:
            try:
                checks = [('heartbeats', self.heartbeats), ('merge_blocked', self.merge_blocked),
                    ('ci_blocked', self.ci_blocked), ('outbox', self.outbox), ('supervisor_actions', self.actions),
                    ('ledger_errors', self.ledger_errors), ('ledger_pending', self.ledger_pending),
                    ('performance_intake', self.performance), ('suspension', self.suspension),
                    ('campaigns', self.campaigns), ('merger_admission', self.admission),
                    ('fixer_admission', self.fixer_admission), ('merge_handoff', self.merge_handoff),
                    ('fixer_handoff', self.fixer_handoff)]
                for table, active, key in [('automerge_batches', BATCH_ACTIVE, 'batch_id'),
                        ('issue_repairs', REPAIR_ACTIVE, 'id'), ('triage_jobs', TRIAGE_ACTIVE, 'id')]:
                    self.check(table + '_sessions', lambda t=table, a=active, k=key: self.session_work(t, a, k))
                for table, active, kind in [('issue_repairs', REPAIR_ACTIVE, 'repair_error'), ('triage_jobs', TRIAGE_ACTIVE, 'triage_error')]:
                    self.check(kind, lambda t=table, a=active, k=kind: self.jobs(t, a, k))
                for table, kind in [('issue_repairs', 'fixer_repeat'), ('issue_repair_deferrals', 'fixer_deferral_repeat')]:
                    self.check(kind, lambda t=table, k=kind: self.repeats(t, k))
                for name, fn in checks:
                    self.check(name, fn)
            finally:
                self.conn.close()
        self.check('disk', self.disks)
        findings = {r['fingerprint']: r for r in self.findings}
        return dict(format=1, probed_at=self.now.astimezone(UTC).isoformat(),
            findings=sorted(findings.values(), key=lambda r: (r['kind'], r['entity'], r['fingerprint'])),
            check_errors=sorted(self.errors, key=lambda r: (r['check'], r['error'])))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=monitor.DB_PATH)
    parser.add_argument('--sessions-json', type=Path)
    parser.add_argument('--now', type=parsed)
    args = parser.parse_args(argv)
    print(json.dumps(Probe(args.db, now=args.now, sessions_json=args.sessions_json).run(), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
