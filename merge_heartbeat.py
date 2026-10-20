"""Factual Slack activity reports, independent of merger narration and verdicts."""
from __future__ import annotations

from datetime import datetime
import html
import json
import re
import shlex
import sqlite3
import textwrap
import time

import monitor
import read_budget
from secret_filter import diagnostic

INTERVAL_MS = 5 * 60 * 1000
SCAN_RETRY_MS = 60 * 1000
READ_SECONDS = 3
DISPLAY_VERSION = 2
DISPLAY_CHARS = 1000
DISPLAY_LINES = 12
TRUNCATED = '\n… (truncated)'
TURN_BOUNDARIES = ('context-cleared:', 'system:turn-interrupted:',
                   'system:work-interrupted:', 'system:session-restarted:')
NO_FOREGROUND = {'idle', 'background', 'expecting', 'goal', 'checking-continuation', 'retry', 'closed'}


def _shorten(text, characters=DISPLAY_CHARS):
    lines = text.splitlines()
    clipped = '\n'.join(lines[:DISPLAY_LINES])
    if len(lines) > DISPLAY_LINES or len(clipped) > characters:
        return clipped[:max(0, characters - len(TRUNCATED))].rstrip() + TRUNCATED
    return clipped


def _pretty_shell(text):
    text = textwrap.dedent(text).strip()
    if '\n' in text:
        return text
    # Keep quoted scripts and escaped semicolons intact. Only introduce a
    # visual line break after an unquoted shell command separator.
    result = []
    quote = None
    escaped = False
    for character in text:
        if escaped:
            escaped = False
        elif character == '\\' and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in ('"', "'", '`'):
            quote = character
        elif character == ';':
            result.append(';\n')
            continue
        if character == ' ' and result and result[-1].endswith('\n'):
            continue
        result.append(character)
    return ''.join(result)


def _call_display(item):
    call = (item.get('body') or {}).get('call') or {}
    args = call.get('rawInput')
    if not isinstance(args, dict):
        return ''
    command = args.get('command')
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        command = shlex.join(command)
    if isinstance(command, str):
        display = _pretty_shell(diagnostic(command))
    else:
        # Other tools get structured parameters; private instructions and
        # message bodies are not useful command diagnostics.
        display = json.dumps(diagnostic(args), indent=2, ensure_ascii=False)
    display = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', display)
    return _shorten(display)


