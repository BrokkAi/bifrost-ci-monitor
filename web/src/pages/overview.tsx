import { Link } from "react-router-dom";
import {
  ArrowUpRight,
  Clock3,
  Layers3,
  Radio,
  ShieldAlert,
  Send,
  Bot,
  TriangleAlert,
  FlaskConical,
} from "lucide-react";
import { useApi, useBatch, useHistory } from "@/api/client";
import type {
  AttentionFailure,
  BatchSummary,
  Overview,
  ReleaseLane,
} from "@/api/types";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { HistoryFooter } from "@/components/history-footer";
import { OutcomeBadge, BatchStatus, StatusBadge } from "@/components/status";
import {
  PageHeading,
  RelativeTime,
  Sha,
  SectionLink,
  Loading,
  ErrorState,
  EmptyState,
  ExternalLink,
} from "@/components/common";
import {
  duration,
  display,
  plural,
  sentenceCase,
  targetLabel,
} from "@/lib/utils";
const SLOTS = {
  primary: {
    label: "01 / Primary batch",
    emptyTitle: "No active batch",
    emptyText: "The next eligible work will appear here.",
  },
  speculative: {
    label: "02 / Speculative successor",
    emptyTitle: "No speculative batch",
    emptyText:
      "One starts when the primary batch registers a tested candidate and eligible PRs are waiting.",
  },
} as const;

