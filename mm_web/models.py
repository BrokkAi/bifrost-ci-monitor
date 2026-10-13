"""Public API contract. Arbitrary diagnostic payloads are scrubbed before use."""
from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict, Field

from mm_web.outcomes import Outcome

T = TypeVar('T')


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


Permission = Literal['write', 'maintain', 'admin']


class ErrorBody(BaseModel):
    code: str
    message: str
    campaign_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class Me(BaseModel):
    login: str
    permission: Permission
    can_release: bool
    auth: Literal['session', 'github_token', 'service_token']


class PullRef(BaseModel):
    number: int
    url: str
    title: str = ''
    head_sha: str | None = None


class Exclusion(PullRef):
    kind: str = 'removed'
    reason: str | None = None


class BatchSummary(BaseModel):
    batch_id: str
    kind: str
    lane: Literal['queue', 'priority', 'operator']
    role: Literal['primary', 'speculative']
    status: str
    phase: str
    terminal_status: str | None = None
    outcome: Outcome
    ci_mode: str
    target_ref: str = 'master'
    created_at: str
    finished_at: str | None = None
    duration_seconds: float
    sources: list[PullRef]
    excluded: list[Exclusion]
    integration_pr: PullRef | None = None
    session_id: str | None = None
    predecessor_id: str | None = None
    attempt_generation: int = 0
    abort_reason: str | None = None
    model_configuration: str


class SupervisorAction(BaseModel):
    action_id: str
    kind: str
    batch_id: str
    status: str
    attempts: int = 0
    updated_at: str
    last_error: str | None = None
    next_attempt_at: str | None = None


class ExecutionSummary(BaseModel):
    execution_id: str
    command: str
    head: str
    exit_code: int | None = None
    duration_seconds: float | None = None
    started_at: str
    attest: dict[str, Any] | None = None
    attest_runs: list[dict[str, Any]] = Field(default_factory=list)


class OutboxIntent(BaseModel):
    intent_id: str
    batch_id: str
    kind: str
    number: int | None = None
    head_sha: str | None = None
    created_at: str
    delivered_at: str | None = None
    cancelled_at: str | None = None
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None


class BatchDetail(BatchSummary):
    state: dict[str, Any]
    supervisor_actions: list[SupervisorAction]
    executions: list[ExecutionSummary]
    execution_count: int
    outbox_count: int
    failover: dict[str, Any] | None = None
    outbox: list[OutboxIntent]
    slack_thread_ts: str | None = None


class QueueItem(PullRef):
    author: str | None = None
    labels: list[str] = Field(default_factory=list)
    priority: Literal['immediate', 'high'] | None = None
    blocked_reason: str | None = None
    batch_id: str | None = None


class QueueGroup(BaseModel):
    key: Literal['in_batch', 'waiting', 'dependency_blocked', 'rejected', 'draft']
    label: str
    items: list[QueueItem]


class Queue(BaseModel):
    generated_at: str
    source: Literal['cached_pr_inventory'] = 'cached_pr_inventory'
    groups: list[QueueGroup]


class QueueCounts(BaseModel):
    in_batch: int = 0
    waiting: int = 0
    dependency_blocked: int = 0
    rejected: int = 0
    draft: int = 0


class KnownFailure(BaseModel):
    workflow: str
    job_name: str
    identity_kind: str
    identity: str
    first_seen_sha: str
    last_seen_sha: str
    last_seen_at: str
    status: str
    triage_outcome: str | None = None
    diagnosis: str | None = None
    linked_pr_url: str | None = None
    linked_issue_url: str | None = None
    triage_issue_url: str | None = None


class TriageJob(BaseModel):
    id: str
    title: str
    status: str
    base_sha: str
    created_at: str
    finished_at: str | None = None
    session_id: str | None = None
    last_error: str | None = None
    observation_count: int
    recovering: bool


class Repair(BaseModel):
    id: str
    issue_number: int
    issue_url: str
    status: str
    session_id: str | None = None
    created_at: str
    finished_at: str | None = None
    repair_pr_number: int | None = None
    retry_pr_number: int | None = None
    last_error: str | None = None
    recovering: bool


class Repairs(BaseModel):
    concurrency_limit: int
    active: list[Repair]
    recent: list[Repair]
    active_count: int
    next_cursor: str | None = None


