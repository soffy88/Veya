/**
 * Team API client — the /team pages talk to the bot team backend through it.
 *
 * Two rules this file exists to keep:
 *
 * 1. **One transport.** Every call goes through `$lib/api.ts` (same-origin,
 *    attaching the bearer token the way `lib/auth.svelte.ts` stores it), so the
 *    /team pages behave exactly like the rest of the app offline / logged out.
 * 2. **Sibling features degrade, they do not error.** Memory (P-sibling) and
 *    routines (P-sibling) live on branches merged later: their endpoints are
 *    called only when the server says they exist, and until then the UI says so
 *    instead of showing a broken panel.
 */

import { api, type ApiResult } from "$lib/api";

// ── types ───────────────────────────────────────────────────────────────

export interface BotView {
	bot_id: string;
	name: string;
	description: string;
	instructions: string;
	provider: string | null;
	model: string | null;
	runner_id: string | null;
	owner_id: string;
	config_version: number;
	model_source?: string;
	model_source_config?: Record<string, unknown>;
	available_skill_ids?: string[];
}

export interface BotMessageMeta {
	/** NANO-W10 W5: "finish_checkpoint" marks a card the user can answer. */
	kind?: string;
	card_id?: string;
	goal_run_id?: string;
	mode?: string;
	round?: number;
}

export interface BotMessage {
	id: string;
	role: "user" | "bot";
	text: string;
	bot_id?: string | null;
	task_id?: string | null;
	ts: number;
	meta?: BotMessageMeta | null;
}

export interface GroupView {
	id: string;
	name: string;
	bot_ids: string[];
	rounds_per_message: number;
	created_at: number;
	admin_bot_id?: string | null;
	reply_mode?: string | null;
	notice?: string | null;
}

export interface PendingPushTask {
	accepted: true;
	task_id: string;
	session_id: string;
	bot_id: string;
	kind: string;
	group_id?: string;
	result_url: string;
	workbench_url?: string;
}

export interface MemoryDoc {
	name: string;
	content: string;
	updated_at?: number;
}

export interface RoutineView {
	id: string;
	objective: string;
	schedule: string;
	timezone: string;
	enabled: boolean;
	last_run_at?: number | null;
	last_fired_utc?: string | null;
	bot_id?: string;
}

export interface PushStatus {
	enabled: boolean;
	sender: string;
	subscriptions: number;
	hint?: string | null;
}

export interface TurnReply {
	reply: string;
	status: string;
	task_id?: string;
	workbench_url?: string;
	proposal?: { id: string; bots: Array<Record<string, unknown>>; accept_url: string } | null;
}

// ── small helpers ───────────────────────────────────────────────────────

function detailOf(result: ApiResult): string {
	if (typeof result.data === "string") return result.data;
	const payload = result.data as { detail?: string } | null;
	if (payload && typeof payload.detail === "string") return payload.detail;
	return result.ok ? "" : `请求失败 (HTTP ${result.status})`;
}

async function unwrap<T>(result: ApiResult): Promise<T> {
	if (result.ok && result.data && typeof result.data === "object") {
		return result.data as T;
	}
	throw new Error(detailOf(result));
}

// ── bot team ────────────────────────────────────────────────────────────

export async function listBots(): Promise<{ bots: BotView[]; chef_bot_id: string; default_bot_id: string }> {
	return unwrap(await api("gateway", "api/v1/bots", { method: "GET" }));
}

export async function createBot(body: Record<string, unknown>): Promise<BotView> {
	const data = await unwrap<{ bot: BotView }>(
		await api("gateway", "api/v1/bots", { method: "POST", body }),
	);
	return data.bot;
}

export async function patchBot(botId: string, body: Record<string, unknown>): Promise<BotView> {
	const data = await unwrap<{ bot: BotView }>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}`, { method: "PATCH", body }),
	);
	return data.bot;
}

/** One bot, resolved the same way the message endpoint resolves it. */
export async function getBot(botId: string): Promise<BotView> {
	const data = await unwrap<{ bot: BotView }>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}`, { method: "GET" }),
	);
	return data.bot;
}

export async function deleteBot(botId: string): Promise<void> {
	const result = await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}`, { method: "DELETE" });
	if (!result.ok) throw new Error(detailOf(result));
}

export async function listBotMessages(botId: string): Promise<BotMessage[]> {
	const data = await unwrap<{ messages: BotMessage[] }>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/messages`, { method: "GET" }),
	);
	return data.messages ?? [];
}

export interface SendTurnOptions {
	/** `true` → 202 + background finish (a bot turn can take minutes). */
	background?: boolean;
	workspaceId?: string | null;
}

