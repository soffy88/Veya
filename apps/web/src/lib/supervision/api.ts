/**
 * Supervision API client — thin wrappers over the existing `api()` helper.
 *
 * Only the canonical `/api/v1/supervision/*` surface is used; the browser never
 * talks to MCP, Hicode/DSH, or any provider directly, and never sees a token
 * other than the user's own Veya session token (attached by `api()`).
 */

import { api } from "../api";
import type {
  Escalation,
  ExecutionReport,
  Mission,
  MissionCreateInput,
  MissionEvent,
  MissionInspect,
  ReviewDecision,
  SupervisorReview,
  SupervisionMode,
} from "./types";

const BASE = "api/v1/supervision";

export interface ApiOutcome<T> {
  ok: boolean;
  status: number;
  data: T | null;
  /** Backend `detail` string (403/404/400) for honest error display. */
  error: string | null;
}

function wrap<T>(res: { ok: boolean; status: number; data: unknown }): ApiOutcome<T> {
  const detail =
    res.data && typeof res.data === "object" && "detail" in (res.data as Record<string, unknown>)
      ? String((res.data as Record<string, unknown>).detail)
      : null;
  return {
    ok: res.ok,
    status: res.status,
    data: res.ok ? (res.data as T) : null,
    error: res.ok ? null : (detail ?? `HTTP ${res.status}`),
  };
}

export async function createMission(input: MissionCreateInput): Promise<ApiOutcome<{ mission: Mission }>> {
  return wrap<{ mission: Mission }>(
    await api("gateway", `${BASE}/missions`, {
      method: "POST",
      body: {
        goal: input.goal,
        supervision_mode: input.supervision_mode,
        workspace: input.workspace ?? "",
        executor: input.executor ?? "",
        acceptance_criteria: input.acceptance_criteria ?? [],
        constraints: input.constraints ?? [],
      },
    }),
  );
}

export async function listMissions(): Promise<ApiOutcome<{ missions: Mission[] }>> {
  return wrap<{ missions: Mission[] }>(
    await api("gateway", `${BASE}/missions`, { method: "GET" }),
  );
}

export async function inspectMission(missionId: string): Promise<ApiOutcome<MissionInspect>> {
  return wrap<MissionInspect>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}`, { method: "GET" }),
  );
}

export async function runMission(missionId: string): Promise<ApiOutcome<Record<string, unknown>>> {
  return wrap<Record<string, unknown>>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}/run`, {
      method: "POST",
    }),
  );
}

export async function cancelMission(missionId: string): Promise<ApiOutcome<Record<string, unknown>>> {
  return wrap<Record<string, unknown>>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}/cancel`, {
      method: "POST",
    }),
  );
}

/**
 * Canonical review/retask path. The UI's Retry uses this with decision RETRY —
 * it never re-issues a fresh `run`, so one mission keeps one lineage.
 */
export async function reviewMission(
  missionId: string,
  review: { decision: ReviewDecision; reason?: string; next_task?: string | null },
): Promise<ApiOutcome<Record<string, unknown>>> {
  return wrap<Record<string, unknown>>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}/continue`, {
      method: "POST",
      body: { review },
    }),
  );
}

export async function setSupervisionMode(
  missionId: string,
  mode: SupervisionMode,
): Promise<ApiOutcome<{ mission: Mission }>> {
  return wrap<{ mission: Mission }>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}/mode`, {
      method: "POST",
      body: { mode },
    }),
  );
}

export async function latestReport(
  missionId: string,
): Promise<ApiOutcome<{ report: ExecutionReport | null }>> {
  return wrap<{ report: ExecutionReport | null }>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}/reports/latest`, {
      method: "GET",
    }),
  );
}

export async function reportByIteration(
  missionId: string,
  iteration: number,
): Promise<ApiOutcome<{ report: ExecutionReport | null }>> {
  return wrap<{ report: ExecutionReport | null }>(
    await api(
      "gateway",
      `${BASE}/missions/${encodeURIComponent(missionId)}/reports/${encodeURIComponent(String(iteration))}`,
      { method: "GET" },
    ),
  );
}

export async function listReviews(
  missionId: string,
): Promise<ApiOutcome<{ reviews: SupervisorReview[] }>> {
  return wrap<{ reviews: SupervisorReview[] }>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}/reviews`, {
      method: "GET",
    }),
  );
}

export async function listEscalations(
  missionId: string,
): Promise<ApiOutcome<{ escalations: Escalation[] }>> {
  return wrap<{ escalations: Escalation[] }>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}/escalations`, {
      method: "GET",
    }),
  );
}

/** Polling fallback for clients without streaming support. */
export async function listEvents(
  missionId: string,
): Promise<ApiOutcome<{ events: MissionEvent[] }>> {
  return wrap<{ events: MissionEvent[] }>(
    await api("gateway", `${BASE}/missions/${encodeURIComponent(missionId)}/events`, {
      method: "GET",
      query: { format: "json" },
    }),
  );
}

export function eventsStreamPath(missionId: string, since = 0): string {
  const qs = new URLSearchParams({ format: "sse", since: String(Math.max(0, since)) });
  return `${BASE}/missions/${encodeURIComponent(missionId)}/events?${qs.toString()}`;
}
