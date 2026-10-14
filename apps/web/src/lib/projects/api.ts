/**
 * Project Brain API Client (W16 Project Brain)
 *
 * Interfaces with /api/v1/projects endpoints.
 */

import { api, type ApiResult } from "$lib/api";
import type { BotMessage } from "$lib/team/api";

export interface ProjectView {
	id: string;
	project_id?: string;
	bot_id?: string;
	name: string;
	icon?: string;
	color?: string;
	project_root?: string;
	workspace_path?: string;
	owner_id?: string;
	description?: string;
	session_count?: number;
	created_at?: string;
	updated_at?: string;
}

export interface BriefRuleView {
	when: string;
	do: string;
	approval: "auto" | "ask";
}

export interface BriefView {
	goal_scope: string;
	key_files: string[];
	rules: BriefRuleView[];
	active_tasks: string;
	conventions: string;
	updated_at?: string;
}

export interface VerifierEvidenceView {
	usable: boolean;
	evidence?: string;
	outcome?: string;
	details?: Record<string, unknown>;
}

export interface ProjectTaskView {
	task_id: string;
	objective: string;
	goal_run_id?: string;
	binding_id?: string;
	routine_id?: string;
	status: "accepted" | "running" | "verifying" | "done" | "failed" | "cancelled";
	last_report_seq?: number;
	verifier?: VerifierEvidenceView | null;
	fix_notes?: string[];
	report_seq?: number;
	max_repairs?: number;
	repairs_made?: number;
	remaining_repairs?: number;
	last_report_at?: number | null;
	created_at?: string;
	updated_at?: string;
}

export interface ProjectBriefResponse {
	project_id: string;
	bot_id: string;
	brief: BriefView;
	markdown: string;
}

export async function listProjects(): Promise<ProjectView[]> {
	const res = await api("gateway", "api/v1/projects");
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || "无法获取项目列表";
		throw new Error(err);
	}
	const data = res.data as { projects?: ProjectView[] };
	return data.projects ?? [];
}

export async function createProject(req: {
	name: string;
	description?: string;
	project_root?: string;
	workspace_path?: string;
	icon?: string;
}): Promise<ProjectView> {
	const res = await api("gateway", "api/v1/projects", {
		method: "POST",
		body: req,
	});
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || "创建项目失败";
		throw new Error(err);
	}
	const data = res.data as { project: ProjectView };
	return data.project;
}

export async function getProject(id: string): Promise<ProjectView> {
	const res = await api("gateway", `api/v1/projects/${encodeURIComponent(id)}`);
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || "项目未找到";
		throw new Error(err);
	}
	const data = res.data as { project: ProjectView };
	return data.project;
}

export async function getProjectBrief(id: string): Promise<ProjectBriefResponse> {
	const res = await api("gateway", `api/v1/projects/${encodeURIComponent(id)}/brief`);
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || "获取简报失败";
		throw new Error(err);
	}
	return res.data as ProjectBriefResponse;
}

export async function updateProjectBrief(
	id: string,
	req: {
		markdown?: string;
		section?: string;
		content?: unknown;
		sections?: Record<string, unknown>;
		rules?: unknown[];
	},
): Promise<ProjectBriefResponse> {
	const res = await api("gateway", `api/v1/projects/${encodeURIComponent(id)}/brief`, {
		method: "PUT",
		body: req,
	});
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || "更新简报失败";
		throw new Error(err);
	}
	return res.data as ProjectBriefResponse;
}

export async function getProjectMessages(id: string): Promise<BotMessage[]> {
	const res = await api("gateway", `api/v1/projects/${encodeURIComponent(id)}/messages`);
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || "获取消息失败";
		throw new Error(err);
	}
	const data = res.data as { messages?: BotMessage[] };
	return data.messages ?? [];
}

export async function postProjectMessage(
	id: string,
	text: string,
	options: { async?: boolean; workspace_id?: string } = {},
): Promise<unknown> {
	const res = await api("gateway", `api/v1/projects/${encodeURIComponent(id)}/messages`, {
		method: "POST",
		query: options.async ? { async: "1" } : undefined,
		body: {
			text,
			workspace_id: options.workspace_id,
		},
	});
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || "发送消息失败";
		throw new Error(err);
	}
	return res.data;
}

export async function getProjectTasks(id: string): Promise<ProjectTaskView[]> {
	const res = await api("gateway", `api/v1/projects/${encodeURIComponent(id)}/tasks`);
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || "获取任务列表失败";
		throw new Error(err);
	}
	const data = res.data as { tasks?: ProjectTaskView[] };
	return data.tasks ?? [];
}

export async function taskAction(
	id: string,
	taskId: string,
	action: "cancel" | "pause" | "resume",
): Promise<unknown> {
	const res = await api(
		"gateway",
		`api/v1/projects/${encodeURIComponent(id)}/tasks/${encodeURIComponent(taskId)}/${action}`,
		{
			method: "POST",
		},
	);
	if (!res.ok) {
		const err = (res.data as { detail?: string })?.detail || `执行操作 ${action} 失败`;
		throw new Error(err);
	}
	return res.data;
}
