"""One-batch lookahead, with durable checkpoints and reusable mj environments.

The scheduler passes its module as ``a`` so the cron script and imported test
module use the same authentication, configuration and subprocess seams.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
import json
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path
import read_budget


def get(row, name, default=None):
    return row[name] if name in row.keys() else default


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ensure_schema(conn, ensure_column):
    for name, declaration in (
        ("candidate_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("predecessor_id", "TEXT"),
        ("role_promoted", "INTEGER NOT NULL DEFAULT 0"),
        ("predecessor_candidate_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("attempt_generation", "INTEGER NOT NULL DEFAULT 0"),
        ("recovery_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("promotion_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("report_after_seq", "INTEGER NOT NULL DEFAULT 0"),
    ):
        ensure_column(conn, "automerge_batches", name, declaration)


def source_revision(a, row, *, base=None):
    inputs = [row["base_sha"] if base is None else base,
              [p.as_json() for p in a.row_pulls(row)], a._excluded_source_heads(row)]
    if a.batch_target(row) != a.BASE_BRANCH:
        inputs.append(a.batch_target(row))
    generation = get(row, "attempt_generation", 0)
    if generation:
        inputs.append(generation)  # Generation zero retains legacy evidence digests.
    return digest(inputs)


def is_speculative(row):
    return bool(get(row, "predecessor_id") and not get(row, "role_promoted", 0))


def candidate(a, row, *, landed=False):
    value = json.loads(get(row, "candidate_json", "{}"))
    if (not value or value["source_revision"] != source_revision(a, row)
            or row["phase"] == "aborting"
            or (row["status"] not in {"running", "launching", "finishing"} and not landed)):
        return None
    return value


def parent_current(a, conn, row):
    if not get(row, "predecessor_id"):
        return True
    parent = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?",
                          (row["predecessor_id"],)).fetchone()
    return bool(parent is not None
                and parent['status'] in {'launching', 'running', 'finishing'}
                and parent['phase'] != 'terminal' and not parent['terminal_status'])


def reserved(a, conn, *, except_batch=None):
    return {p.number for row in conn.execute(
        "SELECT * FROM automerge_batches WHERE status IN ('launching','running','finishing')")
        if row["batch_id"] != except_batch for p in a._active_sources(row)}


def child(conn, parent_id):
    columns = {c[1] for c in conn.execute("PRAGMA table_info(automerge_batches)")}
    if "predecessor_id" not in columns:
        return None
    role_filter = "AND role_promoted=0 " if "role_promoted" in columns else ""
    return conn.execute("SELECT * FROM automerge_batches WHERE predecessor_id=? "
                        + role_filter +
                        "AND status IN ('launching','running','finishing') ORDER BY created_at LIMIT 1",
                        (parent_id,)).fetchone()


def commit_tree(a, head):
    return a.commit_tree_sha(head)


def verify_candidate(a, current, head, conn):
    if a.remote_branch_sha(current['branch']) != head:
        raise ValueError("remote batch head does not match the checkpoint")
    if not a.compare_commit_ancestry(current["base_sha"], head):
        raise ValueError("candidate does not contain its captured base")
    for p in current["sources"]:
        if not a.compare_commit_ancestry(p["head_sha"], head):
            raise ValueError(f"candidate is missing PR #{p['number']}")
    row = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (current['batch_id'],)).fetchone()
    valid, reason = a.verify_source_ancestry(row, head, conn=conn)
    if not valid:
        raise ValueError(reason)
    return commit_tree(a, head)


GUIDANCE = """Candidate checkpoints and lookahead: after completing your merges and fixes, before starting expensive validation, run mm-db candidate --revision <current revision> (add --rebuild for a rebuilt branch). This pushes only your recorded batch branch and records the committed candidate; it opens no PR. Before making further edits, withdraw it with mm-db candidate --revision <current revision> --withdraw. Register the replacement when ready. The supervisor may run the next batch's useful checks concurrently through existing mbx.