def _call_bullets(active, now, available):
    bullets = []
    for index, call in enumerate(active):
        remaining = len(active) - index
        budget = (available - sum(map(len, bullets))) // remaining
        if budget < 90:
            bullets.append(f'\n• … {remaining} more calls')
            break
        state = call['status'] + ', ' if call['status'] in {'pending', 'background'} else ''
        started = call['started_at_ms']
        age = f'{_duration(now - started)} elapsed' if started is not None else 'age unavailable'
        heading = f'\n• {state}{age}\n```\n'
        raw = call['display'] or ('Task details unavailable' if call['status'] == 'background'
                                  else 'Call details unavailable')
        limit = min(DISPLAY_CHARS, budget - len(heading) - 6)
        display = html.escape(_shorten(raw, limit), quote=False).replace('```', '`\u200b``')
        while len(display) > budget - len(heading) - 6:
            limit -= max(1, len(display) - (budget - len(heading) - 6))
            display = html.escape(_shorten(raw, limit), quote=False).replace('```', '`\u200b``')
        bullets.append(heading + display + '\n```')
    return ''.join(bullets)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_activity_heartbeats (
        batch_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        state_json TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_activity_tools (
        batch_id TEXT NOT NULL, session_id TEXT NOT NULL, stable_id TEXT NOT NULL,
        started_at_ms INTEGER NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY (batch_id, session_id, stable_id)
    )""")
    monitor.ensure_column(conn, 'automerge_activity_tools', 'display', "TEXT NOT NULL DEFAULT ''")
    monitor.ensure_column(conn, 'automerge_activity_tools', 'position', 'INTEGER NOT NULL DEFAULT 0')


def _save(conn, row, value):
    conn.execute('INSERT INTO automerge_activity_heartbeats VALUES (?,?,?,?) '
                 'ON CONFLICT(batch_id) DO UPDATE SET session_id=excluded.session_id, '
                 'state_json=excluded.state_json, updated_at=excluded.updated_at',
                 (row['batch_id'], row['session_id'], json.dumps(value), monitor.utc_now()))


def _state(conn, row, now):
    saved = conn.execute('SELECT session_id,state_json FROM automerge_activity_heartbeats '
                         'WHERE batch_id=?', (row['batch_id'],)).fetchone()
    if saved and saved['session_id'] == row['session_id']:
        return json.loads(saved['state_json'])
    try:
        started = int(datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).timestamp() * 1000)
    except (ValueError, TypeError, AttributeError):
        started = now
    return {'cursor': 0, 'assistant_ms': started, 'assistant_seq': 0}


def _assistants(value, items, now):
    # Finished-only relay pages contain only agent entries. Never retain text.
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip():
            continue
        body = item.get('body') or {}
        if body.get('streaming'):
            continue
        stamp = int(item.get('last_changed_at_ms') or item.get('created_at_ms') or now)
        seq = int(item.get('seq', 0))
        if (stamp, seq) > (value['assistant_ms'], value['assistant_seq']):
            value.update(assistant_ms=stamp, assistant_seq=seq)


def _page(session, cursor):
    result = monitor.mj_command(['transcript', '--session', session, '--role', 'agent',
                                 '--role', 'tool', '--role', 'system', '--after-seq', str(cursor),
                                 '--limit', '200', '--json'], timeout=READ_SECONDS)
    if result.returncode:
        # CLI diagnostics can contain private material; report only the category.
        raise monitor.MjError('activity transcript read failed')
    page = json.loads(result.stdout)
    if (not isinstance(page.get('items'), list)
            or not all(isinstance(item, dict) for item in page['items'])
            or int(page['next_after_seq']) < cursor
            or int(page['latest_seq']) < int(page['next_after_seq'])):
        raise ValueError('invalid activity transcript page')
    return page


def _session(session):
    result = monitor.mj_command(['sessions', '--session', session, '--json'], timeout=READ_SECONDS)
    if result.returncode:
        raise monitor.MjError('activity session read failed')
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or data.get('id') != session:
        raise ValueError('invalid activity session')
    return data


def _retire(conn, row, value):
    # Mj #1311: interrupted calls can retain an in_progress transcript status.
    # Reporting state only: native history stays intact, and no command is
    # cancelled. Lifecycle boundaries supersede an unclosed historical status.
    conn.execute("UPDATE automerge_activity_tools SET status='not_current',display='' "
                 "WHERE batch_id=? AND session_id=? AND position>0 AND position<=? "
                 "AND status IN ('pending','in_progress')",
                 (row['batch_id'], row['session_id'], value.get('closed_through', 0)))


def _reconcile(conn, row, value, session):
    outcome = session.get('last_turn_outcome') or {}
    start, end = outcome.get('turn_start_position'), outcome.get('completed_ordinal')
    # A rejected queued prompt never started and cannot discharge another
    # turn's useful calls. Steers likewise aren't completion boundaries.
    if type(start) is int and type(end) is int and 0 < start <= end:
        value['closed_through'] = max(value.get('closed_through', 0), end)
    activity = session.get('activity_state') or {}
    state = activity.get('state')
    known = state in NO_FOREGROUND or state in {'turn', 'tool'}
    if state in NO_FOREGROUND:
        value['closed_through'] = max(value.get('closed_through', 0), value['cursor'])
    _retire(conn, row, value)
    if not known:
        # A disconnected worker's last-known activity is not live evidence.
        return False, []
    work = session.get('background_work')
    tasks = (work.get('tasks') if isinstance(work, dict) else session.get('background_tasks')) or []
    if not isinstance(tasks, list):
        raise ValueError('invalid background task list')
    background, seen = [], set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get('id'), str):
            continue
        if task['id'] in seen:
            continue
        seen.add(task['id'])
        stamp = task.get('started_at_ms')
        background.append({'status': 'background',
                           'started_at_ms': stamp if type(stamp) is int else None,
                           'display': _call_display({'body': {'call': {'rawInput': {
                               'command': task.get('command', '')}}}})})
    return True, background


def _ingest(conn, row, value, items, now):
    _assistants(value, [i for i in items if i.get('role') == 'agent'], now)
    for item in items:
        if item.get('role') == 'system' and item.get('stable_id', '').startswith(TURN_BOUNDARIES):
            value['closed_through'] = max(value.get('closed_through', 0), int(item['position']))
        if item.get('role') != 'tool':
            continue
        identity = item['stable_id']
        stamp = int(item['created_at_ms'])
        # Status updates change the transcript cursor, not the identity/count.
        status = ((item.get('body') or {}).get('call') or {}).get('status')
        if status not in {'pending', 'in_progress', 'completed', 'failed', 'cancelled'}:
            status = 'unknown'
        display = _call_display(item) if status in {'pending', 'in_progress'} else ''
        conn.execute('INSERT INTO automerge_activity_tools '
                     '(batch_id,session_id,stable_id,started_at_ms,status,display,position) VALUES (?,?,?,?,?,?,?) '
                     'ON CONFLICT(batch_id,session_id,stable_id) DO UPDATE SET status=excluded.status, '
                     'position=excluded.position, '
                     "display=CASE WHEN excluded.status NOT IN ('pending','in_progress') THEN '' "
                     "WHEN excluded.display<>'' THEN excluded.display ELSE automerge_activity_tools.display END",
                     (row['batch_id'], row['session_id'], identity, stamp, status, display, int(item['position'])))
    _retire(conn, row, value)


def _duration(milliseconds):
    minutes = max(0, int(milliseconds // 60000))
    return f'{minutes // 60}h {minutes % 60}m' if minutes >= 60 else f'{minutes}m'


def _eligible(a, row):
    return (row is not None and row['status'] == 'running'
            and row['phase'] in {'building', 'fixing'} and row['session_id']
            and row['thread_ts'] and not a.ready_candidate(row))


def observe(a, conn, transport, row, agent_items):
    """Called under the merger lock after normal transcript delivery.

    Only active calls retain a redacted, bounded command/parameter preview.
    Outputs, prompts and connection credentials never enter a heartbeat.
    """
    if transport.kind != 'chat' or not _eligible(a, row):
        return
    end = read_budget.deadline.get()
    token = read_budget.deadline.set(min(end, time.monotonic() + READ_SECONDS)
                                     if end is not None else time.monotonic() + READ_SECONDS)
    try:
        now = int(time.time() * 1000)
        value = _state(conn, row, now)
        if value.get('display_version') != DISPLAY_VERSION:
            # Hydrate creation positions and lifecycle markers on upgrade.
            # Replaying stable IDs cannot add to their counts or reopen calls
            # already superseded by a persisted lifecycle boundary.
            value.update(cursor=0, display_version=DISPLAY_VERSION)
        _assistants(value, agent_items, now)
        with conn:
            _save(conn, row, value)
        if now - max(value['assistant_ms'], value.get('attempt_ms', 0)) < INTERVAL_MS:
            return
        if now - value.get('scan_ms', 0) < SCAN_RETRY_MS:
            return
        value['scan_ms'] = now
        with conn:
            _save(conn, row, value)
        for _ in range(2):
            page = _page(row['session_id'], value['cursor'])
            with conn:
                _ingest(conn, row, value, page['items'], now)
                value['cursor'] = int(page['next_after_seq'])
                _save(conn, row, value)
            if value['cursor'] >= int(page['latest_seq']):
                break
        else:
            # Persist bounded backfill; never advertise a partial count.
            return
        now = int(time.time() * 1000)
        if now - value['assistant_ms'] < INTERVAL_MS or read_budget.expired():
            return
        current = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                               (row['batch_id'],)).fetchone()
        if (not _eligible(a, current) or current['session_id'] != row['session_id']
                or current['thread_ts'] != row['thread_ts']):
            return
        session = _session(row['session_id'])
        with conn:
            known, background = _reconcile(conn, row, value, session)
            _save(conn, row, value)
        if read_budget.expired():
            return
        scope = (row['batch_id'], row['session_id'])
        total = conn.execute('SELECT count(*) FROM automerge_activity_tools '
                             'WHERE batch_id=? AND session_id=?', scope).fetchone()[0]
        same_assistant = value.get('reported_assistant_seq') == value['assistant_seq']
        if same_assistant:
            count = total - value['reported_tools']
            window = 'the previous heartbeat'
        else:
            count = conn.execute('SELECT count(*) FROM automerge_activity_tools '
                                 'WHERE batch_id=? AND session_id=? AND started_at_ms>?',
                                 (*scope, value['assistant_ms'])).fetchone()[0]
            window = 'the last assistant update' if value['assistant_seq'] else 'session start'
        active = conn.execute('SELECT started_at_ms,status,display FROM automerge_activity_tools '
                              'WHERE batch_id=? AND session_id=? '
                              "AND status IN ('pending','in_progress') "
                              'ORDER BY started_at_ms,stable_id', scope).fetchall()
        if not known:
            active = []
        label = ('Speculative supervisor heartbeat' if a.speculation.is_speculative(current)
                 else 'Supervisor heartbeat')
        text = (f'*{label}* — no assistant update for {_duration(now - value["assistant_ms"])}. '
                f'{count} new tool call{"s" if count != 1 else ""} since {window}.')
        if not known:
            text += ' Current call status unavailable.'
        elif active or background:
            running = sum(call['status'] == 'in_progress' for call in active)
            pending = len(active) - running
            labels = []
            for count, label in ((running, 'in-progress'), (pending, 'pending')):
                if count:
                    labels.append(f'{count} {label} call{"s" if count != 1 else ""}')
            if not active:
                text += ' No in-progress calls.'
            if background:
                labels.append(f'{len(background)} background task{"s" if len(background) != 1 else ""}')
            text += ' ' + ', '.join(labels) + ':'
            text += _call_bullets([*active, *background], now, monitor.SLACK_MESSAGE_LIMIT - len(text) - 30)
        else:
            text += ' No in-progress calls.'
        # Persist the attempt first: even an ambiguous Slack response cannot
        # create a burst of retries after a crash. Counts advance only on success.
        value['attempt_ms'] = now
        with conn:
            _save(conn, row, value)
        if monitor.relay_text(transport, current['thread_ts'], text):
            value.update(reported_ms=now, reported_tools=total,
                         reported_assistant_seq=value['assistant_seq'])
            with conn:
                _save(conn, row, value)
    except read_budget.Deferred:
        pass
    except (monitor.MjError, OSError, RuntimeError, ValueError, TypeError, KeyError,
            AttributeError, sqlite3.Error) as exc:
        a.log(f'activity heartbeat deferred for batch {row["batch_id"]}: {type(exc).__name__}')
    finally:
        read_budget.deadline.reset(token)
