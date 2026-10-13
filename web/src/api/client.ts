import createClient from "openapi-fetch";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { paths } from "./schema";
import type {
  BatchDetail,
  Campaign,
  Endpoints,
  HistoryEndpoint,
  Resolution,
  SqlResult,
} from "./types";
import {
  campaigns,
  fixtures,
  mockBatch,
  mockResolution,
  mockSql,
  mockPage,
} from "./mock";
export const isMock = import.meta.env.VITE_MOCK === "1";
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string,
  ) {
    super(message);
  }
}
export const api = createClient<paths>({
  baseUrl: "",
  credentials: "same-origin",
});
api.use({
  onResponse({ response }) {
    if (response.status === 401) window.location.assign("/auth/login");
    return response;
  },
});
function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): T {
  if (!result.response.ok || result.error) {
    const error = result.error as
      | { error?: { message?: string; code?: string } }
      | undefined;
    throw new ApiError(
      error?.error?.message ??
        `The request could not be completed (${result.response.status}).`,
      result.response.status,
      error?.error?.code,
    );
  }
  if (result.data === undefined)
    throw new ApiError(
      "The server returned an empty response.",
      result.response.status,
    );
  return result.data;
}
// The small endpoint dispatch keeps each request checked by the generated OpenAPI types.
async function liveGet<K extends keyof Endpoints>(
  path: K,
  search?: string,
): Promise<Endpoints[K]> {
  const p = new URLSearchParams(search);
  let data: Endpoints[keyof Endpoints];
  const paging = {
    limit: Number(p.get("limit") ?? 50),
    before: p.get("before") ?? undefined,
  };
  switch (path) {
    case "/batches":
      data = unwrap(
        await api.GET("/api/v1/batches", {
          params: {
            query: {
              ...paging,
              state: (p.get("state") ?? "all") as "active" | "recent" | "all",
              kind: p.get("kind") ?? undefined,
            },
          },
        }),
      );
      break;
    case "/me":
      data = unwrap(await api.GET("/api/v1/me"));
      break;
    case "/health":
      data = unwrap(await api.GET("/api/v1/health"));
      break;
    case "/overview":
      data = unwrap(await api.GET("/api/v1/overview"));
      break;
    case "/queue":
      data = unwrap(await api.GET("/api/v1/queue"));
      break;
    case "/stats":
      data = unwrap(
        await api.GET("/api/v1/stats", {
          params: { query: { days: Number(p.get("days") ?? 30) } },
        }),
      );
      break;
    case "/ledger":
      data = unwrap(
        await api.GET("/api/v1/ledger", {
          params: { query: { ...paging, status: p.get("status") ?? "open" } },
        }),
      );
      break;
    case "/outbox":
      data = unwrap(
        await api.GET("/api/v1/outbox", {
          params: {
            query: { ...paging, pending: p.get("pending") !== "false" },
          },
        }),
      );
      break;
    case "/triage":
      data = unwrap(
        await api.GET("/api/v1/triage", {
          params: { query: paging },
        }),
      );
      break;
    case "/repairs":
      data = unwrap(
        await api.GET("/api/v1/repairs", {
          params: { query: paging },
        }),
      );
      break;
    case "/schema":
      data = unwrap(await api.GET("/api/v1/schema"));
      break;
    case "/releases":
      data = unwrap(await api.GET("/api/v1/releases"));
      break;
    case "/releases/resolve":
      data = unwrap(
        await api.GET("/api/v1/releases/resolve", {
          params: { query: { commitish: p.get("commitish") ?? undefined } },
        }),
      );
      break;
    default:
      throw new Error("Unknown endpoint");
  }
  return data as Endpoints[K];
}
export async function get<K extends keyof Endpoints>(
  path: K,
  search?: string,
): Promise<Endpoints[K]> {
  if (isMock) {
    await new Promise((resolve) => setTimeout(resolve, 180));
    if (
      (path === "/releases" || path === "/releases/resolve") &&
      new URLSearchParams(window.location.search).get("unavailable") === "1"
    )
      throw new ApiError(
        "Release machinery is not available on this host yet.",
        503,
        "release_machinery_unavailable",
      );
    if (
      path === "/me" &&
      new URLSearchParams(window.location.search).get("permission") === "write"
    )
      return {
        ...fixtures["/me"],
        permission: "write",
        can_release: false,
      } as Endpoints[K];
    if (
      path === "/batches" ||
      path === "/ledger" ||
      path === "/outbox" ||
      path === "/triage" ||
      path === "/repairs"
    )
      return mockPage(path, search) as Endpoints[K];
    return fixtures[path];
  }
  return liveGet(path, search);
}
export function useApi<K extends keyof Endpoints>(path: K, search?: string) {
  return useQuery({
    queryKey: [path, search],
    queryFn: () => get(path, search),
  });
}
export function useHistory<K extends HistoryEndpoint>(
  path: K,
  search?: string,
) {
  return useInfiniteQuery({
    queryKey: [path, search, "history"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const query = new URLSearchParams(search);
      if (pageParam) query.set("before", pageParam);
      return get(path, query.toString());
    },
    getNextPageParam: (page: Endpoints[K]) => page.next_cursor ?? undefined,
    placeholderData: undefined,
  });
}
export function useBatch(id: string) {
  return useQuery({
    queryKey: ["/batches", id],
    enabled: Boolean(id),
    queryFn: async (): Promise<BatchDetail> =>
      isMock
        ? mockBatch(id)
        : unwrap(
            await api.GET("/api/v1/batches/{batch_id}", {
              params: { path: { batch_id: id } },
            }),
          ),
  });
}
export async function resolveRelease(commitish?: string): Promise<Resolution> {
  if (isMock) {
    if (new URLSearchParams(window.location.search).get("unavailable") === "1")
      throw new ApiError(
        "Release machinery is not available on this host yet.",
        503,
      );
    return mockResolution(commitish);
  }
  return unwrap(
    await api.GET("/api/v1/releases/resolve", {
      params: { query: { commitish } },
    }),
  );
}
const mockReleaseKeys = new Map<string, Campaign>();
export async function createRelease(
  sha: string,
  key: string,
): Promise<Campaign> {
  if (isMock) {
    const existing = mockReleaseKeys.get(key);
    if (existing) return existing;
    if (
      campaigns.some((c) =>
        ["running", "starting", "paused"].includes(c.status),
      )
    )
      throw new ApiError("A stabilization campaign is already live.", 409);
    const campaign: Campaign = {
      ...campaigns[0],
      id: "stable-new",
      status: "starting",
      stage: "push",
      tip_sha: sha,
      issues: [],
      gate_merges: [],
      latest_runs: {},
      created_at: new Date().toISOString(),
      finished_at: null,
    };
    campaigns.unshift(campaign);
    mockReleaseKeys.set(key, campaign);
    return campaign;
  }
  return unwrap(
    await api.POST("/api/v1/releases", {
      body: { sha, idempotency_key: key },
      signal: AbortSignal.timeout(130000),
    }),
  );
}
export async function abortRelease(id: string): Promise<Campaign> {
  if (isMock) {
    const campaign = campaigns.find((c) => c.id === id)!;
    campaign.status = "aborted";
    campaign.finished_at = new Date().toISOString();
    return campaign;
  }
  return unwrap(
    await api.POST("/api/v1/releases/{id}/abort", {
      params: { path: { id } },
      headers: { "Content-Type": "application/json" },
      body: undefined,
    }),
  );
}
export async function runSql(sql: string): Promise<SqlResult> {
  if (
    isMock &&
    new URLSearchParams(window.location.search).get("sql_busy") === "1"
  )
    throw new ApiError("Two queries are already running.", 429, "sql_busy");
  return isMock
    ? mockSql(sql)
    : unwrap(await api.POST("/api/v1/sql", { body: { sql, limit: 1000 } }));
}
