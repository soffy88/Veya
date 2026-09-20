/**
 * Canonical supervision contracts — a field-for-field mirror of the backend.
 *
 * Source of truth: `veya/supervision/models.py` (`to_dict()` of Mission,
 * ExecutionReport and SupervisorReview), `veya/supervision/external.py::inspect`
 * and the raw mission event log. Do NOT add a second state machine or invent
 * status values: everything here is what the backend actually emits.
 *
 * Drift is guarded by `tests/runtime/test_supervision_web_contracts.py`, which
 * compares these field lists against the live canonical `to_dict()` output.
 */

/** Mission lifecycle status (canonical `MissionStatus` values, verbatim). */
export type MissionStatus =
  | "CREATED"
  | "ROUTING_SUPERVISOR"
  | "DESIGNING"
  | "PLANNING"
  | "EXECUTING"
  | "COLLECTING_EVIDENCE"
  | "FAST_DECISION"
  | "REVIEWING"
  | "RETASKING"
  | "WAITING_EXTERNAL_SUPERVISOR"
  | "WAITING_OWNER"
  | "ACCEPTED"
  | "DONE"
  | "BLOCKED"
  | "FAILED"
  | "CANCELLED";

/** `executed` only means the executor stopped — never a success verdict.
 *  `INTERRUPTED` is a *report* status (a mission stays REVIEWING). */
export type ReportStatus = "executed" | "INTERRUPTED" | string;

export type SupervisionMode = "external" | "internal" | "auto";
export type ExecutorKind = "hicode" | "dsh" | "worker" | "builtin" | "native_tool";

export type ReviewDecision =
  | "CONTINUE"
  | "REVISE"
  | "RETRY"
  | "ROLLBACK"
  | "ACCEPT"
  | "DONE"
  | "ESCALATE";

export interface MissionBudget {
  max_iterations: number;
  max_runtime_s: number | null;
  max_external_reviews: number | null;
  max_jev_calls: number | null;
  cost_policy: string;
  latency_policy: string;
}

export interface MissionPolicies {
  supervisor_policy: Record<string, unknown>;
  execution_policy: Record<string, unknown>;
  review_policy: Record<string, unknown>;
}

export interface Mission {
  mission_id: string;
  goal: string;
  supervision_mode: SupervisionMode;
  constraints: string[];
  acceptance_criteria: string[];
  workspace: string;
  authority: Record<string, unknown>;
  autonomy: Record<string, unknown>;
  budget: MissionBudget;
  deadline: number | null;
  priority: string;
  policies: MissionPolicies;
  created_at: number;
  updated_at: number;
  status: MissionStatus;
}

export interface ExecutionReport {
  mission_id: string;
  goalrun_id: string | null;
  iteration: number;
  checkpoint_id: string | null;
  objective: string;
  status: ReportStatus;
  changes: Array<Record<string, unknown>>;
  tests: Array<Record<string, unknown>>;
  artifacts: Array<Record<string, unknown>>;
  runtime_evidence: Array<Record<string, unknown>>;
  git_diff_summary: Record<string, unknown>;
  failures: Array<Record<string, unknown>>;
  unresolved_risks: Array<Record<string, unknown>>;
  deviations: Array<Record<string, unknown>>;
  blocked_items: Array<Record<string, unknown>>;
  jev_decisions: Array<JevDecisionRecord>;
  executor_summary: string;
  proposed_next_action: string | null;
  created_at: number;
}

/** One Jev answer as persisted on the report (advisory only). */
export interface JevDecisionRecord {
  question?: string;
  kind?: string;
  choice?: string | null;
  score?: number | null;
  noul?: string | null;
  confidence?: number | null;
  probabilities?: Record<string, number>;
  [key: string]: unknown;
}

export interface SupervisorReview {
  mission_id: string;
  iteration: number;
  supervisor: "external" | "internal" | string;
  decision: ReviewDecision;
  reason: string;
  next_task: string | null;
  constraints_delta: string[];
  acceptance_delta: string[];
  required_evidence: string[];
  risk_notes: string[];
  confidence: number | null;
  created_at: number;
}

/** Canonical event log record: `{ ts, topic, ...payload }`. */
export interface MissionEvent {
  ts: number;
  topic: string;
  [key: string]: unknown;
}

/** `ESCALATED` events are the owner-facing escalation list. */
export type Escalation = MissionEvent;

export interface MissionInspect {
  mission: Mission;
  current_supervisor: string;
  lineage: Array<Record<string, unknown>>;
  latest_report: ExecutionReport | null;
  latest_review: SupervisorReview | null;
}

export interface MissionCreateInput {
  goal: string;
  supervision_mode: SupervisionMode;
  workspace?: string;
  executor?: ExecutorKind | "";
  acceptance_criteria?: string[];
  constraints?: string[];
}
