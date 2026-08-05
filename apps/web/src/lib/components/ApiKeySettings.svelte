<script lang="ts">
	/**
	 * ApiKeySettings — the user's own LLM key (Phase 1 research + Phase 3 assembly).
	 * Genesis's own element-forging calls never see this key — they use the
	 * server-side GENESIS_API_KEY (server/agents/genesis_agent.py), physically isolated.
	 */
	import { KeyRound } from "lucide-svelte";
	import { apiKeyStore, type Provider } from "$lib/settings.svelte";

	interface Props {
		/** header-bar rendering: no border/label, just the compact select + key input */
		compact?: boolean;
	}
	let { compact = false }: Props = $props();

	const PROVIDERS: [Provider, string][] = [
		["dashscope", "dashscope · qwen"],
		["anthropic", "anthropic · claude"],
		["openai", "openai · gpt"],
	];

	function onChange() {
		apiKeyStore.save();
	}
</script>

<div class={compact ? "flex flex-col gap-1" : "flex flex-col gap-2 rounded-xl border border-terminal-edge bg-terminal-panel p-3"}>
	{#if !compact}
		<div class="flex items-center gap-2">
			<KeyRound class="size-4 text-terminal-dim" />
			<h3 class="font-mono text-xs font-semibold text-terminal-fg">你的 LLM Key</h3>
			<span class="font-mono text-[10px] text-terminal-dim/70">用于 Phase 1 调研与 Phase 3 组装,不会用于 Genesis 锻造</span>
		</div>
	{/if}
	<div class="flex items-center gap-2">
		<select
			bind:value={apiKeyStore.provider}
			onchange={onChange}
			class="rounded-lg border border-terminal-edge bg-terminal-bg px-2 py-1.5 font-mono text-[11px] text-terminal-dim outline-none focus:border-sky-500/60"
		>
			{#each PROVIDERS as [value, label] (value)}
				<option {value}>{label}</option>
			{/each}
		</select>
		<input
			type="password"
			bind:value={apiKeyStore.api_key}
			onchange={onChange}
			placeholder="sk-..."
			class="min-w-0 flex-1 rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-1.5 font-mono text-[12px] text-terminal-fg outline-none placeholder:text-terminal-dim/60 focus:border-sky-500/60"
		/>
	</div>
</div>
