<script lang="ts">
	/**
	 * Veya Web — Coordinator → approval → Genesis HITL flow (Claude-Code/OpenCode style).
	 * Sidebar: 新对话 (chat) / 插件 / 自动化 — three persistent views, one main pane.
	 * Backend: /legacy/flow/phase1 → phase2 → phase3, progress over /legacy/stream/{sid} SSE.
	 */
	import { Cpu, MessageSquare, Package, Clock, Settings } from "lucide-svelte";
	import FlowConsole from "$lib/components/FlowConsole.svelte";
	import PluginPanel from "$lib/components/PluginPanel.svelte";
	import AutomationPanel from "$lib/components/AutomationPanel.svelte";
	import SettingsPanel from "$lib/components/SettingsPanel.svelte";

	type View = "chat" | "plugins" | "automation";

	let flowConsole: ReturnType<typeof FlowConsole> | undefined = $state();
	let settingsOpen = $state(false);
	let view = $state<View>("chat");

	const NAV: [View, string, typeof MessageSquare][] = [
		["chat", "新对话", MessageSquare],
		["plugins", "插件", Package],
		["automation", "自动化", Clock],
	];

	function selectNav(v: View) {
		view = v;
		if (v === "chat") flowConsole?.newFlow();
	}
</script>

<main class="flex h-screen overflow-hidden">
	<!-- ── sidebar ────────────────────────────────────────────────── -->
	<aside class="flex w-56 shrink-0 flex-col border-r border-terminal-edge bg-terminal-panel">
		<div class="flex items-center gap-2 border-b border-terminal-edge px-4 py-3.5">
			<span class="flex size-7 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500 to-violet-600 font-mono text-sm font-bold text-white">V</span>
			<div>
				<h1 class="text-sm font-semibold leading-tight tracking-tight">Veya Workspace</h1>
				<p class="text-xs text-terminal-dim">Coordinator · Genesis</p>
			</div>
		</div>

		<nav class="flex flex-col gap-0.5 p-2.5">
			{#each NAV as [id, label, Icon] (id)}
				<button
					type="button"
					onclick={() => selectNav(id)}
					class="flex items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition {view === id
						? 'bg-white/10 text-terminal-fg'
						: 'text-terminal-dim hover:bg-white/5 hover:text-terminal-fg'}"
				>
					<Icon class="size-4" />
					{label}
				</button>
			{/each}
		</nav>

		<div class="flex-1"></div>

		<div class="border-t border-terminal-edge p-2.5">
			<div class="flex flex-col gap-1 text-xs text-terminal-dim">
				<span class="flex items-center gap-1.5"><span class="size-1.5 rounded-full bg-sky-500"></span>gateway :8767</span>
				<span class="flex items-center gap-1.5"><span class="size-1.5 rounded-full bg-violet-500"></span>legacy :9120</span>
			</div>
		</div>
	</aside>

	<!-- ── main ───────────────────────────────────────────────────── -->
	<section class="flex min-w-0 flex-1 flex-col overflow-hidden">
		<header class="flex shrink-0 items-center gap-4 border-b border-terminal-edge bg-terminal-panel px-6 py-2.5">
			<div class="flex items-center gap-2">
				<Cpu class="size-4 text-sky-400" />
				<span class="text-sm font-semibold text-terminal-fg">Veya Workspace</span>
			</div>
			<span class="flex-1"></span>
			<button
				type="button"
				onclick={() => (settingsOpen = true)}
				class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-3 py-1.5 text-sm text-terminal-dim transition hover:border-sky-500/40 hover:text-terminal-fg"
			>
				<Settings class="size-4" />
				模型
			</button>
		</header>

		<div class="flex min-h-0 flex-1 flex-col" class:hidden={view !== "chat"}>
			<FlowConsole bind:this={flowConsole} />
		</div>
		{#if view === "plugins"}
			<div class="flex-1 overflow-y-auto p-6"><PluginPanel /></div>
		{:else if view === "automation"}
			<div class="flex-1 overflow-y-auto p-6"><AutomationPanel /></div>
		{/if}
	</section>
</main>

<SettingsPanel open={settingsOpen} onClose={() => (settingsOpen = false)} />
