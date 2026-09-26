<script lang="ts">
	import { goto } from "$app/navigation";
	import {
		ArrowRight,
		CircleAlert,
		FolderOpen,
		RefreshCw,
		RotateCcw,
		Square,
		SquareCheckBig,
	} from "lucide-svelte";
	import { api, type ApiResult } from "$lib/api";
	import { cancelMission, listMissions } from "$lib/supervision/api";
	import { relativeTime, statusLabel as missionStatusLabel, statusTone } from "$lib/supervision/format";
	import type { Mission } from "$lib/supervision/types";

	type Task = {
		id: string;
		session_id: string;
		title: string;
		objective: string;
		status: string;
		workspace_id: string | null;
		created_at: string;
		updated_at: string;
		started_at: string | null;
		completed_at: string | null;
		current_step: string | null;
		progress: number | null;
		cost_usd: number | null;
		trace_id: string | null;
		latest_checkpoint_id?: string | null;
	};

	type Filter = "all" | "active" | "attention" | "done" | "failed";

	type WorkItem = {
		key: string;
		kind: "task" | "mission";
		id: string;
		title: string;
		objective: string;
		status: string;
		statusLabel: string;
		tone: string;
		workspace: string;
		updatedMs: number;
		updatedLabel: string;
		progress: number | null;
		currentStep: string;
		href: string;
		cost: number | null;
		checkpoint: string | null;
		trace: string | null;
	};

	const TASK_STATUS_LABEL: Record<string, string> = {
		pending: "待处理",
		running: "执行中",
		waiting_approval: "等待你确认",
		verifying: "验证中",
		finalizing: "收尾中",
		completed: "已完成",
		partial_completed: "部分完成",
		failed: "失败",
		cancelled: "已取消",
	};

	let tasks = $state<Task[]>([]);
	let missions = $state<Mission[]>([]);
	let workspaceFilter = $state("");
	let filter = $state<Filter>("all");
	let error = $state("");
	let busy = $state(false);
	let actionBusy = $state("");
	let displayLimit = $state(30);
	let developerOpen = $state(false);

	function taskTone(status: string): string {
		if (status === "completed") return "ok";
		if (status === "failed") return "bad";
		if (status === "waiting_approval") return "attention";
		if (["running", "verifying", "finalizing", "pending"].includes(status)) return "progress";
		return "neutral";
	}

	function toneClass(tone: string): string {
		if (tone === "ok") return "border-emerald-500/20 bg-emerald-500/[0.07] text-emerald-300";
		if (tone === "bad") return "border-rose-500/20 bg-rose-500/[0.07] text-rose-300";
		if (tone === "attention" || tone === "waiting") return "border-amber-500/20 bg-amber-500/[0.07] text-amber-300";
		if (tone === "progress") return "border-sky-500/20 bg-sky-500/[0.07] text-sky-300";
		return "border-white/10 bg-white/[0.04] text-terminal-dim";
	}

	function taskUpdatedMs(value: string): number {
		const n = Date.parse(value);
		return Number.isFinite(n) ? n : 0;
	}

	function taskUpdatedLabel(value: string): string {
		const n = taskUpdatedMs(value);
		if (!n) return "—";
		const delta = Math.max(0, Date.now() - n);
		const mins = Math.floor(delta / 60000);
		if (mins < 1) return "刚刚";
		if (mins < 60) return `${mins} 分钟前`;
		const hours = Math.floor(mins / 60);
		if (hours < 24) return `${hours} 小时前`;
		return `${Math.floor(hours / 24)} 天前`;
	}

	function missionExecutor(mission: Mission): string {
		const execution = mission.policies?.execution_policy ?? {};
		return String(execution.assignee_hint ?? "");
	}

	const items = $derived.by<WorkItem[]>(() => {
		const taskItems: WorkItem[] = tasks.map((task) => ({
			key: `task:${task.id}`,
			kind: "task",
			id: task.id,
			title: task.title,
			objective: task.objective,
			status: task.status,
			statusLabel: TASK_STATUS_LABEL[task.status] ?? task.status,
			tone: taskTone(task.status),
			workspace: task.workspace_id ?? "",
			updatedMs: taskUpdatedMs(task.updated_at),
			updatedLabel: taskUpdatedLabel(task.updated_at),
			progress: task.progress,
			currentStep: task.current_step ?? "",
			href: `/workbench/${encodeURIComponent(task.id)}`,
			cost: task.cost_usd,
			checkpoint: task.latest_checkpoint_id ?? null,
			trace: task.trace_id,
		}));
		const missionItems: WorkItem[] = missions.map((mission) => ({
			key: `mission:${mission.mission_id}`,
			kind: "mission",
			id: mission.mission_id,
			title: mission.goal,
			objective:
				mission.status === "WAITING_EXTERNAL_SUPERVISOR"
					? "本轮已结束，正在等待外部审查。"
					: mission.status === "WAITING_OWNER"
						? "这项工作正在等待你的输入。"
						: "受监督的长期 Work",
			status: mission.status,
			statusLabel: missionStatusLabel(mission.status),
			tone: statusTone(mission.status),
			workspace: mission.workspace ?? "",
			updatedMs: Number(mission.updated_at ?? 0) * 1000,
			updatedLabel: relativeTime(mission.updated_at),
			progress: null,
			currentStep: missionExecutor(mission) ? `执行：${missionExecutor(mission)}` : "",
			href: `/missions/${encodeURIComponent(mission.mission_id)}`,
			cost: null,
			checkpoint: null,
			trace: String(mission.authority?.execution_id ?? "") || null,
		}));
		return [...taskItems, ...missionItems].sort((a, b) => b.updatedMs - a.updatedMs);
	});

	const workspaces = $derived.by(() => {
		return [...new Set(items.map((item) => item.workspace).filter(Boolean))].sort();
	});

	function matchesFilter(item: WorkItem): boolean {
		if (workspaceFilter && item.workspace !== workspaceFilter) return false;
		if (filter === "all") return true;
		if (filter === "active") {
			return ["pending", "running", "verifying", "finalizing", "CREATED", "DESIGNING", "PLANNING", "EXECUTING", "COLLECTING_EVIDENCE", "FAST_DECISION", "REVIEWING", "RETASKING"].includes(item.status);
		}
		if (filter === "attention") return ["waiting_approval", "WAITING_OWNER", "WAITING_EXTERNAL_SUPERVISOR"].includes(item.status);
		if (filter === "done") return ["completed", "ACCEPTED", "DONE"].includes(item.status);
		if (filter === "failed") return ["failed", "cancelled", "FAILED", "CANCELLED", "BLOCKED", "partial_completed"].includes(item.status);
		return true;
	}

	const visibleItems = $derived(items.filter(matchesFilter));
	const renderedItems = $derived(visibleItems.slice(0, displayLimit));

	async function fetchWork(): Promise<void> {
		busy = true;
		error = "";
		const [taskResult, missionResult] = await Promise.all([
			api("gateway", "api/v1/tasks", { method: "GET", query: { limit: 200 } }) as Promise<ApiResult>,
			listMissions(),
		]);
		if (taskResult.ok && taskResult.data && typeof taskResult.data === "object") {
			tasks = (taskResult.data as { tasks?: Task[] }).tasks ?? [];
		} else {
			error = `普通 Work 加载失败 (HTTP ${taskResult.status})`;
		}
		if (missionResult.ok && missionResult.data) {
			missions = missionResult.data.missions ?? [];
		} else if (!error && ![401, 403].includes(missionResult.status)) {
			error = "部分 Work 暂时无法加载，请稍后刷新。";
		}
		busy = false;
	}

	async function cancelWork(item: WorkItem): Promise<void> {
		actionBusy = item.key;
		if (item.kind === "task") {
			const result = await api("gateway", `api/v1/tasks/${encodeURIComponent(item.id)}/cancel`, { method: "POST" });
			if (!result.ok) error = `取消失败 (HTTP ${result.status})`;
		} else {
			const result = await cancelMission(item.id);
			if (!result.ok) error = result.error ?? `取消失败 (HTTP ${result.status})`;
		}
		actionBusy = "";
		await fetchWork();
	}

	async function resumeTask(item: WorkItem): Promise<void> {
		if (item.kind !== "task") return;
		actionBusy = item.key;
		const result = await api("gateway", `api/v1/tasks/${encodeURIComponent(item.id)}/resume`, { method: "POST" });
		if (!result.ok) error = `恢复失败 (HTTP ${result.status})`;
		actionBusy = "";
		await fetchWork();
	}

	function canCancel(item: WorkItem): boolean {
		return ["pending", "running", "waiting_approval", "verifying", "finalizing", "CREATED", "DESIGNING", "PLANNING", "EXECUTING", "COLLECTING_EVIDENCE", "FAST_DECISION", "REVIEWING", "RETASKING", "WAITING_EXTERNAL_SUPERVISOR", "WAITING_OWNER"].includes(item.status);
	}

	function canResume(item: WorkItem): boolean {
		return item.kind === "task" && ["failed", "cancelled", "partial_completed"].includes(item.status);
	}

	$effect(() => {
		void fetchWork();
	});
