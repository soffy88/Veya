<script lang="ts">
	import { onMount } from "svelte";
	import { goto } from "$app/navigation";
	import { browser } from "$app/environment";
	import {
		Bot,
		Brain,
		Clock,
		Columns3,
		Copy,
		Cpu,
		GitBranch,
		GitFork,
		Hammer,
		LayoutDashboard,
		ListTodo,
		Menu,
		MessageSquare,
		MoreHorizontal,
		Network,
		Package,
		Search,
		Send,
		Settings,
		SquareCheckBig,
		Trash2,
		Users,
		X,
		MonitorSmartphone,
	} from "lucide-svelte";
	import ChatConsole from "$lib/components/ChatConsole.svelte";
											import SettingsPanel from "$lib/components/SettingsPanel.svelte";
		import SearchPalette from "$lib/components/SearchPalette.svelte";
	import AuthGate from "$lib/components/AuthGate.svelte";
import { api, type ApiResult, API_BASE } from "$lib/api";
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

	// Session context menu
	let sessionMenuOpen = $state<string | null>(null);
	let sessionMenuSid = $state<string | null>(null);
	let sessionMenuPos = $state<{ x: number; y: number }>({ x: 0, y: 0 });

	// Handoff dialog
	let handoffOpen = $state(false);
	let handoffSid = $state<string | null>(null);
	let handoffMode = $state<"focused" | "full">("focused");
	let handoffTargetBackend = $state<string>("codex");
	let handoffTargetModel = $state<string>("");
	let handoffNote = $state<string>("");
	let handoffBrief = $state<string>("");
	let handoffCopyablePrompt = $state<string>("");
	let handoffLoading = $state(false);

	// Fork dialog
	let forkOpen = $state(false);
	let forkSid = $state<string | null>(null);
	let forkSummary = $state<string>("");
	let forkLoading = $state(false);

	// Session search
	let sessionSearchQuery = $state<string>("");
	let sessionSearchResults = $state<Array<{ sid: string; revision: number; snippet: string; ts: number; title: string }>>([]);
	let sessionSearchLoading = $state(false);
	let sessionSearchOpen = $state(false);

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

	// Session context menu handlers
	function openSessionMenu(event: MouseEvent, sid: string): void {
		event.stopPropagation();
		event.preventDefault();
		sessionMenuSid = sid;
		sessionMenuPos = { x: event.clientX, y: event.clientY };
		sessionMenuOpen = sid;
	}

	function closeSessionMenu(): void {
		sessionMenuOpen = null;
		sessionMenuSid = null;
	}

	// Handoff handlers
	async function openHandoffDialog(sid: string): Promise<void> {
		closeSessionMenu();
		handoffSid = sid;
		handoffMode = "focused";
		handoffTargetBackend = "codex";
		handoffTargetModel = "";
		handoffNote = "";
		handoffBrief = "";
		handoffCopyablePrompt = "";
		handoffLoading = true;
		handoffOpen = true;

		try {
			const token = localStorage.getItem("veya.auth.token");
			const headers: Record<string, string> = { "content-type": "application/json" };
			if (token) headers.authorization = `Bearer ${token}`;

			const res = await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(sid)}/handoff/preview?mode=focused`, { headers });
			if (res.ok) {
				const data = await res.json();
				handoffBrief = data.brief || "";
			}
		} catch {
			handoffBrief = "Failed to load preview";
		} finally {
			handoffLoading = false;
		}
	}

	async function updateHandoffPreview(): Promise<void> {
		if (!handoffSid) return;
		handoffLoading = true;
		try {
			const token = localStorage.getItem("veya.auth.token");
			const headers: Record<string, string> = { "content-type": "application/json" };
			if (token) headers.authorization = `Bearer ${token}`;

			const res = await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(handoffSid)}/handoff/preview?mode=${handoffMode}`, { headers });
			if (res.ok) {
				const data = await res.json();
				handoffBrief = data.brief || "";
			}
		} catch {
			handoffBrief = "Failed to load preview";
		} finally {
			handoffLoading = false;
		}
	}

	async function confirmHandoff(): Promise<void> {
		if (!handoffSid) return;
		handoffLoading = true;
		try {
			const token = localStorage.getItem("veya.auth.token");
			const headers: Record<string, string> = { "content-type": "application/json" };
			if (token) headers.authorization = `Bearer ${token}`;

			const res = await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(handoffSid)}/handoff`, {
				method: "POST",
				headers,
				body: JSON.stringify({
					target_backend: handoffTargetBackend,
					target_model: handoffTargetModel || undefined,
					mode: handoffMode,
					note: handoffNote || undefined,
				}),
			});

			if (res.ok) {
				const data = await res.json();
				handoffCopyablePrompt = data.copyable_prompt || "";
				alert("Handoff session created! Use the copyable prompt to continue in the target agent.");
			} else {
				const err = await res.json();
				alert(`Handoff failed: ${err.detail || "Unknown error"}`);
			}
		} catch {
			alert("Handoff failed: Network error");
		} finally {
			handoffLoading = false;
		}
	}

	function copyHandoffPrompt(): void {
		if (handoffCopyablePrompt) {
			navigator.clipboard.writeText(handoffCopyablePrompt);
			alert("Copied to clipboard");
		}
	}

	// Fork handlers
	async function openForkDialog(sid: string): Promise<void> {
		closeSessionMenu();
		forkSid = sid;
		forkSummary = "";
		forkLoading = true;
		forkOpen = true;

		try {
			const token = localStorage.getItem("veya.auth.token");
			const headers: Record<string, string> = { "content-type": "application/json" };
			if (token) headers.authorization = `Bearer ${token}`;

			const res = await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(sid)}/fork/preview`, { headers });
			if (res.ok) {
				const data = await res.json();
				forkSummary = data.draft_summary || "";
			}
		} catch {
			forkSummary = "Failed to load preview";
		} finally {
			forkLoading = false;
		}
	}

	async function confirmFork(): Promise<void> {
		if (!forkSid) return;
		forkLoading = true;
		try {
			const token = localStorage.getItem("veya.auth.token");
			const headers: Record<string, string> = { "content-type": "application/json" };
			if (token) headers.authorization = `Bearer ${token}`;

			const res = await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(forkSid)}/fork`, {
				method: "POST",
				headers,
				body: JSON.stringify({
					label: "",
					polish: true,
					summary: forkSummary,
				}),
			});

			if (res.ok) {
				const data = await res.json();
				sessionStore.ensureSession(data.session_id, data.summary?.slice(0, 40) || "Forked session");
				alert("Fork created successfully!");
				forkOpen = false;
			} else {
				const err = await res.json();
				alert(`Fork failed: ${err.detail || "Unknown error"}`);
			}
		} catch {
			alert("Fork failed: Network error");
		} finally {
			forkLoading = false;
		}
	}

	// Session search handlers
	async function doSessionSearch(): Promise<void> {
		if (!sessionSearchQuery.trim()) return;
		sessionSearchLoading = true;
		sessionSearchOpen = true;
		try {
			const token = localStorage.getItem("veya.auth.token");
			const headers: Record<string, string> = {};
			if (token) headers.authorization = `Bearer ${token}`;

			const params = new URLSearchParams({ q: sessionSearchQuery, limit: "20" });
			const res = await fetch(`${API_BASE}/api/v1/sessions/search?${params}`, { headers });
			if (res.ok) {
				const data = await res.json();
				sessionSearchResults = data.results || [];
			} else {
				sessionSearchResults = [];
			}
		} catch {
			sessionSearchResults = [];
		} finally {
			sessionSearchLoading = false;
		}
	}

	function openSearchResult(sid: string): void {
		sessionSearchOpen = false;
		sessionSearchQuery = "";
		sessionSearchResults = [];
		openSession(sid);
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
		if (!browser) return;
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

		<div class="mt-3 min-h-0 flex-1 overflow-y-auto px-2 pb-2">
			<a
				href="/team"
				class="flex min-h-11 items-center gap-2 rounded-lg px-2.5 text-left transition hover:bg-white/[0.05]"
			>
				<Users class="size-3.5 shrink-0 text-sky-300/70" />
				<span class="min-w-0 flex-1 truncate text-[13px] text-terminal-fg">Bot 团队</span>
			</a>
			<a
				href="/remote"
				class="flex min-h-11 items-center gap-2 rounded-lg px-2.5 text-left transition hover:bg-white/[0.05]"
			>
				<MonitorSmartphone class="size-3.5 shrink-0 text-sky-300/70" />
				<span class="min-w-0 flex-1 truncate text-[13px] text-terminal-fg">远程控制</span>
			</a>
			<div class="px-2 pb-1 pt-3 text-xs font-medium text-white/40">Recent</div>

			{#if recentWorks.length > 0}
				<div class="mt-1 px-2 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wider text-violet-300/60">Work</div>
				{#each recentWorks as work (work.key)}
					<button
						type="button"
						onclick={() => openWork(work.href)}
						class="group flex min-h-11 w-full items-center gap-2 rounded-lg px-2.5 text-left transition hover:bg-white/[0.05]"
					>
						<SquareCheckBig class="size-3.5 shrink-0 text-violet-300/70" />
						<span class="min-w-0 flex-1 truncate text-[13px] text-terminal-fg">{work.title}</span>
						<span class="shrink-0 text-[9px] text-terminal-dim/60">{workTime(work.updatedMs)}</span>
					</button>
				{/each}
			{/if}

			{#if sessionStore.sessions.length > 0}
				<div class="mt-2 px-2 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wider text-sky-300/60 flex items-center gap-2">
					Chats
					<input
						type="text"
						placeholder="搜索会话..."
						bind:value={sessionSearchQuery}
						onkeydown={(e) => { if (e.key === "Enter") doSessionSearch(); }}
						class="flex-1 ml-2 rounded-md bg-white/[0.05] border border-white/[0.08] px-2 py-1 text-[11px] text-terminal-fg placeholder:text-terminal-dim/50 focus:outline-none focus:border-sky-400"
					/>
					<button
						type="button"
						onclick={doSessionSearch}
						disabled={sessionSearchLoading || !sessionSearchQuery.trim()}
						class="rounded-md p-1.5 text-terminal-dim hover:text-terminal-fg transition disabled:opacity-50"
						aria-label="搜索"
					>
						<Search class="size-3.5" />
					</button>
				</div>
				{#each sessionGroups as group (group.label)}
					<div class="px-2 pb-1 pt-2 text-[11px] text-terminal-dim/60">{group.label}</div>
					{#each group.sessions as session (session.sid)}
						<div
							class="group relative flex min-h-11 cursor-pointer items-center gap-2 rounded-lg px-2.5 transition {session.sid === sessionStore.activeSid && view === 'chat'
								? 'bg-white/[0.07]'
								: 'hover:bg-white/[0.05]'}"
							role="button"
							tabindex="0"
							onclick={() => { if (sessionMenuOpen !== session.sid) openSession(session.sid); }}
							onkeydown={(event) => {
								if (event.key === "Enter" || event.key === " ") openSession(session.sid);
							}}
							oncontextmenu={(e) => openSessionMenu(e, session.sid)}
						>
							<MessageSquare class="size-3.5 shrink-0 text-sky-300/65" />
							<span class="min-w-0 flex-1 truncate text-[13px] text-terminal-fg">{session.title}</span>
							<span class="shrink-0 text-[9px] text-terminal-dim/50">{sessionTime(session.ts)}</span>
							<button
								type="button"
								aria-label="更多操作"
								onclick={(event) => openSessionMenu(event, session.sid)}
								class="absolute right-6 hidden rounded-md p-1 text-terminal-dim hover:bg-white/10 hover:text-terminal-fg group-hover:block"
							>
								<MoreHorizontal class="size-3.5" />
							</button>
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
				class="flex min-h-11 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg"
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
							class="flex min-h-11 w-full items-center gap-2 rounded-lg px-2.5 text-left text-sm text-terminal-dim hover:bg-white/[0.06] hover:text-terminal-fg"
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
							class="flex min-h-11 w-full items-center gap-2 rounded-lg px-2.5 text-left text-sm text-terminal-dim hover:bg-white/[0.06] hover:text-terminal-fg"
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
				class="inline-flex size-11 items-center justify-center rounded-lg text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg md:hidden"
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
						class="min-h-11 rounded-md px-3 text-xs transition {view === 'chat' ? 'bg-white/10 text-white' : 'text-terminal-dim hover:text-white'}"
					>Chat</button>
					<button
						type="button"
						onclick={() => selectNav("bot")}
						class="min-h-11 rounded-md px-3 text-xs transition {view === 'bot' ? 'bg-white/10 text-white' : 'text-terminal-dim hover:text-white'}"
					>Work</button>
				</div>
			{/if}

			<span class="flex-1"></span>

			<button
				type="button"
				onclick={() => (searchOpen = true)}
				class="hidden min-h-11 items-center gap-2 rounded-lg border border-white/[0.08] px-2.5 text-xs text-terminal-dim transition hover:bg-white/[0.04] hover:text-terminal-fg sm:flex"
			>
				<Search class="size-3.5" />
				搜索
				<kbd class="font-mono text-[10px] text-white/30">⌘K</kbd>
			</button>

			<AuthGate />
			<button
				type="button"
				onclick={() => (settingsOpen = true)}
				class="inline-flex size-11 items-center justify-center rounded-lg text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg"
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

{#if sessionMenuOpen && sessionMenuSid}
	<div
		class="fixed z-50"
		style="top: {sessionMenuPos.y}px; left: {sessionMenuPos.x}px;"
		onclick={closeSessionMenu}
	>
		<div class="rounded-xl border border-white/10 bg-[#121212] p-1.5 shadow-2xl min-w-[180px]">
			<button
				type="button"
				onclick={() => { openForkDialog(sessionMenuSid!); closeSessionMenu(); }}
				class="flex min-h-10 w-full items-center gap-2 rounded-lg px-2.5 text-left text-sm text-terminal-dim hover:bg-white/[0.06] hover:text-terminal-fg"
			>
				<GitFork class="size-4" /> Fork
			</button>
			<button
				type="button"
				onclick={() => { openHandoffDialog(sessionMenuSid!); closeSessionMenu(); }}
				class="flex min-h-10 w-full items-center gap-2 rounded-lg px-2.5 text-left text-sm text-terminal-dim hover:bg-white/[0.06] hover:text-terminal-fg"
			>
				<Send class="size-4" /> Hand off to another agent
			</button>
			<div class="my-1 border-t border-white/[0.06]"></div>
			<button
				type="button"
				onclick={() => { sessionStore.remove(sessionMenuSid!); closeSessionMenu(); }}
				class="flex min-h-10 w-full items-center gap-2 rounded-lg px-2.5 text-left text-sm text-rose-300 hover:bg-rose-500/15"
			>
				<Trash2 class="size-4" /> 删除
			</button>
		</div>
	</div>
{/if}

{#if handoffOpen}
	<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onclick={() => { handoffOpen = false; handoffSid = null; }}>
		<div class="w-[520px] max-h-[80vh] overflow-y-auto rounded-xl border border-white/10 bg-[#0a0a0a] p-4 shadow-2xl" onclick={(e) => e.stopPropagation()}>
			<div class="flex items-center justify-between mb-4">
				<h3 class="text-lg font-semibold">Hand off to another agent</h3>
				<button type="button" onclick={() => { handoffOpen = false; handoffSid = null; }} class="text-terminal-dim hover:text-terminal-fg"><X class="size-5" /></button>
			</div>
			<div class="space-y-4">
				<div>
					<label class="block text-xs font-medium text-terminal-dim mb-1">Target Agent</label>
					<select bind:value={handoffTargetBackend} class="w-full rounded-lg bg-white/[0.05] border border-white/[0.08] px-3 py-2 text-terminal-fg focus:outline-none focus:border-sky-400">
						{#each ["codex", "claude", "opencode", "pi"] as backend (backend)}
							<option value={backend}>{backend}</option>
						{/each}
					</select>
				</div>
				<div>
					<label class="block text-xs font-medium text-terminal-dim mb-1">Model (optional)</label>
					<input type="text" bind:value={handoffTargetModel} placeholder="e.g., gpt-4o" class="w-full rounded-lg bg-white/[0.05] border border-white/[0.08] px-3 py-2 text-terminal-fg focus:outline-none focus:border-sky-400" />
				</div>
				<div>
					<label class="block text-xs font-medium text-terminal-dim mb-1">Mode</label>
					<div class="flex gap-4">
						<label class="flex items-center gap-2 cursor-pointer">
							<input type="radio" bind:group={handoffMode} value="focused" onchange={updateHandoffPreview} />
							<span>Focused (brief)</span>
						</label>
						<label class="flex items-center gap-2 cursor-pointer">
							<input type="radio" bind:group={handoffMode} value="full" onchange={updateHandoffPreview} />
							<span>Full transcript</span>
						</label>
					</div>
				</div>
				<div>
					<label class="block text-xs font-medium text-terminal-dim mb-1">Note (optional)</label>
					<textarea bind:value={handoffNote} rows={2} class="w-full rounded-lg bg-white/[0.05] border border-white/[0.08] px-3 py-2 text-terminal-fg focus:outline-none focus:border-sky-400" placeholder="Additional context for the target agent..." />
				</div>
				<div>
					<label class="block text-xs font-medium text-terminal-dim mb-1">Brief Preview (editable)</label>
					<textarea bind:value={handoffBrief} rows={10} class="w-full rounded-lg bg-white/[0.05] border border-white/[0.08] px-3 py-2 text-terminal-fg focus:outline-none focus:border-sky-400 font-mono text-[11px]" disabled={handoffLoading} />
					{#if handoffMode === "full"}
						<p class="mt-1 text-xs text-rose-300">Warning: Full mode includes entire transcript (slower, costs context).</p>
					{/if}
				</div>
				<div class="flex gap-2 pt-2 border-t border-white/[0.06]">
					<button type="button" onclick={() => { handoffOpen = false; handoffSid = null; }} class="flex-1 rounded-lg px-3 py-2 text-terminal-dim hover:bg-white/[0.05]">Cancel</button>
					<button type="button" onclick={confirmHandoff} disabled={handoffLoading} class="flex-1 rounded-lg bg-sky-500 px-3 py-2 text-white hover:bg-sky-400 disabled:opacity-50">
						{handoffLoading ? "Creating..." : "Create Handoff Session"}
					</button>
				</div>
				{#if handoffCopyablePrompt}
					<div class="pt-2 border-t border-white/[0.06]">
						<label class="block text-xs font-medium text-terminal-dim mb-1">Copyable Prompt (fallback)</label>
						<div class="flex gap-2">
							<textarea readonly bind:value={handoffCopyablePrompt} rows={4} class="flex-1 rounded-lg bg-white/[0.05] border border-white/[0.08] px-3 py-2 text-terminal-fg font-mono text-[11px]" />
							<button type="button" onclick={copyHandoffPrompt} class="rounded-lg bg-white/[0.05] px-3 py-2 text-terminal-dim hover:bg-white/[0.1]">Copy</button>
						</div>
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

{#if forkOpen}
	<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onclick={() => { forkOpen = false; forkSid = null; }}>
		<div class="w-[520px] max-h-[80vh] overflow-y-auto rounded-xl border border-white/10 bg-[#0a0a0a] p-4 shadow-2xl" onclick={(e) => e.stopPropagation()}>
			<div class="flex items-center justify-between mb-4">
				<h3 class="text-lg font-semibold">Fork Session (Polish)</h3>
				<button type="button" onclick={() => { forkOpen = false; forkSid = null; }} class="text-terminal-dim hover:text-terminal-fg"><X class="size-5" /></button>
			</div>
			<div class="space-y-4">
				<div>
					<label class="block text-xs font-medium text-terminal-dim mb-1">Draft Summary (editable)</label>
					<textarea bind:value={forkSummary} rows={12} class="w-full rounded-lg bg-white/[0.05] border border-white/[0.08] px-3 py-2 text-terminal-fg focus:outline-none focus:border-sky-400 font-mono text-[11px]" disabled={forkLoading} />
				</div>
				<div class="flex gap-2 pt-2 border-t border-white/[0.06]">
					<button type="button" onclick={() => { forkOpen = false; forkSid = null; }} class="flex-1 rounded-lg px-3 py-2 text-terminal-dim hover:bg-white/[0.05]">Cancel</button>
					<button type="button" onclick={confirmFork} disabled={forkLoading} class="flex-1 rounded-lg bg-violet-500 px-3 py-2 text-white hover:bg-violet-400 disabled:opacity-50">
						{forkLoading ? "Creating..." : "Create Fork"}
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}

{#if sessionSearchOpen && (sessionSearchResults.length > 0 || sessionSearchLoading)}
	<div class="fixed inset-0 z-50 flex items-start justify-center pt-20 bg-black/60 backdrop-blur-sm" onclick={() => { sessionSearchOpen = false; sessionSearchResults = []; sessionSearchQuery = ""; }}>
		<div class="w-[600px] max-h-[70vh] overflow-y-auto rounded-xl border border-white/10 bg-[#0a0a0a] p-4 shadow-2xl" onclick={(e) => e.stopPropagation()}>
			<div class="flex items-center justify-between mb-4">
				<h3 class="text-lg font-semibold">Session Search Results</h3>
				<button type="button" onclick={() => { sessionSearchOpen = false; sessionSearchResults = []; sessionSearchQuery = ""; }} class="text-terminal-dim hover:text-terminal-fg"><X class="size-5" /></button>
			</div>
			<div class="mb-4 flex gap-2">
				<input
					type="text"
					bind:value={sessionSearchQuery}
					onkeydown={(e) => { if (e.key === "Enter") doSessionSearch(); }}
					placeholder="Search sessions..."
					class="flex-1 rounded-lg bg-white/[0.05] border border-white/[0.08] px-3 py-2 text-terminal-fg focus:outline-none focus:border-sky-400"
				/>
				<button type="button" onclick={doSessionSearch} disabled={sessionSearchLoading || !sessionSearchQuery.trim()} class="rounded-lg bg-sky-500 px-3 py-2 text-white hover:bg-sky-400 disabled:opacity-50">Search</button>
			</div>
			{#if sessionSearchLoading}
				<div class="flex items-center justify-center py-8 text-terminal-dim">Searching...</div>
			{:else if sessionSearchResults.length === 0}
				<div class="text-center py-8 text-terminal-dim">No results found</div>
			{:else}
				<ul class="space-y-2">
					{#each sessionSearchResults as result (result.sid + result.revision)}
						<button
							type="button"
							onclick={() => openSearchResult(result.sid)}
							class="w-full text-left p-3 rounded-lg bg-white/[0.03] hover:bg-white/[0.05] transition"
						>
							<div class="flex items-center gap-2 mb-1">
								<MessageSquare class="size-3.5 text-sky-300/65" />
								<span class="font-medium text-terminal-fg">{result.title}</span>
								<span class="text-[10px] text-terminal-dim ml-auto">{new Date(result.ts * 1000).toLocaleString()}</span>
							</div>
							<div class="text-[11px] text-terminal-dim/70 pl-5">{result.snippet}</div>
						</button>
					{/each}
				</ul>
			{/if}
		</div>
	</div>
{/if}

<SearchPalette
	open={searchOpen}
	onClose={() => (searchOpen = false)}
	onOpenChat={() => (view = "chat")}
	onSelectView={selectFromSearch}
/>
<SettingsPanel open={settingsOpen} onClose={() => (settingsOpen = false)} />