Read mm-db state: your captured base is actual master at launch. Your batch shares no source membership or unlanded work with the current primary. Merge, fix, validate and reject your own exact heads independently against that base. After local pass, record mm-db tests and mm-db ready --revision <current revision>, then wait without publishing while the predecessor is live. When it ends, you take the primary role with the same checkout and running work. If master has not moved, continue readiness and publication normally. If the supervisor queues promotion, run mm-merge --promote, resolve conflicts, and choose reruns from the actual diff against the last tested candidate under the validation policy. Reuse unaffected evidence only with its original tested SHA and an applicability reason. Record a fresh assessment for the resulting HEAD and re-register the post-merge candidate before publication. There is no unchanged-tree shortcut.

"""


def selection(a, conn, parent, *, master=None):
    if a.batch_target(parent) != a.BASE_BRANCH:
        return []
    actual = a.dependency_graph(conn, master=master or a.current_master_sha(),
                                **a.lane_discovery_scope(conn, a.BASE_BRANCH))
    occupied = reserved(a, conn)
    actual.nodes = {n: replace(node, ready=False)
                    if n in occupied or actual.target(n)[0] != a.BASE_BRANCH else node
                    for n, node in actual.nodes.items()}
    actual._eligibility.clear()  # Discovery's earlier selection predates reservations.
    return [a.dependency_pull(actual, n) for n in actual.select() if n not in occupied]


def launch_child(a, conn, parent):
    if (a.batch_target(parent) != a.BASE_BRANCH or not a.SPECULATIVE_LOOKAHEAD or is_speculative(parent)
            or not candidate(a, parent) or child(conn, parent["batch_id"])):
        return
    master = a.current_master_sha()
    pulls = selection(a, conn, parent, master=master)
    if not pulls:
        return
    require_recovery_controls(a)
    checkpoint = candidate(a, parent)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        fresh = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (parent['batch_id'],)).fetchone()
        current = candidate(a, fresh)
        if (is_speculative(fresh) or not current or current['id'] != checkpoint['id']
                or child(conn, parent['batch_id'])):
            return
        a.create_batch(conn, pulls, master, ci_mode=a.CI_MODE,
                       predecessor_id=parent['batch_id'], predecessor_candidate={})


def latest_tests(conn, identifier):
    row = conn.execute("SELECT payload_json FROM automerge_skill_events WHERE batch_id=? AND kind='tests' "
                       "ORDER BY rowid DESC LIMIT 1", (identifier,)).fetchone()
    return json.loads(row[0]) if row else None


def invalidate(a, conn, row):
    if row["phase"] == "resetting":
        return
    old = {"kind": "reset", "stage": "stop", "old_base": row["base_sha"],
           "old_candidate": json.loads(row["candidate_json"]),
           "old_tests": latest_tests(conn, row["batch_id"]),
           "old_generation": row["attempt_generation"]}
    with conn:
        conn.execute("UPDATE automerge_batches SET attempt_generation=attempt_generation+1,phase='resetting',"
                     "candidate_json='{}',promotion_json='{}',recovery_json=?,pending_prompt=NULL,prompt_delivered=0,"
                     "ready_json='{}',prompt_command_id=NULL,agent_final_message='',turn_started_at=NULL WHERE batch_id=?",
                     (json.dumps(old), row["batch_id"]))


def mj(a, args):
    result = a.monitor.require_mj_success(args, timeout=60)
    return json.loads(result or "{}")


def send_once(a, session, text, command_id):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".prompt") as handle:
        handle.write(text)
        handle.flush()
        return mj(a, ["prompt", "--session", session, "--command-id", command_id,
                      "--prompt-file", handle.name, "--json"])


def mj_api(a, endpoint):
    info = mj(a, ["api-info", "--json"])
    token = Path(info["token_path"]).read_text().strip()
    request = urllib.request.Request(info["base_url"].rstrip("/") + endpoint,
                                     headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=read_budget.timeout(30)) as response:
        return json.load(response)


def require_recovery_controls(a):
    """Refuse lookahead until both CLI and daemon expose safe recovery."""
    help_text = a.monitor.require_mj_success(['prompt', '--help'])
    a.monitor.require_mj_success(['clear-queue', '--help'])
    info = mj(a, ['api-info', '--json'])
    token = Path(info['token_path']).read_text().strip()
    request = urllib.request.Request(
        info['base_url'].rstrip('/') + '/sessions/mm-capability-probe/queued-prompts/clear',
        headers={'Authorization': 'Bearer ' + token}, method='OPTIONS')
    try:
        with urllib.request.urlopen(request, timeout=read_budget.timeout(10)) as response:
            supported = 'POST' in response.headers.get('Allow', '')
    except urllib.error.HTTPError as exc:
        supported = exc.code == 405 and 'POST' in exc.headers.get('Allow', '')
    if '--command-id' not in help_text or not supported:
        raise a.AutomergeError('mj recovery controls are unavailable; lookahead remains pending',
                               reason='mj_recovery_unavailable')


def command_outcome(a, session, command_id, cursor=0):
    """Read a bounded slice of existing durable SSE command outcomes."""
    info = mj(a, ['api-info', '--json'])
    token = Path(info['token_path']).read_text().strip()
    request = urllib.request.Request(
        info['base_url'].rstrip('/') + f'/events?session_id={session}&after_seq={cursor}',
        headers={'Authorization': 'Bearer ' + token})
    deadline = time.monotonic() + 2
    try:
        with urllib.request.urlopen(request, timeout=read_budget.timeout(2)) as response:
            while time.monotonic() < deadline:
                line = response.readline(8 * 1024 * 1024)
                if not line:
                    break
                if line.startswith(b'data:'):
                    event = json.loads(line[5:])
                    cursor = max(cursor, int(event.get('seq', cursor)))
                    result = event.get('data') or {}
                    if event.get('type') == 'command_ended' and result.get('command_id') == command_id:
                        return result, cursor
    except TimeoutError:
        pass
    return None, cursor


def clear_id(row, recovery):
    suffix = f"-retry-{recovery['clear_retry']}" if recovery.get('clear_retry') else ''
    return f"mm-clear-{row['batch_id']}-{row['attempt_generation']}" + suffix


def failed_clear(a, conn, row, recovery, result):
    recovery.update(stage='stop', clear_retry=recovery.get('clear_retry', 0) + 1)
    recovery.pop('clear_cursor', None)
    recovery.pop('boundary_seq', None)
    recovery.pop('clear_success', None)
    with conn:
        conn.execute('UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?',
                     (json.dumps(recovery), row['batch_id']))
    raise a.AutomergeError('context clear failed: ' + str(result.get('message') or result['outcome']),
                           reason='mj_clear_failed')


def stop_work(a, row):
    session = row["session_id"]
    mj(a, ["clear-queue", "--session", session, "--json"])
    a.monitor.interrupt_turn(session)
    # mj child sessions share this checkout; close their turns before resetting it.
    children = mj_api(a, f"/sessions/{session}/subagents")
    children_stopped = True
    for agent in children.get("subagents", []):
        state = agent['session']
        identifier = state['id']
        if not a.monitor.session_is_stopped(state) and state.get('state') != 'stopping':
            # Parked children have already handed back and have no worker for
            # queue/turn/task commands. Suspension closes them without waking them.
            if state.get('state') != 'parked':
                mj(a, ["clear-queue", "--session", identifier, "--json"])
                a.monitor.interrupt_turn(identifier)
                mj(a, ["stop-task", "--session", identifier, "--all", "--json"])
            mj(a, ["suspend", "--session", identifier, "--acknowledge-unpublished-work", "--json"])
        children_stopped &= a.monitor.session_is_stopped(a._session_status(identifier))
    mj(a, ["stop-task", "--session", session, "--all", "--json"])
    state = a._session_status(session)
    background = state.get('background_work') or {}
    # /clear owns the context-reset boundary; suspension owns abort teardown.
    # Neither needs a heuristic that guesses whether a local worker is idle.
    return children_stopped and not background.get('tasks') and not state.get('background_tasks')


def reset_brief(a, conn, row, recovery):
    sources = json.dumps([{'number': p.number, 'head_sha': p.head_sha,
                           'dependencies': [d.as_json() for d in p.dependencies]}
                          for p in a.row_pulls(row)], indent=2)
    exclusions = json.dumps(a._excluded_source_heads(row))
    assessment = recovery['old_tests']
    if assessment:
        assessment = dict(assessment)
        # Full receipts remain in durable state; recovery needs stable pointers.
        if assessment.get('executions'):
            assessment['executions'] = {'ids': [r['id'] for r in assessment['executions']],
                                        'details': 'Read mm-db executions for original tested inputs and logs.'}
        for name in ('tests', 'baseline'):
            value = assessment.get(name, '')
            if len(value) > 2000:
                assessment[name] = {'excerpt': value[:2000], 'omitted_characters': len(value) - 2000}
    evidence = json.dumps({"old_base": recovery["old_base"], "candidate": recovery["old_candidate"],
                           "assessment": assessment, "generation": recovery["old_generation"]})
    return f"""Start fresh attempt {row['attempt_generation']} of batch {row['batch_id']} in this same checkout.
