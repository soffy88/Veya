/**
 * Approvals API client (NANO-W6) — the approvals inbox, the deep-link page and
 * the inline chat card all resolve through here, over the shared `$lib/api`
 * transport (same-origin + bearer token).
 *
 * The backend answers 404 on every route while VEYA_APPROVAL_GATES is off; the
 * UI reports that plainly instead of showing an empty inbox.
 */

import { api } from "$lib/api";

export type ApprovalDecision = "once" | "session" | "always" | "deny";

export interface ApprovalView {
	request_id: string;
	scope: string;
	tool: string;
	tool_args?: unknown;
	status: string;
	decision?: string | null;
	decision_reason?: string | null;
	created_at?: number;
	resolved_at?: number | null;
}

export interface GrantView {
	id: number;
	tool: string;
	scope: string;
	scope_pattern?: string;
	created_at?: number;
}

export class GatesDisabledError extends Error {
	constructor() {
		super("审批闸门未开启（VEYA_APPROVAL_GATES）");
	}
}

function fail(status: number, data: unknown): never {
	if (status === 404) throw new GatesDisabledError();
	const detail = (data as { detail?: unknown } | null)?.detail;
	throw new Error(typeof detail === "string" ? detail : `请求失败 (HTTP ${status})`);
}

export async function listApprovals(status = "pending"): Promise<ApprovalView[]> {
	const res = await api("gateway", "api/v1/approvals", { method: "GET", query: { status } });
	if (!res.ok) fail(res.status, res.data);
	return ((res.data as { approvals?: ApprovalView[] }).approvals ?? []) as ApprovalView[];
}

export async function getApproval(id: string): Promise<ApprovalView | null> {
	for (const status of ["pending", ""]) {
		const rows = await listApprovals(status);
		const hit = rows.find((row) => row.request_id === id);
		if (hit) return hit;
	}
	return null;
}

export async function resolveApproval(id: string, decision: ApprovalDecision, reason = ""): Promise<void> {
	const res = await api("gateway", `api/v1/approvals/${encodeURIComponent(id)}/resolve`, {
		method: "POST",
		body: { decision, reason },
	});
	if (!res.ok) fail(res.status, res.data);
	const status = (res.data as { status?: string } | null)?.status;
	if (status && status !== "completed") {
		const err = (res.data as { error?: string }).error;
		throw new Error(err || "这条审批已被处理或已失效");
	}
}

export async function listGrants(): Promise<GrantView[]> {
	const res = await api("gateway", "api/v1/grants", { method: "GET" });
	if (!res.ok) fail(res.status, res.data);
	return ((res.data as { grants?: GrantView[] }).grants ?? []) as GrantView[];
}

export async function revokeGrant(id: number): Promise<void> {
	const res = await api("gateway", `api/v1/grants/${id}`, { method: "DELETE" });
	if (!res.ok) fail(res.status, res.data);
}

export const decisionLabel: Record<ApprovalDecision, string> = {
	once: "允许一次",
	session: "本会话允许",
	always: "总是允许",
	deny: "拒绝",
};

export function argsPreview(args: unknown, limit = 240): string {
	if (args === undefined || args === null) return "";
	const text = typeof args === "string" ? args : JSON.stringify(args);
	return text.length > limit ? `${text.slice(0, limit)}…` : text;
}
