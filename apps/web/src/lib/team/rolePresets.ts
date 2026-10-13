/**
 * W15 role & skill preset client (flag VEYA_ROLE_SKILL_PRESETS).
 *
 * Same transport as the rest of /team (`$lib/api.ts`). While the flag is off
 * the list endpoints answer `available: false` and the UI hides itself.
 */

import { api, type ApiResult } from "$lib/api";
import type { BotView } from "$lib/team/api";

export interface PresetItem {
	id: string;
	kind: "role" | "skill";
	name: string;
	description: string;
	summary_zh: string;
	category: string;
	source: string;
	license: string;
	copyright: string;
}

export interface PresetListing {
	available: boolean;
	reason: string;
	items: PresetItem[];
}

export interface ApplyResult {
	status: string;
	role: string | null;
	role_truncated: boolean;
	skills_added: string[];
	bot: BotView;
}

function detailOf(result: ApiResult): string {
	const payload = result.data as { detail?: string } | null;
	if (payload && typeof payload.detail === "string") return payload.detail;
	return `请求失败 (HTTP ${result.status})`;
}

async function listing(kind: "roles" | "skills"): Promise<PresetListing> {
	const result = await api("gateway", `api/v1/presets/${kind}`, { method: "GET" });
	if (!result.ok || !result.data || typeof result.data !== "object") {
		// An older server without W15 (404) reads as "not available", not an error.
		return { available: false, reason: detailOf(result), items: [] };
	}
	return result.data as PresetListing;
}

export function listRolePresets(): Promise<PresetListing> {
	return listing("roles");
}

export function listSkillPresets(): Promise<PresetListing> {
	return listing("skills");
}

export async function applyPresets(
	botId: string,
	body: { role_id?: string; skill_ids?: string[] },
): Promise<ApplyResult> {
	const result = await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/presets/apply`, {
		method: "POST",
		body,
	});
	if (!result.ok || !result.data || typeof result.data !== "object") {
		throw new Error(detailOf(result));
	}
	return result.data as ApplyResult;
}
