"""Nested diagnostic allowlists: never echo stored briefs or unknown fields."""
from datetime import datetime, timezone
from mm_web.security import scrub

SCALAR = (str, int, float, bool, type(None))


def fields(value, names):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in names.split():
        if key not in value or not isinstance(value[key], SCALAR):
            continue
        item = scrub(value[key])
        if key.endswith('_at') and item is not None:
            if isinstance(item, (int, float)):
                item = datetime.fromtimestamp(item, timezone.utc).isoformat()
            else:
                moment = datetime.fromisoformat(item.replace('Z', '+00:00'))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                item = moment.astimezone(timezone.utc).isoformat()
        result[key] = item
    return result


PULL = 'number title head_sha url priority immediate base_ref'
CANDIDATE = 'id head tree source_revision attempt_generation created_at'
RECOVERY = 'stage status clear_id restart_id command_id turn_id old_turn_id after_seq report_after_seq'
ATTEST = 'format_version run_id head tree mode exit_code published result object suite'
RUN = 'run_id id url html_url workflow status conclusion head_sha sha created_at updated_at completed_at attempt run_attempt'


def attest(value):
    if not isinstance(value, dict):
        return None
    result = fields(value, ATTEST)
    metrics = value.get('metrics')
    if isinstance(metrics, dict):
        result['metrics'] = fields(metrics, 'total passed failed skipped reused executed duration_seconds tests_total tests_passed tests_failed')
    return result


def batch_state(value):
    result = fields(value, 'batch_id branch base_sha target_ref ci_mode status phase source_revision attempt_generation role revision')
    result['sources'] = [fields(p, PULL) for p in value.get('sources', [])]
    result['excluded'] = [fields(p, 'number head_sha kind') for p in value.get('excluded', [])]
    result['candidate'] = fields(value.get('candidate'), CANDIDATE)
    parent = value.get('predecessor')
    result['predecessor'] = ({**fields(parent, 'batch_id current'), 'candidate': fields(parent.get('candidate'), CANDIDATE)}
                             if isinstance(parent, dict) else None)
    result['recovery'] = fields(value.get('recovery'), RECOVERY)
    result['promotion'] = fields(value.get('promotion'), 'stage old_base base status')
    result['tests'] = fields(value.get('tests'), 'head verdict tests baseline source_revision')
    result['publication'] = fields(value.get('publication'), 'number url head')
    result['ready'] = fields(value.get('ready'), 'head tree source_revision attempt_generation kind created_at')
    result['local_findings'] = [fields(p, 'id batch_id session_id kind head_sha identity command created_at status diagnosis triage_outcome triage_issue_url linked_pr_url linked_pr_state')
                               for p in value.get('local_findings', [])]
    result['pending_github_writes'] = [fields(p, 'kind number head_sha') for p in value.get('pending_github_writes', [])]
    return result


def failover(value):
    return fields(value, 'stage status cursor session_id started_at completed_at operation_id profile model continuation_id last_error')


def campaign(value):
    result = fields(value, 'id branch anchor_sha tip_sha requested_ref requested_by status stage round thread_ts '
                    'created_at updated_at finished_at pause_reason start_error branch_url slack_thread_url')
    result['issues'] = [fields(p, 'number stage source state pr_number') for p in value.get('issues', [])]
    result['gate_merges'] = [fields(p, 'stage tip_sha gate_branch pr_number state') for p in value.get('gate_merges', [])]
    runs = value.get('latest_runs', {})
    result['latest_runs'] = {stage: fields(runs[stage], 'sha url conclusion') for stage in ['push', 'hourly', 'nightly']
                             if isinstance(runs, dict) and stage in runs}
    return result
