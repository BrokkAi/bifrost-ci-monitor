import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  GitPullRequest,
  Terminal,
  Clock3,
  ArrowRight,
  Layers3,
} from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type { PullRef, Execution } from "@/api/types";
import { useBatch } from "@/api/client";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import {
  PageHeading,
  Loading,
  ErrorState,
  ExternalLink,
  Sha,
  RelativeTime,
  EmptyState,
} from "@/components/common";
import { DataTable } from "@/components/data-table";
import { StatusBadge, StatusIcon, BatchStatus } from "@/components/status";
import { display, minutes, sentenceCase } from "@/lib/utils";
import { batchCheckpoints } from "@/lib/batch-progress";
const columns: ColumnDef<PullRef>[] = [
  {
    accessorKey: "number",
    header: "PR",
    cell: ({ row }) => (
      <ExternalLink href={row.original.url}>
        #{row.original.number}
      </ExternalLink>
    ),
  },
  {
    accessorKey: "title",
    header: "Source change",
    cell: ({ row }) => (
      <div className="min-w-52 font-medium">{row.original.title}</div>
    ),
  },
  {
    accessorKey: "head_sha",
    header: "Captured head",
    cell: ({ row }) => <Sha value={row.original.head_sha} />,
  },
];
function attestSummary(value: Execution["attest"]) {
  if (!value || !value.metrics || typeof value.metrics !== "object")
    return null;
  const metrics = value.metrics as Record<string, unknown>;
  const passed = metrics.passed ?? metrics.tests_passed;
  const total = metrics.total ?? metrics.tests_total;
  if (typeof passed !== "number" || typeof total !== "number") return null;
  return `${passed} / ${total} tests passed${typeof metrics.reused === "number" && metrics.reused > 0 ? ` · ${metrics.reused} reused` : ""}`;
}
const executions: ColumnDef<Execution>[] = [
  {
    accessorKey: "command",
    header: "Command",
    cell: ({ row }) => (
      <div className="max-w-96">
        <code className="text-[10px] break-all">{row.original.command}</code>
        {attestSummary(row.original.attest) && (
          <p className="text-[9px] text-muted-foreground mt-1">
            {attestSummary(row.original.attest)}
          </p>
        )}
      </div>
    ),
  },
  {
    accessorKey: "exit_code",
    header: "Result",
    cell: ({ row }) => (
      <StatusBadge
        value={
          row.original.exit_code === null
            ? "running"
            : row.original.exit_code === 0
              ? "landed"
              : "failed"
        }
        label={
          row.original.exit_code === null
            ? "Running"
            : row.original.exit_code === 0
              ? "Passed"
              : `Exit ${row.original.exit_code}`
        }
      />
    ),
  },
  {
    accessorKey: "duration_seconds",
    header: "Duration",
    cell: ({ row }) => (
      <span className="numeric text-muted-foreground">
        {minutes(row.original.duration_seconds)}
      </span>
    ),
  },
  {
    accessorKey: "head",
    header: "Tested head",
    cell: ({ row }) => <Sha value={row.original.head} />,
  },
];
export function BatchPage() {
  const { id = "" } = useParams();
  const query = useBatch(id);
  if (query.isPending) return <Loading />;
  if (query.isError)
    return <ErrorState error={query.error} retry={() => query.refetch()} />;
  const b = query.data;
  const checkpoint = b.state.candidate as { head?: string } | null;
  const candidate = checkpoint?.head ?? b.integration_pr?.head_sha;
  const assessment = b.state.tests as
    | { verdict?: string; summary?: string; tests?: string; baseline?: string }
    | undefined;
  const checkpoints = batchCheckpoints(b);
  return (
    <>
      <Link
        to="/"
        className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground mb-5"
      >
        <ArrowLeft size={12} />
        Back to overview
      </Link>
      <PageHeading
        title={`Batch ${b.batch_id.replace("batch-", "")}`}
        description={
          <>
            {b.sources.length} source pull requests. Started{" "}
            <RelativeTime value={b.created_at} />.
            {b.target_ref !== "master" && (
              <span className="block mt-1 break-all">
                Target: {b.target_ref}
              </span>
            )}
          </>
        }
        action={
          <div className="mt-3">
            <BatchStatus batch={b} />
          </div>
        }
      />
      <div className="detail-grid">
        <div className="space-y-5">
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Batch checkpoints</CardTitle>
                <p className="section-caption">
                  Phase: <code>{b.phase}</code>
                </p>
              </div>
              <Layers3 size={15} className="text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div
                className="stage-pipeline"
                aria-label="Recorded batch checkpoints"
              >
                {checkpoints.map((step) => (
                  <div key={step.key} className="stage-node">
                    <div className="stage-icon">
                      <StatusIcon
                        value={step.iconValue ?? step.status}
                        size={15}
                      />
                    </div>
                    <span className="font-medium text-foreground">
                      {step.title}
                    </span>
                    <span className="block mt-1.5 text-[9px]">
                      {step.label}
                    </span>
                  </div>
                ))}
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 pt-3 border-t">
                <div>
                  <div className="metadata-label">Candidate head</div>
                  <Sha value={candidate} />
                </div>
                <div>
                  <div className="metadata-label">CI mode</div>
                  <div className="metadata-value">{b.ci_mode}</div>
                </div>
                <div>
                  <div className="metadata-label">Attempt generation</div>
                  <div className="metadata-value">{b.attempt_generation}</div>
                </div>
                <div>
                  <div className="metadata-label">Source revision</div>
                  <div className="metadata-value">
                    {String(
                      b.state.source_revision ?? b.state.revision ?? "—",
                    ).slice(0, 12)}
                  </div>
                </div>
              </div>
              {assessment && (
                <div className="info-banner mt-5">
                  <StatusBadge
                    value={
                      assessment.verdict === "pass"
                        ? "landed"
                        : assessment.verdict === "fail"
                          ? "failed"
                          : "unknown"
                    }
                    label={
                      assessment.verdict === "pass"
                        ? "Passed"
                        : assessment.verdict === "fail"
                          ? "Failed"
                          : "Recorded"
                    }
                  />
                  <span>
                    {assessment.tests ?? assessment.summary}
                    {assessment.baseline && (
                      <span className="block mt-1">
                        Baseline: {assessment.baseline}
                      </span>
                    )}
                  </span>
                </div>
              )}
              {b.abort_reason && (
                <div className="info-banner mt-4">{b.abort_reason}</div>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Source pull requests</CardTitle>
              </div>
              <GitPullRequest size={15} className="text-muted-foreground" />
            </CardHeader>
            <DataTable data={b.sources} columns={columns} />
          </Card>
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Execution evidence</CardTitle>
                <p className="section-caption">
                  Commands, results and tested commits. Loaded{" "}
                  {b.executions.length} of {b.execution_count} executions.
                </p>
              </div>
              <Terminal size={15} className="text-muted-foreground" />
            </CardHeader>
            <DataTable data={b.executions} columns={executions} />
          </Card>
          {b.excluded.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Excluded sources</CardTitle>
              </CardHeader>
              <CardContent>
                {b.excluded.map((e) => (
                  <div key={e.number} className="flex gap-3 py-2">
                    <StatusBadge value={e.kind} />
                    <div className="text-xs">
                      #{e.number}
                      <p className="text-muted-foreground mt-1">{e.reason}</p>
                    </div>
                  </div>
                ))}
              </CardContent>
            </Card>
          )}
        </div>
        <div className="space-y-5">
          <Card>
            <CardHeader>
              <CardTitle>Batch context</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {[
                {
                  label: "Integration pull request",
                  value: b.integration_pr ? (
                    <ExternalLink href={b.integration_pr.url}>
                      #{b.integration_pr.number}
                    </ExternalLink>
                  ) : (
                    "Not published yet"
                  ),
                },
                {
                  label: "Agent configuration",
                  value: display(b.model_configuration),
                },
                {
                  label: "Session",
                  value: (
                    <code className="text-[10px]">
                      {b.session_id ?? "No agent session"}
                    </code>
                  ),
                },
                {
                  label: "Predecessor",
                  value: b.predecessor_id ? (
                    <Link
                      to={`/batches/${b.predecessor_id}`}
                      className="external-link"
                    >
                      {b.predecessor_id}
                      <ArrowRight size={12} />
                    </Link>
                  ) : (
                    "Primary · no predecessor"
                  ),
                },
                {
                  label: "Started",
                  value: <RelativeTime value={b.created_at} />,
                },
                {
                  label: "Finished",
                  value: <RelativeTime value={b.finished_at} />,
                },
              ].map((item) => (
                <div key={item.label}>
                  <div className="metadata-label">{item.label}</div>
                  <div className="metadata-value">{item.value}</div>
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Supervisor actions</CardTitle>
            </CardHeader>
            <CardContent>
              {b.supervisor_actions.length ? (
                b.supervisor_actions.map((a) => (
                  <div
                    key={a.action_id}
                    className="flex items-start gap-3 py-2"
                  >
                    <Clock3 size={14} className="text-muted-foreground mt-1" />
                    <div>
                      <div className="text-xs font-medium">
                        {sentenceCase(a.kind)}
                      </div>
                      <div className="mt-1.5">
                        <StatusBadge value={a.status} />
                      </div>
                      {a.last_error && (
                        <p className="text-[10px] text-muted-foreground mt-2">
                          {a.last_error}
                        </p>
                      )}
                    </div>
                  </div>
                ))
              ) : (
                <EmptyState title="No pending actions" />
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Publication & delivery</CardTitle>
                <p className="section-caption">
                  Loaded {b.outbox.length} of {b.outbox_count} write
                  {b.outbox_count === 1 ? "" : "s"}
                </p>
              </div>
            </CardHeader>
            <CardContent>
              {b.outbox.length ? (
                b.outbox.map((o) => (
                  <div key={o.intent_id} className="mb-3 last:mb-0">
                    <div className="text-xs font-medium mb-1.5">
                      {sentenceCase(o.kind)}
                    </div>
                    <StatusBadge
                      value={
                        o.delivered_at
                          ? "delivered"
                          : o.last_error
                            ? "failed"
                            : "waiting"
                      }
                    />
                    {o.last_error && (
                      <p className="text-[10px] text-muted-foreground mt-2 leading-relaxed">
                        {o.last_error}
                      </p>
                    )}
                  </div>
                ))
              ) : (
                <EmptyState title="No delivery intents" />
              )}
              <Link
                to="/outbox"
                className="external-link text-[10px] mt-3 text-muted-foreground"
              >
                Open outbox
                <ArrowRight size={12} />
              </Link>
            </CardContent>
          </Card>
          {b.failover && (
            <Card>
              <CardHeader>
                <CardTitle>Provider failover</CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="text-[10px] overflow-auto">
                  {JSON.stringify(b.failover, null, 2)}
                </pre>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