class EntryPointHealth(BaseModel):
    name: Literal['merger', 'monitor', 'triage', 'stabilize']
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_outcome: str | None = None
    last_error: str | None = None
    last_skipped_at: str | None = None
    state: Literal['ok', 'stale', 'failing', 'unknown']


class SkillServiceHealth(BaseModel):
    reachable: bool
    version: str | None = None


class DatabaseHealth(BaseModel):
    path_ok: bool


class Health(BaseModel):
    entry_points: list[EntryPointHealth]
    skill_service: SkillServiceHealth
    db: DatabaseHealth


class Liveness(BaseModel):
    status: Literal['ok'] = 'ok'


class DailyStats(BaseModel):
    date: str
    landed_prs: int
    rejected_prs: int
    batches_completed: int
    batches_failed: int
    median_batch_minutes: float | None = None


class Stats(BaseModel):
    days: list[DailyStats]


class Pipeline(BaseModel):
    primary: BatchSummary | None = None
    speculative: BatchSummary | None = None


class Kpis(BaseModel):
    landed_today: int
    landed_7_days: int
    median_batch_minutes_7_days: float | None = None
    open_ci_failures: int
    pending_outbox_writes: int
    active_repairs: int


class OutboxCounts(BaseModel):
    pending: int
    failing: int


class LedgerCounts(BaseModel):
    open: int
    product: int
    infrastructure: int


class TriageSummary(BaseModel):
    active: int
    recovering: int


class RepairsSummary(BaseModel):
    active: int
    recovering: int
    concurrency_limit: int


class Overview(BaseModel):
    generated_at: str
    health: Health
    pipeline: Pipeline
    queue: QueueCounts
    kpis: Kpis
    recent: list[BatchSummary]
    outbox: OutboxCounts
    ledger: LedgerCounts
    triage: TriageSummary
    repairs: RepairsSummary


class SchemaColumn(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool


class SchemaTable(BaseModel):
    name: str
    columns: list[SchemaColumn]


class Schema(BaseModel):
    tables: list[SchemaTable]


class SqlRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=100000)
    params: list[Any] | dict[str, Any] = Field(default_factory=list)
    limit: int = Field(default=1000, ge=1, le=10000)


class SqlResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


class PushCi(BaseModel):
    run_id: int
    url: str | None = None
    conclusion: str | None = None


class Resolution(BaseModel):
    commitish: str | None = None
    sha: str
    subject: str
    committed_at: str
    on_master: bool
    url: str
    push_ci: PushCi | None = None
    live_campaign_id: str | None = None


class CampaignIssue(BaseModel):
    model_config = ConfigDict(extra='ignore')
    number: int
    stage: str | None = None
    source: str | None = None
    state: str | None = None
    pr_number: int | None = None


class GateMerge(BaseModel):
    model_config = ConfigDict(extra='ignore')
    stage: str
    tip_sha: str | None = None
    gate_branch: str | None = None
    pr_number: int | None = None
    state: str | None = None


class CampaignRun(BaseModel):
    sha: str
    url: str | None = None
    conclusion: str | None = None


class Campaign(BaseModel):
    model_config = ConfigDict(extra='ignore')
    id: str
    branch: str
    anchor_sha: str
    status: Literal['starting', 'running', 'paused', 'completed', 'aborted']
    stage: Literal['push', 'hourly', 'nightly', 'complete']
    round: int = 0
    tip_sha: str | None = None
    requested_ref: str | None = None
    requested_by: str | None = None
    updated_at: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    thread_ts: str | None = None
    pause_reason: str | None = None
    start_error: str | None = None
    issues: list[CampaignIssue] = Field(default_factory=list)
    gate_merges: list[GateMerge] = Field(default_factory=list)
    latest_runs: dict[Literal['push', 'hourly', 'nightly'], CampaignRun | None] = Field(default_factory=dict)
    branch_url: str | None = None
    slack_thread_url: str | None = None


class CreateRelease(BaseModel):
    sha: str = Field(pattern=r'^[0-9a-fA-F]{40}$')
    idempotency_key: str = Field(min_length=1, max_length=128)


class Logout(BaseModel):
    logged_out: bool = True


class BatchesPage(Page[BatchSummary]):
    pass


class OutboxPage(Page[OutboxIntent]):
    pass


class LedgerPage(Page[KnownFailure]):
    pass


class TriagePage(Page[TriageJob]):
    pass
