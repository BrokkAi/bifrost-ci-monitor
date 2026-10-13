import type { BatchDetail, Status } from "@/api/types";
import { outcomeStatus } from "./outcomes";
export type BatchCheckpoint = {
  key: string;
  title: string;
  label: string;
  status: Status;
  iconValue?: string;
};
function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}
function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}
// These are recorded checkpoints, not a guessed sequence of agent activities.
// Terminal outcomes never imply that absent intermediate receipts exist.
export function batchCheckpoints(batch: BatchDetail): BatchCheckpoint[] {
  const candidate = object(batch.state.candidate);
  const tests = object(batch.state.tests);
  const publication = object(batch.state.publication);
  const ready = object(batch.state.ready);
  const head = text(candidate?.head) ?? batch.integration_pr?.head_sha ?? null;
  const testedHead = text(tests?.head);
  const publishedHead =
    text(publication?.head) ?? batch.integration_pr?.head_sha ?? null;
  const readyHead = text(ready?.head);
  const testsMatch = Boolean(head && testedHead && head === testedHead);
  const publicationPresent = Boolean(
    batch.integration_pr || typeof publication?.number === "number",
  );
  const publicationOld = Boolean(
    head && publishedHead && head !== publishedHead,
  );
  const readyOld = Boolean(head && readyHead && head !== readyHead);
  return [
    {
      key: "candidate",
      title: "Candidate",
      label: text(candidate?.head) ? "Registered" : "Not available",
      status: text(candidate?.head) ? "landed" : "unknown",
    },
    {
      key: "tests",
      title: "Tests",
      label: !tests
        ? "Not recorded"
        : !testsMatch
          ? testedHead && head
            ? "Earlier head"
            : "Recorded"
          : tests.verdict === "pass"
            ? "Passed"
            : tests.verdict === "fail"
              ? "Failed"
              : "Recorded",
      status: !tests
        ? "waiting"
        : !testsMatch
          ? "unknown"
          : tests.verdict === "pass"
            ? "landed"
            : tests.verdict === "fail"
              ? "failed"
              : "unknown",
    },
    {
      key: "publication",
      title: "Publication",
      label: !publicationPresent
        ? "Not published"
        : publicationOld
          ? "Earlier head"
          : "Published",
      status: !publicationPresent
        ? "waiting"
        : publicationOld
          ? "unknown"
          : "landed",
    },
    {
      key: "ready",
      title: "Handoff",
      label: !ready ? "Not recorded" : readyOld ? "Earlier head" : "Recorded",
      status: !ready ? "waiting" : readyOld ? "unknown" : "landed",
    },
    {
      key: "landing",
      title: "Landing",
      label:
        batch.outcome !== "other"
          ? batch.outcome[0].toUpperCase() + batch.outcome.slice(1)
          : batch.terminal_status || batch.finished_at
            ? "Other"
            : "Not landed",
      status:
        batch.outcome !== "other"
          ? outcomeStatus(batch.outcome)
          : batch.terminal_status || batch.finished_at
            ? "unknown"
            : "waiting",
      iconValue:
        batch.outcome === "aborted" || batch.outcome === "withdrawn"
          ? batch.outcome
          : undefined,
    },
  ];
}
