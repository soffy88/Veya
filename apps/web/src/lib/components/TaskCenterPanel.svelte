<script lang="ts">
	/**
	 * TaskCenterPanel — P1-03 Web Task Center (docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §6)。
	 *
	 * 视图: 任务列表 + 任务详情 (状态、事件、成本、checkpoint)。
	 * 操作: 刷新 / workspace 过滤 / 取消 / 从 durable session 恢复。
	 * API: GET /api/v1/tasks, POST /api/v1/tasks/{id}/cancel (gateway upstream)。
	 *
	 * A-04 约束: 任务状态仍是投影；取消/恢复只调用后端事实入口，
	 * 本面板不自行决定执行器或推进状态。
	 */
	import { RefreshCw, XCircle, ListTodo, FolderOpen, Eye, Play, X } from "lucide-svelte";
	import { api, API_BASE, type ApiResult } from "$lib/api";

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
		acceptance?: Array<Record<string, unknown>>;
	};

	type TaskEvent = {
		event_id?: string;
		topic?: string;
		ts?: number;
		payload?: Record<string, unknown>;
	};

	const STATUS_LABEL: Record<string, string> = {
		pending: "待处理",
		running: "执行中",
		waiting_approval: "等待审批",
		completed: "已完成",
		failed: "失败",
		cancelled: "已取消",
	};

	const STATUS_STYLE: Record<string, string> = {
		pending: "bg-white/10 text-terminal-dim",
		running: "bg-sky-500/15 text-sky-400",
		waiting_approval: "bg-amber-500/15 text-amber-400",
		completed: "bg-emerald-500/15 text-emerald-400",
		failed: "bg-rose-500/15 text-rose-400",
		cancelled: "bg-white/5 text-terminal-dim/70",
	};

	let tasks = $state<Task[]>([]);
	let workspaces = $state<string[]>([]);
	let workspaceFilter = $state("");
	let statusFilter = $state("");
	let error = $state("");
	let busy = $state(false);
	let selectedTask = $state<Task | null>(null);
	let selectedEvents = $state<TaskEvent[]>([]);
	let detailBusy = $state(false);

	async function fetchTasks(): Promise<void> {
		busy = true;
		error = "";
		const r: ApiResult = await api("gateway", "api/v1/tasks", {
			method: "GET",
			query: {
				workspace: workspaceFilter || undefined,
				status: statusFilter || undefined,
				limit: 200,
			},
		});
		busy = false;
		if (r.ok && r.data && typeof r.data === "object") {
			const data = r.data as { tasks?: Task[] };
			tasks = data.tasks ?? [];
			const ws = new Set<string>();
			for (const t of tasks) if (t.workspace_id) ws.add(t.workspace_id);
			workspaces = [...ws].sort();
		} else {
			error = `任务列表加载失败 (${r.status})`;
		}
	}

	async function cancelTask(id: string): Promise<void> {
		const r = await api("gateway", `api/v1/tasks/${encodeURIComponent(id)}/cancel`, {
			method: "POST",
		});
		if (r.ok) {
			await fetchTasks();
		} else {
			error = `取消失败 (${r.status})`;
		}
	}

	async function resumeTask(id: string): Promise<void> {
		const r = await api("gateway", "api/v1/tasks/" + encodeURIComponent(id) + "/resume", {
			method: "POST",
		});
		if (r.ok) {
			await fetchTasks();
			const refreshed = tasks.find((task) => task.id === id);
			if (selectedTask?.id === id) await openTask(refreshed ?? selectedTask);
		} else {
			error = "恢复失败 (" + r.status + ")";
		}
	}

	async function openTask(task: Task): Promise<void> {
		selectedTask = task;
		selectedEvents = [];
		detailBusy = true;
		const [detail, events] = await Promise.all([
			api("gateway", "api/v1/tasks/" + encodeURIComponent(task.id), { method: "GET" }),
			api("gateway", "api/v1/tasks/" + encodeURIComponent(task.id) + "/events", { method: "GET" }),
		]);
		detailBusy = false;
		if (detail.ok && detail.data && typeof detail.data === "object") {
			selectedTask = (detail.data as { task?: Task }).task ?? task;
		}
		if (events.ok && events.data && typeof events.data === "object") {
			selectedEvents = (events.data as { events?: TaskEvent[] }).events ?? [];
		}
	}

	function fmtCost(value: number | null | undefined): string {
		return typeof value === "number" ? "$" + value.toFixed(6) : "—";
	}

	function fmtTime(ts: string | null | undefined): string {
		if (!ts) return "—";
		const d = new Date(ts);
		return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
	}

	function truncate(s: string, n: number): string {
		return s.length > n ? s.slice(0, n) + "…" : s;
	}

	// 初始加载
	$effect(() => {
		void fetchTasks();
	});