export function PipelineCard({
  batch,
  slot,
}: {
  batch: BatchSummary | null | undefined;
  slot: keyof typeof SLOTS;
}) {
  const detail = useBatch(batch?.batch_id ?? "");
  const checkpoint = detail.data?.state.candidate as
    { head?: string } | null | undefined;
  const { label, emptyTitle, emptyText } = SLOTS[slot];
  if (!batch)
    return (
      <Card className="pipeline-card">
        <span className="pipeline-role">{label}</span>
        <EmptyState title={emptyTitle}>{emptyText}</EmptyState>
      </Card>
    );
  return (
    <Card
      className={`pipeline-card ${slot === "primary" ? "pipeline-primary" : ""}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="pipeline-role">{label}</span>
        <BatchStatus batch={batch} />
      </div>
      <Link
        to={`/batches/${batch.batch_id}`}
        className="pipeline-title flex items-center gap-2"
      >
        {plural(batch.sources.length, "pull request")}
        <ArrowUpRight size={15} className="text-muted-foreground" />
      </Link>
      <div className="pipeline-subtitle">
        Phase: {batchPhase(batch.phase)} <span className="mx-1">·</span>{" "}
        {batch.batch_id}
      </div>
      {batch.target_ref !== "master" && (
        <div
          className="text-[10px] text-muted-foreground mt-2"
          title={batch.target_ref}
        >
          Target: {targetLabel(batch.target_ref)}
        </div>
      )}
      <div className="pipeline-meta">
        <div>
          <div className="metadata-label">Candidate head</div>
          <Sha value={checkpoint?.head ?? batch.integration_pr?.head_sha} />
        </div>
        <div>
          <div className="metadata-label">Started</div>
          <div className="metadata-value">
            <RelativeTime value={batch.created_at} />
          </div>
        </div>
        <div>
          <div className="metadata-label">CI mode</div>
          <div className="metadata-value">{batch.ci_mode}</div>
        </div>
        <div>
          <div className="metadata-label">Integration PR</div>
          <div className="metadata-value">
            {batch.integration_pr ? (
              <ExternalLink href={batch.integration_pr.url}>
                #{batch.integration_pr.number}
              </ExternalLink>
            ) : (
              "Not published"
            )}
          </div>
        </div>
      </div>
      <div className="pipeline-footer">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <Layers3 size={12} />
          {batch.model_configuration === "multi-pr"
            ? "Opus + Luna"
            : display(batch.model_configuration)}
        </span>
        <SectionLink to={`/batches/${batch.batch_id}`}>View batch</SectionLink>
      </div>
    </Card>
  );
}
export function overviewStatus(data: Overview) {
  const primary = data.pipeline.primary;
  const successor = data.pipeline.speculative;
  function state(batch: BatchSummary, role: string) {
    const id = batch.batch_id.replace(/^batch-/, "");
    if (batch.phase === "building" || batch.phase === "fixing")
      return `${role} ${id} is ${batch.phase} ${plural(batch.sources.length, "PR")}.`;
    if (batch.phase === "waiting_parent")
      return `${role} ${id} is waiting for ${batch.predecessor_id ? `batch ${batch.predecessor_id.replace(/^batch-/, "")}` : "its predecessor"}.`;
    if (batch.phase === "waiting_ci" || batch.phase === "direct_waiting_ci")
      return `${role} ${id} is waiting for CI on ${plural(batch.sources.length, "PR")}.`;
    if (batch.phase === "merging" || batch.phase === "direct_merging")
      return `${role} ${id} is merging ${plural(batch.sources.length, "PR")}.`;
    return `${role} ${id} has ${plural(batch.sources.length, "PR")}; phase: ${batch.phase}.`;
  }
  const parts = [
    primary ? state(primary, "Batch") : "No primary batch is active.",
  ];
  if (successor) parts.push(state(successor, "Speculative batch"));
  if (data.pipeline.release) {
    const release = data.pipeline.release;
    parts.push(
      release.status === "paused"
        ? "The release branch campaign is paused."
        : `The release branch campaign is ${release.status}; stage: ${release.stage}.`,
    );
  }
  if (data.outbox.failing)
    parts.push(
      `${plural(data.outbox.failing, "GitHub write is", "GitHub writes are")} failing.`,
    );
  else if (data.outbox.pending)
    parts.push(
      `${plural(data.outbox.pending, "GitHub write is", "GitHub writes are")} pending.`,
    );
  for (const entry of data.health.entry_points.filter(
    (entry) => entry.state === "stale" || entry.state === "failing",
  ))
    parts.push(`${sentenceCase(entry.name)} heartbeat is ${entry.state}.`);
  return parts.join(" ");
}
function batchPhase(phase: string) {
  if (phase === "waiting_parent") return "Waiting for predecessor";
  if (phase === "waiting_ci" || phase === "direct_waiting_ci")
    return "Waiting for CI";
  return sentenceCase(phase);
}

const RELEASE_STAGES = ["push", "hourly", "nightly", "complete"] as const;

function ReleaseCard({ release }: { release: ReleaseLane | null }) {
  if (!release)
    return (
      <Card className="pipeline-card release-card">
        <div className="pipeline-role">03 / Release branch</div>
        <EmptyState title="No release branch campaign">
          <Link to="/releases" className="external-link">
            Start a release branch campaign <ArrowUpRight size={12} />
          </Link>
        </EmptyState>
      </Card>
    );
  const current = RELEASE_STAGES.indexOf(release.stage);
  return (
    <Card className="pipeline-card release-card">
      <div className="flex items-center justify-between gap-2">
        <span className="pipeline-role">03 / Release branch</span>
        <StatusBadge
          value={release.status}
          label={sentenceCase(release.status)}
        />
      </div>
      <h3 className="pipeline-title">
        Release branch from{" "}
        {release.anchor_sha?.slice(0, 7) ?? "an unknown commit"}
      </h3>
      {release.status === "paused" && (
        <div role="status" className="release-pause">
          <ShieldAlert size={14} />
          <span>{release.pause_reason ?? "The campaign is paused."}</span>
        </div>
      )}
      <ol className="release-stages" aria-label="Release stages">
        {RELEASE_STAGES.map((stage, index) => (
          <li
            key={stage}
            aria-current={stage === release.stage ? "step" : undefined}
            className={
              index === current ? "current" : index < current ? "done" : ""
            }
          >
            <span className="release-stage-dot" aria-hidden="true" />
            {sentenceCase(stage)}
          </li>
        ))}
      </ol>
      <div className="pipeline-meta release-meta">
        <div>
          <div className="metadata-label">Round</div>
          <div className="metadata-value">{release.round ?? "—"}</div>
        </div>
        <div>
          <div className="metadata-label">Latest {release.stage} run</div>
          <div className="metadata-value">
            {release.current_run ? (
              <ExternalLink href={release.current_run.url}>
                <StatusBadge
                  value={release.current_run.conclusion ?? "running"}
                  label={sentenceCase(
                    release.current_run.conclusion ?? "running",
                  )}
                  className="health-status"
                />
              </ExternalLink>
            ) : (
              "No run yet"
            )}
          </div>
        </div>
      </div>
      <div className="release-counts">
        <span>{plural(release.open_issues, "open issue")}</span>
        <span>{plural(release.merged_prs, "PR")} merged into the branch</span>
      </div>
      {release.batch && (
        <Link
          to={`/batches/${release.batch.batch_id}`}
          className="release-batch"
        >
          <span>
            Merging {plural(release.batch.sources.length, "PR")} into the branch ·{" "}
            {batchPhase(release.batch.phase).toLowerCase()}
          </span>
          <ArrowUpRight size={12} />
        </Link>
      )}
      <div className="pipeline-footer">
        <SectionLink to="/releases">View release</SectionLink>
      </div>
    </Card>
  );
}

function AttentionSummary({ item }: { item: AttentionFailure }) {
  const checks = plural(item.failures, "failing check");
  if (item.reason === "escalated")
    return (
      <>
        {item.issue_number != null
          ? `Issue #${item.issue_number}`
          : "CI failures"}{" "}
        escalated: {checks}
        {item.last_seen_at && (
          <>
            , last seen <RelativeTime value={item.last_seen_at} />
          </>
        )}
      </>
    );
  if (item.reason === "fix_unconfirmed") {
    return (
      <>
        {item.issue_number != null
          ? `Issue #${item.issue_number}`
          : "A reported fix"}{" "}
        awaits CI confirmation: {plural(item.failures, "check")}
        {item.last_seen_at && (
          <>
            , last failure <RelativeTime value={item.last_seen_at} />
          </>
        )}
      </>
    );
  }
  return item.issue_number != null ? (
    <>
      Issue #{item.issue_number}: {checks} with no repair in progress
    </>
  ) : (
    <>
      {checks} {item.failures === 1 ? "has" : "have"} no issue or repair
    </>
  );
}

