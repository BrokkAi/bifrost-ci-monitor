#!/usr/bin/python3
"""Durable, App-owned stabilization campaigns; Slack delivery is independent."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import re
import sqlite3
import time
import urllib.parse
import uuid

import git_ancestry
import monitor
import stabilize_state as state

LOCK_PATH = monitor.STATE_DIR.parent / 'bifrost-ci-stabilize' / 'stabilize.lock'
WORKFLOW_FILES = {'push': 'ci.yml', 'hourly': 'hourly.yml', 'nightly': 'nightly.yml'}
DISCOVERY_GRACE = 600
GATE_JOBS = {'gate', 'workflow gate', 'branch gate', 'check branch', 'check-branch',
             'pr verification'}


def ensure_schema(conn):
    state.ensure_schema(conn)
    monitor.ensure_known_failure_schema(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS stabilize_runtime (
        campaign_id TEXT PRIMARY KEY, seeded INTEGER NOT NULL DEFAULT 0,
        stage_rounds INTEGER NOT NULL DEFAULT 0, ref_attempt_at TEXT);
    CREATE TABLE IF NOT EXISTS stabilize_dispatches (
        campaign_id TEXT NOT NULL, stage TEXT NOT NULL, tip_sha TEXT NOT NULL,
        attempt INTEGER NOT NULL, intent_at TEXT NOT NULL, after_run_id INTEGER NOT NULL,
        reason TEXT NOT NULL, run_id INTEGER, error TEXT,
        PRIMARY KEY(campaign_id,stage,tip_sha,attempt));
    CREATE TABLE IF NOT EXISTS stabilize_runs (
        campaign_id TEXT NOT NULL, stage TEXT NOT NULL, run_id INTEGER NOT NULL,
        tip_sha TEXT NOT NULL, conclusion TEXT NOT NULL, green INTEGER NOT NULL,
        incomplete INTEGER NOT NULL, processed_at TEXT NOT NULL,
        PRIMARY KEY(campaign_id,run_id));
    CREATE TABLE IF NOT EXISTS stabilize_issue_identities (
        campaign_id TEXT NOT NULL, issue_number INTEGER NOT NULL,
        workflow TEXT NOT NULL, job_name TEXT NOT NULL, identity_kind TEXT NOT NULL,
        identity TEXT NOT NULL, evidence_json TEXT NOT NULL, absent_sha TEXT,
        PRIMARY KEY(campaign_id,issue_number,workflow,job_name,identity_kind,identity));
    CREATE TABLE IF NOT EXISTS stabilize_fix_commits (
        campaign_id TEXT NOT NULL, issue_number INTEGER NOT NULL, fix_sha TEXT NOT NULL,
        PRIMARY KEY(campaign_id,issue_number));
    CREATE TABLE IF NOT EXISTS stabilize_messages (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
        checkpoint TEXT NOT NULL, text TEXT NOT NULL, sent_at TEXT,
        UNIQUE(campaign_id,checkpoint));
    CREATE TABLE IF NOT EXISTS stabilize_writes (
        campaign_id TEXT NOT NULL, checkpoint TEXT NOT NULL, kind TEXT NOT NULL,
        payload TEXT NOT NULL, done_at TEXT, PRIMARY KEY(campaign_id,checkpoint));
    CREATE TABLE IF NOT EXISTS stabilize_gate_intents (
        campaign_id TEXT NOT NULL, stage TEXT NOT NULL, intent_at TEXT,
        PRIMARY KEY(campaign_id,stage));
    """)
    monitor.ensure_column(conn, 'stabilize_runtime', 'ref_error', 'TEXT')
    monitor.ensure_column(conn, 'stabilize_runtime', 'resume_count', 'INTEGER NOT NULL DEFAULT 0')
    monitor.ensure_column(conn, 'stabilize_dispatches', 'permission_denied', 'INTEGER NOT NULL DEFAULT 0')
    monitor.ensure_column(conn, 'stabilize_dispatches', 'cancelled_at', 'TEXT')
    monitor.ensure_column(conn, 'stabilize_writes', 'cancelled_at', 'TEXT')
    monitor.ensure_column(conn, 'stabilize_writes', 'cancel_reason', 'TEXT')
    monitor.ensure_column(conn, 'stabilize_runs', 'url', "TEXT NOT NULL DEFAULT ''")
    monitor.ensure_column(conn, 'stabilize_gate_intents', 'body', 'TEXT')
    monitor.ensure_column(conn, 'stabilize_gate_intents', 'snapshot_attempt', 'INTEGER NOT NULL DEFAULT 1')
    monitor.ensure_column(conn, 'stabilize_gate_intents', 'ref_attempt_at', 'TEXT')


class StabilizeError(Exception):
    """Base class for the web caller's campaign errors."""


class InvalidRef(StabilizeError, ValueError):
    """A commit-ish is unknown/unresolvable, or create received a non-full SHA."""


class CampaignNotFound(StabilizeError, LookupError):
    """The requested campaign does not exist."""


class CampaignAlreadyLive(StabilizeError):
    def __init__(self, campaign_id):
        self.campaign_id = campaign_id
        super().__init__(f'Campaign {campaign_id} already owns the live stabilize request')


class LockTimeout(StabilizeError):
    """The campaign lock remained busy beyond the bounded operator wait."""


# Compatibility for early in-process callers.
UnknownRef = InvalidRef
LiveCampaignError = CampaignAlreadyLive


def gh(args):
    return json.loads(monitor.run_gh(args))


def api(path, *args):
    return gh(['api', f'repos/{monitor.REPO_NAME}/{path}', *args])


def campaign(conn, campaign_id):
    row = conn.execute('SELECT * FROM stabilize_campaigns WHERE id=?', (campaign_id,)).fetchone()
    if row is None:
        raise CampaignNotFound(f'Unknown campaign {campaign_id}')
    return state.campaign_for_branch(conn, row['branch'])


def notify(conn, campaign_id, checkpoint, text):
    # Split before checkpointing so Slack's truncation never loses issue lists.
    limit = monitor.SLACK_MESSAGE_LIMIT - 100
    chunks = [text[i:i + limit] for i in range(0, len(text), limit)] or ['']
    with conn:
        for index, chunk in enumerate(chunks):
            conn.execute('INSERT OR IGNORE INTO stabilize_messages(campaign_id,checkpoint,text) '
                         'VALUES (?,?,?)', (campaign_id, f'{checkpoint}:{index}', chunk))


def queue_write(conn, campaign_id, checkpoint, kind, payload):
    with conn:
        conn.execute('INSERT OR IGNORE INTO stabilize_writes(campaign_id,checkpoint,kind,payload) '
                     'VALUES (?,?,?,?)', (campaign_id, checkpoint, kind, json.dumps(payload)))