</script>

<div class="flex h-full flex-col overflow-hidden">
	<header class="flex flex-wrap items-center gap-3 border-b border-white/[0.06] px-4 py-4 md:px-6">
		<div class="min-w-0 flex-1">
			<div class="flex items-center gap-2">
				<SquareCheckBig class="size-4 text-violet-300" />
				<h2 class="text-base font-semibold text-terminal-fg">Work 历史</h2>
				<span class="text-xs text-terminal-dim">{visibleItems.length} 项</span>
			</div>
			<p class="mt-1 text-xs text-terminal-dim">普通任务和受监督的长期工作统一显示在这里。</p>
		</div>

		{#if workspaces.length > 0}
			<label class="flex min-h-11 items-center gap-2 rounded-lg border border-terminal-edge px-3 text-xs text-terminal-dim">
				<FolderOpen class="size-3.5" />
				<select aria-label="工作区" class="max-w-40 bg-transparent text-terminal-fg outline-none" bind:value={workspaceFilter}>
					<option value="">全部工作区</option>
					{#each workspaces as workspace (workspace)}
						<option value={workspace}>{workspace}</option>
					{/each}
				</select>
			</label>
		{/if}

		<button
			type="button"
			class="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-terminal-edge px-3 text-sm text-terminal-dim hover:text-terminal-fg disabled:opacity-50"
			onclick={() => void fetchWork()}
			disabled={busy}
		>
			<RefreshCw class="size-4 {busy ? 'animate-spin' : ''}" /> 刷新
		</button>
		<button
			type="button"
			class="inline-flex min-h-11 items-center rounded-lg border border-terminal-edge px-3 text-sm text-terminal-dim hover:text-terminal-fg"
			onclick={() => (developerOpen = !developerOpen)}
		>
			{developerOpen ? "隐藏开发者信息" : "开发者信息"}
		</button>
	</header>

	<div class="flex gap-1 overflow-x-auto border-b border-white/[0.05] px-4 py-2 md:px-6">
		{#each [
			["all", "全部"],
			["active", "进行中"],
			["attention", "需要处理"],
			["done", "已完成"],
			["failed", "异常 / 已停止"],
		] as option (option[0])}
			<button
				type="button"
				class="min-h-11 shrink-0 rounded-full px-3 text-xs transition {filter === option[0] ? 'bg-white/10 text-white' : 'text-terminal-dim hover:bg-white/[0.05] hover:text-terminal-fg'}"
				onclick={() => { filter = option[0] as Filter; displayLimit = 30; }}
			>
				{option[1]}
			</button>
		{/each}
	</div>

	{#if error}
		<div class="mx-4 mt-3 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-300 md:mx-6">
			{error}
		</div>
	{/if}

	<div class="min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
		{#if busy && items.length === 0}
			<div class="py-16 text-center text-sm text-terminal-dim">正在读取 Work…</div>
		{:else if visibleItems.length === 0}
			<div class="flex flex-col items-center gap-2 py-16 text-center text-terminal-dim">
				<SquareCheckBig class="size-8 opacity-50" />
				<p class="text-sm">当前筛选条件下没有 Work。</p>
			</div>
		{:else}
			<div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
				{#each renderedItems as item (item.key)}
					<article class="flex min-h-52 min-w-0 flex-col rounded-2xl border border-white/[0.07] bg-white/[0.018] p-4 transition hover:border-white/[0.13] hover:bg-white/[0.025]">
						<div class="flex items-start gap-3">
							<div class="min-w-0 flex-1">
								<div class="flex flex-wrap items-center gap-2">
									<span class="rounded-full border px-2 py-0.5 text-[12px] {toneClass(item.tone)}">{item.statusLabel}</span>
									{#if ["waiting_approval", "WAITING_OWNER", "WAITING_EXTERNAL_SUPERVISOR"].includes(item.status)}
										<span class="inline-flex items-center gap-1 text-[12px] text-amber-300"><CircleAlert class="size-3.5" />需要你处理</span>
									{/if}
								</div>
								<h3 class="mt-3 line-clamp-2 [overflow-wrap:anywhere] text-[15px] font-medium leading-5 text-terminal-fg">{item.title}</h3>
								<p class="mt-1 line-clamp-2 [overflow-wrap:anywhere] text-sm leading-5 text-terminal-dim">{item.objective}</p>
							</div>
						</div>

						{#if item.progress !== null && item.status === "running"}
							<div class="mt-4">
								<div class="mb-1.5 flex items-center justify-between text-xs text-terminal-dim">
									<span class="truncate">{item.currentStep || "正在执行"}</span>
									<span>{Math.round(item.progress * 100)}%</span>
								</div>
								<div class="h-1.5 overflow-hidden rounded-full bg-white/10">
									<div class="h-full rounded-full bg-sky-400" style="width: {Math.round(item.progress * 100)}%"></div>
								</div>
							</div>
						{:else if item.currentStep}
							<p class="mt-3 truncate text-xs text-terminal-dim">{item.currentStep}</p>
						{/if}

						<div class="mt-auto pt-4">
							<div class="flex items-center gap-2 text-xs text-terminal-dim">
								<span>{item.updatedLabel}</span>
								{#if item.workspace}<span class="min-w-0 flex-1 truncate text-right">{item.workspace}</span>{/if}
							</div>

							<div class="mt-3 flex items-center gap-2">
								<button
									type="button"
									class="inline-flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-lg bg-white/10 px-3 text-sm text-terminal-fg hover:bg-white/15"
									onclick={() => void goto(item.href)}
								>
									打开 <ArrowRight class="size-3.5" />
								</button>
								{#if canResume(item)}
									<button
										type="button"
										class="inline-flex size-11 items-center justify-center rounded-lg border border-sky-500/30 text-sky-300 hover:bg-sky-500/10 disabled:opacity-40"
										title="继续 Work"
										aria-label="继续 Work"
										disabled={actionBusy !== ""}
										onclick={() => void resumeTask(item)}
									>
										<RotateCcw class="size-4" />
									</button>
								{/if}
								{#if canCancel(item)}
									<button
										type="button"
										class="inline-flex size-11 items-center justify-center rounded-lg border border-rose-500/25 text-rose-300 hover:bg-rose-500/10 disabled:opacity-40"
										title="取消 Work"
										aria-label="取消 Work"
										disabled={actionBusy !== ""}
										onclick={() => void cancelWork(item)}
									>
										<Square class="size-4" />
									</button>
								{/if}
							</div>

							{#if developerOpen}
								<div class="mt-2 space-y-1 rounded-lg bg-black/15 p-2.5 font-mono text-[11px] text-terminal-dim">
									<div>source: {item.kind}</div>
									<div class="break-all">id: {item.id}</div>
									{#if item.cost !== null}<div>cost: $ {item.cost.toFixed(6)}</div>{/if}
									{#if item.checkpoint}<div class="break-all">checkpoint: {item.checkpoint}</div>{/if}
									{#if item.trace}<div class="break-all">trace: {item.trace}</div>{/if}
								</div>
							{/if}
						</div>
					</article>
				{/each}
			</div>
			{#if visibleItems.length > renderedItems.length}
				<div class="mt-4 flex justify-center">
					<button type="button" class="min-h-11 rounded-lg border border-terminal-edge px-4 text-sm text-terminal-dim hover:bg-white/[0.04] hover:text-terminal-fg" onclick={() => (displayLimit += 30)}>
						显示更多（还有 {visibleItems.length - renderedItems.length} 项）
					</button>
				</div>
			{/if}
		{/if}
	</div>
</div>
