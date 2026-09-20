/**
 * Supervision store — reactive state over the canonical HTTP API.
 *
 * Guarantees:
 *  * loading state only ever calls GET endpoints; opening or refreshing a page
 *    can never start an execution (no `run` call anywhere in a load path);
 *  * execution is always an explicit user action (`startMission`), and starting
 *    is idempotent per mission while a run is already in flight;
 *  * nothing here derives mission status — the backend value is displayed as-is.
 *
 * P9B: state-affecting events trigger a debounced, coalesced refresh of the
 * canonical data. Unknown events are appended but do not trigger refresh.
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

/** Canonical event topics that change visible state.
 *
 * Mirrors the runtime event topics emitted by `veya/supervision`
 * (loop, external, retask, router, reviewer). Audit with:
 *   grep -rE 'MISSION_|EXECUTION_|REPORT_|REVIEW_|ESCALATION_|SUPERVISOR_|EVIDENCE_' v
 * Unknown events are displayed but never trigger a refresh.
 */
export const STATE_AFFECTING_TOPICS: ReadonlySet<string> = new Set([
  "MISSION_CREATED",
  "MISSION_STARTED",
  "SUPERVISOR_SELECTED",
  "SUPERVISION_MODE_CHANGED",
  "SUPERVISOR_SWITCHED",
  "EXECUTOR_STARTED",
  "EXECUTION_STARTED",
  "EXECUTION_COMPLETED",
  "EXECUTOR_COMPLETED",
  "EXECUTION_INTERRUPTED",
  "EXECUTION_PROCESS_GROUP_TERMINATED",
  "REPORT_CREATED",
  "EVIDENCE_COLLECTED",
  "REVIEW_COMPLETED",
  "RETASK_CREATED",
  "ESCALATED",
  "MISSION_ACCEPTED",
  "MISSION_DONE",
  "MISSION_BLOCKED",
  "MISSION_CANCELLED",
  "JEV_DECISION",
]);

/** Detail store for one mission. */
export function createMissionDetailStore(missionId: string) {
  let state = $state<MissionDetailState>(emptyDetail());
  let runInFlight = $state(false);
  let streamError = $state<string | null>(null);
  let disposeStream: (() => void) | null = null;

  // P9B refresh coalescing: burst events collapse into one trailing request.
  let refreshInFlight = false;
  let refreshPending = false;
  const REFRESH_DEBOUNCE_MS = 150;
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;

  /** Read-only refresh: reports, reviews, escalations, events, mission.
   *
   * Used both for explicit reloads and for SSE-driven auto-refresh.
   * `force` bypasses coalescing (explicit UI refresh).
   */
  async function load(force = false): Promise<void> {
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
    // Drain a debounced auto-refresh if one was queued while this ran.
    if (!force && refreshPending) {
      refreshPending = false;
      refreshAfterDebounce();
    }
  }

  /** Debounce + coalesce: multiple events in 150ms → 1 refresh.
   *  If a refresh is in flight when events arrive, exactly one trailing
   *  refresh is queued after the in-flight one finishes (max 1 pending).
   */
  function refreshAfterDebounce(): void {
    if (refreshTimer !== null) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      if (refreshInFlight) {
        refreshPending = true;
        return;
      }
      void refreshCanonical();
    }, REFRESH_DEBOUNCE_MS);
  }

  async function refreshCanonical(): Promise<void> {
    if (refreshInFlight) return;
    refreshInFlight = true;
    try {
      await load();
    } finally {
      refreshInFlight = false;
      if (refreshPending) {
        refreshPending = false;
        refreshAfterDebounce();
      }
    }
  }

  /** Classify whether an event should trigger a canonical data refresh. */
  function isStateAffecting(event: MissionEvent): boolean {
    return STATE_AFFECTING_TOPICS.has(event.topic ?? "");
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
          if (isStateAffecting(event)) refreshAfterDebounce();
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
    if (refreshTimer !== null) clearTimeout(refreshTimer);
    refreshInFlight = false;
    refreshPending = false;
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
    refreshCanonical,
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