def close_is_current(conn, campaign_id, payload):
    """Fence a close to the exact fix and immutable merged gate that authorized it."""
    required = ('gate_stage', 'gate_tip_sha', 'gate_pr_number', 'fix_sha', 'fix_pr_number', 'fix_head_sha')
    if any(key not in payload for key in required):
        return False  # Legacy unfenced closes cannot authorize an issue mutation.
    issue = conn.execute('SELECT * FROM stabilize_issues WHERE campaign_id=? AND issue_number=?',
                         (campaign_id, payload['number'])).fetchone()
    if not issue or issue['state'] != 'landed' or issue['pr_number'] != payload['fix_pr_number'] or issue['pr_head_sha'] != payload['fix_head_sha']:
        return False
    fix = conn.execute('SELECT fix_sha FROM stabilize_fix_commits WHERE campaign_id=? AND issue_number=?',
                       (campaign_id, payload['number'])).fetchone()
    gate = conn.execute('SELECT * FROM stabilize_gate_merges WHERE campaign_id=? AND stage=?',
                        (campaign_id, payload['gate_stage'])).fetchone()
    if (not fix or fix['fix_sha'] != payload['fix_sha'] or not gate or gate['state'] != 'merged' or
            gate['tip_sha'] != payload['gate_tip_sha'] or gate['pr_number'] != payload['gate_pr_number']):
        return False
    tip = gate['tip_sha']
    if not ancestor(fix['fix_sha'], tip):
        return False
    # Check both member evidence and the ledger: newly ingested recurrences may
    # precede refresh_issues, and triage may not have linked that observation yet.
    for identity in conn.execute('SELECT * FROM stabilize_issue_identities WHERE campaign_id=? AND issue_number=?',
                                 (campaign_id, payload['number'])):
        evidence = json.loads(identity['evidence_json'])
        shas = [evidence['last_seen_sha']]
        latest = conn.execute('SELECT last_seen_sha FROM stabilize_failures WHERE campaign_id=? AND workflow=? '
                              'AND job_name=? AND identity_kind=? AND identity=?',
                              (campaign_id, identity['workflow'], identity['job_name'], identity['identity_kind'], identity['identity'])).fetchone()
        if latest:
            shas.append(latest['last_seen_sha'])
        if any(sha != tip and ancestor(tip, sha) for sha in shas):
            return False
    for row in conn.execute('SELECT last_seen_sha FROM stabilize_failures WHERE campaign_id=? AND linked_issue_url=?',
                            (campaign_id, f'https://github.com/{monitor.REPO_NAME}/issues/{payload["number"]}')):
        if row['last_seen_sha'] != tip and ancestor(tip, row['last_seen_sha']):
            return False
    return True


def cancel_write(conn, row, reason):
    with conn:
        conn.execute('UPDATE stabilize_writes SET cancelled_at=?,cancel_reason=? WHERE campaign_id=? AND checkpoint=?',
                     (monitor.utc_now(), reason, row['campaign_id'], row['checkpoint']))


def deliver_writes(conn):
    for row in conn.execute('SELECT * FROM stabilize_writes WHERE done_at IS NULL AND cancelled_at IS NULL').fetchall():
        try:
            payload = json.loads(row['payload'])
            number = payload['number']
            if row['kind'] == 'label':
                label = payload['label']
                monitor.run_gh(['label', 'create', label, '--repo', monitor.REPO_NAME,
                                '--force', '--color', '5319e7', '--description', 'MergeMarshall campaign'])
                api(f"issues/{number}/labels", '--method', 'POST', '-f', f'labels[]={label}')
            elif row['kind'] == 'close':
                # PATCH state and body-independent comment publication reconcile separately.
                if not close_is_current(conn, row['campaign_id'], payload):
                    cancel_write(conn, row, 'Issue/fix/gate changed or a later recurrence superseded this close')
                    continue
                marker = (f"<!-- stabilize-close:{row['campaign_id']}:{number}:"
                          f"{payload['gate_stage']}:{payload['gate_pr_number']}:{payload['gate_tip_sha']} -->")
                comments = api(f'issues/{number}/comments?per_page=100', '--paginate', '--slurp')
                comments = [c for page in comments for c in page] if comments and isinstance(comments[0], list) else comments
                if not any(marker in str(c.get('body', '')) and
                           c.get('user', {}).get('login') == 'mergemarshall[bot]' for c in comments):
                    api(f'issues/{number}/comments', '--method', 'POST', '-f',
                        f"body={payload['comment']}\n{marker}")
                if not close_is_current(conn, row['campaign_id'], payload):
                    cancel_write(conn, row, 'Close superseded during comment reconciliation')
                    continue
                api(f'issues/{number}', '--method', 'PATCH', '-f', 'state=closed')
                with conn:
                    conn.execute("UPDATE stabilize_issues SET state='closed',updated_at=? "
                                 'WHERE campaign_id=? AND issue_number=?',
                                 (monitor.utc_now(), row['campaign_id'], number))
            else:
                raise ValueError('unknown campaign write')
            with conn:
                conn.execute('UPDATE stabilize_writes SET done_at=? WHERE campaign_id=? AND checkpoint=?',
                             (monitor.utc_now(), row['campaign_id'], row['checkpoint']))
        except Exception as exc:
            monitor.log(f"stabilize write {row['checkpoint']} deferred: {exc}")


def deliver_slack(conn, transport=None):
    try:
        transport = transport or monitor.load_slack_transport()
        if transport.kind != 'chat':
            return  # A webhook cannot establish the required campaign thread.
        for row in conn.execute('SELECT * FROM stabilize_campaigns ORDER BY created_at').fetchall():
            thread = row['thread_ts']
            if not thread:
                ok, thread = monitor.slack_send(transport,
                    f"{monitor.slack_project_prefix()} stabilize campaign: `{row['branch']}` "
                    f"at `{row['anchor_sha']}`")
                if not ok or not thread:
                    continue
                with conn:
                    conn.execute('UPDATE stabilize_campaigns SET thread_ts=? WHERE id=?', (thread, row['id']))
            for message in conn.execute('SELECT * FROM stabilize_messages WHERE campaign_id=? '
                    'AND sent_at IS NULL ORDER BY sequence', (row['id'],)).fetchall():
                ok, _ = monitor.slack_send(transport, message['text'], thread_ts=thread)
                if not ok:
                    break
                with conn:
                    conn.execute('UPDATE stabilize_messages SET sent_at=? WHERE sequence=?',
                                 (monitor.utc_now(), message['sequence']))
    except Exception as exc:
        monitor.log(f'stabilize Slack delivery deferred: {exc}')


def _epoch(value):
    parsed = monitor._failure_datetime(value)
    return parsed.timestamp() if parsed else None


def _expired(value):
    epoch = _epoch(value)
    return epoch is not None and dt.datetime.now(dt.timezone.utc).timestamp() - epoch >= DISCOVERY_GRACE


def ref_sha(branch):
    try:
        value = api('git/ref/heads/' + urllib.parse.quote(branch, safe='/'))
    except monitor.CommandError as exc:
        if re.search(r'\b404\b|Not Found', str(exc), re.I):
            return None
        raise
    sha = value.get('object', {}).get('sha')
    if not re.fullmatch('[0-9a-f]{40}', str(sha)):
        raise monitor.CommandError('Invalid branch ref response')
    return sha


def ensure_ref(branch, sha, *, create=True):
    existing = ref_sha(branch)
    if existing:
        if existing != sha:
            raise ValueError(f'Ref {branch} points at a different commit')
        return True
    if not create:
        return False
    try:
        api('git/refs', '--method', 'POST', '-f', f'ref=refs/heads/{branch}', '-f', f'sha={sha}')
    except monitor.CommandError:
        # A lost response is not evidence the write failed.
        if ref_sha(branch) != sha:
            return False
        return True
    return ref_sha(branch) == sha


def ancestor(a, b):
    if a == b:
        return True
    def fallback(base, head):
        result = api(f'compare/{base}...{head}')
        status = result.get('status')
        if status not in ('identical', 'ahead', 'behind', 'diverged'):
            raise monitor.CommandError('Incomplete commit comparison')
        return status in ('identical', 'ahead')
    return monitor.git_cache().ancestor(a, b, fallback)


def resolve_anchor(ref=None):
    if ref:
        result = api('commits/' + urllib.parse.quote(ref, safe=''))
        sha = result.get('sha')
    else:
        runs = monitor._known_failure_runs('CI', 'push', branch=monitor.BRANCH)
        runs = [r for r in runs if r.get('status') == 'completed' and
                r.get('conclusion') not in ('cancelled', 'canceled') and
                r.get('event') == 'push' and r.get('headBranch') == monitor.BRANCH]
        if not runs:
            raise InvalidRef('No completed non-cancelled master CI push run is available')
        runs.sort(key=lambda r: (r.get('createdAt', ''), r['databaseId']), reverse=True)
        sha = runs[0].get('headSha')
    if not re.fullmatch('[0-9a-f]{40}', str(sha)):
        raise InvalidRef('Anchor resolution did not return a full commit SHA')
    return sha


