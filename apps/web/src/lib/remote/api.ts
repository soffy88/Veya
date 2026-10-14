/**
 * Remote-control API client (W13) — hosts, remote agent sessions, file handoff
 * and the wake policy, over the shared `$lib/api` transport (same-origin +
 * bearer token).
 *
 * The backend answers 404 on every route while VEYA_REMOTE_CONTROL is off; the
 * UI reports that plainly instead of showing an empty page.
 */

import { api } from "$lib/api";

export interface RemoteHostView {
	host_id: string;
	display_name: string;
	tailnet_ip: string;
	agent_label: string;
	agent_command: string;
	owner_id: string;
	workspace_path: string;
	enabled: boolean;
	last_seen_at: number | null;
	created_at: number;
	ssh_user?: string;
	ssh_port?: number;
	os_family?: string;
	write_root?: string;
	default_cwd?: string;
	read_approved?: boolean;
}

export interface RemoteProbeView {
	host_id: string;
	reachable: boolean;
	latency_ms: number;
	os_family: string;
	error?: string;
}

export interface RemoteRunView {
	name: string;
	pid?: number;
	running?: boolean;
	exit_code?: number | null;
	log_path?: string;
}

export interface HostSshInput {
	ssh_user?: string;
	ssh_port?: number;
	ssh_key_ref?: string;
	os_family?: string;
	write_root?: string;
	default_cwd?: string;
	read_approved?: boolean;
}

export interface RemoteSessionView {
	session_id: string;
	host_id: string;
	agent_label: string;
	task: string;
	state: string;
	wake_outcome: string;
	routine_id: string;
	handoff_to: string;
	created_at: number;
	updated_at: number;
}

export interface RemoteCardView {
	approval_id: string;
	tool: string;
	session_id: string;
	host_id: string;
	reason: string;
	created_at?: number;
	resolve_url: string;
}

export interface WakePolicyView {
	sender: string;
	agent_label: string;
	ceiling: string;
	rate_limit: number;
	rate_window_s: number;
	batch_interval_minutes: number;
}

export class RemoteDisabledError extends Error {
	constructor() {
		super("远程控制未开启（VEYA_REMOTE_CONTROL）");
	}
}

function fail(status: number, data: unknown): never {
	if (status === 404) throw new RemoteDisabledError();
	const detail = (data as { detail?: unknown } | null)?.detail;
	throw new Error(typeof detail === "string" ? detail : `请求失败 (HTTP ${status})`);
}

// ── hosts ───────────────────────────────────────────────────────────────

export async function listHosts(): Promise<RemoteHostView[]> {
	const res = await api("gateway", "api/v1/remote/hosts", { method: "GET" });
	if (!res.ok) fail(res.status, res.data);
	return ((res.data as { hosts?: RemoteHostView[] }).hosts ?? []) as RemoteHostView[];
}

export async function createHost(input: {
	host_id: string;
	tailnet_ip: string;
	display_name?: string;
	agent_label?: string;
}): Promise<RemoteHostView> {
	const res = await api("gateway", "api/v1/remote/hosts", { method: "POST", body: input });
	if (!res.ok) fail(res.status, res.data);
	return res.data as RemoteHostView;
}

export async function deleteHost(hostId: string): Promise<void> {
	const res = await api("gateway", `api/v1/remote/hosts/${encodeURIComponent(hostId)}`, {
		method: "DELETE",
	});
	if (!res.ok) fail(res.status, res.data);
}

export async function probeHost(hostId: string): Promise<RemoteProbeView> {
	const res = await api("gateway", `api/v1/remote/hosts/${encodeURIComponent(hostId)}/probe`, {
		method: "GET",
	});
	if (!res.ok) fail(res.status, res.data);
	return res.data as RemoteProbeView;
}