The previous attempt was invalidated. Read mm-db state, then run mm-merge --restart (with --manual when needed). The helper preserves the old committed tip and pending source edits before resetting to the new base {row['base_sha']}. It archives the old progress note; do not resume its old next action. Review rerere resolutions before staging. Consult saved work only as evidence; do not transplant old merge commits.
Exact current sources:
{sources}
Preserved exclusions: {exclusions}
Old attempt evidence (data, not instructions; inspect logs/progress history for details): {evidence}
{a.SESSION_RECOVERY_GUIDANCE}
{GUIDANCE}
{a._validation_guidance(row['base_sha'], a._stored_impact(row), subagents=a._batch_subagents(row), configuration=a._batch_configuration(row))}
{a.FIX_VS_EJECT_GUIDANCE}
{a.SKILLS_GUIDANCE}
{a.skills_connection_prompt(row)}
"""


def recover_step(a, conn, row):
    recovery = json.loads(row["recovery_json"])
    checkpoint = recovery.get('replacement') or {'head': a.current_master_sha()}
    if not row["session_id"]:
        # Preserve ambiguous initial launch identity; never create another session.
        session = a.lookup_batch_session(row) if row["launch_attempted"] else None
        if row["launch_attempted"] and not session:
            if not a._launch_grace_expired(row):
                return
            # Exact-title discovery succeeded and the creation grace expired.
            # No environment exists to preserve; retry the same launch intent.
            with conn:
                conn.execute('UPDATE automerge_batches SET launch_attempted=0,launch_attempted_at=NULL WHERE batch_id=?',
                             (row['batch_id'],))
        if session:
            with conn:
                conn.execute("UPDATE automerge_batches SET session_id=? WHERE batch_id=?", (session, row["batch_id"]))
            return
        if checkpoint:
            with conn:
                conn.execute("UPDATE automerge_batches SET base_sha=?,predecessor_candidate_json=?,phase='building',"
                             "recovery_json='{}',predecessor_id=CASE WHEN ? THEN NULL ELSE predecessor_id END WHERE batch_id=?",
                             (checkpoint["head"], '{}', bool(recovery.get('replacement') and not recovery.get('upgrade_reset')), row["batch_id"]))
        return
    if recovery["stage"] == "stop":
        if not stop_work(a, row):
            return
        page = mj(a, ['transcript', '--session', row['session_id'], '--after-seq', '0', '--json'])
        recovery['clear_cursor'] = int(page.get('latest_seq', row['report_after_seq']))
        recovery["stage"] = "clear"
        with conn:
            conn.execute("UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?", (json.dumps(recovery), row["batch_id"]))
        return
    if recovery["stage"] == "clear":
        receipt = send_once(a, row["session_id"], "/clear", clear_id(row, recovery))
        recovery["clear_turn"] = receipt["turn_id"]
        recovery["stage"] = "cleared"
        with conn:
            conn.execute("UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?", (json.dumps(recovery), row["batch_id"]))
        return
    if recovery["stage"] == "cleared":
        # Typed clear has no ordinary agent turn. Its durable transcript divider
        # is the acknowledgement and the lower bound for this attempt's reports.
        if not recovery.get('boundary_seq'):
            cursor = int(recovery.get('clear_cursor', row['report_after_seq']))
            page = mj(a, ["transcript", "--session", row["session_id"], "--after-seq", str(cursor), "--json"])
            boundary = [item for item in page.get("items", []) if str(item.get("stable_id", "")).endswith(
                clear_id(row, recovery))]
            if not boundary:
                recovery['clear_cursor'] = int(page.get('next_after_seq', cursor))
                if not recovery.get('clear_success'):
                    result, recovery['api_cursor'] = command_outcome(
                        a, row['session_id'], clear_id(row, recovery), recovery.get('api_cursor', 0))
                    if result and result['outcome'] != 'succeeded':
                        failed_clear(a, conn, row, recovery, result)
                    recovery['clear_success'] = bool(result)
                with conn:
                    conn.execute("UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?", (json.dumps(recovery), row['batch_id']))
                return
            recovery['boundary_seq'] = max(int(x['seq']) for x in boundary)
            with conn:
                conn.execute("UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?", (json.dumps(recovery), row['batch_id']))
        base = checkpoint["head"]
        impact = a.run_ci_impact(base, [p.head_sha for p in a.row_pulls(row)])
        with conn:
            conn.execute("UPDATE automerge_batches SET base_sha=?,predecessor_candidate_json=?,report_after_seq=?,"
                         "validation_impact_json=? WHERE batch_id=?",
                         (base, '{}', recovery['boundary_seq'], json.dumps(impact), row["batch_id"]))
            latest = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (row["batch_id"],)).fetchone()
            recovery["stage"] = "restart"
            conn.execute("UPDATE automerge_batches SET recovery_json=?,pending_prompt=?,phase='restarting',"
                         "predecessor_id=CASE WHEN ? THEN NULL ELSE predecessor_id END WHERE batch_id=?",
                         (json.dumps(recovery), reset_brief(a, conn, latest, recovery),
                          bool(recovery.get('replacement') and not recovery.get('upgrade_reset')), row["batch_id"]))
        return
    if recovery["stage"] == "restart":
        if not recovery.get('restart_attempted'):
            recovery['restart_attempted'] = True
            with conn:
                conn.execute('UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?',
                             (json.dumps(recovery), row['batch_id']))
        send_once(a, row["session_id"], row["pending_prompt"], f"mm-restart-{row['batch_id']}-{row['attempt_generation']}")
        with conn:
            conn.execute("UPDATE automerge_batches SET phase='building',status='running',recovery_json='{}',"
                         "pending_prompt=NULL,prompt_delivered=0,turn_started_at=?,"
                         "predecessor_id=CASE WHEN ? THEN NULL ELSE predecessor_id END WHERE batch_id=?",
                         (a.utc_now(), bool(recovery.get('replacement') and not recovery.get('upgrade_reset')), row["batch_id"]))


def recover(a, conn, row):
    # Persist every transition, but advance ready stages in this tick rather
    # than adding five minutes of latency for each successful local operation.
    for _ in range(6):
        before = json.loads(row['recovery_json'])
        recover_step(a, conn, row)
        latest = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
        after = json.loads(latest['recovery_json'])
        if latest['phase'] not in {'resetting', 'restarting'}:
            return
        progressed = before.get('stage') != after.get('stage') or (not before.get('clear_success') and after.get('clear_success'))
        if not progressed or (a.OBSERVATION_DEADLINE is not None and time.monotonic() >= a.OBSERVATION_DEADLINE):
            return
        row = latest


def landed_base(a, conn, row, parent):
    """Promotion always incorporates the freshly observed actual master tip."""
    return a.current_master_sha()


def promote_role(a, conn, row, parent):
    """Release the primary slot without changing running validation inputs."""
    with conn:
        conn.execute("UPDATE automerge_batches SET role_promoted=1 WHERE batch_id=?", (row['batch_id'],))


def promote(a, conn, transport, row, parent):
    """Queue master incorporation after the agent's passing handoff."""
    if (row['phase'] != 'waiting_parent' or not a.ready_candidate(row)
            or (get(row, 'predecessor_id') and parent_current(a, conn, row))):
        return
    master = landed_base(a, conn, row, parent)
    if master == row['base_sha']:
        with conn:
            conn.execute("UPDATE automerge_batches SET predecessor_id=NULL,role_promoted=1 WHERE batch_id=?",
                         (row['batch_id'],))
        return
    evidence = latest_tests(conn, row["batch_id"])
    checkpoint = json.loads(row['candidate_json'])
    promotion = {"stage": "incorporating", "base": master, "old_base": row["base_sha"],
                 "previous_tests": evidence, "old_candidate": checkpoint}
    impact = a.run_ci_impact(master, [p.head_sha for p in a.row_pulls(row)])
    prompt = f"""Your batch is now primary. Read mm-db state and run mm-merge --promote to merge fresh master {master}. Resolve conflicts and choose reruns from the actual diff against the last tested candidate under the validation policy. Reuse unaffected results only with original tested SHAs and applicability reasons. Record a fresh assessment for the resulting committed HEAD and re-register the post-merge head with mm-db candidate. Then publish through mm-autopr (use --rebuild if its push needs a lease) and finish with mm-db report. Normal {_mode(a, row)} gates apply. There is no unchanged-tree shortcut.
{a._validation_guidance(master, impact, subagents=a._batch_subagents(row), configuration=a._batch_configuration(row))}
{GUIDANCE}
"""
    import mm_service  # Lazy: the service imports the relationship engine.
    with conn:
        # Match service candidate withdrawal receipts, retaining the old identity.
        withdrawal = {"id": a.uuid.uuid4().hex, "revision": mm_service.state(conn, row["batch_id"])["revision"],
                      "source_revision": source_revision(a, row), "candidate": checkpoint or None}
        event_id = digest(['candidate_withdrawal', withdrawal])
        conn.execute("INSERT OR IGNORE INTO automerge_skill_events VALUES (?,?,?,?,?)",
                     (row['batch_id'], event_id, 'candidate_withdrawal', json.dumps(withdrawal), a.utc_now()))
        conn.execute("UPDATE automerge_batches SET predecessor_id=NULL,role_promoted=1,base_sha=?,"
                     "promotion_json=?,phase='fixing',pending_prompt=?,prompt_delivered=0,"
                     "candidate_json='{}',ready_json='{}',prompt_command_id=?,turn_started_at=NULL,"
                     "validation_impact_json=? WHERE batch_id=?",
                     (master, json.dumps(promotion), prompt, 'mm-promote-' + a.uuid.uuid4().hex,
                      json.dumps(impact), row["batch_id"]))


