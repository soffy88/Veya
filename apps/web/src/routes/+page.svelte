<script lang="ts">
	import { onMount } from "svelte";
	import { goto } from "$app/navigation";
	import {
		Bot,
		Brain,
		Clock,
		Columns3,
		Cpu,
		GitBranch,
		Hammer,
		LayoutDashboard,
		ListTodo,
		Menu,
		MessageSquare,
		MoreHorizontal,
		Network,
		Package,
		Plus,
		Search,
		Settings,
		SquareCheckBig,
		Trash2,
		X,
	} from "lucide-svelte";
	import ChatConsole from "$lib/components/ChatConsole.svelte";
											import SettingsPanel from "$lib/components/SettingsPanel.svelte";
		import SearchPalette from "$lib/components/SearchPalette.svelte";
	import AuthGate from "$lib/components/AuthGate.svelte";
	import { api, type ApiResult } from "$lib/api";
	import { sessionStore } from "$lib/sessionStore.svelte";

	type View =
		| "bot"
		| "chat"
		| "dashboard"
		| "plan"
		| "git"
		| "graph"
		| "genesis"
		| "plugins"
		| "automation"
		| "board"
		| "tasks"
		| "personal";

	type RecentWork = {
		key: string;
		title: string;
		href: string;
		updatedMs: number;
	};

	let flowConsole = $state<any>();
	let ProductShellView = $state<any>(null);
	let FlowConsoleView = $state<any>(null);
	let DashboardView = $state<any>(null);
	let PlanBoardView = $state<any>(null);
	let GitPanelView = $state<any>(null);
	let ProjectMapView = $state<any>(null);
	let PluginPanelView = $state<any>(null);
	let AutomationPanelView = $state<any>(null);
	let KanbanPanelView = $state<any>(null);
	let TaskCenterPanelView = $state<any>(null);
	let PersonalContextPanelView = $state<any>(null);
	let settingsOpen = $state(false);
	let searchOpen = $state(false);
	let moreOpen = $state(false);
	let view = $state<View>("chat");
	let sidebarOpen = $state(false);
	let recentWorks = $state<RecentWork[]>([]);

	const PRIMARY: [View, string, typeof MessageSquare][] = [
		["chat", "Chat", MessageSquare],
		["bot", "Work", Bot],
	];

	const PRODUCT_MORE: [View, string, typeof MessageSquare][] = [
		["tasks", "Work 历史", SquareCheckBig],
		["automation", "自动化", Clock],
		["plugins", "Apps", Package],
		["personal", "个人上下文", Brain],
	];

	const DEVELOPER_MORE: [View, string, typeof MessageSquare][] = [
		["dashboard", "Dashboard", LayoutDashboard],
		["plan", "计划", ListTodo],
		["git", "Git", GitBranch],
		["graph", "图谱", Network],
		["genesis", "Genesis", Hammer],
		["board", "看板", Columns3],
	];

	const ADVANCED_LABELS: Partial<Record<View, string>> = {
		dashboard: "Dashboard",
		plan: "计划",
		git: "Git",
		graph: "图谱",
		genesis: "Genesis",
		plugins: "Apps",
		automation: "自动化",
		board: "看板",
		tasks: "Work 历史",
		personal: "个人上下文",
	};

	const primaryMode = $derived(view === "bot" ? "work" : view === "chat" ? "chat" : "advanced");
	const headerTitle = $derived(
		view === "chat" ? "Chat" : view === "bot" ? "Work" : (ADVANCED_LABELS[view] ?? "Veya"),
	);

	async function ensureView(next: View): Promise<void> {
		switch (next) {
			case "bot":
				if (!ProductShellView) ProductShellView = (await import("$lib/components/ProductShell.svelte")).default;
				break;
			case "dashboard":
				if (!DashboardView) DashboardView = (await import("$lib/components/Dashboard.svelte")).default;
				break;
			case "plan":
				if (!PlanBoardView) PlanBoardView = (await import("$lib/components/PlanBoard.svelte")).default;
				break;
			case "git":
				if (!GitPanelView) GitPanelView = (await import("$lib/components/GitPanel.svelte")).default;
				break;
			case "graph":
				if (!ProjectMapView) ProjectMapView = (await import("$lib/components/ProjectMap.svelte")).default;
				break;
			case "genesis":
				if (!FlowConsoleView) FlowConsoleView = (await import("$lib/components/FlowConsole.svelte")).default;
				break;
			case "plugins":
				if (!PluginPanelView) PluginPanelView = (await import("$lib/components/PluginPanel.svelte")).default;
				break;
			case "automation":
				if (!AutomationPanelView) AutomationPanelView = (await import("$lib/components/AutomationPanel.svelte")).default;
				break;
			case "board":
				if (!KanbanPanelView) KanbanPanelView = (await import("$lib/components/KanbanPanel.svelte")).default;
				break;
			case "tasks":
				if (!TaskCenterPanelView) TaskCenterPanelView = (await import("$lib/components/TaskCenterPanel.svelte")).default;
				break;
			case "personal":
				if (!PersonalContextPanelView) PersonalContextPanelView = (await import("$lib/components/PersonalContextPanel.svelte")).default;
				break;
		}
	}

	function closeSidebar(): void {
		sidebarOpen = false;
	}

	function selectNav(next: View): void {
		view = next;
		moreOpen = false;
		void ensureView(next).then(() => {
			if (next === "genesis") requestAnimationFrame(() => flowConsole?.newFlow());
		});
		closeSidebar();
	}

	function newChat(): void {
		sessionStore.newSession();
		view = "chat";
		closeSidebar();
	}

	function newWork(): void {
		selectNav("bot");
	}

	function openTasks(): void {
		selectNav("tasks");
	}

	function selectFromSearch(next: string): void {
		selectNav(next as View);
	}

	function openSession(sid: string): void {
		sessionStore.open(sid);
		view = "chat";
		closeSidebar();
	}

	function openWork(href: string): void {
		void goto(href);
		closeSidebar();
	}

	function sessionTime(ts: number): string {
		const d = new Date(ts);
		const now = new Date();
		const sameDay = d.toDateString() === now.toDateString();
		if (sameDay) return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
		return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
	}

	function workTime(value: number): string {
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return "";
		return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
	}

	async function loadRecentWork(): Promise<void> {
		const [taskResult, missionResult]: ApiResult[] = await Promise.all([
			api("gateway", "api/v1/tasks", { method: "GET", query: { limit: 8 } }),
			api("gateway", "api/v1/supervision/missions", { method: "GET" }),
		]);
		const rows: RecentWork[] = [];
		if (taskResult.ok && taskResult.data && typeof taskResult.data === "object") {
			for (const task of ((taskResult.data as { tasks?: Array<{ id: string; title: string; updated_at: string }> }).tasks ?? [])) {
				rows.push({ key: `task:${task.id}`, title: task.title, href: `/workbench/${encodeURIComponent(task.id)}`, updatedMs: Date.parse(task.updated_at) || 0 });
			}
		}
		if (missionResult.ok && missionResult.data && typeof missionResult.data === "object") {
			for (const mission of ((missionResult.data as { missions?: Array<{ mission_id: string; goal: string; updated_at: number }> }).missions ?? [])) {
				rows.push({ key: `mission:${mission.mission_id}`, title: mission.goal, href: `/missions/${encodeURIComponent(mission.mission_id)}`, updatedMs: Number(mission.updated_at || 0) * 1000 });
			}
		}
		recentWorks = rows.sort((x, y) => y.updatedMs - x.updatedMs).slice(0, 6);
	}

	function handleGlobalKeydown(event: KeyboardEvent): void {
		if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
			event.preventDefault();
			searchOpen = true;
		}
	}

	const DAY_MS = 86400000;
	function startOfDay(d: Date): number {
		return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
	}
	const sessionGroups = $derived.by(() => {
		const today0 = startOfDay(new Date());
		const labels = ["今天", "昨天", "7 天内", "30 天内", "更早"] as const;
		const buckets: Record<(typeof labels)[number], typeof sessionStore.sessions> = {
			"今天": [],
			"昨天": [],
			"7 天内": [],
			"30 天内": [],
			"更早": [],
		};
		for (const session of sessionStore.sessions.slice(0, 18)) {
			if (session.ts >= today0) buckets["今天"].push(session);
			else if (session.ts >= today0 - DAY_MS) buckets["昨天"].push(session);
			else if (session.ts >= today0 - 7 * DAY_MS) buckets["7 天内"].push(session);
			else if (session.ts >= today0 - 30 * DAY_MS) buckets["30 天内"].push(session);
			else buckets["更早"].push(session);
		}
		return labels.map((label) => ({ label, sessions: buckets[label] })).filter((group) => group.sessions.length > 0);
	});

	onMount(() => {
		void loadRecentWork();
		const requestedView = new URLSearchParams(window.location.search).get("view");
		if (requestedView === "tasks") selectNav("tasks");
		else if (requestedView === "work") selectNav("bot");
	});
