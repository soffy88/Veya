<script lang="ts">
	/**
	 * ApiKeySettings — the user's own LLM key + model (Phase 1 research + Phase 3 assembly).
	 * Genesis's own element-forging calls never see this key — they use the
	 * server-side GENESIS_API_KEY (server/agents/genesis_agent.py), physically isolated.
	 */
	import { KeyRound } from "lucide-svelte";
	import { apiKeyStore, type Provider } from "$lib/settings.svelte";

	const PROVIDERS: [Provider, string][] = [
		["dashscope", "dashscope · qwen"],
		["anthropic", "anthropic · claude"],
		["openai", "openai · gpt"],
	];

	const MODEL_PLACEHOLDER: Record<Provider, string> = {
		dashscope: "留空用默认，例如 qwen-max",
		anthropic: "留空用默认，例如 claude-sonnet-5",
		openai: "留空用默认，例如 gpt-4o",
	};

	function onChange() {
		apiKeyStore.save();
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
		<select
			id="provider-select"
			bind:value={apiKeyStore.provider}
			onchange={onChange}
			class="rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 text-sm text-terminal-fg outline-none focus:border-sky-500/60"
		>
			{#each PROVIDERS as [value, label] (value)}
				<option {value}>{label}</option>
			{/each}
		</select>
	</div>

	<div class="flex flex-col gap-2">
		<label class="text-xs text-terminal-dim" for="api-key-input">API Key</label>
		<input
			id="api-key-input"
			type="password"
			bind:value={apiKeyStore.api_key}
			onchange={onChange}
			placeholder="sk-..."
			class="rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 focus:border-sky-500/60"
		/>
	</div>

	<div class="flex flex-col gap-2">
		<label class="text-xs text-terminal-dim" for="model-input">模型名（可选）</label>
		<input
			id="model-input"
			type="text"
			bind:value={apiKeyStore.model}
			onchange={onChange}
			placeholder={MODEL_PLACEHOLDER[apiKeyStore.provider]}
			class="rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 focus:border-sky-500/60"
		/>
	</div>
</div>