def start_campaign(conn, ref=None, requested_by=None):
    """Persist the exact creation intent before any branch write (safe for Slack callers)."""
    ensure_schema(conn)
    # Resolve first; serialize the live-row admission, including competing API callers.
    if state.live_campaigns(conn):
        raise ValueError('A live stabilize campaign already exists')
    sha = resolve_anchor(ref)
    campaign_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    branch = f'stabilize/{campaign_id}'
    now = monitor.utc_now()
    conn.execute('BEGIN IMMEDIATE')
    try:
        if state.live_campaigns(conn):
            raise ValueError('A live stabilize campaign already exists')
        conn.execute('INSERT INTO stabilize_campaigns(id,branch,requested_ref,requested_by,anchor_sha,'
                     'created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                     (campaign_id, branch, ref, requested_by, sha, now, now))
        conn.execute('INSERT INTO stabilize_runtime(campaign_id) VALUES (?)', (campaign_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    advance_start(conn, campaign_id)
    deliver_writes(conn)
    deliver_slack(conn)
    return campaign(conn, campaign_id)


def issue_number(url):
    match = re.search(r'/issues/(\d+)$', str(url or ''))
    return int(match[1]) if match else None


def workflow_stage(workflow):
    return next((s for s, w in state.STAGE_WORKFLOWS.items() if w == workflow), None)


def add_issue(conn, c, number, stage, source, rows):
    now = monitor.utc_now()
    with conn:
        added = conn.execute('INSERT OR IGNORE INTO stabilize_issues(campaign_id,issue_number,stage,source,'
            'round,added_at,updated_at) VALUES (?,?,?,?,?,?,?)',
            (c.id, number, stage, source, c.round, now, now)).rowcount
        # A cause seen in several suites holds the earliest applicable stage.
        old = conn.execute('SELECT stage FROM stabilize_issues WHERE campaign_id=? AND issue_number=?',
                           (c.id, number)).fetchone()['stage']
        if state.STAGES.index(stage) < state.STAGES.index(old):
            conn.execute('UPDATE stabilize_issues SET stage=?,updated_at=? WHERE campaign_id=? AND issue_number=?',
                         (stage, now, c.id, number))
        for row in rows:
            conn.execute('INSERT OR IGNORE INTO stabilize_issue_identities(campaign_id,issue_number,workflow,'
                'job_name,identity_kind,identity,evidence_json) VALUES (?,?,?,?,?,?,?)',
                (c.id, number, row['workflow'], row['job_name'], row['identity_kind'], row['identity'],
                 json.dumps(dict(row))))
    queue_write(conn, c.id, f'issue-label:{number}', 'label', {'number': number, 'label': state.LABEL})
    if added and source == 'observed':
        notify(conn, c.id, f'issue-added:{number}', f"Issue added: #{number} ({stage}, round {c.round}).")


def seed_issues(conn, c):
    deferred = False
    candidates = conn.execute("SELECT * FROM known_failures WHERE status='open' AND triage_issue_url IS NOT NULL "
                              "AND COALESCE(triage_outcome,'')<>'infrastructure'").fetchall()
    for row in candidates:
        stage = workflow_stage(row['workflow'])
        number = issue_number(row['triage_issue_url'])
        if not stage or not number:
            continue
        try:
            if not ancestor(row['first_seen_sha'], c.anchor_sha):
                continue
        except (git_ancestry.Deferred, OSError, RuntimeError, monitor.CommandError):
            deferred = True
            continue
        issue = gh(['issue', 'view', str(number), '--repo', monitor.REPO_NAME, '--json', 'state,labels'])
        labels = {l['name'].casefold() for l in issue.get('labels', [])}
        if issue.get('state') != 'OPEN' or 'buildfailure' not in labels:
            continue
        add_issue(conn, c, number, stage, 'seeded', [row])
    if deferred:
        return False
    lines = ['Seeded issues (red-since evidence):']
    for row in conn.execute('SELECT i.issue_number,i.stage,e.evidence_json FROM stabilize_issues i '
            'JOIN stabilize_issue_identities e ON e.campaign_id=i.campaign_id AND e.issue_number=i.issue_number '
            "WHERE i.campaign_id=? AND i.source='seeded' ORDER BY i.issue_number", (c.id,)):
        evidence = json.loads(row['evidence_json'])
        lines.append(f"- #{row['issue_number']} [{row['stage']}]: `{evidence['first_seen_sha']}` "
                     f"since {evidence['first_seen_at']} — {evidence['first_seen_run_url']}")
    notify(conn, c.id, 'seeded', '\n'.join(lines) if len(lines) > 1 else 'Seeded issues: none.')
    with conn:
        conn.execute('UPDATE stabilize_runtime SET seeded=1 WHERE campaign_id=?', (c.id,))
    return True


def advance_start(conn, campaign_id):
    c = campaign(conn, campaign_id)
    runtime = conn.execute('SELECT * FROM stabilize_runtime WHERE campaign_id=?', (c.id,)).fetchone()
    attempted = runtime['ref_attempt_at']
    with conn:
        if not attempted or _expired(attempted):
            conn.execute('UPDATE stabilize_runtime SET ref_attempt_at=? WHERE campaign_id=?',
                         (monitor.utc_now(), c.id))
    if not ensure_ref(c.branch, c.anchor_sha, create=not attempted or _expired(attempted)):
        return
    if not runtime['seeded'] and not seed_issues(conn, c):
        return
    with conn:
        conn.execute("UPDATE stabilize_campaigns SET status='running',tip_sha=anchor_sha,round=1,updated_at=? WHERE id=?",
                     (monitor.utc_now(), c.id))
        conn.execute('UPDATE stabilize_runtime SET stage_rounds=1,ref_error=NULL WHERE campaign_id=?', (c.id,))


def pause(conn, c, reason):
    with conn:
        conn.execute("UPDATE stabilize_campaigns SET status='paused',pause_reason=?,updated_at=? WHERE id=?",
                     (reason, monitor.utc_now(), c.id))
        notify(conn, c.id, 'paused:' + reason, f'Campaign paused: {reason}')


def branch_runs(c, stage):
    runs = gh(['run', 'list', '--repo', monitor.REPO_NAME, '--workflow', WORKFLOW_FILES[stage],
               '--branch', c.branch, '--event', 'workflow_dispatch', '--limit', '100', '--json',
               'databaseId,headSha,headBranch,event,status,conclusion,url,createdAt,workflowName'])
    if not isinstance(runs, list):
        raise monitor.CommandError('Invalid campaign run list')
    return [r for r in runs if r.get('headBranch') == c.branch and r.get('event') == 'workflow_dispatch']


def _job_name(key):
    return monitor.normalize_ci_job_name(key.split('/', 1)[-1])


def prove_identities(conn, c, stage, item, report):
    """Keep seed identities separate from branch observations; use identical retirement proofs."""
    success = {_job_name(j) for j in report.successful_jobs}
    incomplete = {_job_name(j) for j in report.incomplete_jobs}
    details = {_job_name(k): v for k, v in report.details.items()}
    steps = {_job_name(k): v for k, v in report.successful_steps.items()}
    for row in conn.execute('SELECT * FROM stabilize_issue_identities WHERE campaign_id=? AND workflow=?',
                           (c.id, state.STAGE_WORKFLOWS[stage])).fetchall():
        evidence = json.loads(row['evidence_json'])
        job = row['job_name']
        detail = details.get(job)
        observed = detail and (row['identity'] in detail.tests if row['identity_kind'] == 'test'
                              else row['identity'] in detail.failed_steps)
        if observed:
            evidence.update(last_seen_sha=item['headSha'], last_seen_run_id=item['databaseId'],
                            last_seen_failed_steps_json=json.dumps(sorted(detail.failed_steps)))
            conn.execute('UPDATE stabilize_issue_identities SET absent_sha=NULL,evidence_json=? '
                         'WHERE campaign_id=? AND issue_number=? AND workflow=? AND job_name=? AND identity_kind=? AND identity=?',
                         (json.dumps(evidence), c.id, row['issue_number'], row['workflow'], job,
                          row['identity_kind'], row['identity']))
        elif (job not in incomplete and item['headSha'] != evidence['last_seen_sha'] and
              (job in success or monitor._failure_absence_proves_recovery(evidence, detail,
                    steps.get(job, frozenset()), incomplete=job in incomplete))):
            conn.execute('UPDATE stabilize_issue_identities SET absent_sha=? WHERE campaign_id=? AND issue_number=? '
                         'AND workflow=? AND job_name=? AND identity_kind=? AND identity=?',
                         (item['headSha'], c.id, row['issue_number'], row['workflow'], job,
                          row['identity_kind'], row['identity']))


def ingest_run(conn, c, stage, item):
    if item.get('status') != 'completed' or conn.execute(
            'SELECT 1 FROM stabilize_runs WHERE campaign_id=? AND run_id=?', (c.id, item['databaseId'])).fetchone():
        return
    from automerge import collect_failure_report_for_run
    cancelled = item.get('conclusion') in ('cancelled', 'canceled')
    green = False
    incomplete = False
    with conn:
        if not cancelled:
            report = collect_failure_report_for_run(item['databaseId'])
            # A late historical run must not overwrite facts from a newer run.
            newer = conn.execute('SELECT 1 FROM stabilize_runs WHERE campaign_id=? AND stage=? AND run_id>?',
                                 (c.id, stage, item['databaseId'])).fetchone()
            if not newer:
                monitor._process_known_failure_run(conn, state.STAGE_WORKFLOWS[stage], item,
                                                  campaign_id=c.id, stage=stage, report=report)
            real_jobs = [j for j in (*report.failed_jobs, *report.successful_jobs)
                         if _job_name(j).casefold() not in GATE_JOBS]
            incomplete = bool(report.incomplete_jobs) or not real_jobs
            # The branch-gate/aggregate job alone is not validation of a suite.
            real_successes = [j for j in report.successful_jobs if _job_name(j).casefold() not in GATE_JOBS]
            green = item.get('conclusion') == 'success' and bool(real_successes) and not incomplete
            if not newer and item['headSha'] == c.tip_sha:
                prove_identities(conn, c, stage, item, report)
        conn.execute('INSERT INTO stabilize_runs(campaign_id,stage,run_id,tip_sha,conclusion,green,incomplete,processed_at) '
                     'VALUES (?,?,?,?,?,?,?,?)', (c.id, stage, item['databaseId'], item['headSha'],
                      item.get('conclusion') or '', int(green), int(incomplete), monitor.utc_now()))
        conn.execute('UPDATE stabilize_runs SET url=? WHERE campaign_id=? AND run_id=?',
                     (item.get('url') or '', c.id, item['databaseId']))
        if not cancelled:
            notify(conn, c.id, f"result:{item['databaseId']}",
                   f"Round {c.round}, {stage} at `{item['headSha'][:12]}`: "
                   f"{'green' if green else item.get('conclusion')} — {item.get('url', '')}")


def ensure_dispatch(conn, c, stage, runs):
    exact = [r for r in runs if r.get('headSha') == c.tip_sha]
    intent = conn.execute('SELECT * FROM stabilize_dispatches WHERE campaign_id=? AND stage=? AND tip_sha=? '
                          'AND cancelled_at IS NULL ORDER BY attempt DESC LIMIT 1', (c.id, stage, c.tip_sha)).fetchone()
    if intent and intent['run_id'] is None:
        discovered = [r for r in exact if r['databaseId'] > intent['after_run_id'] and
                      _epoch(r.get('createdAt')) is not None and
                      _epoch(r['createdAt']) >= _epoch(intent['intent_at'])]
        if not discovered:
            if _expired(intent['intent_at']):
                pause(conn, c, f'{stage} dispatch at {c.tip_sha[:12]} has no discovered run after 10 minutes; reconcile before retry')
            return
        run_id = min(discovered, key=lambda r: r['databaseId'])['databaseId']
        with conn:
            conn.execute('UPDATE stabilize_dispatches SET run_id=? WHERE campaign_id=? AND stage=? AND tip_sha=? AND attempt=?',
                         (run_id, c.id, stage, c.tip_sha, intent['attempt']))
        intent = dict(intent, run_id=run_id)
    latest = max(exact, key=lambda r: r['databaseId']) if exact else None
    reason = 'initial'
    if latest:
        if latest.get('status') != 'completed':
            return
        receipt = conn.execute('SELECT * FROM stabilize_runs WHERE campaign_id=? AND run_id=?',
                               (c.id, latest['databaseId'])).fetchone()
        if not receipt:
            return
        infra = receipt['incomplete'] or conn.execute("SELECT 1 FROM stabilize_failures WHERE campaign_id=? "
            "AND stage=? AND last_seen_run_id=? AND status='open' AND triage_outcome='infrastructure'",
            (c.id, stage, latest['databaseId'])).fetchone()
        if latest.get('conclusion') in ('cancelled', 'canceled'):
            reason = 'cancelled'
        elif infra:
            reason = 'infrastructure'
            retries = conn.execute("SELECT COUNT(*) FROM stabilize_dispatches WHERE campaign_id=? AND stage=? "
                "AND tip_sha=? AND reason='infrastructure' AND cancelled_at IS NULL AND permission_denied=0",
                (c.id, stage, c.tip_sha)).fetchone()[0]
            if retries >= 2:
                pause(conn, c, f'{stage} infrastructure/incomplete jobs persisted after two redispatches at {c.tip_sha[:12]}')
                return
        else:
            return
    # Freshly verify tip before the write; the durable intent owns all ambiguous retries.
    if ref_sha(c.branch) != c.tip_sha:
        return
    attempt = conn.execute('SELECT COALESCE(MAX(attempt),0)+1 FROM stabilize_dispatches WHERE campaign_id=? AND stage=? AND tip_sha=?',
                           (c.id, stage, c.tip_sha)).fetchone()[0]
    now = monitor.utc_now()
    after = max([r['databaseId'] for r in runs] or [0])
    with conn:
        conn.execute('INSERT INTO stabilize_dispatches(campaign_id,stage,tip_sha,attempt,intent_at,after_run_id,reason) '
                     'VALUES (?,?,?,?,?,?,?)', (c.id, stage, c.tip_sha, attempt, now, after, reason))
        notify(conn, c.id, f'dispatch:{stage}:{c.tip_sha}:{attempt}',
               f'Round {c.round}: dispatching {stage} at `{c.tip_sha[:12]}` ({reason}).')
    try:
        monitor.run_gh(['workflow', 'run', WORKFLOW_FILES[stage], '--repo', monitor.REPO_NAME, '--ref', c.branch])
    except monitor.CommandError as exc:
        with conn:
            denied = bool(re.search(r'\b403\b|resource not accessible by integration|actions[^\n]*write|insufficient.*permission', str(exc), re.I))
            conn.execute('UPDATE stabilize_dispatches SET error=?,permission_denied=? WHERE campaign_id=? AND stage=? AND tip_sha=? AND attempt=?',
                         (str(exc)[:1000], int(denied), c.id, stage, c.tip_sha, attempt))
            if denied:
                pause(conn, c, f'GitHub App lacks actions: write for {stage} workflow_dispatch (attempt {attempt}). '
                      f'Grant actions: write, then run stabilize.py resume {c.id}.')


def pull_requests(branch, status='all'):
    return gh(['pr', 'list', '--repo', monitor.REPO_NAME, '--base', branch, '--state', status,
               '--limit', '1000', '--json', 'number,title,state,isDraft,body,headRefOid,headRefName,baseRefName,mergeCommit,url,labels'])


def repair_issue_numbers(body):
    return {int(number) for match in re.findall(
        r'(?im)CI-Repair-Issue:\s*(\d+)\b|Fixes\s+#(\d+)\b', body or '')
        for number in match if number}


def repair_pr_ready(conn, pr):
    """Use the merger's head eligibility predicate, with read-only reconciliation."""
    from automerge import _queue_ready
    return _queue_ready(dict(pr), conn=conn, dry_run=True)


def refresh_issues(conn, c):
    for row in conn.execute("SELECT * FROM stabilize_failures WHERE campaign_id=? AND linked_issue_url IS NOT NULL "
                            "AND COALESCE(triage_outcome,'')<>'infrastructure'", (c.id,)).fetchall():
        number = issue_number(row['linked_issue_url'])
        if number:
            add_issue(conn, c, number, row['stage'], 'observed', [row])
    prs = pull_requests(c.branch)
    for row in conn.execute('SELECT * FROM stabilize_issues WHERE campaign_id=?', (c.id,)).fetchall():
        issue = gh(['issue', 'view', str(row['issue_number']), '--repo', monitor.REPO_NAME, '--json', 'state,labels'])
        labels = {l['name'].casefold() for l in issue.get('labels', [])}
        matching = [p for p in prs if p.get('baseRefName') == c.branch and
                    row['issue_number'] in repair_issue_numbers(p.get('body'))]
        merged = [p for p in matching if p.get('state') == 'MERGED']
        ready = [p for p in matching if repair_pr_ready(conn, p)]
        old = row['state']
        current_failures = conn.execute('SELECT evidence_json,absent_sha FROM stabilize_issue_identities WHERE campaign_id=? AND issue_number=?',
                                        (c.id, row['issue_number'])).fetchall()
        if issue.get('state') == 'OPEN' and (old == 'closed' or (old == 'dropped' and any(
                not e['absent_sha'] and json.loads(e['evidence_json'])['last_seen_sha'] == c.tip_sha for e in current_failures))):
            old = 'open'
        new = old
        pr = None
        if 'escalated' in labels:
            new = 'escalated'
        elif old == 'escalated':
            new = 'open'
        if new != 'escalated':
            identities = conn.execute('SELECT evidence_json,absent_sha FROM stabilize_issue_identities '
                                      'WHERE campaign_id=? AND issue_number=?', (c.id, row['issue_number'])).fetchall()
            if issue.get('state') == 'CLOSED' and not merged and old != 'landed':
                new = 'closed'
            elif merged and old not in ('closed', 'dropped'):
                pr = max(merged, key=lambda p: p['number'])
                fix_sha = (pr.get('mergeCommit') or {}).get('oid') or pr['headRefOid']
                if ancestor(fix_sha, c.tip_sha):
                    recurrence = any(not e['absent_sha'] and
                        json.loads(e['evidence_json'])['last_seen_sha'] != c.anchor_sha and
                        ancestor(fix_sha, json.loads(e['evidence_json'])['last_seen_sha']) for e in identities)
                    new = 'open' if recurrence else 'landed'
                    with conn:
                        conn.execute('INSERT INTO stabilize_fix_commits VALUES (?,?,?) ON CONFLICT(campaign_id,issue_number) '
                                     'DO UPDATE SET fix_sha=excluded.fix_sha', (c.id, row['issue_number'], fix_sha))
                    if recurrence:
                        pr = None
            elif old in ('open', 'pr_ready', 'escalated'):
                new = 'open'
            if new == 'open' and ready:
                pr = max(ready, key=lambda p: p['number'])
                new = 'pr_ready'
            # Landed issues retain their close-on-gate obligation. Unlanded
            # causes can be dropped only with proof for every recorded identity.
            if new in ('open', 'pr_ready') and identities and all(e['absent_sha'] == c.tip_sha for e in identities):
                new = 'dropped'
        if new == 'escalated' and current_failures and all(e['absent_sha'] == c.tip_sha for e in current_failures):
            new = 'dropped'
        with conn:
            conn.execute('UPDATE stabilize_issues SET state=?,pr_number=COALESCE(?,pr_number),'
                         'pr_head_sha=COALESCE(?,pr_head_sha),updated_at=? WHERE campaign_id=? AND issue_number=?',
                         (new, pr['number'] if pr else None, pr['headRefOid'] if pr else None,
                          monitor.utc_now(), c.id, row['issue_number']))
        if new != old:
            notify(conn, c.id, f'issue-state:{row["issue_number"]}:{c.round}:{new}',
                   f"Issue #{row['issue_number']}: {new}.")
    if state.merge_gate_open(conn, c.id):
        notify(conn, c.id, f'merge-gate:{c.round}:{c.stage}',
               f'Round {c.round}: fix merge gate open for {c.branch} ({c.stage}).')


def gate_body(conn, c, tip):
    lines = [f'Campaign `{c.branch}`: stages through {c.stage} passed at `{tip}`.', '',
             'Included campaign issues and fix PRs:']
    for row in conn.execute("SELECT * FROM stabilize_issues WHERE campaign_id=? AND state IN ('landed','closed')", (c.id,)):
        fix = conn.execute('SELECT fix_sha FROM stabilize_fix_commits WHERE campaign_id=? AND issue_number=?',
                           (c.id, row['issue_number'])).fetchone()
        if fix and ancestor(fix['fix_sha'], tip):
            lines.append(f"- Issue #{row['issue_number']}; fix PR #{row['pr_number']}.")
    return '\n'.join(lines)


def open_gate(conn, c):
    # Completed stages may wait for the previous immutable gate to land.
    if conn.execute("SELECT 1 FROM stabilize_gate_merges WHERE campaign_id=? AND state<>'merged' AND stage<>?",
                    (c.id, c.stage)).fetchone():
        return False
    gate = conn.execute('SELECT * FROM stabilize_gate_merges WHERE campaign_id=? AND stage=?', (c.id, c.stage)).fetchone()
    if gate is None:
        branch = f'{c.branch}-gate-{c.stage}'
        now = monitor.utc_now()
        with conn:
            conn.execute('INSERT INTO stabilize_gate_merges(campaign_id,stage,tip_sha,gate_branch,created_at,updated_at) '
                         'VALUES (?,?,?,?,?,?)', (c.id, c.stage, c.tip_sha, branch, now, now))
            conn.execute('INSERT INTO stabilize_gate_intents(campaign_id,stage) VALUES (?,?)', (c.id, c.stage))
        gate = conn.execute('SELECT * FROM stabilize_gate_merges WHERE campaign_id=? AND stage=?', (c.id, c.stage)).fetchone()
    notify(conn, c.id, f'gate-green:{gate["stage"]}:{gate["tip_sha"]}',
           f"{gate['stage']} gate green at `{gate['tip_sha'][:12]}`.")
    branch = gate['gate_branch']
    existing_ref = ref_sha(branch)
    if existing_ref and existing_ref != gate['tip_sha']:
        if gate['pr_number'] is not None:
            pause(conn, c, f"Immutable gate branch {branch} changed after publication")
            return False
        intent_row = conn.execute('SELECT snapshot_attempt FROM stabilize_gate_intents WHERE campaign_id=? AND stage=?',
                                  (c.id, c.stage)).fetchone()
        attempt = intent_row['snapshot_attempt'] + 1
        branch = f'{c.branch}-gate-{c.stage}-{attempt}'
        with conn:
            conn.execute('UPDATE stabilize_gate_merges SET gate_branch=?,updated_at=? WHERE campaign_id=? AND stage=?',
                         (branch, monitor.utc_now(), c.id, c.stage))
            conn.execute('UPDATE stabilize_gate_intents SET snapshot_attempt=?,ref_attempt_at=NULL WHERE campaign_id=? AND stage=?',
                         (attempt, c.id, c.stage))
    if not ensure_ref(branch, gate['tip_sha']):
        return False
    prs = gh(['pr', 'list', '--repo', monitor.REPO_NAME, '--head', branch, '--base', monitor.BRANCH,
              '--state', 'all', '--json', 'number,state,headRefOid,isDraft,baseRefName'])
    existing = [p for p in prs if p.get('headRefOid') == gate['tip_sha'] and p.get('baseRefName') == monitor.BRANCH]
    if existing:
        pr = existing[0]
        if pr.get('state') == 'CLOSED':
            pause(conn, c, f"Gate PR #{pr['number']} closed without merging")
            return False
        if pr.get('isDraft'):
            monitor.run_gh(['pr', 'ready', str(pr['number']), '--repo', monitor.REPO_NAME])
        number = pr['number']
    else:
        intent = conn.execute('SELECT intent_at FROM stabilize_gate_intents WHERE campaign_id=? AND stage=?',
                              (c.id, c.stage)).fetchone()['intent_at']
        if intent and not _expired(intent):
            return False
        cached_body = conn.execute('SELECT body FROM stabilize_gate_intents WHERE campaign_id=? AND stage=?', (c.id, c.stage)).fetchone()['body']
        body = cached_body or gate_body(conn, c, gate['tip_sha'])
        with conn:
            conn.execute('UPDATE stabilize_gate_intents SET intent_at=?,body=? WHERE campaign_id=? AND stage=?',
                         (monitor.utc_now(), body, c.id, c.stage))
        try:
            monitor.run_gh(['label', 'create', 'mergemarshall:high', '--repo', monitor.REPO_NAME, '--force', '--color', '5319e7'])
            monitor.run_gh(['pr', 'create', '--repo', monitor.REPO_NAME, '--head', branch, '--base', monitor.BRANCH,
                '--title', f'Stabilize {c.branch}: {c.stage} green at {gate["tip_sha"][:12]}',
                '--body', body, '--label', 'mergemarshall:high'])
        except monitor.CommandError:
            return False  # Next tick discovers accepted publication by persisted head.
        return False  # Always confirm the PR and exact head before advancing.
    with conn:
        conn.execute("UPDATE stabilize_gate_merges SET pr_number=?,state='open',updated_at=? WHERE campaign_id=? AND stage=?",
                     (number, monitor.utc_now(), c.id, c.stage))
    queue_write(conn, c.id, f'gate-label:{number}', 'label', {'number': number, 'label': 'mergemarshall:high'})
    notify(conn, c.id, f'gate-open:{number}', f'{c.stage} gate PR opened: https://github.com/{monitor.REPO_NAME}/pull/{number}')
    index = state.STAGES.index(c.stage)
    if index + 1 < len(state.STAGES):
        with conn:
            conn.execute('UPDATE stabilize_campaigns SET stage=?,updated_at=? WHERE id=?',
                         (state.STAGES[index + 1], monitor.utc_now(), c.id))
            conn.execute('UPDATE stabilize_runtime SET stage_rounds=1 WHERE campaign_id=?', (c.id,))
    return True


def observe_gates(conn, c):
    for gate in conn.execute("SELECT * FROM stabilize_gate_merges WHERE campaign_id=? AND pr_number IS NOT NULL "
                              "AND state<>'merged'", (c.id,)).fetchall():
        pr = gh(['pr', 'view', str(gate['pr_number']), '--repo', monitor.REPO_NAME, '--json', 'state,labels,headRefOid'])
        labels = {l['name'].casefold() for l in pr.get('labels', [])}
        if pr.get('headRefOid') and pr['headRefOid'] != gate['tip_sha']:
            pause(conn, c, f"Gate PR #{gate['pr_number']} no longer has its immutable tested head")
            return
        if labels & {'mergemarshall:rejected', 'automerge-rejected'} or pr.get('state') == 'CLOSED':
            pause(conn, c, f"Gate PR #{gate['pr_number']} rejected or closed; inspect the merger's rejection evidence")
            return
        if pr.get('state') != 'MERGED':
            continue
        for issue in conn.execute("SELECT * FROM stabilize_issues WHERE campaign_id=? AND state='landed'", (c.id,)):
            fix = conn.execute('SELECT fix_sha FROM stabilize_fix_commits WHERE campaign_id=? AND issue_number=?',
                               (c.id, issue['issue_number'])).fetchone()
            if fix and ancestor(fix['fix_sha'], gate['tip_sha']):
                queue_write(conn, c.id, f"close:{issue['issue_number']}:{gate['pr_number']}:{gate['tip_sha']}", 'close',
                    {'number': issue['issue_number'], 'gate_stage': gate['stage'], 'gate_tip_sha': gate['tip_sha'],
                     'gate_pr_number': gate['pr_number'], 'fix_sha': fix['fix_sha'],
                     'fix_pr_number': issue['pr_number'], 'fix_head_sha': issue['pr_head_sha'],
                     'comment': f"Fixed by campaign {c.branch}; "
                     f"{gate['stage']} gate PR #{gate['pr_number']} landed `{gate['tip_sha']}` in master."})
        with conn:
            conn.execute("UPDATE stabilize_gate_merges SET state='merged',updated_at=? WHERE campaign_id=? AND stage=?",
                         (monitor.utc_now(), c.id, gate['stage']))
            notify(conn, c.id, f"gate-merged:{gate['pr_number']}", f"{gate['stage']} gate PR #{gate['pr_number']} merged.")
        if gate['stage'] == 'nightly':
            with conn:
                conn.execute("UPDATE stabilize_campaigns SET status='completed',updated_at=?,finished_at=? WHERE id=?",
                             (monitor.utc_now(), monitor.utc_now(), c.id))
            completion_notice(conn, c)


def completion_notice(conn, c):
    """Terminal reporting retries independently of the completed campaign."""
    if conn.execute("SELECT 1 FROM stabilize_messages WHERE campaign_id=? AND checkpoint='completed:0'", (c.id,)).fetchone():
        return
    remaining = pull_requests(c.branch, 'open')
    text = 'Campaign complete: nightly gate merged. Remaining open branch PRs: '
    text += ', '.join(p.get('url') or f"#{p['number']}" for p in remaining) or 'none'
    notify(conn, c.id, 'completed', text)


def tick_campaign(conn, campaign_id):
    c = campaign(conn, campaign_id)
    if c.status == 'starting':
        advance_start(conn, c.id)
        c = campaign(conn, c.id)
    if c.status not in ('running', 'paused'):
        return
    if conn.execute("SELECT 1 FROM stabilize_gate_merges WHERE campaign_id=? AND stage='nightly' AND state='merged'",
                    (c.id,)).fetchone():
        # Recover a crash between the durable gate result and terminal campaign update.
        with conn:
            conn.execute("UPDATE stabilize_campaigns SET status='completed',updated_at=?,finished_at=COALESCE(finished_at,?) WHERE id=?",
                         (monitor.utc_now(), monitor.utc_now(), c.id))
        completion_notice(conn, c)
        return
    observe_gates(conn, c)
    c = campaign(conn, c.id)
    if c.status != 'running':
        return
    runtime = conn.execute('SELECT seeded FROM stabilize_runtime WHERE campaign_id=?', (c.id,)).fetchone()
    if not runtime['seeded'] and not seed_issues(conn, c):
        return
    tip = ref_sha(c.branch)
    if not tip:
        pause(conn, c, 'Campaign branch is missing')
        return
    if tip != c.tip_sha:
        with conn:
            conn.execute('UPDATE stabilize_campaigns SET tip_sha=?,round=round+1,updated_at=? WHERE id=?',
                         (tip, monitor.utc_now(), c.id))
            conn.execute('UPDATE stabilize_runtime SET stage_rounds=stage_rounds+1 WHERE campaign_id=?', (c.id,))
        c = campaign(conn, c.id)
        rounds = conn.execute('SELECT stage_rounds FROM stabilize_runtime WHERE campaign_id=?', (c.id,)).fetchone()[0]
        if rounds > 8:
            pause(conn, c, f'Eight rounds exhausted within {c.stage}')
            return
    required = state.STAGES[:state.STAGES.index(c.stage) + 1]
    settled = True
    for stage in required:
        runs = branch_runs(c, stage)
        for item in sorted(runs, key=lambda r: (r.get('createdAt', ''), r['databaseId'])):
            ingest_run(conn, c, stage, item)
        ensure_dispatch(conn, c, stage, runs)
        latest = max((r for r in runs if r.get('headSha') == c.tip_sha), key=lambda r: r['databaseId'], default=None)
        intent = conn.execute('SELECT run_id FROM stabilize_dispatches WHERE campaign_id=? AND stage=? AND tip_sha=? AND cancelled_at IS NULL ORDER BY attempt DESC LIMIT 1',
                              (c.id, stage, c.tip_sha)).fetchone()
        settled = settled and bool(latest and latest.get('status') == 'completed' and (not intent or intent['run_id'] is not None))
        if campaign(conn, c.id).status != 'running':
            return
    refresh_issues(conn, c)
    all_green = all(conn.execute('SELECT green FROM stabilize_runs WHERE campaign_id=? AND stage=? AND tip_sha=? '
                    'ORDER BY run_id DESC LIMIT 1', (c.id, stage, c.tip_sha)).fetchone() and
                    conn.execute('SELECT green FROM stabilize_runs WHERE campaign_id=? AND stage=? AND tip_sha=? '
                    'ORDER BY run_id DESC LIMIT 1', (c.id, stage, c.tip_sha)).fetchone()['green'] for stage in required)
    if settled and all_green:
        open_gate(conn, c)
    else:
        blockers = conn.execute('SELECT state FROM stabilize_issues WHERE campaign_id=? AND stage IN (' +
            ','.join('?' * len(required)) + ") AND state IN ('open','pr_ready','escalated')", (c.id, *required)).fetchall()
        if settled and blockers and all(r['state'] == 'escalated' for r in blockers):
            pause(conn, c, f'Only escalated issues remain in {c.stage} after available fixes landed')


def once(conn, transport=None):
    ensure_schema(conn)
    for c in state.live_campaigns(conn):
        try:
            tick_campaign(conn, c.id)
        except Exception as exc:
            monitor.log(f'stabilize campaign {c.id} deferred: {exc}')
    deliver_writes(conn)
    for row in conn.execute("SELECT id FROM stabilize_campaigns WHERE status='completed'").fetchall():
        try:
            completion_notice(conn, campaign(conn, row['id']))
        except Exception as exc:
            monitor.log(f'Campaign completion notice deferred: {exc}')
    deliver_slack(conn, transport)


def abort_campaign(conn, campaign_id):
    c = campaign(conn, campaign_id)
    if c.status == 'aborted':
        return
    if not c.live:
        raise ValueError('Only live campaigns can be aborted')
    with conn:
        conn.execute("UPDATE stabilize_campaigns SET status='aborted',updated_at=?,finished_at=? WHERE id=?", (monitor.utc_now(), monitor.utc_now(), c.id))
        notify(conn, c.id, 'aborted', f'Campaign aborted: {c.branch}. Branches and PRs retained.')


def resume_campaign(conn, campaign_id, requested_by):
    """Resume a paused campaign; only definitely denied dispatches are released.

    After an administrator grants the App actions: write, run
    `python3 stabilize.py resume CAMPAIGN_ID --requested-by OPERATOR`.
    Ambiguous dispatch intents retain their discovery fence and are never replayed.
    """
    c = campaign(conn, campaign_id)
    if c.status in ('running', 'starting'):
        return
    if c.status != 'paused':
        raise StabilizeError('Only paused campaigns can be resumed')
    with conn:
        conn.execute('UPDATE stabilize_dispatches SET cancelled_at=? WHERE campaign_id=? '
                     'AND permission_denied=1 AND run_id IS NULL AND cancelled_at IS NULL', (monitor.utc_now(), c.id))
        conn.execute("UPDATE stabilize_campaigns SET status=?,pause_reason=NULL,updated_at=? WHERE id=?",
                     ('running' if c.tip_sha else 'starting', monitor.utc_now(), c.id))
        conn.execute('UPDATE stabilize_runtime SET resume_count=resume_count+1 WHERE campaign_id=?', (c.id,))
        count = conn.execute('SELECT resume_count FROM stabilize_runtime WHERE campaign_id=?', (c.id,)).fetchone()[0]
        notify(conn, c.id, f'resumed:{count}', f'Campaign resumed by {requested_by}: {c.branch}.')


@contextmanager
def lock(wait_seconds=0):
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open('a') as handle:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    if wait_seconds:
                        raise LockTimeout('Stabilize lock remained busy for 120 seconds')
                    yield False
                    return
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_status():
    # No schema setup, App token, cache fetch or writes for read-only CLI commands.
    if not monitor.DB_PATH.exists():
        return []
    with sqlite3.connect(monitor.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='stabilize_campaigns'").fetchone():
            return []
        return [dict(r) for r in conn.execute('SELECT * FROM stabilize_campaigns ORDER BY created_at')]


def resolve(commitish=None):
    """Read-only preview for the web UI: REST reads and SQLite reads only."""
    if commitish is not None and (not isinstance(commitish, str) or not commitish.strip()):
        raise InvalidRef("A nonempty commit reference is required")
    try:
        sha = resolve_anchor(commitish)
        commit = api('commits/' + sha)
    except monitor.CommandError as exc:
        if re.search(r'\b404\b|Not Found|No commit found', str(exc), re.I):
            raise UnknownRef(f'Unknown commit reference: {commitish}') from exc
        raise
    master = api('commits/' + urllib.parse.quote(monitor.BRANCH, safe=''))['sha']
    if sha == master:
        on_master = True
    else:
        comparison = api(f'compare/{sha}...{master}')
        if comparison.get('status') not in ('ahead', 'identical', 'behind', 'diverged'):
            raise monitor.CommandError('Incomplete master ancestry comparison')
        on_master = comparison['status'] in ('ahead', 'identical')
    runs = gh(['run', 'list', '--repo', monitor.REPO_NAME, '--workflow', 'CI', '--event', 'push',
               '--branch', monitor.BRANCH, '--commit', sha, '--status', 'completed', '--limit', '100',
               '--json', 'databaseId,headSha,headBranch,event,status,conclusion,url,createdAt'])
    eligible = [r for r in runs if r.get('headSha') == sha and r.get('event') == 'push' and
                r.get('headBranch') == monitor.BRANCH and r.get('status') == 'completed' and
                r.get('conclusion') not in ('cancelled', 'canceled')]
    newest = max(eligible, key=lambda r: r['databaseId']) if eligible else None
    live = next((r['id'] for r in read_status() if r['status'] in state.LIVE_STATUSES), None)
    return {'sha': sha, 'subject': commit.get('commit', {}).get('message', '').split('\n')[0],
            'committed_at': commit.get('commit', {}).get('committer', {}).get('date'),
            'on_master': on_master, 'push_ci': {'run_id': newest['databaseId'], 'url': newest.get('url'),
                'conclusion': newest.get('conclusion')} if newest else None, 'live_campaign_id': live}


def _view(conn, row):
    result = dict(row)
    result['branch_url'] = f"https://github.com/{monitor.REPO_NAME}/tree/{urllib.parse.quote(row['branch'], safe='/')}"
    result['slack_thread_url'] = None
    if row['thread_ts'] and monitor.CHANNEL_PATH.exists():
        channel = monitor.CHANNEL_PATH.read_text().strip()
        if re.fullmatch(r'[A-Z0-9]+', channel):
            result['slack_thread_url'] = f"https://app.slack.com/archives/{channel}/p{row['thread_ts'].replace('.', '')}"
    result['issues'] = [dict(r) for r in conn.execute('SELECT issue_number AS number,stage,source,state,pr_number '
                                                   'FROM stabilize_issues WHERE campaign_id=? ORDER BY issue_number', (row['id'],))]
    result['gate_merges'] = [dict(r) for r in conn.execute('SELECT stage,tip_sha,gate_branch,pr_number,state '
                                                        'FROM stabilize_gate_merges WHERE campaign_id=?', (row['id'],))]
    result['latest_runs'] = {}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'stabilize_runs' in tables:
        for stage in state.STAGES:
            run = conn.execute('SELECT * FROM stabilize_runs WHERE campaign_id=? AND stage=? AND tip_sha=? '
                               'ORDER BY run_id DESC LIMIT 1', (row['id'], stage, row['tip_sha'])).fetchone()
            result['latest_runs'][stage] = {'sha': run['tip_sha'], 'url': run['url'] if 'url' in run.keys() else None,
                                          'conclusion': run['conclusion']} if run else None
    if 'stabilize_runtime' in tables:
        runtime = conn.execute('SELECT * FROM stabilize_runtime WHERE campaign_id=?', (row['id'],)).fetchone()
        result['start_error'] = runtime['ref_error'] if runtime and 'ref_error' in runtime.keys() else None
    return result


def list_campaigns(limit=20):
    if not monitor.DB_PATH.exists():
        return []
    with sqlite3.connect(monitor.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='stabilize_campaigns'").fetchone():
            return []
        return [_view(conn, r) for r in conn.execute('SELECT * FROM stabilize_campaigns ORDER BY created_at DESC,id DESC LIMIT ?',
                                                   (max(1, min(int(limit), 1000)),))]


def get(campaign_id):
    if not monitor.DB_PATH.exists():
        return None
    with sqlite3.connect(monitor.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='stabilize_campaigns'").fetchone():
            return None
        row = conn.execute('SELECT * FROM stabilize_campaigns WHERE id=?', (campaign_id,)).fetchone()
        return _view(conn, row) if row else None


def create(sha, requested_by, idempotency_key):
    if not re.fullmatch(r'[0-9a-f]{40}', str(sha)):
        raise InvalidRef('create requires a full resolved commit SHA')
    if not idempotency_key or not requested_by:
        raise ValueError('requested_by and idempotency_key are required')
    with lock(wait_seconds=120):
        conn = monitor.connect_db()
        try:
            ensure_schema(conn)
            existing = conn.execute('SELECT * FROM stabilize_campaigns WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if existing:
                return _view(conn, existing)
            conn.execute('BEGIN IMMEDIATE')
            try:
                live = state.live_campaigns(conn)
                if live:
                    raise CampaignAlreadyLive(live[0].id)
                campaign_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
                branch = f'stabilize/{campaign_id}'
                now = monitor.utc_now()
                conn.execute('INSERT INTO stabilize_campaigns(id,branch,requested_by,idempotency_key,anchor_sha,created_at,updated_at) '
                             'VALUES (?,?,?,?,?,?,?)', (campaign_id, branch, requested_by, idempotency_key, sha, now, now))
                conn.execute('INSERT INTO stabilize_runtime(campaign_id,ref_attempt_at) VALUES (?,?)', (campaign_id, now))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            try:
                if ensure_ref(branch, sha):
                    with conn:
                        conn.execute("UPDATE stabilize_campaigns SET status='running',tip_sha=anchor_sha,round=1,updated_at=? WHERE id=?",
                                     (monitor.utc_now(), campaign_id))
                        conn.execute('UPDATE stabilize_runtime SET stage_rounds=1 WHERE campaign_id=?', (campaign_id,))
                else:
                    raise monitor.CommandError('Branch creation is unconfirmed; the tick will reconcile its saved intent')
            except Exception as exc:
                with conn:
                    conn.execute('UPDATE stabilize_runtime SET ref_error=? WHERE campaign_id=?', (str(exc)[:1000], campaign_id))
            return _view(conn, conn.execute('SELECT * FROM stabilize_campaigns WHERE id=?', (campaign_id,)).fetchone())
        finally:
            conn.close()


def abort(campaign_id, requested_by):
    with lock(wait_seconds=120):
        conn = monitor.connect_db()
        try:
            ensure_schema(conn)
            abort_campaign(conn, campaign_id)
            notify(conn, campaign_id, 'abort-requester', f'Abort requested by {requested_by}.')
            deliver_slack(conn)
            return _view(conn, conn.execute('SELECT * FROM stabilize_campaigns WHERE id=?', (campaign_id,)).fetchone())
        finally:
            conn.close()


def resume(campaign_id, requested_by='operator'):
    """JSON API for the documented permission-recovery path; waits up to 120s."""
    with lock(wait_seconds=120):
        conn = monitor.connect_db()
        try:
            ensure_schema(conn)
            resume_campaign(conn, campaign_id, requested_by)
            deliver_slack(conn)
            return _view(conn, conn.execute('SELECT * FROM stabilize_campaigns WHERE id=?', (campaign_id,)).fetchone())
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('once')
    commands.add_parser('check')
    status = commands.add_parser('status')
    status.add_argument('--json', action='store_true')
    start = commands.add_parser('start')
    start.add_argument('--ref')
    start.add_argument('--requested-by')
    abort = commands.add_parser('abort')
    abort.add_argument('campaign_id')
    resume_command = commands.add_parser('resume', help='Resume after granting the App actions: write; ambiguous dispatches remain fenced')
    resume_command.add_argument('campaign_id')
    resume_command.add_argument('--requested-by', default='CLI operator')
    args = parser.parse_args()
    if args.command in ('check', 'status'):
        rows = list_campaigns()
        if getattr(args, 'json', False):
            print(json.dumps(rows))
        else:
            for row in rows:
                print(f"{row['id']} {row['status']} {row['stage']} round {row['round']} {row['branch']} "
                      f"{row['tip_sha'] or row['anchor_sha']} {row['pause_reason'] or ''}")
            if not rows:
                print('No stabilize campaigns.')
        return 0
    if args.command == 'start':
        anchor = resolve(args.ref)
        print(create(anchor['sha'], args.requested_by or 'CLI operator', uuid.uuid4().hex)['id'])
    elif args.command == 'abort':
        print(abort(args.campaign_id, 'CLI operator')['id'])
    elif args.command == 'resume':
        print(resume(args.campaign_id, args.requested_by)['id'])
    else:
        with lock() as acquired:
            if not acquired:
                monitor.record_heartbeat(None, 'stabilize', 'skipped')
                monitor.log('Another stabilize command owns the lock; skipping')
                return 0
            with monitor.heartbeat_tick('stabilize'):
                conn = monitor.connect_db()
                try:
                    once(conn)
                finally:
                    conn.close()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
