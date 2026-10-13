import type { components } from "./schema";
type SchemaModel = components["schemas"];
export type Status =
  | "running"
  | "waiting"
  | "blocked"
  | "landed"
  | "rejected"
  | "failed"
  | "unknown";
export type Me = SchemaModel["Me"];
export type PullRef = SchemaModel["PullRef"];
export type Exclusion = SchemaModel["Exclusion"];
export type Outcome = SchemaModel["Outcome"];
export type BatchesPage = SchemaModel["BatchesPage"];
export type LedgerPage = SchemaModel["LedgerPage"];
export type OutboxPage = SchemaModel["OutboxPage"];
export type TriagePage = SchemaModel["TriagePage"];
export type BatchSummary = SchemaModel["BatchSummary"];
export type BatchDetail = SchemaModel["BatchDetail"];
export type OutboxIntent = SchemaModel["OutboxIntent"];
export type Health = SchemaModel["Health"];
export type QueueItem = SchemaModel["QueueItem"];
export type Queue = SchemaModel["Queue"];
export type KnownFailure = SchemaModel["KnownFailure"];
export type TriageJob = SchemaModel["TriageJob"];
export type Repair = SchemaModel["Repair"];
export type Repairs = SchemaModel["Repairs"];
export type Stats = SchemaModel["Stats"];
export type ReleaseLane = SchemaModel["ReleaseLane"];
export type AttentionFailure = SchemaModel["AttentionFailure"];
export type Overview = SchemaModel["Overview"];
export type Execution = SchemaModel["ExecutionSummary"];
export type Resolution = SchemaModel["Resolution"];
export type Campaign = SchemaModel["Campaign"];
export type Schema = SchemaModel["Schema"];
export type SqlResult = SchemaModel["SqlResult"];
export type Endpoints = {
  "/me": Me;
  "/health": Health;
  "/overview": Overview;
  "/queue": Queue;
  "/stats": Stats;
  "/batches": BatchesPage;
  "/ledger": LedgerPage;
  "/outbox": OutboxPage;
  "/triage": TriagePage;
  "/repairs": Repairs;
  "/schema": Schema;
  "/releases": Campaign[];
  "/releases/resolve": Resolution;
};

export type HistoryEndpoint =
  | "/batches"
  | "/ledger"
  | "/outbox"
  | "/triage"
  | "/repairs";
