<script lang="ts">
	import { tick } from "svelte";
	import { goto } from "$app/navigation";
	import { Search, MessageSquare, SquareCheckBig, LayoutGrid, ArrowRight, X } from "lucide-svelte";
	import { api, type ApiResult } from "$lib/api";
	import { sessionStore } from "$lib/sessionStore.svelte";

	type SearchTask = {
		id: string;
		title: string;
		objective: string;
		status: string;
		updated_at: string;
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
	let loadingTasks = $state(false);
	let taskLoadAttempted = $state(false);
	let inputEl = $state<HTMLInputElement>();

	const shortcuts = [
		{ label: "Work", hint: "创建或继续长期任务", view: "bot" },
		{ label: "任务", hint: "查看任务历史与恢复状态", view: "tasks" },
		{ label: "自动化", hint: "管理自动化与后台流程", view: "automation" },
		{ label: "Apps", hint: "查看插件与连接能力", view: "plugins" },
		{ label: "个人上下文", hint: "查看 Personal Runtime", view: "personal" },
		{ label: "开发者工具", hint: "Dashboard、Git、图谱与 Genesis", view: "dashboard" },
	] as const;

	async function loadTasks(): Promise<void> {
		if (taskLoadAttempted) return;
		taskLoadAttempted = true;
		loadingTasks = true;
		const result: ApiResult = await api("gateway", "api/v1/tasks", {
			method: "GET",
			query: { limit: 100 },
		});
		loadingTasks = false;
		if (result.ok && result.data && typeof result.data === "object") {
			tasks = ((result.data as { tasks?: SearchTask[] }).tasks ?? []).slice(0, 100);
		}
	}

	$effect(() => {
		if (!open) return;
		void loadTasks();
		void tick().then(() => inputEl?.focus());
	});

	const normalizedQuery = $derived(query.trim().toLocaleLowerCase("zh-CN"));
	const matchedSessions = $derived.by(() => {
		const list = sessionStore.sessions;
		if (!normalizedQuery) return list.slice(0, 8);
		return list
			.filter((session) => session.title.toLocaleLowerCase("zh-CN").includes(normalizedQuery))
			.slice(0, 8);
	});
	const matchedTasks = $derived.by(() => {
		if (!normalizedQuery) return tasks.slice(0, 8);
		return tasks
			.filter((task) =>
				`${task.title} ${task.objective}`.toLocaleLowerCase("zh-CN").includes(normalizedQuery),
			)
			.slice(0, 8);
	});
	const matchedShortcuts = $derived.by(() => {
		if (!normalizedQuery) return shortcuts.slice(0, 5);
		return shortcuts.filter((item) =>
			`${item.label} ${item.hint}`.toLocaleLowerCase("zh-CN").includes(normalizedQuery),
		);
	});

	function openSession(sid: string): void {
		sessionStore.open(sid);
		onOpenChat();
		onClose();
	}

	function openTask(taskId: string): void {
		onClose();
		void goto(`/workbench/${encodeURIComponent(taskId)}`);
	}

	function openShortcut(view: string): void {
		onSelectView(view);
		onClose();
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key === "Escape") onClose();
	}
</script>

<svelte:window onkeydown={(event) => { if (open) handleKeydown(event); }} />

{#if open}
	<div class="fixed inset-0 z-[80] flex items-start justify-center px-4 pt-[10vh] sm:pt-[14vh]">
		<button
			type="button"
			class="absolute inset-0 bg-black/70 backdrop-blur-sm"
			aria-label="关闭搜索"
			onclick={onClose}
		></button>

		<div
			class="relative flex max-h-[72vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-white/10 bg-[#101010] shadow-2xl"
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
					class="min-w-0 flex-1 bg-transparent text-[15px] text-terminal-fg outline-none placeholder:text-white/30"
					placeholder="搜索对话、任务和功能…"
					aria-label="搜索 Veya"
				/>
				<kbd class="hidden rounded-md border border-white/10 px-1.5 py-0.5 font-mono text-[10px] text-white/35 sm:inline">Esc</kbd>
				<button
					type="button"
					class="rounded-md p-1 text-white/40 hover:bg-white/5 hover:text-white/80 sm:hidden"
					aria-label="关闭搜索"
					onclick={onClose}
				>
					<X class="size-4" />
				</button>
			</div>

			<div class="min-h-0 flex-1 overflow-y-auto p-2">
				{#if matchedSessions.length > 0}
					<div class="px-2 pb-1 pt-2 text-[11px] font-medium text-white/40">对话</div>
					{#each matchedSessions as session (session.sid)}
						<button
							type="button"
							class="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]"
							onclick={() => openSession(session.sid)}
						>
							<MessageSquare class="size-4 shrink-0 text-sky-300" />
							<span class="min-w-0 flex-1 truncate text-sm text-terminal-fg">{session.title}</span>
							<ArrowRight class="size-3.5 shrink-0 text-white/25" />
						</button>
					{/each}
				{/if}

				{#if matchedTasks.length > 0 || loadingTasks}
					<div class="px-2 pb-1 pt-4 text-[11px] font-medium text-white/40">工作</div>
					{#if loadingTasks}
						<div class="px-3 py-3 text-xs text-terminal-dim">正在读取任务…</div>
					{:else}
						{#each matchedTasks as task (task.id)}
							<button
								type="button"
								class="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]"
								onclick={() => openTask(task.id)}
							>
								<SquareCheckBig class="size-4 shrink-0 text-violet-300" />
								<span class="min-w-0 flex-1">
									<span class="block truncate text-sm text-terminal-fg">{task.title}</span>
									<span class="block truncate text-xs text-terminal-dim">{task.objective}</span>
								</span>
								<span class="shrink-0 text-[10px] text-terminal-dim">{task.status}</span>
							</button>
						{/each}
					{/if}
				{/if}

				{#if matchedShortcuts.length > 0}
					<div class="px-2 pb-1 pt-4 text-[11px] font-medium text-white/40">功能</div>
					{#each matchedShortcuts as item (item.view)}
						<button
							type="button"
							class="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-white/[0.06]"
							onclick={() => openShortcut(item.view)}
						>
							<LayoutGrid class="size-4 shrink-0 text-white/45" />
							<span class="min-w-0 flex-1">
								<span class="block text-sm text-terminal-fg">{item.label}</span>
								<span class="block truncate text-xs text-terminal-dim">{item.hint}</span>
							</span>
						</button>
					{/each}
				{/if}

				{#if normalizedQuery && matchedSessions.length === 0 && matchedTasks.length === 0 && matchedShortcuts.length === 0 && !loadingTasks}
					<div class="px-4 py-10 text-center text-sm text-terminal-dim">
						没有找到匹配结果。
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}
