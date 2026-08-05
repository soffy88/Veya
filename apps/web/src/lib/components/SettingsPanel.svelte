<script lang="ts">
	/**
	 * SettingsPanel — slide-over drawer with the settings that don't belong in the
	 * main chat flow: model/key config, plugins, automation (cron schedules).
	 */
	import { X, KeyRound, Package, Clock } from "lucide-svelte";
	import ApiKeySettings from "./ApiKeySettings.svelte";
	import PluginPanel from "./PluginPanel.svelte";
	import AutomationPanel from "./AutomationPanel.svelte";

	interface Props {
		open: boolean;
		onClose: () => void;
	}
	let { open, onClose }: Props = $props();

	type Tab = "model" | "plugins" | "automation";
	let tab = $state<Tab>("model");

	const TABS: [Tab, string, typeof KeyRound][] = [
		["model", "模型", KeyRound],
		["plugins", "插件", Package],
		["automation", "自动化", Clock],
	];
</script>

{#if open}
	<div class="fixed inset-0 z-50 flex justify-end">
		<button type="button" class="absolute inset-0 bg-black/50" onclick={onClose} aria-label="关闭设置"></button>

		<aside class="relative flex h-full w-full max-w-md flex-col border-l border-terminal-edge bg-terminal-bg shadow-2xl">
			<div class="flex shrink-0 items-center justify-between border-b border-terminal-edge px-5 py-4">
				<h2 class="text-base font-semibold text-terminal-fg">设置</h2>
				<button type="button" onclick={onClose} class="flex size-8 items-center justify-center rounded-lg text-terminal-dim transition hover:bg-white/5 hover:text-terminal-fg" aria-label="关闭">
					<X class="size-4" />
				</button>
			</div>

			<div class="flex shrink-0 gap-1 border-b border-terminal-edge px-3 py-2">
				{#each TABS as [id, label, Icon] (id)}
					<button
						type="button"
						onclick={() => (tab = id)}
						class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition {tab === id
							? 'bg-white/10 text-terminal-fg'
							: 'text-terminal-dim hover:bg-white/5 hover:text-terminal-fg'}"
					>
						<Icon class="size-3.5" />
						{label}
					</button>
				{/each}
			</div>

			<div class="flex-1 overflow-y-auto p-5">
				{#if tab === "model"}
					<ApiKeySettings />
				{:else if tab === "plugins"}
					<PluginPanel />
				{:else if tab === "automation"}
					<AutomationPanel />
				{/if}
			</div>
		</aside>
	</div>
{/if}