def _mode(a, row):
    return a._batch_ci_mode(row)


def reconcile(a, conn, transport, row, *, priority_pulls=None):
    """Reconcile scheduling roles; sibling edits never invalidate useful work."""
    if not a._active_sources(row):
        return True
    parent = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?",
                          (row['predecessor_id'],)).fetchone()
    if is_speculative(row) and row['phase'] not in {'resetting', 'restarting'}:
        master = a.current_master_sha()
        if not a.compare_commit_ancestry(row['base_sha'], master):
            # Legacy candidate pins are the sole automatic reset. Persist the
            # replacement before recovery; subsequent ticks cannot reset again.
            invalidate(a, conn, row)
            saved = json.loads(conn.execute('SELECT recovery_json FROM automerge_batches WHERE batch_id=?',
                                            (row['batch_id'],)).fetchone()[0])
            saved.update(replacement={'head': master}, upgrade_reset=True)
            with conn:
                conn.execute('UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?',
                             (json.dumps(saved), row['batch_id']))
            return True
    if not parent_current(a, conn, row) and not get(row, 'role_promoted', 0):
        promote_role(a, conn, row, parent)
    return True


def tick(a, conn, transport, parent):
    """Compatibility entry point; production schedules these actions separately."""
    # A fixed worklist replaces recursive promotion/selection/observation.
    for _ in range(2):
        launch_child(a, conn, parent)
        row = child(conn, parent['batch_id'])
        if row is None:
            return
        if not reconcile(a, conn, transport, row):
            return
        row = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
        if row['status'] == 'completed':
            return
        if row['phase'] != 'waiting_parent' or get(row, 'role_promoted', 0) or not a._active_sources(row):
            a.process_batch(conn, transport, row['batch_id'])
        else:
            recheck_waiting(a, conn, transport, row)
        row = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
        if not get(row, 'role_promoted', 0):
            return
        parent = row