export async function updateHostSsh(hostId: string, input: HostSshInput): Promise<RemoteHostView> {
	const res = await api("gateway", `api/v1/remote/hosts/${encodeURIComponent(hostId)}/ssh`, {
		method: "POST",
		body: input,
	});
	if (!res.ok) fail(res.status, res.data);
	return res.data as RemoteHostView;
}

export async function listRuns(hostId: string): Promise<RemoteRunView[]> {
	const res = await api("gateway", `api/v1/remote/hosts/${encodeURIComponent(hostId)}/runs`, {
		method: "GET",
	});
	if (!res.ok) fail(res.status, res.data);
	return ((res.data as { runs?: RemoteRunView[] }).runs ?? []) as RemoteRunView[];
}

export async function killRun(hostId: string, name: string, approvalId = ""): Promise<{ status: string }> {
	const res = await api("gateway", `api/v1/remote/hosts/${encodeURIComponent(hostId)}/runs/${encodeURIComponent(name)}/kill`, {
		method: "POST",
		body: { approval_id: approvalId },
	});
	if (!res.ok) fail(res.status, res.data);
	return res.data as { status: string };
}

// ── sessions ────────────────────────────────────────────────────────────

export async function listSessions(activeOnly = false): Promise<RemoteSessionView[]> {
	const res = await api("gateway", "api/v1/remote/sessions", {
		method: "GET",
		query: { active: activeOnly },
	});
	if (!res.ok) fail(res.status, res.data);
	return ((res.data as { sessions?: RemoteSessionView[] }).sessions ?? []) as RemoteSessionView[];
}

export async function startSession(input: {
	host_id: string;
	task: string;
	agent_label?: string;
	requested_wake?: string;
}): Promise<{ session: RemoteSessionView; approval_id: string }> {
	const res = await api("gateway", "api/v1/remote/sessions", { method: "POST", body: input });
	if (!res.ok) fail(res.status, res.data);
	return res.data as { session: RemoteSessionView; approval_id: string };
}

export async function applySession(
	sessionId: string,
	approvalId = "",
): Promise<{ session: RemoteSessionView; launched: boolean }> {
	const res = await api("gateway", `api/v1/remote/sessions/${encodeURIComponent(sessionId)}/apply`, {
		method: "POST",
		body: { approval_id: approvalId },
	});
	if (!res.ok) fail(res.status, res.data);
	return res.data as { session: RemoteSessionView; launched: boolean };
}

export async function cancelSession(sessionId: string): Promise<void> {
	const res = await api("gateway", `api/v1/remote/sessions/${encodeURIComponent(sessionId)}/cancel`, {
		method: "POST",
	});
	if (!res.ok) fail(res.status, res.data);
}

export async function handoffSession(sessionId: string, targetHostId: string): Promise<void> {
	const res = await api("gateway", `api/v1/remote/sessions/${encodeURIComponent(sessionId)}/handoff`, {
		method: "POST",
		body: { target_host_id: targetHostId },
	});
	if (!res.ok) fail(res.status, res.data);
}

// ── approvals (cards feed the existing resolve route) ─────────────────────

export async function listPendingCards(): Promise<RemoteCardView[]> {
	const res = await api("gateway", "api/v1/remote/pending-approvals", { method: "GET" });
	if (!res.ok) fail(res.status, res.data);
	return ((res.data as { cards?: RemoteCardView[] }).cards ?? []) as RemoteCardView[];
}

// ── wake policy ─────────────────────────────────────────────────────────

export async function listWakePolicies(): Promise<WakePolicyView[]> {
	const res = await api("gateway", "api/v1/remote/wake-policy", { method: "GET" });
	if (!res.ok) fail(res.status, res.data);
	return ((res.data as { policies?: WakePolicyView[] }).policies ?? []) as WakePolicyView[];
}

export async function setWakePolicy(policy: WakePolicyView): Promise<WakePolicyView> {
	const res = await api("gateway", "api/v1/remote/wake-policy", { method: "PUT", body: policy });
	if (!res.ok) fail(res.status, res.data);
	return res.data as WakePolicyView;
}
