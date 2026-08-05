<script lang="ts">
	/**
	 * Veya Web — Coordinator → approval → Genesis HITL flow (Claude-Code/OpenCode style).
	 * Single-flow console: sidebar (branding + new chat) + FlowConsole taking the full page.
	 * Backend: /legacy/flow/phase1 → phase2 → phase3, progress over /legacy/stream/{sid} SSE.
	 */
	import { Cpu } from "lucide-svelte";
	import FlowConsole from "$lib/components/FlowConsole.svelte";
	import ApiKeySettings from "$lib/components/ApiKeySettings.svelte";

	let flowConsole: ReturnType<typeof FlowConsole> | undefined = $state();
</script>

<main class="flex h-screen overflow-hidden">
	<!-- ── sidebar ────────────────────────────────────────────────── -->
	<aside class="flex w-56 shrink-0 flex-col border-r border-terminal-edge bg-terminal-panel">
		<div class="flex items-center gap-2 border-b border-terminal-edge px-4 py-3.5">
			<span class="flex size-7 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500 to-violet-600 font-mono text-sm font-bold text-white">V</span>
			<div>
				<h1 class="text-sm font-semibold leading-tight tracking-tight">Veya Workspace</h1>
				<p class="font-mono text-[9px] text-terminal-dim">Coordinator · Genesis</p>
			</div>
		</div>

		<div class="p-2.5">
			<button
				type="button"
				onclick={() => flowConsole?.newFlow()}
				class="w-full rounded-lg border border-terminal-edge px-3 py-2 text-left font-mono text-xs text-terminal-dim transition hover:border-sky-500/40 hover:text-terminal-fg"
			>
				＋ 新对话
			</button>
		</div>

		<div class="flex-1"></div>

		<div class="border-t border-terminal-edge p-2.5">
			<div class="flex flex-col gap-1 font-mono text-[9px] text-terminal-dim">
				<span class="flex items-center gap-1.5"><span class="size-1.5 rounded-full bg-sky-500"></span>gateway :8765</span>
				<span class="flex items-center gap-1.5"><span class="size-1.5 rounded-full bg-violet-500"></span>legacy :8010</span>
			</div>
		</div>
	</aside>

	<!-- ── main ───────────────────────────────────────────────────── -->
	<section class="flex min-w-0 flex-1 flex-col overflow-hidden">
		<header class="flex shrink-0 items-center gap-4 border-b border-terminal-edge bg-terminal-panel px-6 py-2.5">
			<div class="flex items-center gap-2">
				<Cpu class="size-4 text-sky-400" />
				<span class="font-mono text-sm font-semibold text-terminal-fg">Veya Workspace</span>
			</div>
			<span class="flex-1"></span>
			<div class="w-[420px] max-w-full">
				<ApiKeySettings compact />
			</div>
		</header>

		<FlowConsole bind:this={flowConsole} />
	</section>
</main>
