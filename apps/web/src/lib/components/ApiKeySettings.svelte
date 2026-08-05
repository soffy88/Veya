<script lang="ts">
	/**
	 * ApiKeySettings — the user's own LLM key + model (Phase 1 research + Phase 3 assembly).
	 * Genesis's own element-forging calls never see this key — they use the
	 * server-side GENESIS_API_KEY (server/agents/genesis_agent.py), physically isolated.
	 */
	import { Check, KeyRound, Plus, Trash2, X } from "lucide-svelte";
	import { apiKeyStore } from "$lib/settings.svelte";

	let addingProvider = $state(false);
	let newLabel = $state("");
	let newEndpoint = $state("");
	let justSaved = $state(false);
	let saveTimer: ReturnType<typeof setTimeout> | undefined;

	function saveKey() {
		apiKeyStore.save();
		justSaved = true;
		clearTimeout(saveTimer);
		saveTimer = setTimeout(() => (justSaved = false), 2000);
	}

	function startAdd() {
		addingProvider = true;
		newLabel = "";
		newEndpoint = "";
	}

	function confirmAdd() {
		if (!newLabel.trim() || !newEndpoint.trim()) return;
		apiKeyStore.addCustomProvider(newLabel, newEndpoint);
		addingProvider = false;
	}

	function removeCurrent() {
		if (!apiKeyStore.current.custom) return;
		apiKeyStore.removeCustomProvider(apiKeyStore.current.id);
	}
</script>

<div class="flex flex-col gap-3 rounded-xl border border-terminal-edge bg-terminal-panel p-4">
	<div class="flex items-center gap-2">
		<KeyRound class="size-4 text-terminal-dim" />
		<h3 class="text-sm font-semibold text-terminal-fg">你的 LLM Key</h3>
	</div>
	<p class="text-xs leading-relaxed text-terminal-dim">用于 Phase 1 调研与 Phase 3 组装，不会用于 Genesis 锻造（那部分用服务端独立的 key）。</p>

	<div class="flex flex-col gap-2">
		<label class="text-xs text-terminal-dim" for="provider-select">服务商</label>
		<div class="flex items-center gap-2">
			<select
				id="provider-select"
				bind:value={apiKeyStore.provider}
				onchange={() => apiKeyStore.save()}
				class="min-w-0 flex-1 rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 text-sm text-terminal-fg outline-none focus:border-sky-500/60"
			>
				{#each apiKeyStore.all as p (p.id)}
					<option value={p.id}>{p.label}</option>
				{/each}
			</select>
			{#if apiKeyStore.current.custom}
				<button
					type="button"
					onclick={removeCurrent}
					class="flex size-9 shrink-0 items-center justify-center rounded-lg border border-terminal-edge text-terminal-dim transition hover:border-rose-500/40 hover:text-rose-300"
					aria-label="删除这个服务商"
				>
					<Trash2 class="size-4" />
				</button>
			{/if}
		</div>
	</div>

	{#if !addingProvider}
		<button
			type="button"
			onclick={startAdd}
			class="flex items-center gap-1.5 self-start text-xs text-sky-400 transition hover:text-sky-300"
		>
			<Plus class="size-3.5" /> 添加自定义服务商
		</button>
	{:else}
		<div class="flex flex-col gap-2 rounded-lg border border-terminal-edge bg-terminal-bg p-3">
			<div class="flex items-center justify-between">
				<span class="text-xs font-medium text-terminal-fg">新服务商（任意 OpenAI 兼容 API）</span>
				<button type="button" onclick={() => (addingProvider = false)} class="text-terminal-dim hover:text-terminal-fg" aria-label="取消">
					<X class="size-3.5" />
				</button>
			</div>
			<input
				bind:value={newLabel}
				placeholder="名称，例如 Groq"
				class="rounded-lg border border-terminal-edge bg-terminal-panel px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 focus:border-sky-500/60"
			/>
			<input
				bind:value={newEndpoint}
				placeholder="Base URL，例如 https://api.groq.com/openai/v1/chat/completions"
				class="rounded-lg border border-terminal-edge bg-terminal-panel px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 focus:border-sky-500/60"
			/>
			<button
				type="button"
				onclick={confirmAdd}
				disabled={!newLabel.trim() || !newEndpoint.trim()}
				class="flex w-fit items-center gap-1.5 rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 px-3 py-1.5 text-sm font-medium text-white transition hover:brightness-110 disabled:opacity-40"
			>
				<Plus class="size-3.5" /> 添加并切换
			</button>
		</div>
	{/if}

	<div class="flex flex-col gap-2">
		<label class="text-xs text-terminal-dim" for="api-key-input">API Key</label>
		<input
			id="api-key-input"
			type="password"
			value={apiKeyStore.api_key}
			oninput={(e) => { apiKeyStore.api_key = e.currentTarget.value; }}
			placeholder="sk-..."
			class="rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 focus:border-sky-500/60"
		/>
	</div>

	<div class="flex flex-col gap-2">
		<label class="text-xs text-terminal-dim" for="model-input">模型名（可选，留空用默认）</label>
		<input
			id="model-input"
			type="text"
			value={apiKeyStore.model}
			oninput={(e) => { apiKeyStore.model = e.currentTarget.value; }}
			placeholder="例如 qwen-max / claude-sonnet-5 / deepseek-chat"
			class="rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 focus:border-sky-500/60"
		/>
	</div>

	<div class="flex items-center gap-3">
		<button
			type="button"
			onclick={saveKey}
			class="flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 px-4 py-2 text-sm font-semibold text-white transition hover:brightness-110"
		>
			保存
		</button>
		{#if justSaved}
			<span class="flex items-center gap-1 text-sm text-emerald-400">
				<Check class="size-4" /> 已保存
			</span>
		{/if}
	</div>
</div>
