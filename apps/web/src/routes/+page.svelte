<script lang="ts">
	/**
	 * Veya Web — Coordinator → approval → Genesis HITL flow (Claude-Code/OpenCode style).
	 * Single-flow console: sidebar (branding + new chat) + FlowConsole taking the full page.
	 * Backend: /legacy/flow/phase1 → phase2 → phase3, progress over /legacy/stream/{sid} SSE.
	 */
	import { Cpu, Settings } from "lucide-svelte";
	import FlowConsole from "$lib/components/FlowConsole.svelte";
	import SettingsPanel from "$lib/components/SettingsPanel.svelte";

	let flowConsole: ReturnType<typeof FlowConsole> | undefined = $state();
	let settingsOpen = $state(false);
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

		<div class="p-2.5">
			<button
				type="button"
				onclick={() => flowConsole?.newFlow()}
				class="w-full rounded-lg border border-terminal-edge px-3 py-2 text-left text-sm text-terminal-dim transition hover:border-sky-500/40 hover:text-terminal-fg"
			>
				＋ 新对话
			</button>
		</div>

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
				设置
			</button>
		</header>

		<FlowConsole bind:this={flowConsole} />
	</section>
</main>

<SettingsPanel open={settingsOpen} onClose={() => (settingsOpen = false)} />
