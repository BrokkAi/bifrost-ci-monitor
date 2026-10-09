"""Native same-session recovery for the triage and issue-fixer supervisors."""
from __future__ import annotations

import json
import sys

import monitor
import speculation


def advance(session, recovery, *, prefix, checkpoint):
    """Advance a durable reset without inspecting session idleness."""
    stage = recovery['stage']
    clear = f"{prefix}-clear-{recovery['id']}-{recovery.get('clear_retry', 0)}"

    def mj(args):
        return json.loads(monitor.require_mj_success(args, timeout=60) or '{}')

    if stage == 'stop':
        state = mj(['sessions', '--session', session, '--json'])
        if str(state.get('state', '')).lower() in {'stopped', 'suspended', 'lost', 'failed'}:
            mj(['resume', '--session', session, '--queue', 'discard', '--json'])
            return
        mj(['clear-queue', '--session', session, '--json'])
        monitor.interrupt_turn(session)
        mj(['stop-task', '--session', session, '--all', '--json'])
        page = mj(['transcript', '--session', session, '--after-seq', '0', '--json'])
        recovery.update(stage='clear', cursor=int(page['latest_seq']))
    elif stage == 'clear':
        speculation.send_once(sys.modules[__name__], session, '/clear', clear)
        recovery['stage'] = 'cleared'
    elif stage == 'cleared':
        page = mj(['transcript', '--session', session, '--after-seq', str(recovery['cursor']), '--json'])
        boundary = next((item for item in page.get('items', [])
                         if item.get('stable_id') == 'context-cleared:' + clear), None)
        if boundary is None:
            recovery['cursor'] = int(page.get('next_after_seq', recovery['cursor']))
            result, recovery['api_cursor'] = speculation.command_outcome(
                sys.modules[__name__], session, clear, recovery.get('api_cursor', 0))
            if result and result['outcome'] != 'succeeded':
                recovery.update(stage='stop', failed_step='clear', clear_retry=recovery.get('clear_retry', 0) + 1)
                checkpoint(recovery)
                raise RuntimeError('context clear failed: ' + str(result.get('message') or result['outcome']))
        else:
            recovery.update(stage='restart', boundary_seq=int(boundary['seq']))
    elif stage == 'restart':
        speculation.send_once(sys.modules[__name__], session, recovery['prompt'], f"{prefix}-restart-{recovery['id']}")
        recovery['stage'] = 'running'
    else:
        raise RuntimeError('invalid agent recovery stage: ' + str(stage))
    checkpoint(recovery)
