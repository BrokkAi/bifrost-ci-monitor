"""Dashboard projections over the supervisor's durable, cached state."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
import os
import statistics
import httpx
import automerge
import issue_fixer
import mm_service
import monitor
import speculation
import supervisor
from mm_web import db, models as m, projections, pagination
from mm_web.outcomes import Outcome, category
from mm_web.errors import ApiError
from mm_web.security import scrub

LIVE = {'launching', 'running', 'finishing'}
LIVE_WHERE = "status IN ('launching','running','finishing')"
REPAIR_ACTIVE = "(status IN ('selected','launching','running','finishing') OR (status='completed' AND cleanup_done=0))"
DETAIL_LIMIT = 200
ACTIVE_LIMIT = 500
BATCH_COLUMNS = '''batch_id,kind,source,status,phase,terminal_status,base_sha,ci_mode,
    created_at,finished_at,target_ref,pull_requests_json,active_pull_requests_json,excluded_source_heads_json,
    integration_pr_number,ci_head_sha,session_id,predecessor_id,role_promoted,attempt_generation,
    abort_reason,agent_configuration'''


def batch_columns(conn):
    if not db.has_table(conn, 'automerge_batches'):
        return BATCH_COLUMNS
    present = {column[1] for column in conn.execute('PRAGMA table_info(automerge_batches)')}
    return BATCH_COLUMNS if 'target_ref' in present else BATCH_COLUMNS.replace('target_ref,', 'NULL AS target_ref,')


def master_lane(conn):
    """SQL condition and parameters selecting batches that land on master, not a release branch."""
    if not db.has_table(conn, 'automerge_batches'):
        return '1', ()
    present = {column[1] for column in conn.execute('PRAGMA table_info(automerge_batches)')}
    if 'target_ref' not in present:
        return '1', ()
    return 'COALESCE(target_ref, ?) = ?', (automerge.BASE_BRANCH, automerge.BASE_BRANCH)


def aggregate(conn, table, expression, where='1', params=()):
    db.require(conn)
    if not db.has_table(conn, table):
        return 0
    return conn.execute(f'SELECT {expression} FROM "{table}" WHERE {where} LIMIT 1', params).fetchone()[0] or 0


def page_rows(conn, table, order, limit, before=None, where='1', params=(), columns='*'):
    following, cursor_params = pagination.after(before, order)
    fetched = db.rows(conn, table, where=f'({where}) AND ({following})', params=(*params, *cursor_params),
                      order=pagination.sql_order(order), limit=limit + 1, columns=columns)
    visible = fetched[:limit]
    cursor = pagination.encode(visible[-1], order) if len(fetched) > limit else None
    return visible, cursor


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    if value is None or value == '' or value == 0:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def parsed(value):
    return datetime.fromisoformat(timestamp(value))


def normalize(row):
    value = scrub(row)
    for name, item in list(value.items()):
        if name.endswith('_at') and item is not None:
            value[name] = timestamp(item)
    return value


def pull(value):
    number = value['number']
    return m.PullRef(number=number, url=f'https://github.com/{monitor.REPO_NAME}/pull/{number}',
                     title=scrub(value.get('title', '')), head_sha=value.get('head_sha'))


def summary(row):
    started = parsed(row['created_at'])
    finished = parsed(row['finished_at']) if row.get('finished_at') else now()
    sources = [pull(p.as_json()) for p in automerge.row_pulls(row)]
    excluded = [m.Exclusion(**pull(p).model_dump(), kind=p['kind'])
                for p in automerge._excluded_source_heads(row)]
    integration = row.get('integration_pr_number')
    return m.BatchSummary(batch_id=row['batch_id'], kind=row['kind'],
        lane=row.get('source') if row.get('source') in {'queue', 'priority', 'operator'} else 'queue',
        role='speculative' if speculation.is_speculative(row) else 'primary',
        status=row['status'], phase=row['phase'], terminal_status=row.get('terminal_status'), outcome=category(row),
        ci_mode=row['ci_mode'], target_ref=automerge.batch_target(row), created_at=timestamp(row['created_at']), finished_at=timestamp(row.get('finished_at')),
        duration_seconds=max(0, (finished - started).total_seconds()), sources=sources, excluded=excluded,
        integration_pr=pull({'number': integration, 'head_sha': row.get('ci_head_sha')}) if integration else None,
        session_id=row.get('session_id'), predecessor_id=row.get('predecessor_id'),
        attempt_generation=row.get('attempt_generation', 0), abort_reason=scrub(row.get('abort_reason')),
        model_configuration=row.get('agent_configuration', 'multi-pr'))


def batches(conn, state='all', kind=None, limit=50, before=None):
    predicates, params = [], []
    if state == 'active':
        predicates.append("status IN ('launching','running','finishing')")
    elif state == 'recent':
        predicates.append("status NOT IN ('launching','running','finishing')")
    if kind:
        predicates.append('kind=?')
        params.append(kind)
    rows, cursor = page_rows(conn, 'automerge_batches', pagination.BATCH, limit, before,
        where=' AND '.join(predicates) or '1', params=tuple(params), columns=batch_columns(conn))
    return m.BatchesPage(items=[summary(r) for r in rows], next_cursor=cursor)


def outbox(conn, pending=True, limit=50, before=None, batch_id=None):
    predicates, params = [], []
    if pending:
        predicates.append('delivered_at IS NULL AND cancelled_at IS NULL')
    if batch_id:
        predicates.append('batch_id=?')
        params.append(batch_id)
    rows, cursor = page_rows(conn, 'automerge_github_outbox', pagination.OUTBOX, limit, before,
        where=' AND '.join(predicates) or '1', params=tuple(params))
    return m.OutboxPage(items=[m.OutboxIntent.model_validate({**normalize(r), 'head_sha': r.get('head_sha') or None})
                              for r in rows], next_cursor=cursor)


def detail(conn, batch_id):
    values = db.rows(conn, 'automerge_batches', where='batch_id=?', params=(batch_id,))
    if not values:
        raise ApiError(404, 'batch_not_found', 'Batch was not found')
    row = values[0]
    # state(), candidate(), source_revision() and parent_current() only read SQL
    # and pure row data. No dispatch/checked/mutation helper is called here.
    state = mm_service.state(conn, batch_id) if row['kind'] == 'batch' else {
        'batch_id': batch_id, 'base_sha': row['base_sha'], 'sources': [p.as_json() for p in automerge.row_pulls(row)],
        'status': row['status'], 'phase': row['phase']}
    state.pop('supervisor_instruction', None)
    actions = [m.SupervisorAction.model_validate(normalize(r)) for r in db.rows(conn, 'automerge_supervisor_actions',
        where='batch_id=?', params=(batch_id,), order='updated_at DESC,action_id DESC', limit=DETAIL_LIMIT)]
    executions = []
    for r in db.rows(conn, 'automerge_executions', where='batch_id=?', params=(batch_id,), order='started_at DESC,execution_id DESC', limit=DETAIL_LIMIT):
        evidence = scrub(json.loads(r['payload_json']))
        executions.append(m.ExecutionSummary(execution_id=r['execution_id'], command=evidence['command'],
            head=evidence['head'], exit_code=evidence.get('exit_code'), duration_seconds=evidence.get('duration_seconds'),
            started_at=timestamp(r['started_at']), attest=projections.attest(evidence.get('attest')), attest_runs=[projections.attest(v) for v in evidence.get('attest_runs', []) if isinstance(v, dict)]))
    failovers = db.rows(conn, 'automerge_provider_failovers', where='batch_id=?', params=(batch_id,))
    failover = projections.failover(json.loads(failovers[0]['state_json'])) if failovers else None
    return m.BatchDetail(**summary(row).model_dump(), state=projections.batch_state(state), supervisor_actions=actions,
        executions=executions, execution_count=aggregate(conn, 'automerge_executions', 'count(*)', 'batch_id=?', (batch_id,)),
        outbox_count=aggregate(conn, 'automerge_github_outbox', 'count(*)', 'batch_id=?', (batch_id,)),
        failover=failover, outbox=outbox(conn, pending=False, limit=DETAIL_LIMIT, batch_id=batch_id).items,
        slack_thread_ts=row.get('thread_ts'))


def queue(conn):
    db.require(conn)
    groups = {key: [] for key in ['in_batch', 'waiting', 'dependency_blocked', 'rejected', 'draft']}
    if db.has_table(conn, 'automerge_pr_inventory') and db.has_table(conn, 'automerge_github_outbox'):
        raw = supervisor.queue_groups(automerge, conn)
        inventory = {r['number']: json.loads(r['data_json']) for r in db.rows(conn, 'automerge_pr_inventory')}
        live = {p.number: r['batch_id'] for r in db.rows(conn, 'automerge_batches', where=LIVE_WHERE, limit=ACTIVE_LIMIT, columns=batch_columns(conn))
                for p in automerge.row_pulls(r)}
        for label, entries in raw.items():
            for number, head, title, reason in entries:
                key = {'dependency blocked': 'dependency_blocked', 'rejected': 'rejected', 'draft': 'draft'}.get(label)
                if key is None:
                    key = 'in_batch' if number in live else 'waiting'
                metadata = inventory.get(number, {})
                labels = sorted(automerge._labels(metadata))
                high, immediate = automerge._priority_flags(set(labels))
                author = metadata.get('author', metadata.get('user', {}))
                author = author.get('login') if isinstance(author, dict) else author
                groups[key].append(m.QueueItem(number=number, url=f'https://github.com/{monitor.REPO_NAME}/pull/{number}',
                    title=scrub(title), head_sha=head, author=scrub(author), labels=scrub(labels),
                    priority='immediate' if immediate else 'high' if high else None,
                    blocked_reason=scrub(reason), batch_id=live.get(number)))
    return m.Queue(generated_at=now().isoformat(), groups=[m.QueueGroup(key=key, label=key.replace('_', ' ').capitalize(), items=items)
                                                         for key, items in groups.items()])


def ledger(conn, status='open', limit=50, before=None):
    where, params = ('status=?', (status,)) if status != 'all' else ('1', ())
    rows, cursor = page_rows(conn, 'known_failures', pagination.LEDGER, limit, before, where=where, params=params)
    return m.LedgerPage(items=[m.KnownFailure.model_validate(normalize(r)) for r in rows], next_cursor=cursor)


def triage(conn, limit=50, before=None):
    jobs, cursor = page_rows(conn, 'triage_jobs', pagination.JOB, limit, before)
    return m.TriagePage(items=[m.TriageJob.model_validate({**normalize(r),
        'observation_count': len(json.loads(r['observations_json'])),
        'recovering': bool(json.loads(r.get('recovery_json') or '{}'))}) for r in jobs], next_cursor=cursor)


def repairs(conn, limit=50, before=None):
    active_rows = db.rows(conn, 'issue_repairs', where=REPAIR_ACTIVE,
                          order=pagination.sql_order(pagination.JOB), limit=ACTIVE_LIMIT)
    recent_rows, cursor = page_rows(conn, 'issue_repairs', pagination.JOB, limit, before,
                                   where=f'NOT {REPAIR_ACTIVE}')

    def render(row):
        return m.Repair.model_validate({**normalize(row), 'recovering': bool(json.loads(row.get('recovery_json') or '{}'))})

    return m.Repairs(concurrency_limit=issue_fixer.concurrency_limit(), active=[render(r) for r in active_rows],
                     recent=[render(r) for r in recent_rows], next_cursor=cursor,
                     active_count=aggregate(conn, 'issue_repairs', 'count(*)', REPAIR_ACTIVE))


def skill_health():
    try:
        with httpx.Client(timeout=0.5, follow_redirects=False, trust_env=False) as client:
            response = client.get(os.environ.get('MM_WEB_SKILL_SERVICE_URL', 'http://127.0.0.1:8769').rstrip('/') + '/health')
        response.raise_for_status()
        value = response.json()
        return m.SkillServiceHealth(reachable=True, version=scrub(str(value.get('version'))) if value.get('version') is not None else None)
    except (httpx.HTTPError, ValueError, TypeError):
        return m.SkillServiceHealth(reachable=False)


def health(conn):
    heartbeat = {r['entry_point']: normalize(r) for r in db.rows(conn, 'entrypoint_heartbeats')} if conn else {}
    entries = []
    for name in ['merger', 'monitor', 'triage', 'stabilize']:
        value = heartbeat.get(name, {})
        if not value.get('last_finished_at'):
            state = 'unknown'
        elif value.get('last_error') or value.get('last_outcome') in {'error', 'failed', 'failure', 'failing'}:
            state = 'failing'
        elif now() - parsed(value['last_finished_at']) > timedelta(minutes=15):
            state = 'stale'
        else:
            state = 'ok'
        entries.append(m.EntryPointHealth(name=name, state=state, **{k: v for k, v in value.items() if k != 'entry_point'}))
    return m.Health(entry_points=entries, skill_service=skill_health(), db=m.DatabaseHealth(path_ok=conn is not None))


def stats(conn, days=30):
    start = now().date() - timedelta(days=days - 1)
    days_data = {start + timedelta(days=i): {'landed_prs': 0, 'rejected_prs': 0, 'batches_completed': 0,
        'batches_failed': 0, 'durations': []} for i in range(days)}
    finish = start + timedelta(days=days)
    lane, lane_params = master_lane(conn)
    for row in db.rows(conn, 'automerge_batches', where=f'finished_at>=? AND finished_at<? AND {lane}',
        params=(datetime.combine(start, datetime.min.time(), timezone.utc).isoformat(),
                datetime.combine(finish, datetime.min.time(), timezone.utc).isoformat(), *lane_params),
        columns=batch_columns(conn)):
        date = parsed(row['finished_at']).date()
        if date not in days_data:
            continue
        day = days_data[date]
        value = summary(row)
        if value.outcome == Outcome.landed:
            day['landed_prs'] += len(value.sources)
            day['batches_completed'] += 1
        elif value.outcome == Outcome.failed:
            day['batches_failed'] += 1
        rejected = {(p.number, p.head_sha) for p in value.excluded if p.kind == 'rejected'}
        if value.outcome == Outcome.rejected:
            rejected.update((p.number, p.head_sha) for p in value.sources)
        day['rejected_prs'] += len(rejected)
        day['durations'].append(value.duration_seconds / 60)
    return m.Stats(days=[m.DailyStats(date=date.isoformat(), **{k: v for k, v in value.items() if k != 'durations'},
        median_batch_minutes=statistics.median(value['durations']) if value['durations'] else None)
        for date, value in days_data.items()])


def overview(conn):
    db.require(conn)
    has_batches = db.has_table(conn, 'automerge_batches')
    primary = automerge.active_batch(conn) if has_batches else None
    child = speculation.child(conn, primary['batch_id']) if primary is not None else None
    campaign = automerge.active_campaign_batch(conn) if has_batches else None
    lane, lane_params = master_lane(conn)
    seven = datetime.combine(now().date() - timedelta(days=6), datetime.min.time(), timezone.utc).isoformat()
    tomorrow = datetime.combine(now().date() + timedelta(days=1), datetime.min.time(), timezone.utc).isoformat()
    today = datetime.combine(now().date(), datetime.min.time(), timezone.utc).isoformat()
    landed_expression = "coalesce(sum(json_array_length(coalesce(active_pull_requests_json,pull_requests_json))),0)"
    landed_where = f"terminal_status='merged' AND finished_at>=? AND finished_at<? AND {lane}"
    landed_today = aggregate(conn, 'automerge_batches', landed_expression, landed_where, (today, tomorrow, *lane_params))
    landed_seven = aggregate(conn, 'automerge_batches', landed_expression, landed_where, (seven, tomorrow, *lane_params))
    median = None
    if has_batches:
        median = conn.execute(f"""WITH durations AS (
            SELECT max(0,(julianday(finished_at)-julianday(created_at))*1440) AS minutes
            FROM automerge_batches WHERE finished_at>=? AND finished_at<? AND {lane}), ranked AS (
            SELECT minutes,row_number() OVER (ORDER BY minutes) AS position,count(*) OVER () AS total FROM durations)
            SELECT avg(minutes) FROM ranked WHERE position IN ((total+1)/2,(total+2)/2) LIMIT 1""",
            (seven, tomorrow, *lane_params)).fetchone()[0]
    pending_where = 'delivered_at IS NULL AND cancelled_at IS NULL'
    pending = aggregate(conn, 'automerge_github_outbox', 'count(*)', pending_where)
    failing = aggregate(conn, 'automerge_github_outbox', 'count(*)', pending_where + " AND coalesce(last_error,'')<>''")
    failures = aggregate(conn, 'known_failures', 'count(*)', "status='open'")
    product = aggregate(conn, 'known_failures', 'count(*)', "status='open' AND triage_outcome='product'")
    infrastructure = aggregate(conn, 'known_failures', 'count(*)', "status='open' AND triage_outcome='infrastructure'")
    triage_where = "status NOT IN ('completed','failed','aborted')"
    investigations = aggregate(conn, 'triage_jobs', 'count(*)', triage_where)
    triage_recovering = aggregate(conn, 'triage_jobs', 'count(*)', triage_where + " AND coalesce(recovery_json,'{}')<>'{}'")
    active_repairs = aggregate(conn, 'issue_repairs', 'count(*)', REPAIR_ACTIVE)
    repairs_recovering = aggregate(conn, 'issue_repairs', 'count(*)', REPAIR_ACTIVE + " AND coalesce(recovery_json,'{}')<>'{}'")
    concurrency = issue_fixer.concurrency_limit()
    queue_counts = m.QueueCounts()
    if db.has_table(conn, 'automerge_pr_inventory') and db.has_table(conn, 'automerge_github_outbox'):
        raw = supervisor.queue_groups(automerge, conn)
        live_numbers = {p.number for r in db.rows(conn, 'automerge_batches', where=LIVE_WHERE, limit=ACTIVE_LIMIT,
                                                columns=batch_columns(conn)) for p in automerge.row_pulls(r)}
        otherwise = raw.get('otherwise ineligible', [])
        included = sum(item[0] in live_numbers for item in otherwise)
        queue_counts = m.QueueCounts(in_batch=included, waiting=len(otherwise)-included,
            dependency_blocked=len(raw.get('dependency blocked', [])), rejected=len(raw.get('rejected', [])),
            draft=len(raw.get('draft', [])))
    return m.Overview(generated_at=now().isoformat(), health=health(conn),
        pipeline=m.Pipeline(primary=summary(dict(primary)) if primary is not None else None,
                            speculative=summary(dict(child)) if child is not None else None,
                            campaign=summary(dict(campaign)) if campaign is not None else None),
        queue=queue_counts,
        kpis=m.Kpis(landed_today=landed_today, landed_7_days=landed_seven,
            median_batch_minutes_7_days=median, open_ci_failures=failures,
            pending_outbox_writes=pending, active_repairs=active_repairs),
        recent=batches(conn, state='recent', limit=10).items,
        outbox=m.OutboxCounts(pending=pending, failing=failing),
        ledger=m.LedgerCounts(open=failures, product=product, infrastructure=infrastructure),
        triage=m.TriageSummary(active=investigations, recovering=triage_recovering),
        repairs=m.RepairsSummary(active=active_repairs, recovering=repairs_recovering, concurrency_limit=concurrency))
