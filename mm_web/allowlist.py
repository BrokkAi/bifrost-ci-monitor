"""SQL exposes structured scalars only; diagnostic text is curated by the API.

Names, titles, commands, errors, diagnoses, raw JSON and arbitrary prose are
private even when a SQL expression encodes or extracts their contents. Newly
added columns remain private until reviewed.
"""


def columns(value):
    return frozenset(value.split())


COLUMNS = {
    'automerge_batches': columns('agent_configuration attempt_generation base_ci_run_id base_ci_source base_sha baseline_dispatch_after_run_id baseline_dispatch_grace_until baseline_dispatch_intent_at baseline_dispatch_requested_at baseline_dispatch_sha batch_id ci_head_sha ci_mode ci_not_worse ci_result_conclusion ci_result_head_sha ci_result_run_id ci_round created_at expansion_count finished_at github_write_retry_after github_write_retry_attempts integration_merge_commit_sha integration_pr_number integration_pr_url kind launch_attempted launch_attempted_at outcome_posted phase predecessor_id priority report_after_seq retry_rescan_pending role_promoted session_id source speculative_thread_ts start_notification_role start_notification_sent start_pr_list_sent status suspend_pending suspend_verify_failures terminal_status thread_ts turn_started_at verdict_status_description verdict_status_sha verdict_status_state'),
    'automerge_github_outbox': columns('alerted_at attempts batch_id cancelled_at created_at delivered_at head_sha intent_id kind next_attempt_at number'),
    'automerge_pr_inventory': columns('head_sha number'),
    'automerge_pr_dependencies': columns('head_sha number prerequisite_head prerequisite_number'),
    'automerge_commit_ancestry': columns('ancestor descendant present'),
    'automerge_supervisor_actions': columns('action_id attempts batch_id kind next_attempt_at status updated_at'),
    'automerge_queue_summaries': columns('completed_at created_at next_message summary_id thread_ts'),
    'automerge_skill_events': columns('batch_id created_at event_id kind'),
    'automerge_executions': columns('batch_id execution_id session_id started_at'),
    'automerge_provider_failovers': columns('batch_id session_id updated_at'),
    'automerge_blocked_notifications': columns('batch_id created_at slack_notification_attempted'),
    'automerge_relayed_messages': columns('batch_id seq stable_id'),
    'known_failures': columns('first_seen_at first_seen_run_id first_seen_run_url first_seen_sha fixed_at fixed_by_sha identity_kind last_seen_at last_seen_run_id last_seen_run_url last_seen_sha linked_issue_state linked_issue_url linked_pr_state linked_pr_url status triage_issue_url triage_outcome updated_at'),
    'known_failure_runs': columns('conclusion processed_at run_id sha'),
    'known_failure_errors': columns('failure_count notified_at updated_at'),
    'triage_jobs': columns('base_sha cleanup_done created_at feedback_digest finished_at id report_after_seq session_id status'),
    'triage_observations': columns('completed_at fingerprint issue_url job_id resolved_run_id'),
    'triage_publications': columns('group_index issue_number job_id'),
    'triage_product_announcements': columns('completed_at job_id posted_count thread_ts'),
    'triage_infrastructure_threads': columns('channel last_notice_at thread_ts'),
    'issue_repairs': columns('base_sha cleanup_done created_at feedback_digest finished_at id issue_number issue_url outcome_sent rejected_head_sha repair_pr_number repair_pr_url report_after_seq retry_pr_number session_id start_notified status thread_ts transcript_seq work_key'),
    'issue_repair_messages': columns('job_id stable_id'),
    'local_findings': columns('batch_id created_at head_sha id kind linked_pr_state linked_pr_url session_id status triage_issue_url triage_outcome'),
    'entrypoint_heartbeats': columns('entry_point last_finished_at last_outcome last_skipped_at last_started_at'),
    'attest_ci_imports': columns('attempt run_id status updated_at'),
    'invocations': columns('attempt_count codex_session_id conclusion exit_code finished_at issue_url observed_at sha started_at status workflow_run_id'),
    'monitor_events': columns('created_at kind sha slack_notification_attempted'),
    'blocked_notifications': columns('created_at slack_notification_attempted workflow_run_id'),
    'relayed_messages': columns('seq stable_id workflow_run_id'),
    'escalation_gate': columns('escalated issue_url last_reported_run_id opened_at sha signature thread_ts'),
    'stabilize_campaigns': columns('anchor_sha base_sha branch_url campaign_id created_at finished_at id idempotency_key round slack_thread_url stage status thread_ts tip_sha updated_at'),
    'stabilize_issues': columns('campaign_id created_at issue_number number pr_number source stage state updated_at'),
    'stabilize_gate_merges': columns('campaign_id created_at merged_at pr_number stage state tip_sha updated_at'),
    'stabilize_failures': columns('campaign_id created_at identity_kind issue_number resolved_at run_id stage status tip_sha updated_at'),
    'sqlite_master': columns('rootpage type'),
    'sqlite_schema': columns('rootpage type'),

}


def allowed(table, column):
    return column in COLUMNS.get(table, ())
