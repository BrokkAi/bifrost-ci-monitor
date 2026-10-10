"""Durable, ordered merger actions derived from the existing batch state.

Only the owner of the merger lock runs these actions. An action is an intent,
not permission to merge: the existing phase handlers retain every landing gate.
State changes replace obsolete intents; ambiguous writes retain their existing
outbox/command identities. Read deferrals are resumable without a blocked alert.
"""
from __future__ import annotations

import hashlib
import json
import time

import git_ancestry
import monitor
import speculation
import merge_failover


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_supervisor_actions (
        action_id TEXT PRIMARY KEY, kind TEXT NOT NULL, batch_id TEXT NOT NULL,
        input_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        last_error TEXT, next_attempt_at REAL NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_queue_summaries (
        summary_id TEXT PRIMARY KEY, thread_ts TEXT, messages_json TEXT NOT NULL,
        next_message INTEGER NOT NULL DEFAULT 0, completed_at TEXT, created_at TEXT NOT NULL
    )""")


def snapshot(row):
    return {key: digest(row[key]) if key.endswith('_json') else row[key] for key in (
        'status', 'phase', 'attempt_generation', 'pull_requests_json', 'active_pull_requests_json',
        'excluded_source_heads_json',
        'base_sha', 'session_id', 'predecessor_id', 'role_promoted', 'candidate_json',
        'ready_json', 'prompt_command_id', 'prompt_delivered', 'recovery_json',
        'integration_pr_number', 'terminal_status')}


def plan(a, conn, *, priority_checked, queue_checked, maintenance_done, reconciled):
    actions = []
    primary = a.active_batch(conn)
    rows = conn.execute("SELECT * FROM automerge_batches WHERE status IN ('launching','running','finishing') "
                        "ORDER BY created_at,batch_id").fetchall()

    def add(priority, kind, row=None, inputs=None):
        owner = str(row['batch_id']) if row is not None else '__queue__'
        inputs = inputs if inputs is not None else snapshot(row) if row is not None else {}
        identifier = digest([kind, owner, inputs])
        actions.append((priority, identifier, kind, owner, inputs))

    for row in rows:
        if row['phase'] in {'aborting', 'resetting', 'restarting'}:
            add(0, 'advance_batch', row)
            continue
        failover = merge_failover.state(conn, row['batch_id'])
        if merge_failover.pending(failover):
            add(1, 'advance_provider_failover', row, [snapshot(row), failover])
            continue
        if (row['session_id'] and row['phase'] in {'building', 'fixing'}
                and not a.ready_candidate(row) and a._batch_subagents(row)
                and row['agent_configuration'] != 'flash-luna'):
            add(3, 'observe_provider_errors', row)
        # Message acceptance is independent of queue discovery and turn idleness.
        if row['session_id'] and row['pending_prompt'] and not row['prompt_delivered']:
            add(2, 'deliver_guidance', row)
        if row['predecessor_id'] and not row['role_promoted']:
            parent = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?',
                                  (row['predecessor_id'],)).fetchone()
            add(12, 'reconcile_predecessor', row,
                [snapshot(row), snapshot(parent) if parent is not None else None])
        urgent = bool(a.ready_candidate(row) or row['phase'] in {'merging','waiting_ci','direct_merging'}
                      or row['role_promoted'] and row['phase'] == 'waiting_parent')
        if not speculation.is_speculative(row) or row['batch_id'] in reconciled:
            add(20 if not speculation.is_speculative(row) and urgent else
                30 if not speculation.is_speculative(row) else 60,
                'advance_batch', row)
    if not priority_checked:
        add(8, 'check_priority')
    if primary is None and not queue_checked:
        add(40, 'select_primary')
    if primary is not None and a.SPECULATIVE_LOOKAHEAD and speculation.candidate(a, primary):
        if speculation.child(conn, primary['batch_id']) is None:
            add(50, 'select_successor', primary)
    if not maintenance_done:
        add(80, 'maintenance')
    return sorted(actions)


def idle_summary(a, conn, transport):
    if transport.kind != 'chat':
        a.log('idle PR summary needs the threaded Slack chat transport')
        return
    groups = {'dependency blocked': [], 'rejected': [], 'draft': [], 'otherwise ineligible': []}
    rejected = {(r['number'], r['head_sha']) for r in conn.execute(
        "SELECT number,head_sha FROM automerge_github_outbox WHERE kind='reject_head' AND cancelled_at IS NULL")}
    for row in conn.execute('SELECT * FROM automerge_pr_inventory ORDER BY number'):
        item = json.loads(row['data_json'])
        if str(item.get('state')).lower() != 'open' or a._is_integration_pull(item):
            continue
        if (row['number'], row['head_sha']) in rejected:
            group = 'rejected'
        elif item.get('draft', item.get('isDraft', False)):
            group = 'draft'
        elif row['blocked_reason']:
            group = 'dependency blocked'
        else:
            group = 'otherwise ineligible'
        groups[group].append((row['number'], row['head_sha'], item.get('title', ''), row['blocked_reason']))
    identifier = digest(groups)
    saved = conn.execute('SELECT * FROM automerge_queue_summaries WHERE summary_id=?', (identifier,)).fetchone()
    if saved and saved['completed_at']:
        return
    if saved is None:
        count = sum(len(items) for items in groups.values())
        messages = [f'No eligible PRs to start. {count} open PRs' +
                    (': ' + ', '.join(f'{len(items)} {kind}' for kind, items in groups.items() if items)
                     if count else '') + '.']
        for kind, items in groups.items():
            if not items:
                continue
            text = f'*{kind.capitalize()}*'
            for number, _, title, reason in items:
                line = f'\n• <https://github.com/{a.REPO_NAME}/pull/{number}|#{number}> {a.html.escape(str(title)[:160], quote=False)}'
                if reason:
                    line += ' — ' + a.html.escape(reason[:300], quote=False)
                if len(text) + len(line) > 3000:
                    messages.append(text)
                    text = f'*{kind.capitalize()} (continued)*'
                text += line
            messages.append(text)
        last = conn.execute("SELECT thread_ts FROM automerge_batches WHERE thread_ts IS NOT NULL "
                            "AND (predecessor_id IS NULL OR role_promoted=1) ORDER BY created_at DESC LIMIT 1").fetchone()
        with conn:
            conn.execute('INSERT INTO automerge_queue_summaries '
                         '(summary_id,thread_ts,messages_json,created_at) VALUES (?,?,?,?)',
                         (identifier, last[0] if last else None, json.dumps(messages), a.utc_now()))
        saved = conn.execute('SELECT * FROM automerge_queue_summaries WHERE summary_id=?', (identifier,)).fetchone()
    thread = saved['thread_ts']
    if not thread:
        ok, thread = monitor.slack_send(transport, monitor.slack_project_prefix() + ' merge queue idle:')
        if not ok or not thread:
            return
        with conn:
            conn.execute('UPDATE automerge_queue_summaries SET thread_ts=? WHERE summary_id=?', (thread, identifier))
    messages = json.loads(saved['messages_json'])
    for index in range(saved['next_message'], len(messages)):
        ok, _ = monitor.slack_send(transport, messages[index], thread)
        if not ok:
            return
        with conn:
            conn.execute('UPDATE automerge_queue_summaries SET next_message=? WHERE summary_id=?', (index + 1, identifier))
    with conn:
        conn.execute('UPDATE automerge_queue_summaries SET completed_at=? WHERE summary_id=?', (a.utc_now(), identifier))


def run(a, conn, transport):
    attempted = set()
    deferred = set()
    priority_checked = queue_checked = maintenance_done = False
    priority_pulls = []
    reconciled = set()
    result = 0
    for _ in range(32):
        if a.OBSERVATION_DEADLINE is not None and time.monotonic() >= a.OBSERVATION_DEADLINE:
            break
        options = plan(a, conn, priority_checked=priority_checked, queue_checked=queue_checked,
                       maintenance_done=maintenance_done, reconciled=reconciled)
        wanted = {item[1] for item in options}
        with conn:
            for _, identifier, kind, owner, inputs in options:
                conn.execute('INSERT OR IGNORE INTO automerge_supervisor_actions '
                             '(action_id,kind,batch_id,input_json,updated_at) VALUES (?,?,?,?,?)',
                             (identifier, kind, owner, json.dumps(inputs), a.utc_now()))
            for old in conn.execute("SELECT action_id FROM automerge_supervisor_actions WHERE status IN ('pending','running')").fetchall():
                if old[0] not in wanted:
                    conn.execute("UPDATE automerge_supervisor_actions SET status='obsolete',updated_at=? WHERE action_id=?",
                                 (a.utc_now(), old[0]))
        if not priority_checked:
            options = [item for item in options if item[0] <= 8]
        selected = next((item for item in options if item[1] not in attempted and
                         conn.execute('SELECT next_attempt_at FROM automerge_supervisor_actions WHERE action_id=?',
                                      (item[1],)).fetchone()[0] <= time.time()), None)
        if selected is None:
            # Spend spare read budget on a cold fetch only after independent
            # actions have advanced. A quick result can land in this same poll.
            remaining = (a.OBSERVATION_DEADLINE - time.monotonic()
                         if a.OBSERVATION_DEADLINE is not None else 0)
            if deferred and a.ANCESTRY_CACHE is not None and remaining > 1:
                if a.ANCESTRY_CACHE.wait_for_fetch(min(10, remaining - 1)):
                    attempted.difference_update(deferred)
                    deferred.clear()
                    continue
            break
        _, identifier, kind, owner, _ = selected
        attempted.add(identifier)
        with conn:
            conn.execute("UPDATE automerge_supervisor_actions SET status='running',attempts=attempts+1,updated_at=? "
                         "WHERE action_id=?", (a.utc_now(), identifier))
        try:
            row = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (owner,)).fetchone()
            if kind == 'deliver_guidance':
                a.deliver_pending_prompt(conn, transport, row)
            elif kind == 'observe_provider_errors':
                merge_failover.probe(a, conn, row)
            elif kind == 'advance_provider_failover':
                merge_failover.advance(a, conn, row)
            elif kind == 'check_priority':
                # One cheap listing avoids global discovery on an ordinary tick.
                if any(a._priority_flags(a._labels(item))[0] for item in a.list_open_pull_requests()
                       if not a._is_integration_pull(item)):
                    priority_pulls = a.select_eligible_pull_requests(conn=conn, priority_only=True)
                primary = a.active_batch(conn)
                if primary is not None:
                    a.preempt_batch_for_priority(conn, transport, primary, priority_pulls)
                priority_checked = True
            elif kind == 'reconcile_predecessor':
                if speculation.reconcile(a, conn, transport, row, priority_pulls=priority_pulls):
                    reconciled.add(owner)
            elif kind == 'advance_batch':
                if speculation.is_speculative(row) and row['phase'] == 'waiting_parent':
                    speculation.recheck_waiting(a, conn, transport, row)
                else:
                    a.process_batch(conn, transport, owner)
            elif kind == 'select_primary':
                pulls = a.select_eligible_pull_requests(conn=conn)
                a.report_dependency_blocks(conn, transport)
                if pulls:
                    a.create_selected_batch(conn, pulls, a.current_master_sha())
                else:
                    idle_summary(a, conn, transport)
                queue_checked = True
            elif kind == 'select_successor':
                speculation.launch_child(a, conn, row)
                a.report_dependency_blocks(conn, transport)
            elif kind == 'maintenance':
                merge_failover.notify(a, conn, transport)
                a.retry_pending_notifications(conn, transport)
                a.retry_pending_aborted_outcomes(conn, transport)
                a.enqueue_active_membership_labels(conn)
                a.retry_github_outbox(conn, transport)
                monitor.update_known_failures(conn, transport)
                a.check_pending_suspensions(conn, transport)
                for completed in conn.execute("SELECT * FROM automerge_batches WHERE status='completed' "
                                              "AND outcome_posted=0 AND COALESCE(terminal_status,'')<>'aborted'").fetchall():
                    a.finish_batch(conn, transport, completed)
                maintenance_done = True
        except git_ancestry.Deferred as exc:
            deferred.add(identifier)
            with conn:
                conn.execute("UPDATE automerge_supervisor_actions SET status='pending',last_error=?,updated_at=? WHERE action_id=?",
                             (str(exc), a.utc_now(), identifier))
            a.log(f'{kind} deferred: {exc}')
            # Other independent actions can advance while history fetches run.
            continue
        except (a.AutomergeError, monitor.CommandError, monitor.MjError,
                OSError, RuntimeError, ValueError, a.sqlite3.Error) as exc:
            reason = exc.reason if isinstance(exc, (a.AutomergeError, monitor.MjError)) else 'automerge_failed'
            details = str(exc)
            if kind in {'observe_provider_errors', 'advance_provider_failover'}:
                reason = 'mj_provider_failover_failed'
                details = f"Bedrock failover supervision for session {row['session_id']}: {exc}"
            with conn:
                conn.execute("UPDATE automerge_supervisor_actions SET status='pending',last_error=?,updated_at=?,"
                             "next_attempt_at=? WHERE action_id=?",
                             (str(exc), a.utc_now(), time.time() + 60, identifier))
            a.notify_blocked_once(conn, transport, owner, reason, details)
            a.log(f'{kind} blocked ({reason}): {exc}')
            result = 4
            if kind == 'check_priority':
                break
            continue
        with conn:
            conn.execute("UPDATE automerge_supervisor_actions SET status='complete',last_error=NULL,next_attempt_at=0,"
                         "updated_at=? WHERE action_id=?", (a.utc_now(), identifier))
    return result
