<script lang="ts">
	/**
	 * Veya Web — 全能力控制台（写代码测试工作台）。
	 *
	 * 七个分区：
	 *   🛠️ 代码测试   — 编辑器 + 沙箱执行 + pytest
	 *   🤖 Agent 引擎 — SSE 对话 + 全部 agent 模式
	 *   🔬 代码智能   — AST / 语义搜索 / 跨语言 / 缓存
	 *   🖼️ 多模态     — 图像 / 文档 / 语音 / 视觉
	 *   🌐 外部世界   — 浏览器 / 外部 agent / 通知
	 *   📋 项目协作   — 看板 / 收件箱 / 模板 / 会话
	 *   ⚙️ 系统安全   — 模型 / 密钥 / 审计 / 权限 / MCP
	 *
	 * 后端接线：/api/proxy/gateway → L4 网关 :8765，
	 *           /api/proxy/legacy  → 老 FastAPI 服务 :8010。
	 */
	import ArtifactChat from "$lib/components/ArtifactChat.svelte";
	import CapabilityRunner from "$lib/components/CapabilityRunner.svelte";
	import FlowConsole from "$lib/components/FlowConsole.svelte";
	import KanbanPanel from "$lib/components/KanbanPanel.svelte";
	import Workspace from "$lib/components/Workspace.svelte";
	import { SECTIONS } from "$lib/capabilities";

	let active = $state("agent");

	const current = $derived(SECTIONS.find((s) => s.id === active) ?? SECTIONS[0]);
	const custom = $derived(
		current.id === "workspace"
			? "workspace"
			: current.id === "agent"
				? "agent"
				: current.id === "chat"
					? "chat"
					: current.id === "project"
						? "project"
						: "",
	);
</script>

<main class="flex h-screen overflow-hidden">
	<!-- ── sidebar ────────────────────────────────────────────────── -->
	<aside class="flex w-44 shrink-0 flex-col border-r border-terminal-edge bg-terminal-panel">
		<div class="flex items-center gap-2 border-b border-terminal-edge px-3 py-3">
			<span class="flex size-7 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500 to-violet-600 font-mono text-sm font-bold text-white">V</span>
			<div>
				<h1 class="text-sm font-semibold leading-tight tracking-tight">Veya Web</h1>
				<p class="font-mono text-[9px] text-terminal-dim">Layer 4 · 全能力</p>
			</div>
		</div>

		<nav class="flex flex-1 flex-col gap-0.5 overflow-y-auto p-2">
			{#each SECTIONS as s (s.id)}
				<button
					type="button"
					onclick={() => (active = s.id)}
					class="flex items-center gap-2 rounded-lg px-2.5 py-2 text-left transition {active === s.id
						? 'bg-white/10 text-terminal-fg'
						: 'text-terminal-dim hover:bg-white/5 hover:text-terminal-fg'}"
				>
					<span class="text-sm">{s.emoji}</span>
					<div class="min-w-0">
						<div class="font-mono text-xs font-semibold">{s.label}</div>
						<div class="truncate text-[9px] text-terminal-dim/70">{s.desc}</div>
					</div>
					<span class="ml-auto font-mono text-[9px] text-terminal-dim/50">{s.caps.length}</span>
				</button>
			{/each}
		</nav>

		<div class="border-t border-terminal-edge p-2.5">
			<div class="flex flex-col gap-1 font-mono text-[9px] text-terminal-dim">
				<span class="flex items-center gap-1.5"><span class="size-1.5 rounded-full bg-sky-500"></span>gateway :8765</span>
				<span class="flex items-center gap-1.5"><span class="size-1.5 rounded-full bg-violet-500"></span>legacy :8010</span>
			</div>
		</div>
	</aside>

	<!-- ── main ───────────────────────────────────────────────────── -->
	<section class="flex min-w-0 flex-1 flex-col overflow-y-auto">
		<!-- section header -->
		<div class="flex shrink-0 items-center justify-between border-b border-terminal-edge bg-terminal-panel px-4 py-2.5">
			<div class="flex items-center gap-2">
				<span class="text-lg">{current.emoji}</span>
				<div>
					<h2 class="font-mono text-sm font-semibold">{current.label}</h2>
					<p class="font-mono text-[10px] text-terminal-dim">{current.desc}</p>
				</div>
			</div>
			<span class="rounded-full border border-terminal-edge px-2 py-0.5 font-mono text-[10px] text-terminal-dim">
				{current.caps.length} 项能力
			</span>
		</div>

		<div class="flex min-h-0 flex-1 flex-col gap-4 p-4">
			<!-- custom section panels -->
			{#if custom === "workspace"}
				<Workspace />
			{:else if custom === "agent"}
				<FlowConsole />
			{:else if custom === "chat"}
				<ArtifactChat />
			{:else if custom === "project"}
				<KanbanPanel />
			{/if}

			<!-- capability grid -->
			{#if custom}
				<div class="flex items-center gap-2">
					<span class="h-px flex-1 bg-terminal-edge"></span>
					<span class="shrink-0 font-mono text-[10px] tracking-wide text-terminal-dim/70">更多 {current.label} 能力</span>
					<span class="h-px flex-1 bg-terminal-edge"></span>
				</div>
			{/if}
			<div class="grid grid-cols-1 gap-3 xl:grid-cols-2 2xl:grid-cols-3">
				{#each current.caps as cap (cap.id)}
					<CapabilityRunner {cap} />
				{/each}
			</div>
		</div>
	</section>
</main>
