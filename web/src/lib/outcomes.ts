import type { Outcome, Status } from "@/api/types";
export function outcomeStatus(outcome: Outcome): Status {
  return outcome === "landed" || outcome === "rejected" || outcome === "failed"
    ? outcome
    : "unknown";
}
