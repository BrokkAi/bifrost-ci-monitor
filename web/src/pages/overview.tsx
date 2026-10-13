import { Link } from "react-router-dom";
import {
  ArrowRight,
  ArrowUpRight,
  Clock3,
  GitMerge,
  GitPullRequest,
  Layers3,
  Radio,
  ShieldAlert,
  Send,
  Bot,
  TriangleAlert,
  FlaskConical,
} from "lucide-react";
import {
  BarChart,
  Bar,
  CartesianGrid,
  XAxis,
  YAxis,
  LineChart,
  Line,
  ReferenceDot,
} from "recharts";
import { useApi, useBatch, useHistory } from "@/api/client";
import type { BatchSummary, Overview } from "@/api/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HistoryFooter } from "@/components/history-footer";
import { Button } from "@/components/ui/button";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
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
import { minutes, display, sentenceCase } from "@/lib/utils";
export function PipelineCard({
  batch,
}: {
  batch: BatchSummary | null | undefined;
}) {
  const detail = useBatch(batch?.batch_id ?? "");
  const checkpoint = detail.data?.state.candidate as
    | { head?: string }
    | null
    | undefined;
  if (!batch)
    return (
      <Card className="pipeline-card">
        <EmptyState title="No active batch">
          The next eligible work will appear here.
        </EmptyState>
      </Card>
    );
  return (
    <Card
      className={`pipeline-card ${batch.role === "primary" ? "pipeline-primary" : ""}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="pipeline-role">
          {batch.role === "primary"
            ? "01 / Primary batch"
            : "02 / Speculative successor"}
        </span>
        <BatchStatus batch={batch} />
      </div>
      <Link
        to={`/batches/${batch.batch_id}`}
        className="pipeline-title flex items-center gap-2"
      >
        {batch.sources.length} pull requests
        <ArrowUpRight size={15} className="text-muted-foreground" />
      </Link>
      <div className="pipeline-subtitle">
        Phase: {batch.phase} <span className="mx-1">·</span> {batch.batch_id}
      </div>
      {batch.target_ref !== "master" && (
        <div className="text-[10px] text-muted-foreground mt-2 break-all">
          Target: {batch.target_ref}
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
      return `${role} ${id} is ${batch.phase} ${batch.sources.length} PR${batch.sources.length === 1 ? "" : "s"}.`;
    if (batch.phase === "waiting_parent")
      return `${role} ${id} is waiting for ${batch.predecessor_id ? `batch ${batch.predecessor_id.replace(/^batch-/, "")}` : "its predecessor"}.`;
    if (batch.phase === "waiting_ci" || batch.phase === "direct_waiting_ci")
      return `${role} ${id} is waiting for CI on ${batch.sources.length} PR${batch.sources.length === 1 ? "" : "s"}.`;
    if (batch.phase === "merging" || batch.phase === "direct_merging")
      return `${role} ${id} is merging ${batch.sources.length} PR${batch.sources.length === 1 ? "" : "s"}.`;
    return `${role} ${id} has ${batch.sources.length} PR${batch.sources.length === 1 ? "" : "s"}; phase: ${batch.phase}.`;
  }
  const parts = [
    primary ? state(primary, "Batch") : "No primary batch is active.",
  ];
  if (successor) parts.push(state(successor, "Speculative batch"));
  if (data.outbox.failing)
    parts.push(
      `${data.outbox.failing} GitHub write${data.outbox.failing === 1 ? " is" : "s are"} failing.`,
    );
  else if (data.outbox.pending)
    parts.push(
      `${data.outbox.pending} GitHub write${data.outbox.pending === 1 ? " is" : "s are"} pending.`,
    );
  for (const entry of data.health.entry_points.filter(
    (entry) => entry.state === "stale" || entry.state === "failing",
  ))
    parts.push(`${sentenceCase(entry.name)} heartbeat is ${entry.state}.`);
  return parts.join(" ");
}
export function OverviewPage() {
  const overview = useApi("/overview");
  const stats = useApi("/stats", "days=30");
  const history = useHistory("/batches", "state=recent&limit=5");
  if (overview.isPending) return <Loading />;
  if (overview.isError)
    return (
      <ErrorState error={overview.error} retry={() => overview.refetch()} />
    );
  const d = overview.data;
  const kpis = [
    {
      label: "Landed today",
      value: d.kpis.landed_today,
      footnote: "Pull requests on master",
      icon: GitMerge,
    },
    {
      label: "Landed · 7 days",
      value: d.kpis.landed_7_days,
      footnote: "Across integration batches",
      icon: GitPullRequest,
    },
    {
      label: "Median duration",
      value: `${d.kpis.median_batch_minutes_7_days ?? "—"}`,
      unit: "min",
      footnote: "Completed batches · 7 days",
      icon: Clock3,
    },
    {
      label: "Open CI failures",
      value: d.kpis.open_ci_failures,
      footnote: "In the shared failure ledger",
      icon: FlaskConical,
    },
    {
      label: "Pending writes",
      value: d.kpis.pending_outbox_writes,
      footnote: `${d.outbox.failing} delivery needs attention`,
      icon: Send,
    },
    {
      label: "Active repairs",
      value: d.kpis.active_repairs,
      footnote: `Of ${d.repairs.concurrency_limit} available slots`,
      icon: Bot,
    },
  ];
  const days = stats.data?.days ?? [];
  const recentBatches =
    history.data?.pages.flatMap((page) => page.items) ?? d.recent.slice(0, 5);
  return (
    <>
      <PageHeading
        title="Overview"
        description={overviewStatus(d)}
        action={
          <div className="flex items-center gap-2 mt-3 text-[10px] text-muted-foreground">
            <Radio size={12} />
            <span>
              Updated <RelativeTime value={d.generated_at} />
            </span>
          </div>
        }
      />
      <div className="kpi-grid">
        {kpis.map((k) => (
          <Card key={k.label} className="kpi">
            <div className="kpi-label">
              {k.label}
              <k.icon size={12} strokeWidth={1.5} />
            </div>
            <div className="kpi-value">
              {k.value}
              {k.unit && (
                <span className="ml-1.5 text-xs font-normal text-muted-foreground tracking-normal">
                  {k.unit}
                </span>
              )}
            </div>
            <div className="kpi-footnote">{k.footnote}</div>
          </Card>
        ))}
      </div>
      <div className="flex justify-between items-center">
        <div>
          <h2 className="section-title">Merge pipeline</h2>
        </div>
        <SectionLink to="/queue">View queue</SectionLink>
      </div>
      <div className="pipeline-grid">
        <PipelineCard batch={d.pipeline.primary} />
        <PipelineCard batch={d.pipeline.speculative} />
        <Card className="pipeline-card">
          <div className="pipeline-role">03 / Pull request queue</div>
          <Link to="/queue" className="pipeline-title flex items-center gap-2">
            {Object.values(d.queue).reduce((a, b) => a + b, 0)} open
            <ArrowUpRight size={15} className="text-muted-foreground" />
          </Link>
          <div className="pipeline-subtitle">
            Cached inventory · all readiness groups
          </div>
          <div className="mt-3">
            {(
              [
                { key: "waiting", label: "Waiting", status: "waiting" },
                {
                  key: "dependency_blocked",
                  label: "Dependency blocked",
                  status: "blocked",
                },
                {
                  key: "rejected",
                  label: "Rejected heads",
                  status: "rejected",
                },
                { key: "draft", label: "Draft", status: "waiting" },
              ] as const
            ).map((g) => (
              <Link
                key={g.key}
                to={`/queue?group=${g.key}`}
                className="queue-summary-row"
              >
                <span className="flex items-center gap-1.5">
                  <StatusBadge
                    value={g.status}
                    label={g.label}
                    className="health-status"
                  />
                </span>
                <strong>{d.queue[g.key]}</strong>
              </Link>
            ))}
          </div>
          <div className="pipeline-footer">
            <span className="text-muted-foreground">
              {d.queue.in_batch} already in a batch
            </span>
            <ArrowRight size={13} className="text-muted-foreground" />
          </div>
        </Card>
      </div>
      <div
        className={`chart-grid ${stats.isFetching && !stats.isPending ? "opacity-65" : ""}`}
      >
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Pull requests landed</CardTitle>
              <p className="section-caption">
                Pull requests landed per day · last 30 days
              </p>
            </div>
            <GitMerge size={15} className="text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {stats.isError ? (
              <div className="py-12 text-xs text-muted-foreground">
                Daily statistics are unavailable.
              </div>
            ) : (
              <ChartContainer
                config={{
                  landed_prs: {
                    label: "Pull requests",
                    color: "var(--series-1)",
                  },
                }}
              >
                <BarChart
                  data={days}
                  accessibilityLayer
                  margin={{ top: 10, left: -26, right: 0, bottom: 0 }}
                  barSize={12}
                >
                  <CartesianGrid vertical={false} />
                  <XAxis
                    dataKey="date"
                    tickLine={false}
                    axisLine={false}
                    tickMargin={10}
                    minTickGap={37}
                    tickFormatter={(s) =>
                      new Date(`${s}T00:00:00`).toLocaleDateString("en", {
                        month: "short",
                        day: "numeric",
                      })
                    }
                  />
                  <YAxis
                    tickLine={false}
                    axisLine={false}
                    allowDecimals={false}
                  />
                  <ChartTooltip
                    shared={false}
                    cursor={false}
                    content={<ChartTooltipContent />}
                  />
                  <Bar
                    isAnimationActive={false}
                    dataKey="landed_prs"
                    fill="var(--color-landed_prs)"
                    radius={[3, 3, 0, 0]}
                  />
                </BarChart>
              </ChartContainer>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Batch duration</CardTitle>
              <p className="section-caption">Daily median duration · minutes</p>
            </div>
            <Clock3 size={15} className="text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <ChartContainer
              config={{
                median_batch_minutes: {
                  label: "Minutes",
                  color: "var(--series-1)",
                },
              }}
            >
              <LineChart
                data={days}
                accessibilityLayer
                margin={{ top: 10, left: -26, right: 25, bottom: 0 }}
              >
                <CartesianGrid vertical={false} />
                <XAxis
                  dataKey="date"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={10}
                  minTickGap={42}
                  tickFormatter={(s) =>
                    new Date(`${s}T00:00:00`).toLocaleDateString("en", {
                      month: "short",
                      day: "numeric",
                    })
                  }
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  domain={[0, "auto"]}
                  tickFormatter={(n) => String(n)}
                />
                <ChartTooltip
                  cursor={{ strokeWidth: 1 }}
                  content={<ChartTooltipContent />}
                />
                <Line
                  isAnimationActive={false}
                  dataKey="median_batch_minutes"
                  type="linear"
                  stroke="var(--color-median_batch_minutes)"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
                  connectNulls={false}
                />
                {days.length > 0 &&
                  days[days.length - 1].median_batch_minutes != null && (
                    <ReferenceDot
                      x={days[days.length - 1].date}
                      y={days[days.length - 1].median_batch_minutes!}
                      r={4}
                      fill="var(--series-1)"
                      stroke="var(--card)"
                      strokeWidth={2}
                      label={{
                        value: `${days[days.length - 1].median_batch_minutes} min`,
                        position: "top",
                        fill: "var(--foreground)",
                        fontSize: 10,
                      }}
                    />
                  )}
              </LineChart>
            </ChartContainer>
          </CardContent>
        </Card>
      </div>
      <details className="mt-3 text-[10px] text-muted-foreground">
        <summary className="cursor-pointer">
          View daily statistics as a table
        </summary>
        <Card className="mt-3 max-h-60 overflow-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Landed PRs</th>
                <th>Median minutes</th>
              </tr>
            </thead>
            <tbody>
              {days.map((day) => (
                <tr key={day.date}>
                  <td>{day.date}</td>
                  <td>{day.landed_prs}</td>
                  <td>{day.median_batch_minutes ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </details>
      <div className="lower-grid">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Recent outcomes</CardTitle>
              <p className="section-caption">Completed batches</p>
            </div>
            <span className="text-[10px] text-muted-foreground">
              {recentBatches.length} shown
            </span>
          </CardHeader>
          {recentBatches.length ? (
            recentBatches.map((batch) => (
              <Link
                to={`/batches/${batch.batch_id}`}
                key={batch.batch_id}
                className="recent-row"
              >
                <div>
                  <div className="font-medium">
                    {batch.sources.length} pull request
                    {batch.sources.length !== 1 ? "s" : ""}
                    <span className="ml-2 text-[10px] text-muted-foreground font-normal">
                      {batch.batch_id}
                    </span>
                  </div>
                  <div className="mt-1 text-[9px] text-muted-foreground">
                    <RelativeTime value={batch.finished_at} />{" "}
                    <span className="mx-1">·</span>{" "}
                    {minutes(batch.duration_seconds)}
                  </div>
                  {batch.target_ref !== "master" && (
                    <div className="mt-1 text-[9px] text-muted-foreground break-all">
                      Target: {batch.target_ref}
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
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Needs attention</CardTitle>
            </div>
            <TriangleAlert size={15} className="text-muted-foreground" />
          </CardHeader>
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
                    Last completed <RelativeTime value={e.last_finished_at} />.{" "}
                    {e.last_error}
                  </p>
                </div>
              </div>
            ))}
          {d.repairs.recovering + d.triage.recovering > 0 && (
            <Link to="/agents" className="attention-item">
              <Bot size={14} />
              <div>
                <h3>
                  {d.repairs.recovering + d.triage.recovering} session
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
                  {d.queue.dependency_blocked} pull requests awaiting
                  prerequisites
                </h3>
                <p>Exact captured heads must land before their dependents.</p>
              </div>
              <ArrowUpRight size={12} className="ml-auto" />
            </Link>
          )}
        </Card>
      </div>
      <div className="mt-5 flex justify-end">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/releases">
            Prepare a stable release
            <ArrowRight />
          </Link>
        </Button>
      </div>
    </>
  );
}