</script>

<svelte:window onkeydown={handleGlobalKeydown} />

<main class="flex h-dvh overflow-hidden">
	{#if sidebarOpen}
		<div
			class="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm md:hidden"
			role="presentation"
			onclick={closeSidebar}
		></div>
	{/if}

	<aside
		class="fixed inset-y-0 left-0 z-50 flex w-64 shrink-0 flex-col border-r border-white/[0.06] bg-[#0a0a0a] transition-transform duration-200 ease-out md:static md:translate-x-0 {sidebarOpen
			? 'translate-x-0'
			: '-translate-x-full'}"
	>
		<div class="flex items-center gap-2.5 px-4 py-4">
			<span class="flex size-8 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 to-violet-600 font-mono text-sm font-bold text-white">V</span>
			<div class="min-w-0 flex-1">
				<h1 class="text-sm font-semibold tracking-tight">Veya</h1>
				<p class="text-xs text-terminal-dim">Personal Intelligence</p>
			</div>
			<button
				type="button"
				aria-label="关闭菜单"
				onclick={closeSidebar}
				class="rounded-md p-1.5 text-terminal-dim transition hover:bg-white/10 hover:text-terminal-fg md:hidden"
			>
				<X class="size-5" />
			</button>
		</div>

		<div class="grid grid-cols-2 gap-1.5 px-2.5">
			<button
				type="button"
				onclick={newChat}
				class="flex items-center justify-center gap-1.5 rounded-lg bg-white/10 px-2.5 py-2 text-xs font-medium text-white transition hover:bg-white/15"
			>
				<Plus class="size-3.5" /> Chat
			</button>
			<button
				type="button"
				onclick={newWork}
				class="flex items-center justify-center gap-1.5 rounded-lg border border-white/10 px-2.5 py-2 text-xs font-medium text-white/80 transition hover:bg-white/[0.06]"
			>
				<Plus class="size-3.5" /> Work
			</button>
		</div>

		<button
			type="button"
			onclick={() => (searchOpen = true)}
			class="mx-2.5 mt-2 flex items-center gap-2 rounded-lg border border-white/[0.08] px-3 py-2 text-left text-sm text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg"
		>
			<Search class="size-4" />
			<span class="flex-1">搜索</span>
			<kbd class="rounded border border-white/10 px-1.5 py-0.5 font-mono text-[10px] text-white/35">⌘K</kbd>
		</button>

		<nav class="mt-3 grid grid-cols-2 gap-1 px-2.5">
			{#each PRIMARY as [id, label, Icon] (id)}
				<button
					type="button"
					onclick={() => selectNav(id)}
					class="flex items-center justify-center gap-1.5 rounded-lg px-2 py-2 text-sm transition {view === id
						? 'bg-white/10 text-terminal-fg'
						: 'text-terminal-dim hover:bg-white/5 hover:text-terminal-fg'}"
				>
					<Icon class="size-4" />
					{label}
				</button>
			{/each}
		</nav>

		<div class="mt-3 min-h-0 flex-1 overflow-y-auto px-2 pb-2">
			<div class="px-2 pb-1 text-xs font-medium text-white/40">Recent</div>

			{#if recentWorks.length > 0}
				<div class="mt-1 px-2 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wider text-violet-300/60">Work</div>
				{#each recentWorks as work (work.key)}
					<button
						type="button"
						onclick={() => openWork(work.href)}
						class="group flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left transition hover:bg-white/[0.05]"
					>
						<SquareCheckBig class="size-3.5 shrink-0 text-violet-300/70" />
						<span class="min-w-0 flex-1 truncate text-[13px] text-terminal-fg">{work.title}</span>
						<span class="shrink-0 text-[9px] text-terminal-dim/60">{workTime(work.updatedMs)}</span>
					</button>
				{/each}
			{/if}

			{#if sessionStore.sessions.length > 0}
				<div class="mt-2 px-2 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wider text-sky-300/60">Chats</div>
				{#each sessionGroups as group (group.label)}
					<div class="px-2 pb-1 pt-2 text-[11px] text-terminal-dim/60">{group.label}</div>
					{#each group.sessions as session (session.sid)}
						<div
							class="group relative flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 transition {session.sid === sessionStore.activeSid && view === 'chat'
								? 'bg-white/[0.07]'
								: 'hover:bg-white/[0.05]'}"
							role="button"
							tabindex="0"
							onclick={() => openSession(session.sid)}
							onkeydown={(event) => {
								if (event.key === "Enter" || event.key === " ") openSession(session.sid);
							}}
						>
							<MessageSquare class="size-3.5 shrink-0 text-sky-300/65" />
							<span class="min-w-0 flex-1 truncate text-[13px] text-terminal-fg">{session.title}</span>
							<span class="shrink-0 text-[9px] text-terminal-dim/50">{sessionTime(session.ts)}</span>
							<button
								type="button"
								aria-label="删除会话"
								onclick={(event) => {
									event.stopPropagation();
									sessionStore.remove(session.sid);
								}}
								class="absolute right-1.5 hidden rounded-md p-1 text-terminal-dim hover:bg-rose-500/15 hover:text-rose-300 group-hover:block"
							>
								<Trash2 class="size-3.5" />
							</button>
						</div>
					{/each}
				{/each}
			{:else if recentWorks.length === 0}
				<p class="px-2 py-4 text-xs text-terminal-dim/60">还没有最近工作。</p>
			{/if}
		</div>

		<div class="relative border-t border-white/[0.06] p-2.5">
			<button
				type="button"
				onclick={() => (moreOpen = !moreOpen)}
				class="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg"
				aria-expanded={moreOpen}
			>
				<MoreHorizontal class="size-4" />
				<span class="flex-1">更多</span>
			</button>

			{#if moreOpen}
				<div class="absolute bottom-12 left-2.5 z-50 w-[236px] rounded-xl border border-white/10 bg-[#121212] p-1.5 shadow-2xl">
					<div class="px-2 pb-1 pt-1 text-[11px] font-medium uppercase tracking-wider text-white/40">Workspace</div>
					{#each PRODUCT_MORE as [id, label, Icon] (id)}
						<button
							type="button"
							onclick={() => selectNav(id)}
							class="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm text-terminal-dim hover:bg-white/[0.06] hover:text-terminal-fg"
						>
							<Icon class="size-4" /> {label}
						</button>
					{/each}
					<div class="my-1 border-t border-white/[0.06]"></div>
					<div class="px-2 pb-1 pt-1 text-[11px] font-medium uppercase tracking-wider text-white/40">Developer tools</div>
					{#each DEVELOPER_MORE as [id, label, Icon] (id)}
						<button
							type="button"
							onclick={() => selectNav(id)}
							class="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm text-terminal-dim hover:bg-white/[0.06] hover:text-terminal-fg"
						>
							<Icon class="size-4" /> {label}
						</button>
					{/each}
				</div>
			{/if}
		</div>
	</aside>

	<section class="flex min-w-0 flex-1 flex-col overflow-hidden">
		<header class="flex h-14 shrink-0 items-center gap-3 border-b border-white/[0.05] px-3 md:px-5">
			<button
				type="button"
				aria-label="打开菜单"
				onclick={() => (sidebarOpen = true)}
				class="rounded-lg p-2 text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg md:hidden"
			>
				<Menu class="size-5" />
			</button>

			<div class="flex min-w-0 items-center gap-2">
				<Cpu class="size-4 shrink-0 text-sky-400" />
				<span class="truncate text-sm font-medium text-terminal-fg">{headerTitle}</span>
			</div>

			{#if primaryMode !== "advanced"}
				<div class="ml-2 hidden items-center rounded-lg bg-white/[0.05] p-0.5 sm:flex">
					<button
						type="button"
						onclick={() => selectNav("chat")}
						class="rounded-md px-3 py-1.5 text-xs transition {view === 'chat' ? 'bg-white/10 text-white' : 'text-terminal-dim hover:text-white'}"
					>Chat</button>
					<button
						type="button"
						onclick={() => selectNav("bot")}
						class="rounded-md px-3 py-1.5 text-xs transition {view === 'bot' ? 'bg-white/10 text-white' : 'text-terminal-dim hover:text-white'}"
					>Work</button>
				</div>
			{/if}

			<span class="flex-1"></span>

			<button
				type="button"
				onclick={() => (searchOpen = true)}
				class="hidden items-center gap-2 rounded-lg border border-white/[0.08] px-2.5 py-1.5 text-xs text-terminal-dim transition hover:bg-white/[0.04] hover:text-terminal-fg sm:flex"
			>
				<Search class="size-3.5" />
				搜索
				<kbd class="font-mono text-[10px] text-white/30">⌘K</kbd>
			</button>
			<AuthGate />
			<button
				type="button"
				onclick={() => (settingsOpen = true)}
				class="rounded-lg p-2 text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg"
				aria-label="设置"
				title="设置"
			>
				<Settings class="size-4" />
			</button>
		</header>

		<div class="flex min-h-0 flex-1 flex-col" class:hidden={view !== "chat"}>
			<ChatConsole />
		</div>
		<div class="flex min-h-0 flex-1 flex-col" class:hidden={view !== "bot"}>
			{#if ProductShellView}
				<ProductShellView onOpenSettings={() => (settingsOpen = true)} onOpenTasks={openTasks} />
			{:else if view === "bot"}
				<div class="flex flex-1 items-center justify-center text-sm text-terminal-dim">正在加载 Work…</div>
			{/if}
		</div>

		{#if view === "dashboard"}
			<div class="flex min-h-0 flex-1 flex-col">{#if DashboardView}<DashboardView />{:else}<div class="p-6 text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "plan"}
			<div class="flex min-h-0 flex-1 flex-col">{#if PlanBoardView}<PlanBoardView />{:else}<div class="p-6 text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "git"}
			<div class="flex min-h-0 flex-1 flex-col">{#if GitPanelView}<GitPanelView />{:else}<div class="p-6 text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "graph"}
			<div class="flex min-h-0 flex-1 flex-col">{#if ProjectMapView}<ProjectMapView />{:else}<div class="p-6 text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "genesis"}
			<div class="flex min-h-0 flex-1 flex-col">{#if FlowConsoleView}<FlowConsoleView bind:this={flowConsole} />{:else}<div class="p-6 text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "plugins"}
			<div class="flex-1 overflow-y-auto p-6">{#if PluginPanelView}<PluginPanelView />{:else}<div class="text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "automation"}
			<div class="flex-1 overflow-y-auto p-6">{#if AutomationPanelView}<AutomationPanelView />{:else}<div class="text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "board"}
			<div class="flex-1 overflow-y-auto">{#if KanbanPanelView}<KanbanPanelView />{:else}<div class="p-6 text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "tasks"}
			<div class="flex-1 overflow-hidden">{#if TaskCenterPanelView}<TaskCenterPanelView />{:else}<div class="p-6 text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{:else if view === "personal"}
			<div class="flex-1 overflow-hidden">{#if PersonalContextPanelView}<PersonalContextPanelView />{:else}<div class="p-6 text-sm text-terminal-dim">正在加载…</div>{/if}</div>
		{/if}
	</section>
</main>

<SearchPalette
	open={searchOpen}
	onClose={() => (searchOpen = false)}
	onOpenChat={() => (view = "chat")}
	onSelectView={selectFromSearch}
/>
<SettingsPanel open={settingsOpen} onClose={() => (settingsOpen = false)} />
