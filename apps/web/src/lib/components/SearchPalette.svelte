<script lang="ts">
	import { tick } from "svelte";
	import { goto } from "$app/navigation";
	import {
		ArrowRight,
		Boxes,
		FileText,
		Folder,
		LayoutGrid,
		MessageSquare,
		PackageCheck,
		Search,
		SquareCheckBig,
		X,
	} from "lucide-svelte";
	import { api, type ApiResult } from "$lib/api";
	import { sessionStore } from "$lib/sessionStore.svelte";

	type SearchTask = {
		id: string;
		title: string;
		objective: string;
		status: string;
		updated_at: string;
	};

	type SearchMission = {
		mission_id: string;
		goal: string;
		status: string;
		updated_at: number;
	};

	type SearchProject = {
		id: string;
		name: string;
		icon?: string;
		session_count?: number;
	};

	type FileEntry = {
		name: string;
		type: "dir" | "file";
		path: string;
		size?: number;
		children?: FileEntry[];
	};

	type SearchArtifact = {
		taskId: string;
		taskTitle: string;
		name: string;
		available: boolean;
	};

	interface Props {
		open: boolean;
		onClose: () => void;
		onOpenChat: () => void;
		onSelectView: (view: string) => void;
	}

	let { open, onClose, onOpenChat, onSelectView }: Props = $props();
	let query = $state("");
	let tasks = $state<SearchTask[]>([]);
	let missions = $state<SearchMission[]>([]);
	let projects = $state<SearchProject[]>([]);
	let files = $state<FileEntry[]>([]);
	let artifacts = $state<SearchArtifact[]>([]);
	let loading = $state(false);
	let loadAttempted = $state(false);
	let inputEl = $state<HTMLInputElement>();
	let dialogEl = $state<HTMLDivElement>();

	const shortcuts = [
		{ label: "Work", hint: "创建或继续长期工作", view: "bot" },
		{ label: "Work 历史", hint: "查看全部工作、状态与恢复入口", view: "tasks" },
		{ label: "自动化", hint: "管理自动化与后台流程", view: "automation" },
		{ label: "Apps", hint: "查看插件与连接能力", view: "plugins" },
		{ label: "个人上下文", hint: "查看 Personal Runtime", view: "personal" },
		{ label: "开发者工具", hint: "Dashboard、Git、图谱与 Genesis", view: "dashboard" },
	] as const;

	function flattenFiles(entries: FileEntry[], out: FileEntry[] = []): FileEntry[] {
		for (const entry of entries) {
			if (out.length >= 600) break;
			if (entry.type === "file") out.push(entry);
			if (entry.children?.length) flattenFiles(entry.children, out);
		}
		return out;
	}

	async function loadArtifacts(taskRows: SearchTask[]): Promise<void> {
		const recent = taskRows.slice(0, 20);
		const results = await Promise.all(
			recent.map(async (task) => {
				const result = await api("gateway", `api/v1/workbench/${encodeURIComponent(task.id)}`, { method: "GET" });
				if (!result.ok || !result.data || typeof result.data !== "object") return [];
				const rows = ((result.data as { artifacts?: Array<{ name?: string; available?: boolean }> }).artifacts ?? []);
				return rows
					.filter((item) => item.name)
					.map((item) => ({
						taskId: task.id,
						taskTitle: task.title,
						name: String(item.name),
						available: item.available !== false,
					}));
			}),
		);
		artifacts = results.flat();
	}

	async function loadSearchData(): Promise<void> {
		if (loadAttempted) return;
		loadAttempted = true;
		loading = true;

		const [taskResult, missionResult, projectResult, fileResult]: ApiResult[] = await Promise.all([
			api("gateway", "api/v1/tasks", { method: "GET", query: { limit: 100 } }),
			api("gateway", "api/v1/supervision/missions", { method: "GET" }),
			api("gateway", "projects", { method: "GET" }),
			api("gateway", "api/v1/fs/tree", { method: "GET" }),
		]);

		if (taskResult.ok && taskResult.data && typeof taskResult.data === "object") {
			tasks = ((taskResult.data as { tasks?: SearchTask[] }).tasks ?? []).slice(0, 100);
		}
		if (missionResult.ok && missionResult.data && typeof missionResult.data === "object") {
			missions = ((missionResult.data as { missions?: SearchMission[] }).missions ?? []).slice(0, 100);
		}
		if (projectResult.ok && projectResult.data && typeof projectResult.data === "object") {
			projects = ((projectResult.data as { projects?: SearchProject[] }).projects ?? []).slice(0, 100);
		}
		if (fileResult.ok && fileResult.data && typeof fileResult.data === "object") {
			files = flattenFiles((fileResult.data as { entries?: FileEntry[] }).entries ?? []);
		}

		loading = false;
		void loadArtifacts(tasks);
	}

	$effect(() => {
		if (!open) return;
		void loadSearchData();
		void tick().then(() => inputEl?.focus());
	});

	const normalizedQuery = $derived(query.trim().toLocaleLowerCase("zh-CN"));

	function contains(value: string): boolean {
		return value.toLocaleLowerCase("zh-CN").includes(normalizedQuery);
	}

	const matchedSessions = $derived.by(() => {
		const list = sessionStore.sessions;
		if (!normalizedQuery) return list.slice(0, 6);
		return list.filter((session) => contains(session.title)).slice(0, 8);
	});
	const matchedTasks = $derived.by(() => {
		if (!normalizedQuery) return tasks.slice(0, 6);
		return tasks.filter((task) => contains(`${task.title} ${task.objective}`)).slice(0, 8);
	});
	const matchedMissions = $derived.by(() => {
		if (!normalizedQuery) return missions.slice(0, 4);
		return missions.filter((mission) => contains(`${mission.goal} ${mission.status}`)).slice(0, 8);
	});
	const matchedProjects = $derived.by(() => {
		if (!normalizedQuery) return projects.slice(0, 4);
		return projects.filter((project) => contains(`${project.name} ${project.id}`)).slice(0, 8);
	});
	const matchedFiles = $derived.by(() => {
		if (!normalizedQuery) return [];
		return files.filter((file) => contains(`${file.name} ${file.path}`)).slice(0, 10);
	});
	const matchedArtifacts = $derived.by(() => {
		if (!normalizedQuery) return artifacts.slice(0, 4);
		return artifacts.filter((artifact) => contains(`${artifact.name} ${artifact.taskTitle}`)).slice(0, 8);
	});
	const matchedShortcuts = $derived.by(() => {
		if (!normalizedQuery) return shortcuts.slice(0, 4);
		return shortcuts.filter((item) => contains(`${item.label} ${item.hint}`));
	});

	const resultCount = $derived(
		matchedSessions.length +
			matchedTasks.length +
			matchedMissions.length +
			matchedProjects.length +
			matchedFiles.length +
			matchedArtifacts.length +
			matchedShortcuts.length,
	);

	function openSession(sid: string): void {
		sessionStore.open(sid);
		onOpenChat();
		onClose();
	}

	function openTask(taskId: string): void {
		onClose();
		void goto(`/workbench/${encodeURIComponent(taskId)}`);
	}

	function openMission(missionId: string): void {
		onClose();
		void goto(`/missions/${encodeURIComponent(missionId)}`);
	}

	function workStatusLabel(status: string): string {
		const labels: Record<string, string> = { pending: "待处理", running: "执行中", waiting_approval: "等待你确认", verifying: "验证中", finalizing: "收尾中", partial_completed: "部分完成", completed: "已完成", failed: "失败", cancelled: "已取消", CREATED: "已创建", ROUTING_SUPERVISOR: "准备中", DESIGNING: "设计中", PLANNING: "规划中", EXECUTING: "执行中", COLLECTING_EVIDENCE: "收集证据", FAST_DECISION: "决策中", REVIEWING: "审查中", RETASKING: "返工中", WAITING_EXTERNAL_SUPERVISOR: "等待外部审查", WAITING_OWNER: "等待你处理", ACCEPTED: "已接受", DONE: "已完成", BLOCKED: "已阻塞", FAILED: "失败", CANCELLED: "已取消" };
		return labels[status] ?? status;
	}

	function openArtifact(artifact: SearchArtifact): void {
		onClose();
		void goto(
			`/workbench/${encodeURIComponent(artifact.taskId)}?artifact=${encodeURIComponent(artifact.name)}`,
		);
	}

	function insertFile(path: string): void {
		window.dispatchEvent(new CustomEvent("veya:insert-chat-text", { detail: `@${path} ` }));
		onOpenChat();
		onClose();
	}

	function openProject(project: SearchProject): void {
		window.dispatchEvent(
			new CustomEvent("veya:insert-chat-text", {
				detail: `关于项目「${project.name}」(${project.id})：`,
			}),
		);
		onOpenChat();
		onClose();
	}

	function openShortcut(view: string): void {
		onSelectView(view);
		onClose();
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key === "Escape") {
			onClose();
			return;
		}
		if (event.key !== "Tab" || !dialogEl) return;

		const focusable = Array.from(
			dialogEl.querySelectorAll<HTMLElement>(
				'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
			),
		).filter((element) => element.getClientRects().length > 0);
		if (focusable.length === 0) return;

		const active = document.activeElement as HTMLElement | null;
		const index = active ? focusable.indexOf(active) : -1;
		const nextIndex = event.shiftKey
			? index <= 0
				? focusable.length - 1
				: index - 1
			: index < 0 || index >= focusable.length - 1
				? 0
				: index + 1;
		event.preventDefault();
		focusable[nextIndex]?.focus();
	}