def recheck_waiting(a, conn, transport, row):
    if row['phase'] == 'waiting_parent':
        keep, removed = a._recheck_sources(a._active_sources(row), conn=conn, batch_id=row['batch_id'])
        if removed:
            a._append_removed(conn, row['batch_id'], removed)
            latest = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
            a._rebuild_or_finish(conn, transport, latest, keep, 'speculative source changed while waiting')
            return
        # Discovery can refresh dependency metadata without removing a source.
        # The row read before discovery can still show a now-stale handoff.
        latest = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
        if not a.ready_candidate(latest):
            a._queue_missing_handoff(
                conn, latest, 'the candidate handoff became stale while waiting for the predecessor',
            )


def finished(a, conn, transport, row, final):
    """Legacy report recovery; live successors hand off through mm-db ready."""
    a._record_agent_exclusions(conn, row, final)
    row = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (row["batch_id"],)).fetchone()
    if not a._active_sources(row):
        a._end_empty_batch(conn, transport, row, "No speculative sources remain.")
        return
    keep, removed = a._recheck_sources(a._active_sources(row), conn=conn, batch_id=row['batch_id'])
    if removed:
        a._append_removed(conn, row['batch_id'], removed)
        row = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
        a._rebuild_or_finish(conn, transport, row, keep, 'speculative sources changed before local assessment')
        return
    evidence = latest_tests(conn, row["batch_id"])
    checkpoint = candidate(a, row)
    if (evidence and evidence["verdict"] == "pass" and checkpoint
            and evidence["head"] == checkpoint["head"] and evidence["source_revision"] == source_revision(a, row)):
        with conn:
            receipt = a.make_ready_receipt(row, evidence, "ready", notes=final)
            conn.execute("UPDATE automerge_batches SET ready_json=? WHERE batch_id=?",
                         (json.dumps(receipt), row["batch_id"]))
            conn.execute("INSERT INTO automerge_skill_events VALUES (?,?,?,?,?)",
                         (row["batch_id"], receipt["id"], "ready", json.dumps(receipt), a.utc_now()))
            conn.execute("UPDATE automerge_batches SET phase='waiting_parent',pending_prompt=NULL,"
                         "prompt_delivered=0,turn_started_at=NULL WHERE batch_id=?", (row["batch_id"],))
    else:
        a._request_rebuild(conn, row, a._active_sources(row), "speculative local assessment needs completion; do not publish until promotion")