export function OverviewPage() {
  const overview = useApi("/overview");
  const history = useHistory("/batches", "state=recent&limit=5");
  if (overview.isPending) return <Loading />;
  if (overview.isError)
    return (
      <ErrorState error={overview.error} retry={() => overview.refetch()} />
    );
  const d = overview.data;
  const recentBatches =
    history.data?.pages.flatMap((page) => page.items) ?? d.recent.slice(0, 5);
  const ciAttention = d.ci_attention ?? [];
  const hasAttention =
    ciAttention.length > 0 ||
    d.outbox.failing > 0 ||
    d.health.entry_points.some((e) => e.state !== "ok") ||
    d.repairs.recovering + d.triage.recovering > 0 ||
    d.queue.dependency_blocked > 0;
  return (
    <>
      <PageHeading
        title="Overview"
        description={overviewStatus(d)}
        action={
          <div className="flex shrink-0 items-center gap-2 mt-3 whitespace-nowrap text-[10px] text-muted-foreground">
            <Radio size={12} />
            <span>
              Updated <RelativeTime value={d.generated_at} />
            </span>
          </div>
        }
      />
      <div className="overview-summary-grid">
        <Card className="overview-summary-card">
          <CardHeader className="shrink-0">
            <div>
              <CardTitle id="attention-heading">Needs attention</CardTitle>
              <p className="section-caption">
                CI failures and supervisor updates
              </p>
            </div>
            <TriangleAlert size={15} className="text-muted-foreground" />
          </CardHeader>
          <div
            className="overview-card-scroll"
            role="region"
            aria-labelledby="attention-heading"
            tabIndex={0}
          >
            {ciAttention.map((item, index) => {
              const content = (
                <>
                  <FlaskConical size={14} />
                  <div>
                    <h3>
                      <AttentionSummary item={item} />
                    </h3>
                    <p>{item.summary}</p>
                  </div>
                  {item.issue_url && (
                    <ArrowUpRight size={12} className="ml-auto" />
                  )}
                </>
              );
              return item.issue_url ? (
                <a
                  key={`${item.reason}-${item.issue_number}-${index}`}
                  href={item.issue_url}
                  target="_blank"
                  rel="noreferrer"
                  className="attention-item"
                  draggable={false}
                >
                  {content}
                </a>
              ) : (
                <div key={`${item.reason}-${index}`} className="attention-item">
                  {content}
                </div>
              );
            })}
            {d.outbox.failing > 0 && (
              <Link to="/outbox" className="attention-item">
                <Send size={14} />
                <div>
                  <h3>
                    {d.outbox.failing} GitHub write
                    {d.outbox.failing === 1 ? "" : "s"} retrying
                  </h3>
                </div>
                <ArrowUpRight size={12} className="ml-auto" />
              </Link>
            )}
            {d.health.entry_points
              .filter((e) => e.state !== "ok")
              .map((e) => (
                <div key={e.name} className="attention-item">
                  <Clock3 size={14} />
                  <div>
                    <h3>
                      {sentenceCase(e.name)} heartbeat is {e.state}
                    </h3>
                    <p>
                      Last completed <RelativeTime value={e.last_finished_at} />
                      . {e.last_error}
                    </p>
                  </div>
                </div>
              ))}
            {d.repairs.recovering + d.triage.recovering > 0 && (
              <Link to="/agents" className="attention-item">
                <Bot size={14} />
                <div>
                  <h3>
                    {plural(
                      d.repairs.recovering + d.triage.recovering,
                      "session",
                    )}{" "}
                    recovering
                  </h3>
                  <p>Session recovery is in progress.</p>
                </div>
                <ArrowUpRight size={12} className="ml-auto" />
              </Link>
            )}
            {d.queue.dependency_blocked > 0 && (
              <Link
                to="/queue?group=dependency_blocked"
                className="attention-item"
              >
                <ShieldAlert size={14} />
                <div>
                  <h3>
                    {plural(d.queue.dependency_blocked, "pull request")}{" "}
                    awaiting prerequisites
                  </h3>
                  <p>Exact captured heads must land before their dependents.</p>
                </div>
                <ArrowUpRight size={12} className="ml-auto" />
              </Link>
            )}
            {!hasAttention && <EmptyState title="Nothing needs attention" />}
          </div>
        </Card>
        <Card className="overview-summary-card">
          <CardHeader className="shrink-0">
            <div>
              <CardTitle id="outcomes-heading">Recent outcomes</CardTitle>
              <p className="section-caption">
                Landed, failed and other finished batches
              </p>
            </div>
            <span className="text-[10px] text-muted-foreground">
              {recentBatches.length} shown
            </span>
          </CardHeader>
          <div
            className="overview-card-scroll"
            role="region"
            aria-labelledby="outcomes-heading"
            tabIndex={0}
          >
            {recentBatches.length ? (
              recentBatches.map((batch) => (
                <Link
                  to={`/batches/${batch.batch_id}`}
                  key={batch.batch_id}
                  className="recent-row"
                >
                  <div>
                    <div className="font-medium">
                      {plural(batch.sources.length, "pull request")}
                      <span className="ml-2 text-[10px] text-muted-foreground font-normal">
                        {batch.batch_id}
                      </span>
                    </div>
                    <div className="mt-1 text-[9px] text-muted-foreground">
                      <RelativeTime value={batch.finished_at} />{" "}
                      <span className="mx-1">·</span>{" "}
                      {duration(batch.duration_seconds)}
                    </div>
                    {batch.target_ref !== "master" && (
                      <div
                        className="mt-1 text-[9px] text-muted-foreground"
                        title={batch.target_ref}
                      >
                        Target: {targetLabel(batch.target_ref)}
                      </div>
                    )}
                  </div>
                  <OutcomeBadge outcome={batch.outcome} />
                  <ArrowUpRight size={13} className="text-muted-foreground" />
                </Link>
              ))
            ) : (
              <EmptyState title="No recent batches" />
            )}
          </div>
          <HistoryFooter
            hasMore={Boolean(history.hasNextPage)}
            loading={history.isFetchingNextPage}
            loaded={recentBatches.length}
            loadMore={() =>
              history.data ? history.fetchNextPage() : history.refetch()
            }
            error={history.isError ? history.error : null}
          />
        </Card>
      </div>
      <div className="flex justify-between items-center mt-7">
        <h2 className="section-title">Merge pipeline</h2>
      </div>
      <div className="pipeline-grid">
        <PipelineCard slot="primary" batch={d.pipeline.primary} />
        <PipelineCard slot="speculative" batch={d.pipeline.speculative} />
        <ReleaseCard release={d.pipeline.release ?? null} />
      </div>
    </>
  );
}
