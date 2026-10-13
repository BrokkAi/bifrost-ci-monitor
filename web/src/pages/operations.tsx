import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  Bot,
  Check,
  Database,
  FileCode2,
  FlaskConical,
  Info,
  Play,
  Send,
  Terminal,
} from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type { KnownFailure, OutboxIntent, SqlResult } from "@/api/types";
import { runSql, useApi, useHistory, ApiError } from "@/api/client";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { HistoryFooter } from "@/components/history-footer";
import { DataTable } from "@/components/data-table";
import {
  PageHeading,
  Loading,
  ErrorState,
  ExternalLink,
  Sha,
  RelativeTime,
  EmptyState,
} from "@/components/common";
import { StatusBadge } from "@/components/status";
import { display, plural, sentenceCase } from "@/lib/utils";
const ledgerColumns: ColumnDef<KnownFailure>[] = [
  {
    accessorKey: "identity",
    header: "Failure identity",
    cell: ({ row }) => (
      <div className="max-w-[370px] min-w-60">
        <code className="text-[10px] break-all">{row.original.identity}</code>
        <p className="text-[10px] text-muted-foreground mt-1.5 leading-relaxed">
          {row.original.diagnosis ?? "Awaiting a validated triage diagnosis."}
        </p>
      </div>
    ),
  },
  {
    accessorKey: "workflow",
    header: "Workflow / job",
    cell: ({ row }) => (
      <div className="whitespace-nowrap">
        {row.original.workflow}
        <p className="text-[10px] text-muted-foreground mt-1">
          {row.original.job_name}
        </p>
      </div>
    ),
  },
  {
    accessorKey: "triage_outcome",
    header: "Diagnosis",
    cell: ({ row }) => (
      <span className="text-muted-foreground">
        {display(row.original.triage_outcome)}
      </span>
    ),
  },
  {
    accessorKey: "last_seen_at",
    header: "Last observed",
    cell: ({ row }) => (
      <div className="whitespace-nowrap text-muted-foreground">
        <RelativeTime value={row.original.last_seen_at} />
        <div className="mt-1">
          <Sha value={row.original.last_seen_sha} />
        </div>
      </div>
    ),
  },
  {
    accessorKey: "linked_issue_url",
    header: "Related work",
    cell: ({ row }) => (
      <div className="space-y-1.5">
        {(row.original.triage_issue_url ?? row.original.linked_issue_url) && (
          <div>
            <ExternalLink
              href={
                row.original.triage_issue_url ?? row.original.linked_issue_url
              }
            >
              Issue #
              {(row.original.triage_issue_url ?? row.original.linked_issue_url)!
                .split("/")
                .pop()}
            </ExternalLink>
          </div>
        )}
        {row.original.linked_pr_url && (
          <div>
            <ExternalLink href={row.original.linked_pr_url}>
              PR #{row.original.linked_pr_url.split("/").pop()}
            </ExternalLink>
          </div>
        )}
        {!row.original.linked_issue_url &&
          !row.original.triage_issue_url &&
          !row.original.linked_pr_url &&
          "—"}
      </div>
    ),
  },
];
export function LedgerPage() {
  const q = useHistory("/ledger", "status=open&limit=50");
  if (q.isPending) return <Loading />;
  if (q.isError && !q.data)
    return <ErrorState error={q.error} retry={() => q.refetch()} />;
  const observations = q.data!.pages.flatMap((page) => page.items);
  return (
    <>
      <PageHeading
        title="CI failure ledger"
        description="Observed failures on master, with the evidence and repair work that follows."
        action={
          <StatusBadge
            value={observations.length ? "blocked" : "landed"}
            label={`${plural(observations.length, "observation")}${q.hasNextPage ? " loaded" : ""}`}
            className="mt-3"
          />
        }
      />
      <div className="grid sm:grid-cols-3 gap-4 mb-5">
        {[
          {
            label: q.hasNextPage
              ? "Open observations loaded"
              : "Open observations",
            value: observations.length,
            icon: FlaskConical,
          },
          {
            label: "With a diagnosis",
            value: observations.filter((f) => f.diagnosis).length,
            icon: FileCode2,
          },
          {
            label: "Linked to repair work",
            value: observations.filter((f) => f.linked_pr_url).length,
            icon: Check,
          },
        ].map((k) => (
          <Card className="kpi" key={k.label}>
            <div className="kpi-label">
              {k.label}
              <k.icon size={14} />
            </div>
            <div className="kpi-value">{k.value}</div>
          </Card>
        ))}
      </div>
      <Card>
        <DataTable
          data={observations}
          columns={ledgerColumns}
          searchPlaceholder="Search failure identity, workflow or diagnosis…"
        />
        <HistoryFooter
          hasMore={Boolean(q.hasNextPage)}
          loading={q.isFetchingNextPage}
          loaded={observations.length}
          loadMore={() => q.fetchNextPage()}
          error={q.isFetchNextPageError ? q.error : null}
        />
      </Card>
      <div className="info-banner mt-5">
        <Info size={14} />
        <span>
          Issue and PR links provide context. Recovery is established by passing
          observations at the tested commit.
        </span>
      </div>
    </>
  );
}
export function AgentsPage() {
  const triage = useHistory("/triage", "limit=2");
  const repairs = useHistory("/repairs", "limit=2");
  if (triage.isPending || repairs.isPending) return <Loading />;
  if (triage.isError && !triage.data)
    return <ErrorState error={triage.error} retry={() => triage.refetch()} />;
  if (repairs.isError && !repairs.data)
    return <ErrorState error={repairs.error} retry={() => repairs.refetch()} />;
  const repairPage = repairs.data!.pages[0];
  const repairHistory = repairs.data!.pages.flatMap((page) => page.recent);
  const triageHistory = triage.data!.pages.flatMap((page) => page.items);
  return (
    <>
      <PageHeading
        title="Agent workspace"
        description="Active and recent triage investigations and issue repairs."
        action={
          <div className="mt-3 text-xs text-muted-foreground flex gap-2 items-center">
            <Bot size={14} />
            {repairPage.active_count} active repairs · concurrency limit{" "}
            {repairPage.concurrency_limit}
          </div>
        }
      />
      <div className="detail-grid">
        <div className="space-y-5">
          <div>
            <h2 className="section-title mb-3">Issue repairs</h2>
            {repairPage.active_count > repairPage.active.length && (
              <div className="info-banner mb-3">
                Showing {repairPage.active.length} of{" "}
                {plural(repairPage.active_count, "active repair")}.
              </div>
            )}
            <div className="space-y-3">
              {repairPage.active.length ? (
                repairPage.active.map((r) => (
                  <Card key={r.id}>
                    <CardContent className="pt-5">
                      <div className="flex justify-between gap-3">
                        <div className="flex items-start gap-3">
                          <div className="rounded-lg border bg-muted p-2">
                            <Bot size={17} className="text-muted-foreground" />
                          </div>
                          <div>
                            <h3 className="text-sm font-medium">
                              <ExternalLink href={r.issue_url}>
                                Repair issue #{r.issue_number}
                              </ExternalLink>
                            </h3>
                            <p className="text-[10px] text-muted-foreground mt-1">
                              {r.id} <span className="mx-1">·</span> Started{" "}
                              <RelativeTime value={r.created_at} />
                            </p>
                          </div>
                        </div>
                        <StatusBadge
                          value={r.recovering ? "blocked" : r.status}
                          label={
                            r.recovering ? "Recovering" : sentenceCase(r.status)
                          }
                        />
                      </div>
                      <div className="grid grid-cols-2 gap-4 mt-5 pt-4 border-t">
                        <div>
                          <div className="metadata-label">Original session</div>
                          <code className="text-[10px]">
                            {r.session_id ?? "Awaiting launch"}
                          </code>
                        </div>
                        <div>
                          <div className="metadata-label">
                            Repair pull request
                          </div>
                          <div className="text-[11px]">
                            {r.repair_pr_number ? (
                              <span>#{r.repair_pr_number}</span>
                            ) : (
                              "Not published yet"
                            )}
                            {r.retry_pr_number &&
                              ` · retry #${r.retry_pr_number}`}
                          </div>
                        </div>
                      </div>
                      {r.last_error && (
                        <div className="info-banner mt-4">
                          <Info size={13} />
                          {r.last_error}
                        </div>
                      )}
                    </CardContent>
                  </Card>
                ))
              ) : (
                <Card>
                  <EmptyState title="No active repairs">
                    Eligible issues will fill available slots.
                  </EmptyState>
                </Card>
              )}
            </div>
          </div>
          {repairHistory.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Recent repairs</CardTitle>
              </CardHeader>
              <CardContent>
                {repairHistory.map((r) => (
                  <div
                    key={r.id}
                    className="flex justify-between py-3 border-t first:border-0 text-xs"
                  >
                    <ExternalLink href={r.issue_url}>
                      #{r.issue_number}
                    </ExternalLink>
                    <StatusBadge
                      value={r.status}
                      label={sentenceCase(r.status)}
                    />
                  </div>
                ))}
              </CardContent>
              <HistoryFooter
                hasMore={Boolean(repairs.hasNextPage)}
                loading={repairs.isFetchingNextPage}
                loaded={repairHistory.length}
                loadMore={() => repairs.fetchNextPage()}
                error={repairs.isFetchNextPageError ? repairs.error : null}
              />
            </Card>
          )}
        </div>
        <div>
          <h2 className="section-title mb-3">Triage investigations</h2>
          <div className="space-y-3">
            {triageHistory.length ? (
              triageHistory.map((t) => (
                <Card key={t.id}>
                  <CardContent className="pt-5">
                    <div className="flex justify-between items-start gap-3">
                      <h3 className="font-medium text-xs leading-relaxed">
                        {t.title}
                      </h3>
                      <StatusBadge
                        value={t.recovering ? "blocked" : t.status}
                        label={
                          t.recovering ? "Recovering" : sentenceCase(t.status)
                        }
                      />
                    </div>
                    <p className="text-[10px] text-muted-foreground mt-3">
                      {plural(t.observation_count, "observation")}{" "}
                      <span className="mx-1">·</span> {t.id}
                    </p>
                    <div className="grid grid-cols-2 gap-4 border-t pt-4 mt-4">
                      <div>
                        <div className="metadata-label">Captured base</div>
                        <Sha value={t.base_sha} />
                      </div>
                      <div>
                        <div className="metadata-label">Started</div>
                        <div className="metadata-value">
                          <RelativeTime value={t.created_at} />
                        </div>
                      </div>
                    </div>
                    <code className="block text-[9px] text-muted-foreground mt-3">
                      {t.session_id}
                    </code>
                    {t.last_error && (
                      <p className="text-xs text-muted-foreground mt-3">
                        {t.last_error}
                      </p>
                    )}
                  </CardContent>
                </Card>
              ))
            ) : (
              <Card>
                <EmptyState title="No triage investigations" />
              </Card>
            )}
          </div>
          <div className="mt-3">
            <HistoryFooter
              hasMore={Boolean(triage.hasNextPage)}
              loading={triage.isFetchingNextPage}
              loaded={triageHistory.length}
              loadMore={() => triage.fetchNextPage()}
              error={triage.isFetchNextPageError ? triage.error : null}
            />
          </div>
          <div className="info-banner mt-4">
            <Info size={13} />
            <span>
              Live sessions retain the original checkout, saved progress and
              reusable test evidence.
            </span>
          </div>
        </div>
      </div>
    </>
  );
}
const outboxColumns: ColumnDef<OutboxIntent>[] = [
  {
    accessorKey: "kind",
    header: "Intent",
    cell: ({ row }) => (
      <div>
        <div className="font-medium whitespace-nowrap">
          {sentenceCase(row.original.kind)}
        </div>
        <p className="text-[9px] text-muted-foreground mt-1">
          {row.original.intent_id}
        </p>
      </div>
    ),
  },
  {
    accessorKey: "batch_id",
    header: "Batch / target",
    cell: ({ row }) => (
      <div>
        <Link
          to={`/batches/${row.original.batch_id}`}
          className="external-link text-[10px]"
        >
          {row.original.batch_id}
        </Link>
        <p className="text-[10px] text-muted-foreground mt-1">
          {row.original.number ? `PR #${row.original.number}` : "—"}
        </p>
      </div>
    ),
  },
  {
    id: "delivery",
    header: "Delivery",
    cell: ({ row }) => (
      <StatusBadge
        value={
          row.original.cancelled_at
            ? "rejected"
            : row.original.delivered_at
              ? "landed"
              : row.original.last_error
                ? "failed"
                : "waiting"
        }
        label={
          row.original.cancelled_at
            ? "Cancelled"
            : row.original.delivered_at
              ? "Delivered"
              : row.original.last_error
                ? "Retrying"
                : "Pending"
        }
      />
    ),
  },
  {
    accessorKey: "attempts",
    header: "Attempts",
    cell: ({ row }) => <span className="numeric">{row.original.attempts}</span>,
  },
  {
    accessorKey: "last_error",
    header: "Delivery detail",
    cell: ({ row }) => (
      <div className="min-w-48 max-w-80 text-[10px] text-muted-foreground">
        {row.original.last_error ?? "No delivery error."}
        {row.original.next_attempt_at && !row.original.delivered_at && (
          <p className="mt-1">
            Next attempt <RelativeTime value={row.original.next_attempt_at} />
          </p>
        )}
      </div>
    ),
  },
  {
    accessorKey: "created_at",
    header: "Created",
    cell: ({ row }) => (
      <span className="whitespace-nowrap text-muted-foreground">
        <RelativeTime value={row.original.created_at} />
      </span>
    ),
  },
];
export function OutboxPage() {
  const [pending, setPending] = useState(true);
  const q = useHistory("/outbox", `pending=${pending}&limit=10`);
  if (q.isPending) return <Loading />;
  if (q.isError && !q.data)
    return <ErrorState error={q.error} retry={() => q.refetch()} />;
  const allItems = q.data!.pages.flatMap((page) => page.items);
  const items = pending
    ? allItems.filter((o) => !o.delivered_at && !o.cancelled_at)
    : allItems;
  return (
    <>
      <PageHeading
        title="GitHub outbox"
        description="Pending and completed GitHub writes, including delivery errors and retries."
        action={
          <Button variant="outline" size="sm" asChild>
            <Link to="/queue">
              View queue
              <ArrowRight />
            </Link>
          </Button>
        }
      />
      <div
        className="filter-tabs mb-5"
        role="group"
        aria-label="Delivery filters"
      >
        <button
          className="filter-tab"
          aria-pressed={pending}
          onClick={() => setPending(true)}
        >
          Pending delivery
        </button>
        <button
          className="filter-tab"
          aria-pressed={!pending}
          onClick={() => setPending(false)}
        >
          All intents
        </button>
      </div>
      <Card>
        <DataTable
          data={items}
          columns={outboxColumns}
          searchPlaceholder="Search intent kind, batch or error…"
        />
        <HistoryFooter
          hasMore={Boolean(q.hasNextPage)}
          loading={q.isFetchingNextPage}
          loaded={allItems.length}
          loadMore={() => q.fetchNextPage()}
          error={q.isFetchNextPageError ? q.error : null}
        />
      </Card>
      <div className="info-banner mt-5">
        <Send size={14} />
        <span>
          Accepted writes are reconciled before retrying. Delivery failures
          preserve the tested candidate and do not change the agent's batch
          revision.
        </span>
      </div>
    </>
  );
}
export function QueryPage() {
  const schema = useApi("/schema");
  const [sql, setSql] = useState(
    "SELECT batch_id, status, phase, created_at\nFROM automerge_batches\nORDER BY created_at DESC\nLIMIT 20;",
  );
  const mutation = useMutation({ mutationFn: runSql });
  const result: SqlResult | undefined = mutation.data;
  return (
    <>
      <PageHeading
        title="Query console"
        description="Read-only SQL queries against the supervisor database."
        action={
          <StatusBadge value="landed" label="Read-only" className="mt-3" />
        }
      />
      <div className="detail-grid">
        <div className="space-y-5">
          <Card>
            <CardHeader>
              <div className="flex items-center gap-2">
                <Terminal size={14} className="text-muted-foreground" />
                <CardTitle>SQL editor</CardTitle>
              </div>
              <span className="text-[10px] text-muted-foreground">
                SELECT / WITH
              </span>
            </CardHeader>
            <textarea
              aria-label="Read-only SQL statement"
              className="sql-editor border-t"
              value={sql}
              onChange={(e) => setSql(e.target.value)}
              spellCheck={false}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                  e.preventDefault();
                  if (!mutation.isPending && sql.trim()) mutation.mutate(sql);
                }
              }}
            />
            <div className="flex justify-between items-center border-t px-5 py-3">
              <span className="text-[10px] text-muted-foreground">
                Ctrl / ⌘ + Enter to run · 1,000 row limit
              </span>
              <Button
                size="sm"
                onClick={() => mutation.mutate(sql)}
                disabled={mutation.isPending || !sql.trim()}
              >
                <Play size={12} />
                {mutation.isPending ? "Running…" : "Run query"}
              </Button>
            </div>
          </Card>
          {mutation.isError && (
            <div role="alert" className="info-banner">
              <Info size={14} />
              {mutation.error instanceof ApiError &&
              mutation.error.status === 429
                ? "Two queries are already running. Wait for one to finish, then try again."
                : mutation.error.message}
            </div>
          )}
          <Card>
            <CardHeader>
              <CardTitle>Results</CardTitle>
              {result && (
                <span className="text-[10px] text-muted-foreground">
                  {plural(result.rows.length, "row")}{result.truncated && " · truncated"}
                </span>
              )}
            </CardHeader>
            {result ? (
              result.rows.length ? (
                <DataTable
                  data={result.rows.map((row, i) =>
                    Object.fromEntries([
                      ["_id", i],
                      ...result.columns.map((name, index) => [
                        name,
                        row[index],
                      ]),
                    ]),
                  )}
                  columns={result.columns.map((name) => ({
                    accessorKey: name,
                    header: name,
                    cell: ({ getValue }) => (
                      <span className="font-mono text-[10px]">
                        {getValue() === null ? (
                          "NULL"
                        ) : typeof getValue() === "object" ? (
                          JSON.stringify(getValue())
                        ) : typeof getValue() === "string" &&
                          /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(
                            String(getValue()),
                          ) &&
                          !Number.isNaN(Date.parse(String(getValue()))) ? (
                          <RelativeTime value={String(getValue())} />
                        ) : (
                          String(getValue())
                        )}
                      </span>
                    ),
                  }))}
                />
              ) : (
                <EmptyState title="The query returned no rows" />
              )
            ) : (
              <EmptyState title="Your result will appear here">
                Run a query to explore supervisor state.
              </EmptyState>
            )}
          </Card>
        </div>
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Database schema</CardTitle>
              <p className="section-caption">
                Only structured columns are available. Free-text columns are not
                exposed.
              </p>
            </div>
            <Database size={15} className="text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {schema.isPending && (
              <p className="text-xs text-muted-foreground">Loading schema…</p>
            )}
            {schema.isError && (
              <p className="text-xs text-muted-foreground">
                {schema.error.message}
              </p>
            )}
            {schema.data?.tables.map((table) => (
              <details
                key={table.name}
                className="border-t py-3 first:border-0 first:pt-0"
                open={table.name === "automerge_batches"}
              >
                <summary className="text-[10px] font-mono cursor-pointer break-all">
                  {table.name}
                </summary>
                <div className="mt-3 space-y-2 pl-3">
                  {table.columns.map((column) => (
                    <div
                      key={column.name}
                      className="flex justify-between text-[10px] gap-3"
                    >
                      <span className="font-mono text-muted-foreground">
                        {column.name}
                      </span>
                      <span className="text-[8px] text-muted-foreground">
                        {column.type}
                      </span>
                    </div>
                  ))}
                  <button
                    className="text-[10px] external-link mt-3"
                    onClick={() =>
                      setSql(`SELECT *\nFROM ${table.name}\nLIMIT 20;`)
                    }
                  >
                    Explore table
                    <ArrowRight size={11} />
                  </button>
                </div>
              </details>
            ))}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
