<script lang="ts">
	/**
	 * Veya Web — human chat + Genesis construction + plugins + automation.
	 *
	 * Five views:
	 *   chat     → ChatConsole  (human ⇄ Master Brain, streaming, multi-session)
	 *   genesis  → FlowConsole  (master-model → Genesis 施工通道, 非人机对话)
	 *   plugins  → PluginPanel
	 *   automation → AutomationPanel
	 *   board    → KanbanPanel  (多 Agent 编排看板: worktree 隔离 + 依赖链)
	 */
	import { Bot, Cpu, Hammer, LayoutDashboard, ListTodo, MessageSquare, Package, Clock, Settings, Trash2, Plus, Columns3, Menu, X } from "lucide-svelte";
	import ChatConsole from "$lib/components/ChatConsole.svelte";
	import FlowConsole from "$lib/components/FlowConsole.svelte";
	import Dashboard from "$lib/components/Dashboard.svelte";
	import PlanBoard from "$lib/components/PlanBoard.svelte";
	import PluginPanel from "$lib/components/PluginPanel.svelte";
	import AutomationPanel from "$lib/components/AutomationPanel.svelte";
	import KanbanPanel from "$lib/components/KanbanPanel.svelte";
	import SettingsPanel from "$lib/components/SettingsPanel.svelte";
	import { sessionStore } from "$lib/sessionStore.svelte";

	type View = "chat" | "dashboard" | "plan" | "genesis" | "plugins" | "automation" | "board";

	let flowConsole: ReturnType<typeof FlowConsole> | undefined = $state();
	let settingsOpen = $state(false);
	let view = $state<View>("chat");
	let sidebarOpen = $state(false); // mobile drawer

	function closeSidebar() {
		sidebarOpen = false;
	}

	const NAV: [View, string, typeof MessageSquare][] = [
		["chat", "对话", MessageSquare],
		["dashboard", "Dashboard", LayoutDashboard],
		["plan", "计划", ListTodo],
		["genesis", "Genesis 施工", Hammer],
		["plugins", "插件", Package],
		["automation", "自动化", Clock],
		["board", "看板", Columns3],
	];

	function selectNav(v: View) {
		view = v;
		if (v === "genesis") flowConsole?.newFlow();
		closeSidebar();
	}

	function newChat() {
		sessionStore.newSession();
		view = "chat";
	}

	function sessionTime(ts: number): string {
		const d = new Date(ts);
		const now = new Date();
		const sameDay = d.toDateString() === now.toDateString();
		if (sameDay) return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
		return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
	}
</script>

