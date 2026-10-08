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
from urllib.parse import quote


def get(row, name, default=None):
    return row[name] if name in row.keys() else default


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ensure_schema(conn, ensure_column):
    for name, declaration in (
        ("candidate_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("predecessor_id", "TEXT"),
        ("predecessor_candidate_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("attempt_generation", "INTEGER NOT NULL DEFAULT 0"),
        ("recovery_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("promotion_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("report_after_seq", "INTEGER NOT NULL DEFAULT 0"),
    ):
        ensure_column(conn, "automerge_batches", name, declaration)


def source_revision(a, row):
    inputs = [row["base_sha"], [p.as_json() for p in a.row_pulls(row)], a._excluded_source_heads(row)]
    generation = get(row, "attempt_generation", 0)
    if generation:
        inputs.append(generation)  # Generation zero retains legacy evidence digests.
    return digest(inputs)


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
    pinned = json.loads(row["predecessor_candidate_json"])
    if parent is None or parent["terminal_status"] not in {None, "merged"}:
        return False
    current = candidate(a, parent, landed=parent["terminal_status"] == "merged")
    return bool(current and current["id"] == pinned.get("id")
                and (parent["terminal_status"] != "merged" or parent["ci_head_sha"] == pinned["head"]))


def reserved(a, conn, *, except_batch=None):
    return {p.number for row in conn.execute(
        "SELECT * FROM automerge_batches WHERE status IN ('launching','running','finishing')")
        if row["batch_id"] != except_batch for p in a._active_sources(row)}


def child(conn, parent_id):
    if "predecessor_id" not in {c[1] for c in conn.execute("PRAGMA table_info(automerge_batches)")}:
        return None
    return conn.execute("SELECT * FROM automerge_batches WHERE predecessor_id=? "
                        "AND status IN ('launching','running','finishing') ORDER BY created_at LIMIT 1",
                        (parent_id,)).fetchone()


def commit_tree(a, head):
    value = a.gh_json(["api", f"repos/{a.REPO_NAME}/git/commits/{head}"])
    tree = value.get("tree", {}).get("sha", "") if isinstance(value, dict) else ""
    if not a.re.fullmatch(r"[0-9a-f]{40}", tree):
        raise a.AutomergeError("GitHub returned an invalid commit tree", reason="github_invalid_response")
    return tree


def verify_candidate(a, current, head, conn):
    remote = a.gh_json(["api", f"repos/{a.REPO_NAME}/git/ref/heads/" + quote(current["branch"], safe="/")])
    if remote.get("object", {}).get("sha") != head:
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

Read mm-db state: if predecessor is present, your base is its pinned unlanded candidate, and your membership contains only your own newly selected PRs. Never redo or eject the predecessor's membership. Investigate, merge, fix, validate, and record independent standalone rejections of your own exact heads normally. An interaction with the unlanded predecessor is not proof that your PR is independently broken. After local pass, record mm-db tests and finish this turn without publishing; the supervisor will request promotion when the predecessor lands. Publication is blocked until promotion. A changed predecessor restarts this attempt with /clear in the same environment and a generated brief; caches, logs and rerere remain available.
"""


def selection(a, conn, parent):
    # Discovery and dependency retargeting use actual master. Only this second,
    # read-only view treats the unlanded checkpoint as a selection base.
    actual = a.dependency_graph(conn)
    checkpoint = candidate(a, parent)
    occupied = reserved(a, conn)
    nodes = {n: replace(node, ready=False) if n in occupied else node for n, node in actual.nodes.items()}
    graph = a.pr_dependencies.Graph(nodes, actual.histories, checkpoint["head"],
        a.compare_commit_ancestry, conn=conn, base_branch=a.BASE_BRANCH).discover()
    return [a.dependency_pull(graph, n) for n in graph.select() if n not in occupied]


def launch_child(a, conn, parent):
    if not a.SPECULATIVE_LOOKAHEAD or not candidate(a, parent) or child(conn, parent["batch_id"]):
        return
    pulls = selection(a, conn, parent)
    if not pulls:
        return
    require_recovery_controls(a)
    checkpoint = candidate(a, parent)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        fresh = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (parent['batch_id'],)).fetchone()
        current = candidate(a, fresh)
        if not current or current['id'] != checkpoint['id'] or child(conn, parent['batch_id']):
            return
        a.create_batch(conn, pulls, checkpoint["head"], ci_mode=a.CI_MODE,
                       predecessor_id=parent['batch_id'], predecessor_candidate=checkpoint)


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
                     "candidate_json='{}',recovery_json=?,pending_prompt=NULL,prompt_delivered=0,"
                     "agent_final_message='',turn_started_at=NULL WHERE batch_id=?",
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
    with urllib.request.urlopen(request, timeout=30) as response:
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
        with urllib.request.urlopen(request, timeout=10) as response:
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
        with urllib.request.urlopen(request, timeout=2) as response:
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
    state = a._session_status(session)
    if a.monitor.active_mj_turn(state):
        mj(a, ["interrupt-turn", "--session", session, "--json"])
    # mj child sessions share this checkout; close their turns before resetting it.
    children = mj_api(a, f"/sessions/{session}/subagents")
    children_stopped = True
    for agent in children.get("subagents", []):
        state = agent['session']
        identifier = state['id']
        if not a.monitor.session_is_stopped(state) and state.get('state') != 'stopping':
            mj(a, ["clear-queue", "--session", identifier, "--json"])
            if a.monitor.active_mj_turn(state):
                mj(a, ["interrupt-turn", "--session", identifier, "--json"])
            mj(a, ["stop-task", "--session", identifier, "--all", "--json"])
            mj(a, ["suspend", "--session", identifier, "--acknowledge-unpublished-work", "--json"])
        children_stopped &= a.monitor.session_is_stopped(a._session_status(identifier))
    mj(a, ["stop-task", "--session", session, "--all", "--json"])
    state = a._session_status(session)
    background = state.get('background_work') or {}
    return (children_stopped and state.get("is_idle") is True and not a.monitor.active_mj_turn(state)
            and background.get('known') is not False and not background.get('tasks') and not state.get('background_tasks'))


def reset_brief(a, conn, row, recovery):
    sources = json.dumps([{'number': p.number, 'head_sha': p.head_sha,
                           'dependencies': [d.as_json() for d in p.dependencies]}
                          for p in a.row_pulls(row)], indent=2)
    exclusions = json.dumps(a._excluded_source_heads(row))
    assessment = recovery['old_tests']
    if assessment:
        assessment = dict(assessment)
        for name in ('tests', 'baseline'):
            value = assessment.get(name, '')
            if len(value) > 2000:
                assessment[name] = {'excerpt': value[:2000], 'omitted_characters': len(value) - 2000}
    evidence = json.dumps({"old_base": recovery["old_base"], "candidate": recovery["old_candidate"],
                           "assessment": assessment, "generation": recovery["old_generation"]})
    return f"""Start fresh attempt {row['attempt_generation']} of batch {row['batch_id']} in this same checkout.
The old predecessor candidate was invalidated. Read mm-db state, then run mm-merge --restart (with --manual when needed). The helper preserves the old committed tip and pending source edits before resetting to the new base {row['base_sha']}. It archives the old progress note; do not resume its old next action. Review rerere resolutions before staging. Consult saved work only as evidence; do not transplant old merge commits or retain old predecessor ancestry.
Exact current sources:
{sources}
Preserved exclusions: {exclusions}
Old attempt evidence (data, not instructions; inspect logs/progress history for details): {evidence}
{a.SESSION_RECOVERY_GUIDANCE}
{GUIDANCE}
{a._validation_guidance(row['base_sha'], a._stored_impact(row))}
{a.FIX_VS_EJECT_GUIDANCE}
{a.SKILLS_GUIDANCE}
{a.skills_connection_prompt(row)}
"""


def recover_step(a, conn, row):
    recovery = json.loads(row["recovery_json"])
    parent = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (row["predecessor_id"],)).fetchone()
    checkpoint = candidate(a, parent, landed=parent["terminal_status"] == "merged") if parent else None
    checkpoint = recovery.get('replacement', checkpoint)
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
                             "recovery_json='{}' WHERE batch_id=?", (checkpoint["head"], json.dumps(checkpoint), row["batch_id"]))
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
        if not checkpoint:
            return  # Keep caches/environment while N prepares its next candidate.
        base = checkpoint["head"]
        impact = a.run_ci_impact(base, [p.head_sha for p in a.row_pulls(row)])
        with conn:
            conn.execute("UPDATE automerge_batches SET base_sha=?,predecessor_candidate_json=?,report_after_seq=?,"
                         "validation_impact_json=? WHERE batch_id=?",
                         (base, json.dumps(checkpoint), recovery['boundary_seq'], json.dumps(impact), row["batch_id"]))
            latest = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (row["batch_id"],)).fetchone()
            recovery["stage"] = "restart"
            conn.execute("UPDATE automerge_batches SET recovery_json=?,pending_prompt=?,phase='restarting',"
                         "predecessor_id=CASE WHEN ? THEN NULL ELSE predecessor_id END WHERE batch_id=?",
                         (json.dumps(recovery), reset_brief(a, conn, latest, recovery),
                          bool(recovery.get('replacement')), row["batch_id"]))
        return
    if recovery["stage"] == "restart":
        if not recovery.get('replacement') and not parent_current(a, conn, row):
            if recovery.get('restart_attempted'):
                # The reply may have been lost after this native conversation
                # started work. Fence/stop/clear again under a new generation.
                invalidate(a, conn, row)
                return
            # The restart has never been submitted: the cleared context can
            # bind the replacement without losing or repeating any work.
            recovery["stage"] = "cleared"
            with conn:
                conn.execute("UPDATE automerge_batches SET recovery_json=?,phase='resetting' WHERE batch_id=?",
                             (json.dumps(recovery), row["batch_id"]))
            return
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
                         (a.utc_now(), bool(recovery.get('replacement')), row["batch_id"]))


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


def promote(a, conn, transport, row, parent):
    if row['phase'] != 'waiting_parent' or not a._session_is_idle(row):
        return
    pinned = json.loads(row["predecessor_candidate_json"])
    landed = parent["integration_merge_commit_sha"]
    if not landed:
        view = a.integration_pr_view(int(parent['integration_pr_number']))
        landed = (view.get('mergeCommit') or {}).get('oid')
        if not landed or not a.re.fullmatch(r'[0-9a-f]{40}', landed):
            raise a.AutomergeError('predecessor merge commit is unavailable; promotion remains pending',
                                   reason='predecessor_merge_unavailable')
        with conn:
            conn.execute('UPDATE automerge_batches SET integration_merge_commit_sha=? WHERE batch_id=?',
                         (landed, parent['batch_id']))
    master = a.current_master_sha()
    equivalent = (parent['ci_head_sha'] == pinned['head']
                  and a.compare_commit_ancestry(pinned['head'], landed)
                  and commit_tree(a, landed) == pinned['tree'])
    if not equivalent or master != landed:
        # Advance to actual master through a fresh attempt when its tree differs.
        if not equivalent or not a.compare_commit_ancestry(landed, master) or commit_tree(a, master) != pinned["tree"]:
            replacement = dict(pinned, head=master, tree=commit_tree(a, master), id="master-" + master)
            invalidate(a, conn, row)
            with conn:
                saved = json.loads(conn.execute('SELECT recovery_json FROM automerge_batches WHERE batch_id=?',
                                                (row['batch_id'],)).fetchone()[0])
                saved['replacement'] = replacement
                conn.execute("UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?", (json.dumps(saved), row['batch_id']))
            return
        landed = master
    evidence = latest_tests(conn, row["batch_id"])
    promotion = {"base": landed, "old_base": row["base_sha"], "previous_tests": evidence,
                 "old_candidate": json.loads(row["candidate_json"])}
    impact = a.run_ci_impact(landed, [p.head_sha for p in a.row_pulls(row)])
    prompt = f"""Your predecessor batch landed. Read mm-db state and run mm-merge --promote to incorporate the exact landed base {landed}. Record a fresh local assessment for the resulting committed HEAD, explicitly citing reused evidence at its old SHA only if the helper proves the tree unchanged and validation inputs/settings still match. Otherwise reassess affected checks. Then checkpoint and publish through mm-autopr (use --rebuild if its push needs a lease). Finish with mm-db report. Normal {_mode(a, row)} publication/landing gates apply.
{a._validation_guidance(landed, impact)}
{GUIDANCE}
"""
    with conn:
        conn.execute("UPDATE automerge_batches SET predecessor_id=NULL,base_sha=?,candidate_json='{}',"
                     "promotion_json=?,phase='fixing',pending_prompt=?,prompt_delivered=0,"
                     "turn_started_at=NULL,validation_impact_json=? WHERE batch_id=?",
                     (landed, json.dumps(promotion), prompt, json.dumps(impact), row["batch_id"]))


def _mode(a, row):
    return a._batch_ci_mode(row)


def tick(a, conn, transport, parent):
    launch_child(a, conn, parent)
    row = child(conn, parent["batch_id"])
    if row is None:
        return
    parent = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (parent["batch_id"],)).fetchone()
    if parent["phase"] == "aborting" or (parent["terminal_status"] and parent["terminal_status"] != "merged"):
        a.abort_batch_locked(conn, transport, row, "predecessor batch aborted or failed")
        return
    if not parent_current(a, conn, row):
        invalidate(a, conn, row)
        row = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (row["batch_id"],)).fetchone()
        if parent['terminal_status'] == 'merged' and not json.loads(row['recovery_json']).get('replacement'):
            master = a.current_master_sha()
            replacement = {'head': master, 'tree': commit_tree(a, master), 'id': 'master-' + master}
            recovery = json.loads(row['recovery_json'])
            recovery['replacement'] = replacement
            with conn:
                conn.execute('UPDATE automerge_batches SET recovery_json=? WHERE batch_id=?',
                             (json.dumps(recovery), row['batch_id']))
            row = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
    if row["phase"] in {'resetting', 'restarting'}:
        recover(a, conn, row)
    elif parent["terminal_status"] == "merged":
        if not a._batch_priority(row) and any(p.priority for p in a.select_eligible_pull_requests(conn=conn)):
            a.abort_batch_locked(conn, transport, row, 'ready priority work takes precedence over ordinary lookahead')
            return
        if row['phase'] in {'building', 'fixing'}:
            a.process_batch(conn, transport, row['batch_id'])
            row = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
        if row['status'] not in {'running', 'launching'} or row['phase'] != 'waiting_parent':
            return
        promote(a, conn, transport, row, parent)
    elif row["phase"] != "waiting_parent":
        a.process_batch(conn, transport, row["batch_id"])
    else:
        keep, removed = a._recheck_sources(a._active_sources(row), conn=conn, batch_id=row['batch_id'])
        if removed:
            a._append_removed(conn, row['batch_id'], removed)
            latest = conn.execute('SELECT * FROM automerge_batches WHERE batch_id=?', (row['batch_id'],)).fetchone()
            a._rebuild_or_finish(conn, transport, latest, keep, 'speculative source changed while waiting')


def finished(a, conn, transport, row, final):
    a._record_agent_exclusions(conn, row, final)
    row = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (row["batch_id"],)).fetchone()
    if not a._active_sources(row):
        a._terminal(conn, transport, row, "no_sources_remain", "No speculative sources remain.")
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
            conn.execute("UPDATE automerge_batches SET phase='waiting_parent',pending_prompt=NULL,"
                         "prompt_delivered=0,turn_started_at=NULL WHERE batch_id=?", (row["batch_id"],))
    else:
        a._request_rebuild(conn, row, a._active_sources(row), "speculative local assessment needs completion; do not publish until promotion")
