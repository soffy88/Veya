<script lang="ts">
	/**
	 * Dashboard — 四 Agent 工作台 (Claude Code / Codex / Grok Build / Pi)。
	 *
	 * 整块自适应网格: 屏幕够宽 4 列并排, 中屏 2×2, 手机 1 列。
	 * 每块 = 独立 AgentConsole (独立引擎/会话/SSE), 可同时分别给任务。
	 */
	import AgentConsole from "./AgentConsole.svelte";
	import { LayoutDashboard } from "lucide-svelte";

	const AGENTS = [
		{ engine: "claude", label: "Claude Code", accent: "violet", promptPrefix: "❯", variant: "default" },
		{ engine: "codex", label: "Codex", accent: "blue", promptPrefix: "", variant: "default" },
		{ engine: "grok", label: "Grok Build", accent: "teal", promptPrefix: "", variant: "default" },
		{ engine: "pi", label: "Pi", accent: "emerald", promptPrefix: ">", variant: "tui" },
	] as const;
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="flex shrink-0 items-center gap-2 border-b border-white/5 px-4 py-2">
		<LayoutDashboard class="size-4 text-white/50" />
		<span class="font-mono text-[11px] uppercase tracking-wider text-white/40">Agent Dashboard</span>
		<span class="flex-1"></span>
		<span class="hidden font-mono text-[10px] text-white/25 md:inline">四引擎并行 · 各自独立会话</span>
	</div>
	<!-- 自适应四块: 宽屏 4 列 / 中屏 2 列 / 手机 1 列 -->
	<div class="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-2 xl:grid-cols-4">
		{#each AGENTS as a (a.engine)}
			<div class="min-h-0 border-r border-white/5 last:border-r-0">
				<AgentConsole
					engine={a.engine}
					label={a.label}
					accent={a.accent}
					variant={a.variant ?? "default"}
					promptPrefix={a.promptPrefix}
				/>
			</div>
		{/each}
	</div>
</div>
