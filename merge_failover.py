"""Same-session Bedrock failover, owned by the merger's existing lock.

Native Move owns interruption and recovery. A bounded CLI observation may end
before Move does; its durable native journal, not session idleness, tells the
next poll what happened. Profile-only Move keeps the container and its evidence.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3

import monitor
import read_budget


PROFILE = 'deepseek'
MODEL = 'deepseek-flash'
BEDROCK_503 = re.compile(r'^API Error:\s*503\b[^\n]*\bBedrock\b', re.I)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_provider_failovers (
        batch_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        state_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
    )""")


def state(conn, batch_id):
    row = conn.execute('SELECT state_json FROM automerge_provider_failovers WHERE batch_id=?',
                       (batch_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(a, conn, row, value):
    with conn:
        conn.execute('INSERT INTO automerge_provider_failovers VALUES (?,?,?,?) '
                     'ON CONFLICT(batch_id) DO UPDATE SET state_json=excluded.state_json, '
                     'updated_at=excluded.updated_at',
                     (row['batch_id'], row['session_id'], json.dumps(value), a.utc_now()))


def pending(value):
    return value.get('stage') in {'moving', 'configuring', 'continuing'}


def read_move(session_id):
    # Read-only: this journal belongs to mj. Never copy its recovery prompts or
    # connection material into supervisor state, logs or notifications.
    directory = Path(os.environ.get('MJ_DATA_DIR', Path.home() / '.local/share/mjolnir')).expanduser().resolve()
    with closing(sqlite3.connect((directory / 'mj.sqlite3').as_uri() + '?mode=ro', uri=True,
                                timeout=2)) as native:
        row = native.execute('SELECT operation_json FROM session_moves WHERE session_id=?',
                             (session_id,)).fetchone()
    return json.loads(row[0]) if row else None


def read_items(session_id, cursor):
    raw = monitor.require_mj_success(['transcript', '--session', session_id, '--finished-only',
                                     '--after-seq', str(cursor), '--json'], timeout=10)
    page = json.loads(raw)
    if not isinstance(page.get('items'), list):
        raise monitor.MjError('invalid provider-error transcript page')
    return page


def observe_items(value, items):
    """Count distinct provider errors; normal agent/tool progress breaks a streak."""
    errors = list(value.get('errors', []))
    for item in sorted(items, key=lambda i: int(i.get('seq', 0))):
        if int(item.get('seq', 0)) <= value.get('cursor', 0):
            continue
        body = item.get('body') or {}
        text = str(item.get('text') or '').strip()
        kind = body.get('kind') or item.get('role')
        if kind == 'agent' and text:
            if BEDROCK_503.match(text):
                identity = item.get('stable_id') or 'seq:' + str(item['seq'])
                if identity not in errors:
                    errors = (errors + [identity])[-2:]
            else:
                errors = []
        elif kind == 'tool' and (body.get('call') or {}).get('status') == 'completed':
            errors = []
        # User retry prompts and lifecycle notices are not provider successes.
    value['errors'] = errors


def probe(a, conn, row):
    value = state(conn, row['batch_id'])
    if value.get('stage') == 'completed' or pending(value) or a.ready_candidate(row):
        return
    session = a._session_status(row['session_id'])
    if session.get('target_id') != a.MJ_TARGET or session.get('profile_id') == PROFILE:
        return
    if value.get('session_id') != row['session_id']:
        value = {'stage': 'observing', 'session_id': row['session_id'], 'cursor': 0, 'errors': []}
    # Don't fail over for old errors on an earlier page if later work succeeded.
    page = read_items(row['session_id'], value['cursor'])
    observe_items(value, page['items'])
    cursor = int(page.get('next_after_seq', value['cursor']))
    if cursor < value['cursor']:
        raise monitor.MjError('provider-error transcript cursor moved backwards')
    value['cursor'] = cursor
    caught_up = cursor >= int(page.get('latest_seq', cursor))
    if caught_up and len(value['errors']) >= 2:
        previous = read_move(row['session_id'])
        value.update(stage='moving', move_attempted=False,
                     prior_operation_id=previous.get('operation_id') if previous else None)
    save(a, conn, row, value)


def prepare(session_id):
    return json.loads(monitor.require_mj_success(
        ['move', '--session', session_id, '--profile', PROFILE, '--queue', 'start',
         '--prepare', '--json'], timeout=15))


def submit_move(session_id, *, timeout=15):
    # No target/mount/resource override: native in-place Move retains the Git
    # directory, ignored logs, build caches and any useful Luna child work.
    # The caller reserves this observation budget before checkpointing intent.
    # Rechecking the shared deadline after that checkpoint could defer without
    # ever submitting, leaving a falsely ambiguous Move. Keep the reserved bound.
    token = read_budget.deadline.set(None)
    try:
        result = monitor.mj_command(['move', '--session', session_id, '--profile', PROFILE,
                                     '--queue', 'start', '--yes', '--json'], timeout=timeout)
    finally:
        read_budget.deadline.reset(token)
    try:
        outcome = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise monitor.MjError('Move reply was lost or invalid; inspect its native journal') from exc
    if result.returncode or outcome.get('outcome') != 'completed':
        raise monitor.MjError('native Move did not complete; original session and recovery are retained')
    return outcome


def matching_move(value, operation):
    if not operation or operation.get('operation_id') == value.get('prior_operation_id'):
        return False
    selection = operation.get('selection') or {}
    return (selection.get('session_id') == value['session_id'] and
            selection.get('profile_id') == PROFILE and
            (not value.get('operation_id') or operation['operation_id'] == value['operation_id']))


def model_value(value):
    if isinstance(value, dict):
        if value.get('id', value.get('key')) == 'model' or value.get('category') == 'model':
            return value.get('current_value', value.get('value'))
        for child in value.values():
            if isinstance(child, (dict, list)):
                found = model_value(child)
                if found:
                    return found
    elif isinstance(value, list):
        for child in value:
            found = model_value(child)
            if found:
                return found
    return None


def advance(a, conn, row):
    value = state(conn, row['batch_id'])
    if not pending(value):
        return
    session_id = value['session_id']
    if session_id != row['session_id']:
        raise monitor.MjError('failover session identity changed; original Move is retained')
    if value['stage'] == 'moving':
        operation = read_move(session_id)
        if not matching_move(value, operation):
            if value['move_attempted']:
                raise monitor.MjError('Move acceptance is uncertain and its journal is absent; '
                                      'inspect mj before retrying this same-session failover')
            preparation = prepare(session_id)
            if not preparation.get('in_place'):
                raise monitor.MjError('profile Move cannot retain this workspace in place; '
                                      'operator must preserve its private evidence before moving')
            timeout = read_budget.timeout(15)
            if timeout < 1:
                raise read_budget.Deferred('Move submission continues next poll')
            value['move_attempted'] = True
            save(a, conn, row, value)
            try:
                outcome = submit_move(session_id, timeout=timeout)
                value['operation_id'] = outcome['operation_id']
            except (monitor.MjError, read_budget.Deferred):
                # A detached CLI never cancels the daemon-owned Move.
                operation = read_move(session_id)
                if not matching_move(value, operation):
                    raise
            operation = read_move(session_id)
        if not matching_move(value, operation):
            raise monitor.MjError('Move journal does not match the accepted failover')
        value['operation_id'] = operation['operation_id']
        save(a, conn, row, value)
        if operation['phase'] in {'failed', 'cancelled'}:
            raise monitor.MjError('DeepSeek Move ' + operation['phase'] + '; inspect native recovery '
                                  'for session ' + session_id)
        if operation['phase'] != 'completed':
            return
        if not operation.get('in_place'):
            raise monitor.MjError('Move changed the environment; reconcile saved execution evidence')
        value['stage'] = 'configuring'
        save(a, conn, row, value)
    if value['stage'] == 'configuring':
        session = a._session_status(session_id)
        if session.get('profile_id') != PROFILE or session.get('id') != session_id:
            raise monitor.MjError('Move destination identity does not match this merger')
        options = json.loads(monitor.require_mj_success(
            ['set-config', '--session', session_id, '--json'], timeout=10))
        if model_value(options) != MODEL:
            catalogue = json.loads(monitor.require_mj_success(
                ['models', '--profile', PROFILE, '--json'], timeout=10))
            flash = next((m['value'] for m in catalogue['models'] if m['name'] == MODEL), None)
            if not flash:
                raise monitor.MjError('DeepSeek Flash is absent from the destination catalogue')
            monitor.require_mj_success(['set-config', '--session', session_id, '--key', 'model',
                                       '--value', flash, '--json'], timeout=10)
            return  # Confirm applied configuration on the next bounded poll.
        value['stage'] = 'continuing'
        save(a, conn, row, value)
    if value['stage'] == 'continuing':
        # Atomically checkpoint the continuation with its normal stable message
        # ID. A lost reply uses the existing delivery retry, never another prompt.
        if not value.get('continuation_queued'):
            latest = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                                  (row['batch_id'],)).fetchone()
            if a.ready_candidate(latest):
                value['stage'] = 'completed'
                save(a, conn, row, value)
                return
            prompt = ('Bedrock returned two consecutive 503 errors. Native Move switched this '
                      'same session to DeepSeek Flash, retaining the container, Git checkout, '
                      'private progress note, execution logs and current delegation policy. '
                      'Continue the original assignment. Reconcile mm-db state, Git HEAD, '
                      'pending edits and saved evidence once; reuse matching validation. '
                      'Changing providers alone does not require new tests.\n\n' +
                      str(latest['pending_prompt'] or ''))
            def checkpoint():
                conn.execute("UPDATE automerge_batches SET agent_configuration='flash-luna' "
                             'WHERE batch_id=?', (row['batch_id'],))
                value['continuation_queued'] = True
                value['stage'] = 'completed'
                conn.execute('UPDATE automerge_provider_failovers SET state_json=?,updated_at=? '
                             'WHERE batch_id=?', (json.dumps(value), a.utc_now(), row['batch_id']))
            a.queue_agent_prompt(conn, latest, prompt, checkpoint=checkpoint, preserve_ready=True)
        value['stage'] = 'completed'
        save(a, conn, row, value)


def notify(a, conn, transport):
    for saved in conn.execute('SELECT * FROM automerge_provider_failovers').fetchall():
        value = json.loads(saved['state_json'])
        if value.get('stage') != 'completed' or value.get('notice_sent'):
            continue
        row = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                           (saved['batch_id'],)).fetchone()
        if not row or transport.kind == 'chat' and not row['thread_ts']:
            continue
        if monitor.relay_text(transport, row['thread_ts'],
                              'Bedrock returned two consecutive 503 errors. This merge is '
                              'continuing on DeepSeek Flash in the same session; its checkout '
                              'and execution evidence were retained.'):
            value['notice_sent'] = True
            save(a, conn, row, value)
