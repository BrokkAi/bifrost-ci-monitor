import type {
  BatchDetail,
  BatchSummary,
  Campaign,
  Endpoints,
  KnownFailure,
  OutboxIntent,
  PullRef,
  Queue,
  Repair,
  Resolution,
  Repairs,
  SqlResult,
  Stats,
  TriageJob,
} from "./types";
const date = (minutes = 0) =>
  new Date(Date.now() - minutes * 60000).toISOString();
export const sha = (seed: number) =>
  `${seed.toString(16).padStart(8, "0")}ac37f09b51d6e4802c9e135a6f1b4820`;
const url = (number: number) =>
  `https://github.com/BrokkAi/bifrost-dev/pull/${number}`;
const titles = [
  "Preserve context across incremental indexing",
  "Cache dependency graph for warm builds",
  "Fix streaming tool result ordering",
  "Improve workspace search relevance",
  "Handle interrupted agent checkpoints",
  "Refresh project discovery on file rename",
  "Add structured tracing for inference",
  "Reduce editor startup latency",
  "Recover stale background task handles",
  "Update Python toolchain discovery",
  "Respect per-project model preferences",
  "Fix git status refresh after checkout",
  "Add cancellation to repository scans",
  "Keep diagnostics stable during edits",
  "Simplify deployment configuration",
  "Support multi-root workspaces",
  "Fix completion menu keyboard navigation",
  "Retain terminal scrollback on reconnect",
  "Deduplicate concurrent repository fetches",
  "Show model usage in session history",
];
function pull(i: number): PullRef {
  return {
    number: 4730 + i,
    url: url(4730 + i),
    title: titles[i % titles.length],
    head_sha: sha(38591238 + i * 197),
  };
}
export const primary: BatchSummary = {
  batch_id: "batch-8f21c9",
  kind: "batch",
  target_ref: "master",
  lane: "queue",
  role: "primary",
  status: "running",
  phase: "building",
  outcome: "other",
  terminal_status: null,
  ci_mode: "async",
  created_at: date(24),
  finished_at: null,
  duration_seconds: 1440,
  sources: Array.from({ length: 6 }, (_, i) => pull(i)),
  excluded: [],
  integration_pr: {
    number: 4761,
    url: url(4761),
    title: "Integrate six workspace and inference improvements",
    head_sha: sha(315821749),
  },
  session_id: "mj-a7429f",
  predecessor_id: null,
  attempt_generation: 1,
  abort_reason: null,
  model_configuration: "multi-pr",
};
export const speculative: BatchSummary = {
  ...primary,
  batch_id: "batch-a914e2",
  role: "speculative",
  phase: "waiting_parent",
  created_at: date(8),
  sources: [pull(6), pull(7), pull(8)],
  integration_pr: null,
  predecessor_id: primary.batch_id,
  session_id: "mj-b17d32",
};
export const recent = Array.from(
  { length: 7 },
  (_, i): BatchSummary => ({
    ...primary,
    batch_id: `batch-${["7a3b92", "24e581", "d529a0", "18cb43", "9d7231", "fab712", "b41273"][i]}`,
    target_ref: i === 0 ? "stabilize/stable-20261012" : "master",
    status: i === 3 ? "failed" : "completed",
    phase: "terminal",
    terminal_status: [
      "merged",
      "direct_rejected_at_head",
      "aborted",
      "ci_round_limit",
      "no_sources_remain",
      "direct_source_changed",
      "merged",
    ][i],
    outcome: (
      [
        "landed",
        "rejected",
        "aborted",
        "failed",
        "withdrawn",
        "withdrawn",
        "landed",
      ] as const
    )[i],
    created_at: date(70 + i * 112),
    finished_at: date(42 + i * 112),
    duration_seconds: 1680 + i * 87,
    sources: Array.from({ length: [4, 2, 7, 1, 5, 3, 4][i] }, (_, n) =>
      pull(n + 20 + i),
    ),
    integration_pr: { ...pull(50 + i), title: "Integration batch" },
    abort_reason:
      i === 3
        ? "Standalone source failure; head returned to its author."
        : null,
  }),
);
export const queue: Queue = {
  generated_at: date(),
  source: "cached_pr_inventory",
  groups: (
    ["in_batch", "waiting", "dependency_blocked", "rejected", "draft"] as const
  ).map((key, g) => ({
    key,
    label: ["In a batch", "Waiting", "Dependency blocked", "Rejected", "Draft"][
      g
    ],
    items: Array.from({ length: [9, 13, 5, 3, 6][g] }, (_, n) => ({
      ...pull(n + [0, 9, 22, 27, 30][g]),
      author: ["davidbe", "jonathan", "maya-chen", "brokk-service"][n % 4],
      labels:
        g === 3 ? ["mergemarshall:rejected"] : n % 4 === 0 ? ["ci-fix"] : [],
      priority: g === 1 && n === 0 ? ("high" as const) : null,
      blocked_reason:
        g === 2
          ? `Waiting for prerequisite #${4712 + n} to land at its captured head.`
          : g === 3
            ? "Standalone test failure at the exact published head."
            : null,
      batch_id:
        g === 0 ? (n < 6 ? primary.batch_id : speculative.batch_id) : null,
    })),
  })),
};
export const outbox: OutboxIntent[] = [
  {
    intent_id: "intent-c189",
    batch_id: primary.batch_id,
    kind: "integration_comment",
    number: 4761,
    head_sha: primary.integration_pr!.head_sha,
    created_at: date(12),
    delivered_at: null,
    cancelled_at: null,
    attempts: 3,
    next_attempt_at: new Date(Date.now() + 180000).toISOString(),
    last_error:
      "GitHub returned 502. Delivery will retry with the original intent ID.",
  },
  {
    intent_id: "intent-314a",
    batch_id: speculative.batch_id,
    kind: "in_progress_label",
    number: 4736,
    head_sha: pull(6).head_sha,
    created_at: date(2),
    delivered_at: null,
    cancelled_at: null,
    attempts: 0,
    next_attempt_at: date(),
    last_error: null,
  },
  {
    intent_id: "intent-912f",
    batch_id: recent[0].batch_id,
    kind: "verdict",
    number: 4780,
    head_sha: sha(91921),
    created_at: date(46),
    delivered_at: date(42),
    cancelled_at: null,
    attempts: 1,
    next_attempt_at: null,
    last_error: null,
  },
];
outbox.push(
  ...Array.from(
    { length: 12 },
    (_, i): OutboxIntent => ({
      ...outbox[2],
      intent_id: `intent-older-${i}`,
      created_at: date(90 + i * 30),
      delivered_at: date(85 + i * 30),
      batch_id: recent[i % recent.length].batch_id,
    }),
  ),
);
export const ledger: KnownFailure[] = [
  "test_checkpoint_restores_workspace",
  "test_tool_result_preserves_order",
  "test_index_incremental_rename",
  "test_repository_fetch_dedup",
  "test_completion_keyboard_selection",
].map((identity, i) => ({
  workflow: ["CI", "Hourly CI", "Nightly CI"][i % 3],
  job_name: ["Rust tests", "Python tests", "UI integration"][i % 3],
  identity_kind: "test",
  identity,
  first_seen_sha: sha(17782 + i),
  last_seen_sha: sha(21273),
  last_seen_at: date(36 + i * 17),
  status: "open",
  triage_outcome: i === 0 ? "product_defect" : i === 1 ? "flaky" : null,
  diagnosis: [
    "Checkpoint restores HEAD but loses the workspace marker.",
    "Intermittent ordering mismatch during concurrent tool completion.",
    "Rename invalidation leaves a stale index entry.",
    null,
    "Focus is not restored after the completion menu closes.",
  ][i],
  linked_pr_url: i < 2 ? url(4752 + i) : null,
  linked_issue_url: `https://github.com/BrokkAi/bifrost-dev/issues/${4690 + i}`,
  triage_issue_url: `https://github.com/BrokkAi/bifrost-dev/issues/${4690 + i}`,
}));
export const triage: TriageJob[] = [
  {
    id: "triage-271",
    title: "Investigate incremental index and repository failures",
    status: "running",
    base_sha: sha(21273),
    created_at: date(17),
    finished_at: null,
    session_id: "mj-c31d27",
    last_error: null,
    observation_count: 3,
    recovering: false,
  },
  {
    id: "triage-269",
    title: "Classify nightly completion failures",
    status: "completed",
    base_sha: sha(17782),
    created_at: date(128),
    finished_at: date(96),
    session_id: "mj-c271a9",
    last_error: null,
    observation_count: 2,
    recovering: false,
  },
];
export const repairs: Repair[] = Array.from({ length: 3 }, (_, i) => ({
  id: `repair-${148 + i}`,
  issue_number: 4690 + i,
  issue_url: `https://github.com/BrokkAi/bifrost-dev/issues/${4690 + i}`,
  status: "running",
  session_id: `mj-f12${i}3a`,
  created_at: date(33 + i * 21),
  finished_at: null,
  repair_pr_number: i < 2 ? 4752 + i : null,
  retry_pr_number: null,
  last_error:
    i === 2 ? "Recovering the original session after a stopped turn." : null,
  recovering: i === 2,
}));
triage.push(
  ...Array.from(
    { length: 4 },
    (_, i): TriageJob => ({
      ...triage[1],
      id: `triage-${267 - i}`,
      created_at: date(300 + i * 60),
      finished_at: date(280 + i * 60),
      observation_count: 1 + i,
    }),
  ),
);
export const recentRepairs: Repair[] = Array.from({ length: 5 }, (_, i) => ({
  ...repairs[0],
  id: `repair-${147 - i}`,
  issue_number: 4670 + i,
  issue_url: `https://github.com/BrokkAi/bifrost-dev/issues/${4670 + i}`,
  status: "completed",
  created_at: date(180 + i * 60),
  finished_at: date(140 + i * 60),
  last_error: null,
  recovering: false,
  repair_pr_number: 4710 + i,
}));
function page<T>(items: T[], search?: string) {
  const params = new URLSearchParams(search);
  const offset = params.get("before") ? Number(atob(params.get("before")!)) : 0;
  const limit = Number(params.get("limit") ?? 50);
  return {
    items: items.slice(offset, offset + limit),
    next_cursor:
      offset + limit < items.length ? btoa(String(offset + limit)) : null,
  };
}
export function mockPage<
  K extends "/batches" | "/ledger" | "/outbox" | "/triage" | "/repairs",
