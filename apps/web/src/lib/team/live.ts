/**
 * Live steps for a bot turn (W11 W-2).
 *
 * A bot turn is a real product task, so its tool calls are already recorded as
 * canonical events by the tool registry (``tool.requested`` / ``tool.completed``
 * / ``tool.failed`` with the tool name and args). The read-only workbench
 * projection (``GET /api/v1/workbench/{task_id}``) renders exactly those events
 * as a redacted timeline, plus the turn's pending side-effect approvals.
 *
 * This module maps that projection onto the `ToolStep` shape `WorkProcess` already
 * renders, so the bot chat shows the same human-readable steps as the console
 * instead of a second progress widget. Polling is deliberate: the bot turn's
 * async path installs no ``on_step`` callback, so ``/stream/{session_id}``
 * carries only ``session.created`` for it — claiming a live SSE feed here would
 * be a claim the backend does not back.
 *
 * It reads only. Nothing here starts, schedules or records anything.
 */

import { api } from "$lib/api";
import type { ToolStep } from "$lib/chatTypes";

export interface CanonicalEvent {
	event_id?: string;
	topic?: string;
	ts?: number | string;
	payload?: Record<string,unknown>;
	[key: string]: unknown;
}

/** Topics that are a tool call the user should see, mapped to a step type. */
const STEP_TOPICS: Record<string, ToolStep["type"]> = {
	"tool.requested": "tool_call",
	"tool.call": "tool_call",
	"tool.started": "tool_call",
	"tool.completed": "tool_result",
	"tool.result": "tool_result",
	"tool.failed": "tool_error",
	"tool.cancelled": "tool_error",
};

/** Payload key holding a short human string for each topic. */
const DETAIL_KEYS = ["detail", "summary", "output", "result", "message", "error", "reason"];

function firstString(payload: Record<string, unknown>, keys: string[]): string {
	for (const key of keys) {
		const value = payload[key];
		if (typeof value === "string" && value.trim()) return value;
	}
	return "";
}

/**
 * One canonical envelope → one `ToolStep`, or `null` when the event is not a
 * step the user should read (turn bookkeeping, internal activity, approvals —
 * approvals get their own card, not a step).
 */
export function stepFromEvent(event: CanonicalEvent): ToolStep | null {
	const topic = String(event.topic ?? "");
	const type = STEP_TOPICS[topic];
	if (!type) return null;
	const payload = (event.payload ?? {}) as Record<string, unknown>;
	const step: ToolStep = {
		type,
		tool_name: firstString(payload, ["tool_name", "tool", "name"]) || "tool",
		status: String(payload.status ?? ""),
	};
	const args = payload.tool_args ?? payload.args ?? payload.arguments;
	if (args && typeof args === "object") step.tool_args = args;
	const detail = firstString(payload, DETAIL_KEYS);
	if (detail) step.detail = detail;
	return step;
}

/**
 * A redacted timeline → the steps to show, oldest first. One event is one step:
 * there is no client-side re-ordering of what the server recorded.
 */
export function stepsFromTimeline(timeline: unknown): ToolStep[] {
	const rows = Array.isArray(timeline) ? (timeline as CanonicalEvent[]) : [];
	const steps: ToolStep[] = [];
	for (const row of rows) {
		const step = stepFromEvent(row);
		if (step) steps.push(step);
	}
	return steps.slice(-60);
}

export interface PendingApproval {
	request_id: string;
	tool_name: string;
	reason: string;
	tool_args?: unknown;
}

/** The turn's pending side-effect approvals, as the projection describes them. */
export function pendingFromView(view: unknown): PendingApproval[] {
	const rows = (view as { approvals?: { pending?: unknown[] } } | null)?.approvals?.pending;
	if (!Array.isArray(rows)) return [];
	return rows
		.map((row) => row as Record<string, unknown>)
		.filter((row) => String(row.status ?? "pending") === "pending")
		.map((row) => ({
			request_id: String(row.request_id ?? ""),
			tool_name: String(row.tool_name ?? "tool"),
			reason: String(row.reason ?? "需要你批准"),
			tool_args: row.tool_args,
		}))
		.filter((row) => row.request_id);
}

export interface TaskLive {
	/** `true` while the task's own status says it is still running. */
	running: boolean;
	status: string;
	steps: ToolStep[];
	approvals: PendingApproval[];
}

const RUNNING_STATUSES = new Set(["running", "pending", "waiting_approval", "verifying", "finalizing"]);

/**
 * One read of the turn's live state. Returns `null` when the projection cannot
 * answer (task not visible yet, or a flaky minute) — the caller keeps whatever
 * it already had rather than blanking the panel.
 */
export async function fetchTaskLive(taskId: string): Promise<TaskLive | null> {
	const result = await api("gateway", `api/v1/workbench/${encodeURIComponent(taskId)}`, { method: "GET" });
	if (!result.ok || !result.data || typeof result.data !== "object") return null;
	const view = result.data as Record<string, unknown>;
	const status = String((view.state as { status?: unknown } | undefined)?.status ?? "");
	return {
		running: RUNNING_STATUSES.has(status),
		status,
		steps: stepsFromTimeline(view.timeline),
		approvals: pendingFromView(view),
	};
}

/** Answer one pending side-effect approval through the existing control route. */
export async function resolveTaskApproval(taskId: string, requestId: string, approved: boolean): Promise<void> {
	const result = await api(
		"gateway",
		`api/v1/workbench/${encodeURIComponent(taskId)}/approval`,
		{ method: "POST", body: { request_id: requestId, approved } },
	);
	if (!result.ok) {
		const detail =
			typeof result.data === "string"
				? result.data
				: ((result.data as { detail?: unknown } | null)?.detail ?? `HTTP ${result.status}`);
		throw new Error(String(detail));
	}
}
