import {
  CircleCheck,
  CircleHelp,
  CircleX,
  Clock3,
  LoaderCircle,
  ShieldAlert,
  Ban,
  CircleSlash,
  Undo2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { Status, BatchSummary, Outcome } from "@/api/types";
const neutralIcons: Record<string, typeof CircleSlash> = {
  aborted: CircleSlash,
  withdrawn: Undo2,
};
const icons = {
  running: LoaderCircle,
  waiting: Clock3,
  blocked: ShieldAlert,
  landed: CircleCheck,
  rejected: Ban,
  failed: CircleX,
  unknown: CircleHelp,
};
export function normalizeStatus(value: string | null | undefined): Status {
  if (!value) return "unknown";
  if (
    [
      "running",
      "finishing",
      "integrating",
      "validating",
      "starting",
      "investigating",
      "repairing",
      "in_progress",
      "launching",
      "active",
      "selected",
    ].includes(value)
  )
    return "running";
  if (
    [
      "landed",
      "complete",
      "completed",
      "success",
      "passed",
      "pass",
      "delivered",
      "merged",
      "published",
      "submitted",
      "resolved",
      "ok",
    ].includes(value)
  )
    return "landed";
  if (
    [
      "waiting",
      "pending",
      "queued",
      "stale",
      "draft",
      "ci_wait",
      "waiting_for_predecessor",
    ].includes(value)
  )
    return "waiting";
  if (
    ["blocked", "paused", "dependency_blocked", "recovering", "open"].includes(
      value,
    )
  )
    return "blocked";
  if (["rejected"].includes(value)) return "rejected";
  if (["failed", "failing", "failure", "error"].includes(value))
    return "failed";
  return "unknown";
}
export function StatusBadge({
  value,
  label,
  className,
}: {
  value: string | null | undefined;
  label?: string;
  className?: string;
}) {
  const status = normalizeStatus(value);
  const Icon = neutralIcons[value ?? ""] ?? icons[status];
  return (
    <span className={cn("status-badge", `status-${status}`, className)}>
      <Icon size={12} strokeWidth={1.9} />
      <span>
        {label ??
          (value && neutralIcons[value]
            ? value[0].toUpperCase() + value.slice(1)
            : status[0].toUpperCase() + status.slice(1))}
      </span>
    </span>
  );
}

export function StatusIcon({
  value,
  size = 14,
}: {
  value: string;
  size?: number;
}) {
  const status = normalizeStatus(value);
  const Icon = neutralIcons[value ?? ""] ?? icons[status];
  return (
    <Icon
      size={size}
      aria-hidden="true"
      style={{ color: `var(--status-${status})` }}
    />
  );
}

export function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  return (
    <StatusBadge
      value={outcome}
      label={outcome[0].toUpperCase() + outcome.slice(1)}
    />
  );
}
export function BatchStatus({
  batch,
}: {
  batch: Pick<
    BatchSummary,
    "outcome" | "status" | "terminal_status" | "finished_at"
  >;
}) {
  return batch.outcome !== "other" ||
    batch.terminal_status ||
    batch.finished_at ||
    batch.status === "completed" ||
    batch.status === "failed" ? (
    <OutcomeBadge outcome={batch.outcome} />
  ) : (
    <StatusBadge value={batch.status} />
  );
}