>(path: K, search?: string): Endpoints[K] {
  const params = new URLSearchParams(search);
  switch (path) {
    case "/batches": {
      const state = params.get("state") ?? "all";
      const items =
        state === "active"
          ? [primary, speculative]
          : state === "recent"
            ? recent
            : [primary, speculative, ...recent];
      return page(
        items
          .filter(
            (item) => !params.get("kind") || item.kind === params.get("kind"),
          )
          .slice()
          .sort(
            (a, b) =>
              b.created_at.localeCompare(a.created_at) ||
              b.batch_id.localeCompare(a.batch_id),
          ),
        search,
      ) as Endpoints[K];
    }
    case "/ledger":
      return page(ledger, search) as Endpoints[K];
    case "/outbox":
      return page(
        (params.get("pending") !== "false"
          ? outbox.filter((o) => !o.delivered_at && !o.cancelled_at)
          : outbox
        )
          .slice()
          .sort(
            (a, b) =>
              b.created_at.localeCompare(a.created_at) ||
              b.intent_id.localeCompare(a.intent_id),
          ),
        search,
      ) as Endpoints[K];
    case "/triage":
      return page(triage, search) as Endpoints[K];
    case "/repairs": {
      const history = page(recentRepairs, search);
      const response: Repairs = {
        concurrency_limit: 5,
        active_count: repairs.length,
        active: repairs,
        recent: history.items,
        next_cursor: history.next_cursor,
      };
      return response as Endpoints[K];
    }
    default:
      throw new Error("Unknown history endpoint");
  }
}
export const stats: Stats = {
  days: Array.from({ length: 30 }, (_, i) => ({
    date: new Date(Date.now() - (29 - i) * 86400000).toISOString().slice(0, 10),
    landed_prs: [
      12, 18, 9, 22, 16, 8, 4, 14, 23, 17, 21, 28, 7, 5, 19, 26, 15, 23, 30, 9,
      6, 22, 31, 18, 25, 24, 11, 7, 21, 18,
    ][i],
    rejected_prs: i % 5 === 0 ? 2 : 0,
    batches_completed: 2 + (i % 4),
    batches_failed: i % 7 === 0 ? 1 : 0,
    median_batch_minutes: [
      42, 39, 45, 36, 38, 41, 37, 35, 33, 40, 34, 32, 36, 30, 34, 31, 29, 32,
      28, 30, 27, 31, 25, 28, 26, 29, 24, 28, 25, 27,
    ][i],
  })),
};
export const campaigns: Campaign[] = [
  {
    id: "stable-20261012",
    branch: "stabilize/stable-20261012",
    anchor_sha: sha(871422),
    requested_ref: "master",
    start_error: null,
    status: "paused",
    stage: "hourly",
    round: 2,
    tip_sha: sha(871423),
    pause_reason: "GitHub App lacks actions: write",
    requested_by: "jonathan",
    updated_at: date(3),
    created_at: date(180),
    finished_at: null,
    branch_url:
      "https://github.com/BrokkAi/bifrost-dev/tree/stabilize/stable-20261012",
    slack_thread_url: "https://slack.com/app_redirect?channel=ci",
    issues: [
      {
        number: 4690,
        stage: "hourly",
        source: "test_checkpoint_restores_workspace",
        state: "open",
        pr_number: 4752,
      },
      {
        number: 4691,
        stage: "hourly",
        source: "test_tool_result_preserves_order",
        state: "open",
        pr_number: 4753,
      },
    ],
    gate_merges: [
      {
        stage: "push",
        tip_sha: sha(871422),
        gate_branch: "stabilize/gate-push",
        pr_number: 4749,
        state: "merged",
      },
    ],
    latest_runs: {
      push: {
        sha: sha(871422),
        url: "https://github.com/BrokkAi/bifrost-dev/actions/runs/89127401",
        conclusion: "success",
      },
      hourly: {
        sha: sha(871423),
        url: "https://github.com/BrokkAi/bifrost-dev/actions/runs/89128244",
        conclusion: "failure",
      },
      nightly: null,
    },
  },
  ...Array.from(
    { length: 3 },
    (_, i): Campaign => ({
      id: `stable-2026100${9 - i * 2}`,
      branch: `stabilize/stable-2026100${9 - i * 2}`,
      anchor_sha: sha(871200 - i * 34),
      requested_ref: "master",
      start_error: null,
      status: i === 2 ? "aborted" : "completed",
      stage: i === 2 ? "nightly" : "complete",
      round: 2 + i,
      tip_sha: sha(871218 - i * 34),
      pause_reason: i === 2 ? "Superseded by a newer master anchor." : null,
      requested_by: "jonathan",
      updated_at: date(3000 + i * 2880),
      created_at: date(4400 + i * 2880),
      finished_at: date(3000 + i * 2880),
      branch_url: null,
      slack_thread_url: null,
      issues: [],
      gate_merges: [],
      latest_runs: {},
    }),
  ),
];
const health: Endpoints["/health"] = {
  entry_points: (["merger", "monitor", "triage", "stabilize"] as const).map(
    (name, i) => ({
      name,
      last_started_at: date(i === 2 ? 23 : 3),
      last_finished_at: date(i === 2 ? 21 : 2),
      last_outcome: "completed",
      last_error: null,
      last_skipped_at: null,
      state: i === 2 ? "stale" : "ok",
    }),
  ),
  skill_service: { reachable: true, version: "1.0" },
  db: { path_ok: true },
};
export const fixtures: Endpoints = {
  "/me": {
    login: "jonathan",
    permission: "admin",
    can_release: true,
    auth: "session",
  },
  "/health": health,
  "/overview": {
    generated_at: date(),
    health,
    pipeline: {
      primary,
      // `?campaign=1` previews a running release-branch batch.
      campaign:
        typeof window !== "undefined" &&
        new URLSearchParams(window.location.search).get("campaign") === "1"
          ? {
              ...speculative,
              batch_id: "batch-c71e0a",
              role: "primary",
              phase: "building",
              target_ref: "stabilize/20261013-204031-a9e122e8",
              predecessor_id: null,
              integration_pr: null,
            }
          : null,
      // `?speculative=none` previews the empty successor slot.
      speculative:
        typeof window !== "undefined" &&
        new URLSearchParams(window.location.search).get("speculative") === "none"
          ? null
          : speculative,
    },
    queue: {
      in_batch: 9,
      waiting: 13,
      dependency_blocked: 5,
      rejected: 3,
      draft: 6,
    },
    kpis: {
      landed_today: 18,
      landed_7_days: 124,
      median_batch_minutes_7_days: 27,
      open_ci_failures: 5,
      pending_outbox_writes: 2,
      active_repairs: 3,
    },
    recent,
    outbox: { pending: 2, failing: 1 },
    ledger: { open: 5, product: 5, infrastructure: 0 },
    triage: { active: 1, recovering: 0 },
    repairs: { active: 3, recovering: 1, concurrency_limit: 5 },
  },
  "/queue": queue,
  "/stats": stats,
  "/batches": page(
    [primary, speculative, ...recent].sort(
      (a, b) =>
        b.created_at.localeCompare(a.created_at) ||
        b.batch_id.localeCompare(a.batch_id),
    ),
  ),
  "/ledger": page(ledger),
  "/outbox": page(outbox),
  "/triage": page(triage),
  "/repairs": {
    concurrency_limit: 5,
    active_count: repairs.length,
    active: repairs,
    recent: recentRepairs,
    next_cursor: null,
  },
  "/schema": {
    tables: [
      {
        name: "automerge_batches",
        columns: [
          {
            name: "batch_id",
            type: "TEXT",
            nullable: false,
            primary_key: true,
          },
          { name: "status", type: "TEXT", nullable: true, primary_key: false },
          { name: "phase", type: "TEXT", nullable: true, primary_key: false },
          {
            name: "created_at",
            type: "TEXT",
            nullable: true,
            primary_key: false,
          },
        ],
      },
      {
        name: "known_failures",
        columns: [
          {
            name: "identity_kind",
            type: "TEXT",
            nullable: true,
            primary_key: false,
          },
          { name: "status", type: "TEXT", nullable: true, primary_key: false },
          {
            name: "last_seen_sha",
            type: "TEXT",
            nullable: true,
            primary_key: false,
          },
        ],
      },
      {
        name: "entrypoint_heartbeats",
        columns: [
          {
            name: "entry_point",
            type: "TEXT",
            nullable: true,
            primary_key: false,
          },
          {
            name: "last_finished_at",
            type: "TEXT",
            nullable: true,
            primary_key: false,
          },
        ],
      },
    ],
  },
  "/releases": campaigns,
  "/releases/resolve": {
    commitish: null,
    url: "https://github.com/BrokkAi/bifrost-dev/commit/" + sha(871419),
    sha: sha(871419),
    subject: "Preserve context during workspace recovery",
    committed_at: date(220),
    on_master: true,
    push_ci: {
      run_id: 89127102,
      url: "https://github.com/BrokkAi/bifrost-dev/actions/runs/89127102",
      conclusion: "success",
    },
    live_campaign_id: campaigns[0].id,
  },
};
export function mockBatch(id: string): BatchDetail {
  const batch = [primary, speculative, ...recent].find(
    (b) => b.batch_id === id,
  );
  if (!batch) throw new Error("Batch not found");
  const head = batch.integration_pr?.head_sha ?? sha(315821982);
  return {
    ...batch,
    execution_count: 3,
    outbox_count: outbox.filter((x) => x.batch_id === id).length,
    state: {
      revision: "12fb33a1",
      source_revision: "9eaa7201",
      candidate: { head },
      base_sha: sha(21273),
      tests: {
        head,
        verdict: "pass",
        tests:
          "Targeted checks pass at the exact candidate head. Two baseline observations registered.",
      },
      publication: batch.integration_pr
        ? {
            number: batch.integration_pr.number,
            url: batch.integration_pr.url,
            head: batch.integration_pr.head_sha,
          }
        : null,
      ready:
        batch.role === "speculative"
          ? {
              head,
              source_revision: "9eaa7201",
              attempt_generation: batch.attempt_generation,
              kind: "speculative",
              created_at: date(2),
            }
          : null,
    },
    supervisor_actions: [
      {
        action_id: "act-21a",
        attempts: 0,
        kind: "observe_primary",
        status: "pending",
        batch_id: id,
        updated_at: date(3),
        last_error: null,
        next_attempt_at: null,
      },
    ],
    executions: [
      "cargo nextest run -p bifrost-core",
      "uv run pytest tests/workspace -q",
      "npm run test -- --run",
    ].map((command, i) => ({
      command,
      execution_id: `exec-${i}`,
      attest_runs: [],
      head,
      exit_code:
        batch.role === "primary" && !batch.terminal_status && i === 2
          ? null
          : 0,
      duration_seconds: [418, 97, 161][i],
      started_at: date(11 - i * 3),
      attest:
        i === 0
          ? {
              mode: "local",
              metrics: { total: 428, passed: 428, failed: 0, reused: 0 },
            }
          : null,
    })),
    failover: null,
    outbox: outbox.filter((x) => x.batch_id === id),
    slack_thread_ts: null,
  };
}
export function mockSql(sql: string): SqlResult {
  if (
    !/^\s*(select|with)\b/i.test(sql) ||
    /\b(insert|update|delete|drop|attach|pragma)\b/i.test(sql)
  )
    throw new Error("Only a read-only SELECT or WITH statement is allowed.");
  return {
    columns: ["batch_id", "status", "phase", "created_at"],
    rows: [primary, speculative, ...recent].map((b) => [
      b.batch_id,
      b.status,
      b.phase,
      b.created_at,
    ]),
    truncated: false,
  };
}
export function mockResolution(commitish?: string): Resolution {
  return {
    ...fixtures["/releases/resolve"],
    sha: /^[a-f0-9]{40}$/i.test(commitish ?? "")
      ? commitish!
      : fixtures["/releases/resolve"].sha,
    live_campaign_id:
      campaigns.find((c) =>
        ["running", "starting", "paused"].includes(c.status),
      )?.id ?? null,
  };
}