<main class="flex h-screen overflow-hidden">
	<!-- ── mobile backdrop ────────────────────────────────────────── -->
	{#if sidebarOpen}
		<div
			class="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm md:hidden"
			role="presentation"
			onclick={closeSidebar}
		></div>
	{/if}

	<!-- ── sidebar (drawer on mobile, static on desktop) ──────────── -->
	<aside
		class="fixed inset-y-0 left-0 z-50 flex w-60 shrink-0 flex-col border-r border-white/5 bg-[#0a0a0a] transition-transform duration-200 ease-out md:static md:translate-x-0 {sidebarOpen
			? 'translate-x-0'
			: '-translate-x-full'}"
	>
		<div class="flex items-center gap-2 px-4 py-4">
			<span class="flex size-7 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500 to-violet-600 font-mono text-sm font-bold text-white">V</span>
			<div class="flex-1">
				<h1 class="text-sm font-semibold leading-tight tracking-tight">Veya</h1>
				<p class="text-xs text-terminal-dim">Agent OS</p>
			</div>
			<button
				type="button"
				title="关闭菜单"
				onclick={closeSidebar}
				class="rounded-md p-1.5 text-terminal-dim transition hover:bg-white/10 hover:text-terminal-fg md:hidden"
			>
				<X class="size-5" />
			</button>
		</div>

		<button
			type="button"
			onclick={newChat}
			class="mx-2.5 mt-1 flex items-center justify-center gap-1.5 rounded-lg bg-white/10 px-3 py-2 text-sm text-white transition hover:bg-white/20"
		>
			<Plus class="size-4" />
			新对话
		</button>

		{#if view === "chat"}
			<div class="flex min-h-0 flex-1 flex-col">
				<div class="px-4 pb-1 pt-3 font-mono text-[10px] uppercase tracking-wider text-terminal-dim/60">会话</div>
				<div class="flex-1 overflow-y-auto px-2 pb-2">
					{#if sessionStore.sessions.length === 0}
						<p class="px-2 py-3 font-mono text-xs text-terminal-dim/60">暂无历史会话</p>
					{:else}
						{#each sessionStore.sessions as s (s.sid)}
							<div
								class="group relative flex cursor-pointer flex-col gap-0.5 rounded-lg px-3 py-2 transition {s.sid === sessionStore.activeSid
									? 'bg-white/10'
									: 'hover:bg-white/5'}"
								onclick={() => sessionStore.open(s.sid)}
							>
								<span class="truncate pr-6 text-[13px] text-terminal-fg">{s.title}</span>
								<span class="flex items-center justify-between font-mono text-[10px] text-terminal-dim/70">
									<span>{sessionTime(s.ts)}</span>
									{#if s.cost > 0}
										<span>${s.cost.toFixed(4)}</span>
									{/if}
								</span>
								<button
									type="button"
									title="删除会话"
									onclick={(e) => {
										e.stopPropagation();
										sessionStore.remove(s.sid);
									}}
									class="absolute right-1.5 top-1.5 hidden rounded-md p-1 text-terminal-dim transition hover:bg-rose-500/20 hover:text-rose-400 group-hover:block"
								>
									<Trash2 class="size-3.5" />
								</button>
							</div>
						{/each}
					{/if}
				</div>
			</div>
		{:else}
			<div class="flex-1"></div>
		{/if}

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

		<div class="border-t border-white/5 p-2.5">
			<div class="flex flex-col gap-1 text-xs text-terminal-dim">
				<span class="flex items-center gap-1.5"><span class="size-1.5 rounded-full bg-sky-500"></span>Master Brain :9120</span>
				<span class="flex items-center gap-1.5"><span class="size-1.5 rounded-full bg-violet-500"></span>Genesis 3O 施工</span>
			</div>
		</div>
	</aside>

	<!-- ── main ───────────────────────────────────────────────────── -->
	<section class="flex min-w-0 flex-1 flex-col overflow-hidden">
		<header class="flex shrink-0 items-center gap-4 px-6 py-3">
			<button
				type="button"
				title="打开菜单"
				onclick={() => (sidebarOpen = true)}
				class="rounded-lg border border-terminal-edge p-2 text-terminal-dim transition hover:border-sky-500/40 hover:text-terminal-fg md:hidden"
			>
				<Menu class="size-5" />
			</button>
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
			<ChatConsole />
		</div>
		<div class="flex min-h-0 flex-1 flex-col" class:hidden={view !== "dashboard"}>
			<Dashboard />
		</div>
		<div class="flex min-h-0 flex-1 flex-col" class:hidden={view !== "plan"}>
			<PlanBoard />
		</div>
		<div class="flex min-h-0 flex-1 flex-col" class:hidden={view !== "genesis"}>
			<FlowConsole bind:this={flowConsole} />
		</div>
		{#if view === "plugins"}
			<div class="flex-1 overflow-y-auto p-6"><PluginPanel /></div>
		{:else if view === "automation"}
			<div class="flex-1 overflow-y-auto p-6"><AutomationPanel /></div>
			<div class="flex-1 overflow-y-auto"><KanbanPanel /></div>
		{/if}
	</section>
</main>

<SettingsPanel open={settingsOpen} onClose={() => (settingsOpen = false)} />