</script>

<div class="flex h-full flex-col gap-4 p-6">
	<!-- ── 头部: 标题 + 过滤 + 刷新 ────────────────────────────── -->
	<div class="flex flex-wrap items-center gap-3">
		<div class="flex items-center gap-2">
			<ListTodo class="size-4 text-sky-400" />
			<h2 class="text-sm font-semibold text-terminal-fg">任务中心</h2>
			<span class="font-mono text-[11px] text-terminal-dim/70">{tasks.length} 项</span>
		</div>
		<span class="flex-1"></span>

		{#if workspaces.length > 0}
			<label class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-2.5 py-1.5 text-xs text-terminal-dim">
				<FolderOpen class="size-3.5" />
				<select
					class="bg-transparent text-terminal-fg outline-none"
					bind:value={workspaceFilter}
					onchange={() => void fetchTasks()}
				>
					<option value="">全部工作区</option>
					{#each workspaces as ws (ws)}
						<option value={ws}>{ws}</option>
					{/each}
				</select>
			</label>
		{/if}

		<label class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-2.5 py-1.5 text-xs text-terminal-dim">
			<select
				class="bg-transparent text-terminal-fg outline-none"
				bind:value={statusFilter}
				onchange={() => void fetchTasks()}
			>
				<option value="">全部状态</option>
				{#each Object.entries(STATUS_LABEL) as [k, v] (k)}
					<option value={k}>{v}</option>
				{/each}
			</select>
		</label>

		<button
			type="button"
			onclick={() => void fetchTasks()}
			class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-3 py-1.5 text-sm text-terminal-dim transition hover:border-sky-500/40 hover:text-terminal-fg disabled:opacity-50"
			disabled={busy}
		>
			<RefreshCw class="size-4 {busy ? 'animate-spin' : ''}" />
			刷新
		</button>
	</div>

	{#if error}
		<div class="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 font-mono text-xs text-rose-400">{error}</div>
	{/if}

	<!-- ── 任务列表 ─────────────────────────────────────────────── -->
	<div class="min-h-0 flex-1 overflow-y-auto rounded-lg border border-terminal-edge">
		{#if tasks.length === 0}
			<div class="flex flex-col items-center gap-2 py-16 text-terminal-dim/60">
				<ListTodo class="size-8" />
				<p class="font-mono text-xs">暂无任务 — 发起对话后自动登记</p>
			</div>
		{:else}
			<table class="w-full text-left text-sm">
				<thead class="sticky top-0 border-b border-terminal-edge bg-[#0d0d0d]">
					<tr class="font-mono text-[10px] uppercase tracking-wider text-terminal-dim/70">
						<th class="px-4 py-2.5">状态</th>
						<th class="px-4 py-2.5">任务</th>
						<th class="hidden px-4 py-2.5 md:table-cell">工作区</th>
						<th class="hidden px-4 py-2.5 md:table-cell">开始</th>
						<th class="hidden px-4 py-2.5 lg:table-cell">更新</th>
						<th class="px-4 py-2.5 text-right">操作</th>
					</tr>
				</thead>
				<tbody>
					{#each tasks as t (t.id)}
						<tr class="border-b border-white/5 transition hover:bg-white/[0.03]">
							<td class="px-4 py-3">
								<span class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 font-mono text-[11px] {STATUS_STYLE[t.status] ?? 'bg-white/10 text-terminal-dim'}">
									<span class="size-1.5 rounded-full bg-current"></span>
									{STATUS_LABEL[t.status] ?? t.status}
								</span>
							</td>
							<td class="max-w-xs px-4 py-3">
								<button
									type="button"
									class="flex max-w-full items-center gap-1 truncate text-left text-[13px] text-terminal-fg hover:text-sky-300"
									title="查看任务详情"
									onclick={() => void openTask(t)}
								>
									<span class="truncate">{t.title}</span>
									<Eye class="size-3 shrink-0 text-terminal-dim" />
								</button>
								<p class="truncate font-mono text-[11px] text-terminal-dim/60" title={t.objective}>
									{truncate(t.objective, 60)}
								</p>
								{#if t.progress !== null && t.progress !== undefined && t.status === "running"}
									<div class="mt-1.5 h-1 w-28 overflow-hidden rounded-full bg-white/10">
										<div class="h-full rounded-full bg-sky-500" style="width: {Math.round(t.progress * 100)}%"></div>
									</div>
								{/if}
							</td>
							<td class="hidden px-4 py-3 font-mono text-xs text-terminal-dim md:table-cell">{t.workspace_id ?? "—"}</td>
							<td class="hidden px-4 py-3 font-mono text-xs text-terminal-dim md:table-cell">{fmtTime(t.started_at)}</td>
							<td class="hidden px-4 py-3 font-mono text-xs text-terminal-dim lg:table-cell">{fmtTime(t.updated_at)}</td>
							<td class="px-4 py-3 text-right">
								{#if t.status === "failed" || t.status === "cancelled"}
									<button
										type="button"
										title="从 durable session 恢复"
										onclick={() => void resumeTask(t.id)}
										class="rounded-md p-1.5 text-terminal-dim transition hover:bg-sky-500/20 hover:text-sky-400"
									>
										<Play class="size-4" />
									</button>
								{/if}
								{#if t.status === "running" || t.status === "pending" || t.status === "waiting_approval"}
									<button
										type="button"
										title="取消任务"
										onclick={() => void cancelTask(t.id)}
										class="rounded-md p-1.5 text-terminal-dim transition hover:bg-rose-500/20 hover:text-rose-400"
									>
										<XCircle class="size-4" />
									</button>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>

	{#if selectedTask}
		<section class="max-h-80 overflow-y-auto rounded-lg border border-sky-500/25 bg-sky-500/[0.03] p-4">
			<div class="flex items-start gap-3">
				<div class="min-w-0 flex-1">
					<div class="flex flex-wrap items-center gap-2">
						<h3 class="truncate text-sm font-semibold text-terminal-fg">{selectedTask.title}</h3>
						<span class="rounded-full px-2 py-0.5 font-mono text-[10px] {STATUS_STYLE[selectedTask.status] ?? 'bg-white/10 text-terminal-dim'}">
							{STATUS_LABEL[selectedTask.status] ?? selectedTask.status}
						</span>
					</div>
					<p class="mt-1 text-xs text-terminal-dim">{selectedTask.objective}</p>
					<div class="mt-3 grid grid-cols-2 gap-2 font-mono text-[10px] text-terminal-dim md:grid-cols-4">
						<span>成本 {fmtCost(selectedTask.cost_usd)}</span>
						<span>checkpoint {selectedTask.latest_checkpoint_id ?? "—"}</span>
						<span>trace {selectedTask.trace_id ?? "—"}</span>
						<span>事件 {selectedEvents.length}</span>
					</div>
					<a href={`/workbench/${encodeURIComponent(selectedTask.id)}`} class="mt-3 inline-flex items-center rounded-md border border-sky-500/30 px-2.5 py-1.5 text-xs text-sky-300 hover:bg-sky-500/10">打开统一 Workbench</a>
				</div>
				<button type="button" title="关闭详情" onclick={() => (selectedTask = null)} class="rounded-md p-1 text-terminal-dim hover:text-terminal-fg">
					<X class="size-4" />
				</button>
			</div>
			{#if detailBusy}
				<p class="mt-3 font-mono text-[10px] text-terminal-dim">加载任务事实…</p>
			{:else if selectedEvents.length > 0}
				<div class="mt-3 space-y-1 border-t border-white/5 pt-3">
					{#each selectedEvents.slice(-20).reverse() as event, index (event.event_id ?? (event.topic ?? "event") + "-" + String(event.ts ?? index))}
						<div class="flex items-center gap-2 font-mono text-[10px] text-terminal-dim">
							<span class="text-sky-300">{event.topic ?? "event"}</span>
							<span class="truncate opacity-70">{String((event.payload ?? {}).tool_name ?? (event.payload ?? {}).status ?? "")}</span>
						</div>
					{/each}
				</div>
			{:else}
				<p class="mt-3 font-mono text-[10px] text-terminal-dim">暂无事件。</p>
			{/if}
		</section>
	{/if}

	<p class="font-mono text-[10px] text-terminal-dim/50">
		任务状态为执行投影 (A-04) — 仅供查看, 不参与执行控制。数据源: GET {API_BASE}/api/v1/tasks
	</p>
</div>
