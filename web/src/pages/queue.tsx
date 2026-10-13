import { useSearchParams, Link } from "react-router-dom";
import { plural } from "@/lib/utils";
import { Info, GitPullRequest, Flag } from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type { QueueItem } from "@/api/types";
import { useApi } from "@/api/client";
import { Card } from "@/components/ui/card";
import { DataTable } from "@/components/data-table";
import {
  PageHeading,
  Loading,
  ErrorState,
  ExternalLink,
  Sha,
  RelativeTime,
} from "@/components/common";
import { StatusBadge } from "@/components/status";
const columns: ColumnDef<QueueItem & { group: string }>[] = [
  {
    accessorKey: "number",
    header: "PR",
    cell: ({ row }) => (
      <ExternalLink href={row.original.url}>
        <span className="text-muted-foreground">#</span>
        {row.original.number}
      </ExternalLink>
    ),
  },
  {
    accessorKey: "title",
    header: "Change",
    cell: ({ row }) => (
      <div className="min-w-56 max-w-[410px]">
        <ExternalLink href={row.original.url} className="font-medium">
          {row.original.title}
        </ExternalLink>
        {row.original.blocked_reason && (
          <p className="text-[10px] text-muted-foreground mt-1.5">
            {row.original.blocked_reason}
          </p>
        )}
        {(row.original.labels ?? []).length > 0 && (
          <div className="flex gap-1 mt-1.5">
            {(row.original.labels ?? []).map((label) => (
              <span
                key={label}
                className="rounded border px-1.5 py-0.5 text-[8px] text-muted-foreground"
              >
                {label}
              </span>
            ))}
          </div>
        )}
      </div>
    ),
  },
  {
    accessorKey: "author",
    header: "Author",
    cell: ({ row }) => (
      <span className="text-muted-foreground">
        {row.original.author ?? "—"}
      </span>
    ),
  },
  {
    accessorKey: "group",
    header: "Readiness",
    cell: ({ row }) => (
      <StatusBadge
        value={
          {
            in_batch: "running",
            waiting: "waiting",
            dependency_blocked: "blocked",
            rejected: "rejected",
            draft: "waiting",
          }[row.original.group]
        }
        label={
          {
            in_batch: "In batch",
            waiting: "Waiting",
            dependency_blocked: "Blocked",
            rejected: "Rejected",
            draft: "Draft",
          }[row.original.group]
        }
      />
    ),
  },
  {
    accessorKey: "head_sha",
    header: "Head",
    cell: ({ row }) => <Sha value={row.original.head_sha} />,
  },
  {
    accessorKey: "batch_id",
    header: "Batch / priority",
    cell: ({ row }) =>
      row.original.batch_id ? (
        <Link
          to={`/batches/${row.original.batch_id}`}
          className="external-link text-[10px]"
        >
          {row.original.batch_id}
        </Link>
      ) : row.original.priority ? (
        <span className="flex items-center gap-1">
          <Flag size={11} />
          {row.original.priority}
        </span>
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
];
export function QueuePage() {
  const query = useApi("/queue");
  const [params, setParams] = useSearchParams();
  const group = params.get("group") ?? "all";
  if (query.isPending) return <Loading />;
  if (query.isError)
    return <ErrorState error={query.error} retry={() => query.refetch()} />;
  const rows = query.data.groups.flatMap((g) =>
    g.items.map((item) => ({ ...item, group: g.key })),
  );
  const visible =
    group === "all" ? rows : rows.filter((r) => r.group === group);
  return (
    <>
      <PageHeading
        title="Pull request queue"
        description="Open pull requests from the cached inventory, grouped by status."
        action={
          <div className="flex items-center gap-2 text-xs text-muted-foreground mt-3">
            <GitPullRequest size={14} />
            {plural(rows.length, "open pull request")}
          </div>
        }
      />
      <div
        className="filter-tabs mb-5"
        role="group"
        aria-label="Queue readiness filters"
      >
        {[
          { key: "all", label: "All changes", items: rows },
          ...query.data.groups,
        ].map((g) => (
          <button
            key={g.key}
            className="filter-tab"
            aria-pressed={group === g.key}
            onClick={() => setParams(g.key === "all" ? {} : { group: g.key })}
          >
            {g.label}
            <span className="filter-tab-count">{g.items.length}</span>
          </button>
        ))}
      </div>
      <Card>
        <DataTable
          columns={columns}
          data={visible}
          searchPlaceholder="Search changes, authors or heads…"
          pageSize={12}
        />
      </Card>
      <div className="info-banner mt-5">
        <Info size={14} />
        <span>
          Derived from the cached PR inventory, rather than a live selection
          run. Snapshot updated <RelativeTime value={query.data.generated_at} />
          . Dependencies are verified by commit ancestry.
        </span>
      </div>
    </>
  );
}
