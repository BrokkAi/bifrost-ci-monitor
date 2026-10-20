"""Threaded reports of accepted mm milestones; never a merge or delivery gate."""
from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import re
import sqlite3
import time

import monitor
from secret_filter import diagnostic

KINDS = ('candidate', 'candidate_withdrawal', 'exclusion', 'tests', 'publication', 'ready')
RETRY_SECONDS = 60
EVENT_LIMIT = 10


def ensure_schema(conn):
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='automerge_skill_notifications'").fetchone()
    conn.execute('''CREATE TABLE IF NOT EXISTS automerge_skill_notifications (
        batch_id TEXT PRIMARY KEY, after_rowid INTEGER NOT NULL DEFAULT 0,
        attempted_at REAL NOT NULL DEFAULT 0
    )''')
    if not existed:
        # Install once at the existing tail. New batches start at zero; opening
        # another connection must never swallow their as-yet unreported events.
        conn.execute('''INSERT OR IGNORE INTO automerge_skill_notifications (batch_id,after_rowid)
            SELECT b.batch_id,COALESCE(MAX(e.rowid),0) FROM automerge_batches b
            LEFT JOIN automerge_skill_events e USING(batch_id) GROUP BY b.batch_id''')


def _text(value, limit=500):
    if not isinstance(value, str):
        return ''
    # Explicit projection precedes scrubbing. No report/evidence/prompt object
    # is ever passed to this renderer, even when it accompanies a safe event.
    value = ' '.join(diagnostic(value).split())
    value = html.escape(value, quote=False).replace('`', '')
    return value if len(value) <= limit else value[:limit - 1] + '…'


def _head(payload):
    value = payload.get('head')
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40,64}', value):
        raise ValueError('invalid milestone head')
    return value[:10]


def _pr(payload):
    number = payload.get('number')
    if type(number) is not int or number <= 0:
        raise ValueError('invalid milestone PR')
    return f'<https://github.com/{monitor.REPO_NAME}/pull/{number}|#{number}>'


def render(event):
    """Project reviewed fields, not native tool inputs or a stored payload dump."""
    kind = event['kind']
    payload = json.loads(event['payload_json'])
    if not isinstance(payload, dict):
        raise ValueError('invalid milestone payload')
    if kind == 'candidate':
        text = f'Candidate checkpoint: `{_head(payload)}`.'
    elif kind == 'candidate_withdrawal':
        candidate = payload.get('candidate')
        head = f' `{_head(candidate)}`' if isinstance(candidate, dict) and candidate else ''
        text = f'Candidate checkpoint withdrawn{head}.'
    elif kind == 'exclusion':
        disposition = {'rejected': 'PR rejection recorded', 'removed': 'PR removed from batch'}[payload['kind']]
        text = f'{disposition}: {_pr(payload)} at `{_head(payload)}`. {_text(payload.get("reason"))}'
    elif kind == 'tests':
        verdict = {'pass': 'PASS', 'fail': 'FAIL'}[payload['verdict']]
        text = (f'Local test assessment: *{verdict}* at `{_head(payload)}`. '
                f'{_text(payload.get("tests"), 380)} '
                f'Baseline: {_text(payload.get("baseline"), 250)}')
    elif kind == 'publication':
        text = f'Integration PR published: {_pr(payload)} at `{_head(payload)}`.'
    elif kind == 'ready':
        text = f'Validated handoff recorded at `{_head(payload)}`; supervisor gates still apply.'
    else:
        return None
    try:
        stamp = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00'))
        timestamp = stamp.astimezone(timezone.utc).strftime('%H:%M UTC · ')
    except (ValueError, TypeError, AttributeError):
        timestamp = ''
    return f'• {timestamp}{text.strip()}'


def deliver(a, conn, transport, row):
    """Existing merger lock owns delivery; failed Slack never changes the batch."""
    if transport.kind != 'chat' or not row['thread_ts']:
        return
    try:
        saved = conn.execute('SELECT after_rowid,attempted_at FROM automerge_skill_notifications '
                             'WHERE batch_id=?', (row['batch_id'],)).fetchone()
        cursor, attempted = tuple(saved) if saved else (0, 0)
        now = time.time()
        if now - attempted < RETRY_SECONDS:
            return
        placeholders = ','.join('?' for _ in KINDS)
        events = conn.execute(f'SELECT rowid,* FROM automerge_skill_events WHERE batch_id=? '
                              f'AND rowid>? AND kind IN ({placeholders}) ORDER BY rowid LIMIT ?',
                              (row['batch_id'], cursor, *KINDS, EVENT_LIMIT)).fetchall()
        if not events:
            return
        label = 'Speculative mm updates' if a.speculation.is_speculative(row) else 'mm updates'
        text = f'*{label}*'
        after = cursor
        for event in events:
            try:
                line = render(event)
            except (ValueError, TypeError, KeyError, AttributeError):
                a.log(f'mm update omitted invalid event {event["rowid"]}')
                line = None
            if line and len(text) + len(line) + 1 > monitor.SLACK_MESSAGE_LIMIT:
                break
            if line:
                text += '\n' + line
            after = event['rowid']
        with conn:
            conn.execute('INSERT INTO automerge_skill_notifications VALUES (?,?,?) '
                         'ON CONFLICT(batch_id) DO UPDATE SET attempted_at=excluded.attempted_at',
                         (row['batch_id'], cursor, now))
        if text == f'*{label}*' or monitor.relay_text(transport, row['thread_ts'], text):
            with conn:
                conn.execute('UPDATE automerge_skill_notifications SET after_rowid=? WHERE batch_id=?',
                             (after, row['batch_id']))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as exc:
        a.log(f'mm updates deferred for batch {row["batch_id"]}: {type(exc).__name__}')


def retry(a, conn, transport):
    if transport.kind != 'chat':
        return
    placeholders = ','.join('?' for _ in KINDS)
    # Include terminal batches: a publication/handoff may land between polls.
    try:
        rows = conn.execute(f'''SELECT b.* FROM automerge_batches b
            LEFT JOIN automerge_skill_notifications n USING(batch_id)
            WHERE b.thread_ts IS NOT NULL AND b.thread_ts<>'' AND COALESCE(n.attempted_at,0)<=?
            AND EXISTS (SELECT 1 FROM automerge_skill_events e WHERE e.batch_id=b.batch_id
                AND e.rowid>COALESCE(n.after_rowid,0) AND e.kind IN ({placeholders}))
            ORDER BY COALESCE(n.attempted_at,0),b.created_at LIMIT 20''',
            (time.time() - RETRY_SECONDS, *KINDS)).fetchall()
    except sqlite3.Error as exc:
        a.log(f'mm updates deferred: {type(exc).__name__}')
        return
    for row in rows:
        deliver(a, conn, transport, row)