export async function sendBotMessage(
	botId: string,
	text: string,
	options: SendTurnOptions = {},
): Promise<PendingPushTask | TurnReply> {
	const query: Record<string, string> = {};
	if (options.background) query.async = "1";
	if (options.workspaceId) query.workspace_id = options.workspaceId;
	const result = await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/messages`, {
		method: "POST",
		body: { text },
		query,
	});
	if (result.status === 202) return unwrap<PendingPushTask>(result);
	return unwrap<TurnReply>(result);
}

export interface CorrectionResult {
	status: string;
	bot_id: string;
	action: string;
	state?: string;
	reason?: string;
}

/**
 * Send a correction while the bot's turn is still running (NANO-W10 W4).
 *
 * This is not a second message channel: the backend lands it through the item-1
 * turn queue's steer, so the running turn sees it at its next safe point. A 409
 * means there was no running turn to correct.
 */
export async function sendBotCorrection(botId: string, text: string): Promise<CorrectionResult> {
	const result = await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/corrections`, {
		method: "POST",
		body: { text },
	});
	return unwrap<CorrectionResult>(result);
}

export interface CheckpointVerdict {
	status: string;
	bot_id: string;
	card_id: string;
	verdict: "accept" | "send_back";
	action: string;
}

/**
 * Answer a W5 finish checkpoint card.
 *
 * `send_back` carries the note to the run through the item-1 turn queue — the
 * same path a normal message takes, so a card sent back is just a correction.
 */
export async function answerCheckpoint(
	botId: string,
	cardId: string,
	verdict: "accept" | "send_back",
	note = "",
): Promise<CheckpointVerdict> {
	const result = await api(
		"gateway",
		`api/v1/bots/${encodeURIComponent(botId)}/checkpoints/${encodeURIComponent(cardId)}`,
		{ method: "POST", body: { verdict, note } },
	);
	return unwrap<CheckpointVerdict>(result);
}

// ── groups ──────────────────────────────────────────────────────────────

export async function listGroups(): Promise<GroupView[]> {
	const data = await unwrap<{ groups: GroupView[] }>(
		await api("gateway", "api/v1/bots/groups", { method: "GET" }),
	);
	return data.groups ?? [];
}

export async function createGroup(name: string, botIds: string[]): Promise<GroupView> {
	const data = await unwrap<{ group: GroupView }>(
		await api("gateway", "api/v1/bots/groups", { method: "POST", body: { name, bot_ids: botIds } }),
	);
	return data.group;
}

export interface GroupRolesPatch {
	name?: string;
	admin_bot_id?: string | null;
	reply_mode?: string | null;
	notice?: string | null;
}

/**
 * Change a group's roles (W11 W-5). Flag-gated server-side
 * (``VEYA_GROUP_ROLES``): a deployment without it answers 404, which the group
 * page reads as "roles are not on this deployment".
 */
export async function patchGroupRoles(groupId: string, body: GroupRolesPatch): Promise<GroupView> {
	const data = await unwrap<{ group: GroupView }>(
		await api("gateway", `api/v1/nano-w10-c/groups/${encodeURIComponent(groupId)}/roles`, {
			method: "PATCH",
			body,
		}),
	);
	return data.group;
}

export async function listGroupMessages(groupId: string): Promise<BotMessage[]> {
	const data = await unwrap<{ messages: BotMessage[] }>(
		await api("gateway", `api/v1/bots/groups/${encodeURIComponent(groupId)}/messages`, { method: "GET" }),
	);
	return data.messages ?? [];
}

export async function sendGroupMessage(
	groupId: string,
	text: string,
	options: SendTurnOptions = {},
): Promise<PendingPushTask | { status: string; replies: BotMessage[]; passed: string[]; skipped: string[] }> {
	const query: Record<string, string> = {};
	if (options.background) query.async = "1";
	if (options.workspaceId) query.workspace_id = options.workspaceId;
	const result = await api("gateway", `api/v1/bots/groups/${encodeURIComponent(groupId)}/messages`, {
		method: "POST",
		body: { text },
		query,
	});
	if (result.status === 202) return unwrap<PendingPushTask>(result);
	return unwrap<{ status: string; replies: BotMessage[]; passed: string[]; skipped: string[] }>(result);
}

// ── Chef proposals ──────────────────────────────────────────────────────

export async function acceptProposal(proposalId: string): Promise<void> {
	const result = await api("gateway", `api/v1/bots/proposals/${encodeURIComponent(proposalId)}/accept`, {
		method: "POST",
	});
	if (!result.ok) throw new Error(detailOf(result));
}

// ── memory (W4: the endpoints exist; the shapes are the server's) ──────────

