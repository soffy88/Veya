<script lang="ts">
	/** Product landing surface: setup, capability bindings, and the single New Task entry. */
	import { AlertCircle, ArrowRight, Bot, CheckCircle2, Settings, RefreshCw } from "lucide-svelte";
	import { api, type ApiResult } from "$lib/api";
	import { apiKeyStore } from "$lib/settings.svelte";

	type BotState = {
		bot?: { id?: string; name?: string; lifecycle?: string };
		onboarding?: { completed?: boolean; required?: boolean };
		provider?: { id?: string; model?: string; configured?: boolean; credential?: { ref?: string | null } };
		workspace?: { path?: string | null; configured?: boolean; exists?: boolean };
		bindings?: Record<string, unknown>;
		runtime?: { status?: string; authority?: string };
	};

	interface Props {
		onNewTask: () => void;
		onOpenSettings: () => void;
		onOpenTasks: () => void;
	}

	let { onNewTask, onOpenSettings, onOpenTasks }: Props = $props();
	let botState = $state<BotState | null>(null);
	let workspace = $state("");
	let loading = $state(true);
	let saving = $state(false);
	let error = $state("");
	let saved = $state(false);

	async function refresh(): Promise<void> {
		loading = true;
		error = "";
		const result: ApiResult = await api("gateway", "api/v1/bot", { method: "GET" });
		loading = false;
		if (result.ok && result.data && typeof result.data === "object") {
			botState = result.data as BotState;
			if (!workspace && botState.workspace?.path) workspace = botState.workspace.path;
		} else {
			error = `Bot 状态加载失败 (${result.status})`;
		}
	}

	async function completeOnboarding(): Promise<void> {
		saving = true;
		error = "";
		saved = false;
		const provider = apiKeyStore.provider;
		const model = apiKeyStore.model.trim() || apiKeyStore.current.defaultModel || undefined;
		// The browser key stays in the existing local provider store.  Only a
		// non-secret reference crosses the product-shell API boundary.
		const credential_ref = apiKeyStore.api_key.trim() ? `local:web:${provider}` : undefined;
		const result: ApiResult = await api("gateway", "api/v1/bot/onboarding", {
			method: "POST",
			body: { provider, model, workspace: workspace.trim() || undefined, credential_ref },
		});
		saving = false;
		if (result.ok && result.data && typeof result.data === "object") {
			botState = result.data as BotState;
			saved = true;
		} else {
			error = result.data && typeof result.data === "object" && "detail" in result.data
				? String((result.data as { detail?: unknown }).detail ?? "配置失败")
				: `首次配置失败 (${result.status})`;
		}
	}

	$effect(() => {
		void refresh();
	});
</script>