</script>

<svelte:window onkeydown={(event) => { if (open) handleKeydown(event); }} />

{#if open}
	<div class="fixed inset-0 z-[80] flex items-start justify-center px-4 pt-[8vh] sm:pt-[12vh]">
		<button
			type="button"
			class="absolute inset-0 bg-black/70 backdrop-blur-sm"
			aria-label="关闭搜索"
			onclick={onClose}
		></button>

		<div
			class="relative flex max-h-[78vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-white/10 bg-[#101010] shadow-2xl"
			bind:this={dialogEl}
			role="dialog"
			tabindex="-1"
			aria-modal="true"
			aria-label="搜索 Veya"
		>
			<div class="flex items-center gap-3 border-b border-white/10 px-4 py-3">
				<Search class="size-5 shrink-0 text-white/50" />
				<input
					bind:this={inputEl}
					bind:value={query}
					class="min-h-11 min-w-0 flex-1 bg-transparent text-[15px] text-terminal-fg outline-none placeholder:text-white/30"
					placeholder="搜索对话、Work、项目、文件和产物…"
					aria-label="搜索 Veya"
				/>
				<kbd class="hidden rounded-md border border-white/10 px-1.5 py-0.5 text-[11px] text-white/35 sm:inline">Esc</kbd>
				<button
					type="button"
					class="inline-flex size-11 items-center justify-center rounded-lg text-white/40 hover:bg-white/5 hover:text-white/80 sm:hidden"
					aria-label="关闭搜索"
					onclick={onClose}
				>
					<X class="size-4" />
				</button>
			</div>

			<div class="min-h-0 flex-1 overflow-y-auto p-2">
				{#if matchedSessions.length > 0}
					<div class="px-2 pb-1 pt-2 text-xs font-medium text-white/40">对话</div>
					{#each matchedSessions as session (session.sid)}
						<button type="button" class="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]" onclick={() => openSession(session.sid)}>
							<MessageSquare class="size-4 shrink-0 text-sky-300" />
							<span class="min-w-0 flex-1 truncate text-sm text-terminal-fg">{session.title}</span>
							<ArrowRight class="size-3.5 shrink-0 text-white/25" />
						</button>
					{/each}
				{/if}

				{#if matchedTasks.length > 0 || matchedMissions.length > 0}
					<div class="px-2 pb-1 pt-4 text-xs font-medium text-white/40">Work</div>
					{#each matchedTasks as task (task.id)}
						<button type="button" class="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]" onclick={() => openTask(task.id)}>
							<SquareCheckBig class="size-4 shrink-0 text-violet-300" />
							<span class="min-w-0 flex-1">
								<span class="block truncate text-sm text-terminal-fg">{task.title}</span>
								<span class="block truncate text-xs text-terminal-dim">{task.objective}</span>
							</span>
							<span class="shrink-0 text-xs text-terminal-dim">{workStatusLabel(task.status)}</span>
						</button>
					{/each}
					{#each matchedMissions as mission (mission.mission_id)}
						<button type="button" class="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]" onclick={() => openMission(mission.mission_id)}>
							<SquareCheckBig class="size-4 shrink-0 text-violet-300" />
							<span class="min-w-0 flex-1">
								<span class="block overflow-hidden text-ellipsis whitespace-nowrap text-sm text-terminal-fg">{mission.goal}</span>
								<span class="block text-xs text-terminal-dim">受监督的长期 Work</span>
							</span>
							<span class="shrink-0 text-xs text-terminal-dim">{workStatusLabel(mission.status)}</span>
						</button>
					{/each}
				{/if}

				{#if matchedProjects.length > 0}
					<div class="px-2 pb-1 pt-4 text-xs font-medium text-white/40">Projects</div>
					{#each matchedProjects as project (project.id)}
						<button type="button" class="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]" onclick={() => openProject(project)}>
							<Boxes class="size-4 shrink-0 text-emerald-300" />
							<span class="min-w-0 flex-1">
								<span class="block overflow-hidden text-ellipsis whitespace-nowrap text-sm text-terminal-fg">{project.name}</span>
								<span class="block overflow-hidden text-ellipsis whitespace-nowrap text-xs text-terminal-dim">{project.id}</span>
							</span>
							<span class="shrink-0 text-xs text-terminal-dim">在 Chat 中使用</span>
						</button>
					{/each}
				{/if}

				{#if matchedFiles.length > 0}
					<div class="px-2 pb-1 pt-4 text-xs font-medium text-white/40">Files</div>
					{#each matchedFiles as file (file.path)}
						<button type="button" class="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]" onclick={() => insertFile(file.path)}>
							<FileText class="size-4 shrink-0 text-amber-200" />
							<span class="min-w-0 flex-1">
								<span class="block truncate text-sm text-terminal-fg">{file.name}</span>
								<span class="block truncate text-xs text-terminal-dim">{file.path}</span>
							</span>
							<span class="shrink-0 text-[11px] text-terminal-dim">加入 Chat</span>
						</button>
					{/each}
				{/if}

				{#if matchedArtifacts.length > 0}
					<div class="px-2 pb-1 pt-4 text-xs font-medium text-white/40">Artifacts</div>
					{#each matchedArtifacts as artifact (`${artifact.taskId}:${artifact.name}`)}
						<button type="button" disabled={!artifact.available} class="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06] disabled:opacity-40" onclick={() => openArtifact(artifact)}>
							<PackageCheck class="size-4 shrink-0 text-cyan-300" />
							<span class="min-w-0 flex-1">
								<span class="block truncate text-sm text-terminal-fg">{artifact.name}</span>
								<span class="block truncate text-xs text-terminal-dim">{artifact.taskTitle}</span>
							</span>
							<ArrowRight class="size-3.5 shrink-0 text-white/25" />
						</button>
					{/each}
				{/if}

				{#if matchedShortcuts.length > 0}
					<div class="px-2 pb-1 pt-4 text-xs font-medium text-white/40">功能</div>
					{#each matchedShortcuts as item (item.view)}
						<button type="button" class="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]" onclick={() => openShortcut(item.view)}>
							<LayoutGrid class="size-4 shrink-0 text-white/45" />
							<span class="min-w-0 flex-1">
								<span class="block text-sm text-terminal-fg">{item.label}</span>
								<span class="block truncate text-xs text-terminal-dim">{item.hint}</span>
							</span>
						</button>
					{/each}
				{/if}

				{#if loading}
					<div class="px-4 py-5 text-center text-sm text-terminal-dim">正在建立搜索索引…</div>
				{:else if normalizedQuery && resultCount === 0}
					<div class="px-4 py-10 text-center text-sm text-terminal-dim">没有找到匹配结果。</div>
				{/if}
			</div>
		</div>
	</div>
{/if}