export async function listMemory(botId: string): Promise<MemoryDoc[]> {
	const data = await unwrap<{ files?: string[]; names?: string[] }>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/memory`, { method: "GET" }),
	);
	// The listing carries file *names*; the body of each is a separate read
	// (getMemory), which is what keeps a long memory cheap to list.
	const names = Array.isArray(data.files) ? data.files : (data.names ?? []);
	return names.map((name) => ({ name: String(name), content: "" }));
}

export async function getMemory(botId: string, name: string): Promise<MemoryDoc> {
	return unwrap<MemoryDoc>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/memory/${encodeURIComponent(name)}`, {
			method: "GET",
		}),
	);
}

export async function putMemory(botId: string, name: string, content: string): Promise<MemoryDoc> {
	return unwrap<MemoryDoc>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/memory/${encodeURIComponent(name)}`, {
			method: "PUT",
			body: { text: content },
		}),
	);
}

export async function deleteMemory(botId: string, name: string): Promise<void> {
	const result = await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/memory/${encodeURIComponent(name)}`, {
		method: "DELETE",
	});
	if (!result.ok) throw new Error(detailOf(result));
}

// ── routines (W4) ─────────────────────────────────────────────────────────

export async function listRoutines(botId: string): Promise<RoutineView[]> {
	// The collection answers with the array itself, not a wrapper object.
	const data = await unwrap<RoutineView[] | { routines?: RoutineView[] }>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/routines`, { method: "GET" }),
	);
	return Array.isArray(data) ? data : (data.routines ?? []);
}

export async function createRoutine(botId: string, body: Record<string, unknown>): Promise<RoutineView> {
	// The create response is the stored spec itself (not wrapped in "routine").
	return unwrap<RoutineView>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/routines`, { method: "POST", body }),
	);
}

export async function patchRoutine(botId: string, routineId: string, body: Record<string, unknown>): Promise<RoutineView> {
	return unwrap<RoutineView>(
		await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/routines/${encodeURIComponent(routineId)}`, {
			method: "PATCH",
			body,
		}),
	);
}

export async function deleteRoutine(botId: string, routineId: string): Promise<void> {
	const result = await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/routines/${encodeURIComponent(routineId)}`, {
		method: "DELETE",
	});
	if (!result.ok) throw new Error(detailOf(result));
}

export async function runRoutineNow(botId: string, routineId: string): Promise<void> {
	const result = await api("gateway", `api/v1/bots/${encodeURIComponent(botId)}/routines/${encodeURIComponent(routineId)}/run_now`, {
		method: "POST",
	});
	if (!result.ok) throw new Error(detailOf(result));
}

/**
 * Probe an optional sibling feature once, so its panel can render either the
 * feature or an honest "not on this deployment" line — never a broken panel.
 */
export async function featureAvailable(probe: () => Promise<unknown>): Promise<boolean> {
	try {
		await probe();
		return true;
	} catch {
		return false;
	}
}

// ── models (the create-bot picker's catalog) ──────────────────────────────

export interface ModelOption {
	id: string;
	provider: string;
	provider_label: string;
	/** The model a default bot would use — what the picker preselects. */
	default: boolean;
}

export interface ModelsResponse {
	default_model: string | null;
	default_provider: string | null;
	models: ModelOption[];
}

/**
 * Selectable models. Unlike ``/api/v1/providers`` this route is not
 * flag-gated, so the picker works on a default deployment; it is read-only and
 * login-gated, so a logged-out visitor gets 401 and the picker falls back to a
 * plain model field.
 */
export async function listModels(): Promise<ModelsResponse> {
	return unwrap<ModelsResponse>(await api("gateway", "api/v1/models", { method: "GET" }));
}

// ── providers (NANO-W8 VEYA_PROVIDER_REGISTRY) ────────────────────────────

export interface ProviderView {
	name: string;
	display_name: string;
	protocol_family: string;
	base_url: string | null;
	capabilities: string[];
	models: string[];
	default_model: string | null;
	description: string;
	available: boolean;
	requires_api_key: boolean;
	has_user_key: boolean;
}

export interface CliAgentView {
	available: boolean;
	version?: string;
	path?: string;
}

export interface ProvidersResponse {
	providers: Record<string, ProviderView>;
	cli_agents: Record<string, CliAgentView>;
	user_keys: string[];
}

export async function listProviders(): Promise<ProvidersResponse> {
	return unwrap(await api("gateway", "api/v1/providers", { method: "GET" }));
}

export interface CustomProviderRequest {
	name: string;
	display_name: string;
	protocol_family: "openai" | "anthropic" | "gemini" | "custom";
	base_url?: string;
	capabilities: string[];
	models: string[];
	default_model?: string;
	description: string;
}

export async function registerCustomProvider(body: CustomProviderRequest): Promise<{ status: string; provider: string }> {
	const data = await unwrap<{ status: string; provider: string }>(
		await api("gateway", "api/v1/providers/custom", { method: "POST", body }),
	);
	return data;
}