<div class="flex h-full flex-col gap-6 overflow-y-auto p-6">
	<div class="flex flex-wrap items-start gap-4">
		<div class="flex size-12 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-sky-500 to-violet-600 text-white shadow-lg shadow-sky-500/10">
			<Bot class="size-6" />
		</div>
		<div class="min-w-0 flex-1">
			<div class="flex flex-wrap items-center gap-2">
			<h2 class="text-lg font-semibold text-terminal-fg">{botState?.bot?.name ?? "Veya Bot"}</h2>
				{#if botState?.bot?.lifecycle}
					<span class="rounded-full bg-white/10 px-2 py-0.5 font-mono text-[10px] text-terminal-dim">{botState.bot.lifecycle}</span>
				{/if}
			</div>
			<p class="mt-1 text-sm text-terminal-dim">你的长期任务入口：对话、执行、审批、验证和结果都回到同一条真实状态链。</p>
		</div>
		<button type="button" onclick={onNewTask} class="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 px-4 py-2 text-sm font-semibold text-white transition hover:brightness-110">
			新建任务 <ArrowRight class="size-4" />
		</button>
	</div>

	{#if error}
		<div class="flex items-center gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300"><AlertCircle class="size-4 shrink-0" />{error}</div>
	{/if}

	{#if loading}
		<div class="flex items-center gap-2 rounded-xl border border-terminal-edge bg-terminal-panel p-5 text-sm text-terminal-dim"><RefreshCw class="size-4 animate-spin" />读取 Bot 状态…</div>
	{:else if botState}
		{#if botState.onboarding?.required}
			<section class="rounded-xl border border-amber-500/30 bg-amber-500/[0.05] p-5">
				<div class="flex items-start gap-3">
					<AlertCircle class="mt-0.5 size-5 shrink-0 text-amber-400" />
					<div class="min-w-0 flex-1">
						<h3 class="text-sm font-semibold text-terminal-fg">首次启动：完成 Bot 配置</h3>
						<p class="mt-1 text-xs leading-relaxed text-terminal-dim">先配置现有模型设置，再把 Bot 的 non-secret 配置和引用登记到本地产品配置。API key 不会进入此接口。</p>
						<div class="mt-4 grid gap-3 md:grid-cols-2">
							<label class="flex flex-col gap-1.5 text-xs text-terminal-dim">工作目录（可选）
								<input bind:value={workspace} placeholder="留空沿用已有配置" class="rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 focus:border-sky-500/60" />
							</label>
							<div class="flex flex-col justify-end gap-2 sm:flex-row sm:items-end">
								<button type="button" onclick={onOpenSettings} class="inline-flex items-center justify-center gap-1.5 rounded-lg border border-terminal-edge px-3 py-2 text-xs text-terminal-dim hover:border-sky-500/40 hover:text-terminal-fg"><Settings class="size-3.5" />模型 / Key 设置</button>
								<button type="button" onclick={() => void completeOnboarding()} disabled={saving} class="inline-flex items-center justify-center gap-1.5 rounded-lg bg-amber-500/90 px-3 py-2 text-xs font-semibold text-black hover:bg-amber-400 disabled:opacity-50">{saving ? "保存中…" : "完成首次配置"}</button>
							</div>
						</div>
						{#if saved}<p class="mt-3 flex items-center gap-1.5 text-xs text-emerald-400"><CheckCircle2 class="size-3.5" />配置已保存；当前 readiness 以 provider/workspace 实际状态为准。</p>{/if}
					</div>
				</div>
			</section>
		{/if}

		<div class="grid gap-4 lg:grid-cols-3">
			<section class="rounded-xl border border-terminal-edge bg-terminal-panel p-4">
				<h3 class="text-xs font-semibold uppercase tracking-wider text-terminal-dim">当前执行配置</h3>
				<div class="mt-3 space-y-2 font-mono text-xs text-terminal-dim">
					<div class="flex justify-between gap-3"><span>provider</span><span class="text-terminal-fg">{botState.provider?.id ?? "—"}</span></div>
					<div class="flex justify-between gap-3"><span>model</span><span class="truncate text-terminal-fg">{botState.provider?.model ?? "—"}</span></div>
					<div class="flex justify-between gap-3"><span>credential</span><span class={botState.provider?.configured ? "text-emerald-400" : "text-amber-400"}>{botState.provider?.configured ? "configured" : "missing"}</span></div>
					<div class="flex justify-between gap-3"><span>workspace</span><span class={botState.workspace?.exists ? "text-emerald-400" : "text-amber-400"}>{botState.workspace?.exists ? "ready" : "missing"}</span></div>
				</div>
			</section>
			<section class="rounded-xl border border-terminal-edge bg-terminal-panel p-4">
				<h3 class="text-xs font-semibold uppercase tracking-wider text-terminal-dim">能力绑定</h3>
				<div class="mt-3 space-y-2 text-xs text-terminal-dim">
					<div><span class="text-terminal-fg">Memory / Skills</span> · 读取现有 Personal Runtime</div>
					<div><span class="text-terminal-fg">Tools / MCP</span> · 复用 ActionGateway 治理</div>
					<div><span class="text-terminal-fg">Computer / Browser</span> · Supervisor + takeover</div>
				</div>
			</section>
			<section class="rounded-xl border border-terminal-edge bg-terminal-panel p-4">
				<h3 class="text-xs font-semibold uppercase tracking-wider text-terminal-dim">可恢复入口</h3>
				<div class="mt-3 flex flex-col gap-2">
					<button type="button" onclick={onOpenTasks} class="rounded-lg border border-terminal-edge px-3 py-2 text-left text-xs text-terminal-dim hover:border-sky-500/40 hover:text-terminal-fg">任务历史 / Resume</button>
					<p class="font-mono text-[10px] leading-relaxed text-terminal-dim/60">会话、GoalRun、Workbench 和 artifacts 继续由各自 canonical backend state 恢复。</p>
				</div>
			</section>
		</div>
	{:else}
		<div class="rounded-xl border border-terminal-edge bg-terminal-panel p-5 text-sm text-terminal-dim">Bot 状态暂不可用，请刷新后重试。</div>
	{/if}
</div>
