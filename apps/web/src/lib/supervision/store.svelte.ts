/**
 * Supervision store — reactive state over the canonical HTTP API.
 *
 * Guarantees:
 *  * loading state only ever calls GET endpoints; opening or refreshing a page
 *    can never start an execution (no `run` call anywhere in a load path);
 *  * execution is always an explicit user action (`startMission`), and starting
 *    is idempotent per mission while a run is already in flight;
 *  * nothing here derives mission status — the backend value is displayed as-is.
 */

import {
  cancelMission,
  createMission,
  inspectMission,
  latestReport,
  listEscalations,
  listMissions,
  listReviews,
  reviewMission,
  runMission,
  setSupervisionMode,
} from "./api";
import { subscribeMissionEvents } from "./events";
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

export interface MissionDetailState {
  inspect: MissionInspect | null;
  report: ExecutionReport | null;
  reviews: SupervisorReview[];
  escalations: Escalation[];
  events: MissionEvent[];
  loading: boolean;
  error: string | null;
}

function emptyDetail(): MissionDetailState {
  return {
    inspect: null,
    report: null,
    reviews: [],
    escalations: [],
    events: [],
    loading: false,
    error: null,
  };
}

/** List store (factory: call once per page/component tree). */
export function createMissionListStore() {
  let missions = $state<Mission[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load(): Promise<void> {
    loading = true;
    error = null;
    const res = await listMissions();
    if (res.ok) missions = res.data?.missions ?? [];
    else error = res.error ?? "加载失败";
    loading = false;
  }

  return {
    get missions() {
      return missions;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    load,
  };
}

/** Detail store for one mission. */
export function createMissionDetailStore(missionId: string) {
  let state = $state<MissionDetailState>(emptyDetail());
  let runInFlight = $state(false);
  let streamError = $state<string | null>(null);
  let disposeStream: (() => void) | null = null;

  /** Read-only refresh: reports, reviews, escalations, events, mission. */
  async function load(): Promise<void> {
    state.loading = true;
    state.error = null;
    const [inspect, report, reviews, escalations] = await Promise.all([
      inspectMission(missionId),
      latestReport(missionId),
      listReviews(missionId),
      listEscalations(missionId),
    ]);
    if (inspect.ok && inspect.data) state.inspect = inspect.data;
    else state.error = inspect.error ?? "加载失败";
    if (report.ok) state.report = report.data?.report ?? null;
    if (reviews.ok) state.reviews = reviews.data?.reviews ?? [];
    if (escalations.ok) state.escalations = escalations.data?.escalations ?? [];
    state.loading = false;
  }

  /** Live events (SSE, polling fallback). Resumes from the count already held. */
  function subscribe(): void {
    disposeStream?.();
    streamError = null;
    disposeStream = subscribeMissionEvents(
      missionId,
      state.events.length,
      {
        onEvent: (event) => {
          state.events = [...state.events, event];
        },
        onError: (message) => {
          streamError = message;
        },
      },
    );
  }

  function dispose(): void {
    disposeStream?.();
    disposeStream = null;
  }

  /** Explicit user action — the only path that can start an execution. */
  async function start(): Promise<void> {
    if (runInFlight) return; // idempotent: a second click cannot double-dispatch
    runInFlight = true;
    try {
      const res = await runMission(missionId);
      if (!res.ok) state.error = res.error ?? "启动失败";
      await load();
    } finally {
      runInFlight = false;
    }
  }

  async function cancel(): Promise<void> {
    const res = await cancelMission(missionId);
    if (!res.ok) state.error = res.error ?? "取消失败";
    await load();
  }

  /** Retry goes through the canonical review/retask path, never a second run. */
  async function retry(reason = "retry requested from Web UI"): Promise<void> {
    const decision: ReviewDecision = "RETRY";
    const res = await reviewMission(missionId, { decision, reason });
    if (!res.ok) state.error = res.error ?? "重试失败";
    await load();
  }

  async function applyReview(decision: ReviewDecision, reason = ""): Promise<void> {
    const res = await reviewMission(missionId, { decision, reason });
    if (!res.ok) state.error = res.error ?? "审查提交失败";
    await load();
  }

  async function switchMode(mode: SupervisionMode): Promise<void> {
    const res = await setSupervisionMode(missionId, mode);
    if (!res.ok) state.error = res.error ?? "切换失败";
    await load();
  }

  return {
    get state() {
      return state;
    },
    get runInFlight() {
      return runInFlight;
    },
    get streamError() {
      return streamError;
    },
    load,
    subscribe,
    dispose,
    start,
    cancel,
    retry,
    applyReview,
    switchMode,
  };
}

/** Create-page helper: creates the Mission, then hands off to the detail route. */
export async function submitMission(
  input: MissionCreateInput,
): Promise<{ missionId: string | null; error: string | null }> {
  const res = await createMission(input);
  if (!res.ok || !res.data) return { missionId: null, error: res.error ?? "创建失败" };
  return { missionId: res.data.mission.mission_id, error: null };
}