export async function unregisterCustomProvider(name: string): Promise<{ status: string; provider: string }> {
	const data = await unwrap<{ status: string; provider: string }>(
		await api("gateway", `api/v1/providers/custom/${encodeURIComponent(name)}`, { method: "DELETE" }),
	);
	return data;
}

export async function setUserApiKey(provider: string, api_key: string): Promise<{ status: string; provider: string }> {
	const data = await unwrap<{ status: string; provider: string }>(
		await api("gateway", "api/v1/providers/keys", { method: "POST", body: { provider, api_key } }),
	);
	return data;
}

export async function deleteUserApiKey(provider: string): Promise<{ status: string; provider: string }> {
	const data = await unwrap<{ status: string; provider: string }>(
		await api("gateway", `api/v1/providers/keys/${encodeURIComponent(provider)}`, { method: "DELETE" }),
	);
	return data;
}

export async function listCliAgents(): Promise<Record<string, CliAgentView>> {
	const data = await unwrap<{ cli_agents: Record<string, CliAgentView> }>(
		await api("gateway", "api/v1/providers/cli-agents", { method: "GET" }),
	);
	return data.cli_agents;
}

export interface BotModelSourceRequest {
	bot_id: string;
	source: "server" | "user_key" | "cli";
	provider?: string;
	model?: string;
	api_key_ref?: string;
	cli_agent?: string;
	cli_args?: string[];
}

export async function setBotModelSource(body: BotModelSourceRequest): Promise<{ status: string; bot_id: string; source: string }> {
	const data = await unwrap<{ status: string; bot_id: string; source: string }>(
		await api("gateway", "api/v1/providers/bot-model-source", { method: "POST", body }),
	);
	return data;
}

export async function getBotModelSource(botId: string): Promise<{ bot_id: string; source: string; provider: string; model: string }> {
	return unwrap(await api("gateway", `api/v1/providers/bot-model-source/${encodeURIComponent(botId)}`, { method: "GET" }));
}

export async function getEffectiveModel(botId: string): Promise<Record<string, unknown>> {
	return unwrap(await api("gateway", `api/v1/providers/effective-model/${encodeURIComponent(botId)}`, { method: "GET" }));
}

// ── runners (NANO-W8 hands capability) ────────────────────────────────────

export interface RunnerView {
	runner_id: string;
	name: string;
	base_url: string;
	token_ref: string;
	last_seen: number | null;
	status: string;
	implicit?: boolean;
	/**
	 * What this machine can do with hands. `null` means *not known yet* — a
	 * remote runner reports its own capability, so the hub must not answer on
	 * its behalf before that runner has said anything.
	 */
	hands_capable?: boolean | null;
	hands_tools?: string[];
	hands_known?: boolean;
	version?: string;
	/** `null` until the runner has been checked against a configured manifest. */
	update_available?: boolean | null;
	update_checked?: boolean;
}

export interface RunnersResponse {
	runners: RunnerView[];
}

export async function listRunners(): Promise<RunnersResponse> {
	return unwrap(await api("gateway", "api/v1/runners", { method: "GET" }));
}

export interface RunnerHealthResponse {
	runner_id: string;
	status: string;
	last_seen: number | null;
	hands_capable: boolean | null;
	hands_tools: string[];
	hands_known?: boolean;
}

export async function getRunnerHealth(runnerId: string): Promise<RunnerHealthResponse> {
	return unwrap(await api("gateway", `api/v1/runners/${encodeURIComponent(runnerId)}/health`, { method: "GET" }));
}

// ── secrets (NANO-W10 C, VEYA_BOT_SECRETS) ────────────────────────────────

export interface BotSecretView {
	name: string;
	purpose: string;
	scope: string;
	requesting_bot_id: string;
	status: string;
	created_at: number;
	last_used_at: number;
}

/**
 * Metadata for the secrets this bot may use — never a value (the endpoint
 * refuses to return one, and the settings tab only ever renders this metadata).
 * Flag-gated server-side: a deployment without ``VEYA_BOT_SECRETS`` answers
 * 404, which the tab reads as "secrets are not on this deployment".
 */
export async function listBotSecrets(botId: string): Promise<BotSecretView[]> {
	const data = await unwrap<{ secrets?: Array<Record<string, unknown>> }>(
		await api("gateway", "api/v1/nano-w10-c/secrets", { method: "GET" }),
	);
	const rows = Array.isArray(data.secrets) ? data.secrets : [];
	return rows
		.map((row) => ({
			name: String(row.name ?? ""),
			purpose: String(row.purpose ?? ""),
			scope: String(row.scope ?? "this bot"),
			requesting_bot_id: String(row.requesting_bot_id ?? ""),
			status: String(row.status ?? ""),
			created_at: Number(row.created_at ?? 0),
			last_used_at: Number(row.last_used_at ?? 0),
		}))
		.filter((row) => row.name && (!botId || row.scope !== "this bot" || row.requesting_bot_id === botId));
}
