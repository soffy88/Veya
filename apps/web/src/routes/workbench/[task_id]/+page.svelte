<script lang="ts">
	import { onMount } from "svelte";
	import { ArrowLeft, Check, ChevronDown, CircleX, Code2, ExternalLink, Eye, Pause, Play, RefreshCw, Shield, UserRound } from "lucide-svelte";
	import { api, formatResult } from "$lib/api";

	type AnyRecord = Record<string, any>;

	let { data: routeData } = $props<{ data: { taskId: string } }>();
	let view = $state<AnyRecord | null>(null);
	let loading = $state(true);
	let busy = $state("");
	let error = $state("");
	let artifact = $state<AnyRecord | null>(null);
	let artifactBusy = $state(false);
	let developerOpen = $state(false);

	const STATUS_LABEL: Record<string, string> = {
		created: "已创建",
		contract_ready: "准备中",
		worktree_ready: "准备中",
		goalrun_created: "准备中",
		pending: "待运行",
		running: "运行中",
		waiting_approval: "等待你确认",
		verifying: "验证中",
		finalizing: "收尾中",
		completed: "已完成",
		failed: "失败",
		cancelled: "已取消",
		partial_completed: "部分完成",
		quarantined: "已隔离",
		not_started: "未开始",
		started: "已开始",
		success: "已完成",
		succeeded: "已完成",
	};

	const taskId = $derived(routeData.taskId);
	const pendingApprovalCount = $derived(view?.approvals?.pending?.length ?? 0);
	const canCancel = $derived(["running", "pending", "waiting_approval", "verifying", "finalizing"].includes(String(view?.state?.status ?? "")));
	const canResume = $derived(["failed", "cancelled", "partial_completed"].includes(String(view?.state?.status ?? "")));

	function scrollToAttention(): void {
		document.getElementById("workbench-attention")?.scrollIntoView({ behavior: "smooth", block: "center" });
	}

	function statusLabel(value: unknown): string {
		const raw = String(value ?? "unknown");
		return STATUS_LABEL[raw] ?? raw;
	}

	function statusClass(value: unknown): string {
		const raw = String(value ?? "");
		if (["completed", "success", "succeeded"].includes(raw)) return "text-emerald-300";
		if (["failed", "quarantined", "cancelled"].includes(raw)) return "text-rose-300";
		if (["waiting_approval", "pending"].includes(raw)) return "text-amber-300";
		return "text-sky-300";
	}

	const INTERNAL_ACTIVITY_TOPICS = new Set([
		"trajectory.recorded",
		"checkpoint.created",
		"tool.requested",
		"tool.started",
		"tool.completed",
		"tool_call",
		"master_round",
	]);

	const activityEntries = $derived.by(() => {
		const rows = Array.isArray(view?.timeline) ? view.timeline : [];
		return rows.filter((event: AnyRecord) => !INTERNAL_ACTIVITY_TOPICS.has(String(event?.topic ?? ""))).slice(-20).reverse();
	});

	function activityLabel(event: AnyRecord): string {
		const message = String(event?.payload?.message ?? "").trim();
		if (message && !/^[a-z0-9_.:/-]+$/i.test(message)) return message;
		const topic = String(event?.topic ?? "");
		const labels: Record<string, string> = {
			"message.assistant_added": "Veya 更新了结果",
			"message.user_added": "收到你的消息",
			"task.updated": "工作状态已更新",
			"task.completed": "工作已完成",
			"task.failed": "工作执行失败",
			"task.cancelled": "工作已取消",
			"master_rounds_exhausted": "本轮执行已结束",
			"completed": "完成一个执行阶段",
			"failed": "执行阶段失败",
			"approval.requested": "需要你的确认",
			"approval.resolved": "确认已处理",
			"artifact.created": "生成了新产物",
			"artifact.verified": "产物已验证",
		};
		const status = String(event?.payload?.status ?? "").trim();
		return labels[topic] ?? (status ? statusLabel(status) : "工作进度已更新");
	}

	function formatTime(value: unknown): string {
		if (!value) return "—";
		const date = new Date(typeof value === "number" ? value * 1000 : String(value));
		return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
	}

	function json(value: unknown): string {
		if (value === undefined || value === null) return "";
		return typeof value === "string" ? value : JSON.stringify(value);
	}

	async function loadWorkbench(): Promise<void> {
		loading = view === null;
		const result = await api("gateway", `api/v1/workbench/${encodeURIComponent(taskId)}`, { method: "GET" });
		if (result.ok && result.data && typeof result.data === "object") {
			view = result.data as AnyRecord;
			error = "";
		} else {
			error = typeof result.data === "string" ? result.data : `Workbench 加载失败 (HTTP ${result.status})`;
		}
		loading = false;
	}

	async function approval(requestId: string, approved: boolean): Promise<void> {
		busy = `approval:${requestId}`;
		const result = await api("gateway", `api/v1/workbench/${encodeURIComponent(taskId)}/approval`, {
			method: "POST",
			body: { request_id: requestId, approved, expected_version: view?.state?.version },
		});
		busy = "";
		if (!result.ok) {
			error = result.status === 409 ? "审批已过期，已刷新当前状态。" : `审批失败 (HTTP ${result.status})`;
			await loadWorkbench();
			return;
		}
		view = result.data as AnyRecord;
	}

	async function browserControl(action: "takeover" | "return_control"): Promise<void> {
		busy = `browser:${action}`;
		const browser = view?.browser ?? {};
		const result = await api("gateway", `api/v1/workbench/${encodeURIComponent(taskId)}/browser/control`, {
			method: "POST",
			body: {
				action,
				browser_session_id: browser.session_id,
				expected_handle_version: browser.version,
			},
		});
		busy = "";
		if (!result.ok) {
			error = result.status === 409 ? "浏览器状态已变化，已刷新当前状态。" : `浏览器控制失败 (HTTP ${result.status})`;
			await loadWorkbench();
			return;
		}
		view = result.data as AnyRecord;
	}

	async function taskControl(action: "cancel" | "resume"): Promise<void> {
		busy = action;
		const result = await api("gateway", `api/v1/workbench/${encodeURIComponent(taskId)}/task`, {
			method: "POST",
			body: { action, expected_version: view?.state?.version },
		});
		busy = "";
		if (!result.ok) error = `任务${action === "cancel" ? "取消" : "恢复"}失败 (HTTP ${result.status})`;
		await loadWorkbench();
	}

	async function openArtifact(name: string): Promise<void> {
		artifactBusy = true;
		const result = await api("gateway", `api/v1/workbench/${encodeURIComponent(taskId)}/artifact/${encodeURIComponent(name)}`, { method: "GET" });
		artifactBusy = false;
		if (result.ok && result.data && typeof result.data === "object") artifact = result.data as AnyRecord;
		else error = `产物读取失败 (HTTP ${result.status})`;
	}

	onMount(() => {
		void (async () => {
			await loadWorkbench();
			const requestedArtifact = new URL(window.location.href).searchParams.get("artifact");
			if (requestedArtifact) await openArtifact(requestedArtifact);
		})();
		const timer = window.setInterval(() => void loadWorkbench(), 3000);
		return () => window.clearInterval(timer);
	});
</script>

<svelte:head>
	<title>{view?.task?.title ?? "Workbench"} · Veya</title>
</svelte:head>

<main class="min-h-dvh overflow-y-auto bg-[#080808] px-3 pb-24 pt-3 text-terminal-fg sm:px-4 sm:pb-24 sm:pt-5 md:px-8 md:pb-6">
	<div class="mx-auto max-w-6xl space-y-4">
		<header class="flex flex-wrap items-start gap-3 border-b border-white/10 pb-4">
			<a href="/" class="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-terminal-edge px-3 text-sm text-terminal-dim hover:text-terminal-fg"><ArrowLeft class="size-3.5" /> 返回</a>
			<div class="min-w-0 flex-1">
				<p class="text-[11px] uppercase tracking-[0.18em] text-violet-300/70">Work</p>
				<h1 class="truncate text-lg font-semibold">{view?.task?.title ?? taskId}</h1>
				<p class="mt-1 truncate text-sm text-terminal-dim">{view?.task?.objective ?? "正在读取任务目标…"}</p>
			</div>
			{#if view}
				<span class="text-xs {statusClass(view.state?.status)}">● {statusLabel(view.state?.status)}</span>
			{/if}
			<button type="button" class="inline-flex size-11 items-center justify-center rounded-lg border border-terminal-edge text-terminal-dim hover:text-terminal-fg disabled:opacity-40" onclick={() => void loadWorkbench()} disabled={loading} title="刷新任务状态"><RefreshCw class="size-4 {loading ? 'animate-spin' : ''}" /></button>
		</header>

		{#if error}<div class="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">{error}</div>{/if}
		{#if loading && !view}<div class="rounded-xl border border-terminal-edge p-8 text-center text-sm text-terminal-dim">正在读取任务状态…</div>
		{:else if view}
			<div class="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(360px,0.65fr)]">
				<section class="order-2 space-y-4 xl:order-1">
					<div class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center gap-2"><UserRound class="size-4 text-sky-400" /><h2 class="text-sm font-semibold">对话</h2></div>
						{#if view.conversation?.length}
							<div class="space-y-3">{#each view.conversation as message (message.event_id)}<article class="rounded-lg border border-white/5 p-3 {message.role === 'user' ? 'bg-sky-400/[0.04]' : 'bg-white/[0.02]'}"><div class="mb-1 flex items-center justify-between text-[11px] text-terminal-dim"><span>{message.role === "user" ? "你" : "Veya"}</span><span>{formatTime(message.ts)}</span></div><p class="whitespace-pre-wrap text-sm leading-6">{message.content}</p></article>{/each}</div>
						{:else}<p class="text-sm text-terminal-dim">当前还没有对话记录。</p>{/if}
					</div>

					<div class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center gap-2"><Eye class="size-4 text-violet-400" /><h2 class="text-sm font-semibold">活动</h2><span class="text-xs text-terminal-dim">最近 {activityEntries.length} 项</span></div>
						{#if activityEntries.length}<div class="max-h-[520px] space-y-1 overflow-y-auto pr-1">{#each activityEntries as event (event.event_id)}<div class="flex items-start gap-3 rounded-lg px-2.5 py-2 hover:bg-white/[0.025]"><span class="mt-2 size-1.5 shrink-0 rounded-full bg-white/30"></span><span class="min-w-0 flex-1"><span class="block text-sm text-terminal-fg">{activityLabel(event)}</span><span class="mt-0.5 block text-xs text-terminal-dim">{formatTime(event.ts)}</span></span></div>{/each}</div>
						{:else}<p class="text-sm text-terminal-dim">任务开始后，关键进展会显示在这里。</p>{/if}
					</div>
				</section>

				<aside class="order-1 space-y-4 xl:order-2">
					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<h2 class="mb-3 text-sm font-semibold">任务控制</h2>
						<div class="flex flex-wrap gap-2">
							{#if ["running", "pending", "waiting_approval", "verifying", "finalizing"].includes(view.state?.status)}<button type="button" class="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-rose-500/30 px-3 py-2 text-xs text-rose-300 hover:bg-rose-500/10 disabled:opacity-40" onclick={() => void taskControl("cancel")} disabled={busy !== ""}><CircleX class="size-3.5" />取消任务</button>{/if}
							{#if ["failed", "cancelled", "partial_completed"].includes(view.state?.status)}<button type="button" class="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-sky-500/30 px-3 py-2 text-xs text-sky-300 hover:bg-sky-500/10 disabled:opacity-40" onclick={() => void taskControl("resume")} disabled={busy !== ""}><Play class="size-3.5" />继续任务</button>{/if}
							{#if !["running", "pending", "waiting_approval", "verifying", "finalizing", "failed", "cancelled", "partial_completed"].includes(view.state?.status)}<span class="text-xs text-terminal-dim">当前无需操作。</span>{/if}
						</div>
					</section>



					{#if view.approvals?.pending?.length}
					<section id="workbench-attention" class="rounded-xl border border-amber-500/25 bg-amber-500/[0.03] p-4">
						<div class="mb-3 flex items-center gap-2"><Shield class="size-4 text-amber-400" /><h2 class="text-sm font-semibold">需要你确认</h2></div>
						{#if view.approvals?.pending?.length}{#each view.approvals.pending as item (item.request_id)}<div class="mb-2 rounded-lg border border-amber-500/20 p-3"><div class="text-xs"><span class="font-medium text-amber-100">{item.tool_name}</span></div><p class="mt-1 text-xs text-terminal-dim">{item.reason}</p><div class="mt-2 flex gap-2"><button type="button" class="inline-flex min-h-11 items-center gap-1 rounded-lg bg-emerald-500/15 px-3 py-2 text-xs text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-40" onclick={() => void approval(item.request_id, true)} disabled={busy !== ""}><Check class="size-3.5" />批准</button><button type="button" class="inline-flex min-h-11 items-center gap-1 rounded-lg bg-rose-500/15 px-3 py-2 text-xs text-rose-300 hover:bg-rose-500/25 disabled:opacity-40" onclick={() => void approval(item.request_id, false)} disabled={busy !== ""}><CircleX class="size-3.5" />拒绝</button></div></div>{/each}{:else}<p class="text-sm text-terminal-dim">当前没有需要你确认的操作。</p>{/if}
					</section>
					{/if}

					{#if view.browser?.session_id}
					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center gap-2"><Eye class="size-4 text-cyan-400" /><h2 class="text-sm font-semibold">浏览器</h2></div>
						<div class="space-y-1 text-xs text-terminal-dim"><p class="overflow-hidden text-ellipsis whitespace-nowrap">{view.browser?.current_url ?? "当前任务没有活动页面"}</p><p>控制权：<span class={view.browser?.control_state === "HUMAN_CONTROL" ? "text-amber-300" : "text-sky-300"}>{view.browser?.control_state === "HUMAN_CONTROL" ? "你" : "Veya"}</span></p></div>
						{#if view.browser?.session_id}<div class="mt-3 flex gap-2">{#if view.browser?.control_state === "HUMAN_CONTROL"}<button type="button" class="min-h-11 rounded-lg border border-sky-500/30 px-3 py-2 text-xs text-sky-300 hover:bg-sky-500/10 disabled:opacity-40" onclick={() => void browserControl("return_control")} disabled={busy !== ""}>交还 Veya</button>{:else}<button type="button" class="min-h-11 rounded-lg border border-amber-500/30 px-3 py-2 text-xs text-amber-300 hover:bg-amber-500/10 disabled:opacity-40" onclick={() => void browserControl("takeover")} disabled={busy !== ""}>接管浏览器</button>{/if}</div>{:else}<p class="mt-3 text-xs text-terminal-dim">当前任务没有浏览器会话。</p>{/if}
						{#if developerOpen && view.browser?.snapshot}<pre class="mt-3 max-h-48 overflow-auto rounded-md bg-black/30 p-2 font-mono text-[10px] text-terminal-dim">{json(view.browser.snapshot)}</pre>{/if}
					</section>
					{/if}



					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center justify-between gap-2"><h2 class="text-sm font-semibold">结果</h2>{#if view.verification?.acceptance_passed === true}<span class="text-xs text-emerald-300">验证通过</span>{:else if view.verification?.acceptance_passed === false}<span class="text-xs text-rose-300">验证未通过</span>{:else if view.state?.status === "completed"}<span class="text-xs text-emerald-300">工作已完成</span>{:else if view.state?.status === "failed"}<span class="text-xs text-rose-300">执行失败</span>{:else}<span class="text-xs text-terminal-dim">等待结果</span>{/if}</div>{#if view.verification?.changed_files?.length}<p class="mb-3 text-xs text-terminal-dim">已变更 {view.verification.changed_files.length} 个文件</p>{/if}{#if view.artifacts?.length}<div class="mt-3 space-y-1">{#each view.artifacts as item (item.name)}<button type="button" class="flex min-h-11 w-full items-center justify-between rounded-lg border border-white/5 px-3 py-2 text-left text-xs text-sky-300 hover:bg-white/5 disabled:opacity-40" onclick={() => void openArtifact(item.name)} disabled={!item.available || artifactBusy}><span>{item.name}</span><ExternalLink class="size-3" /></button>{/each}</div>{:else}<p class="mt-3 text-xs text-terminal-dim">任务产生的文件和报告会显示在这里。</p>{/if}
					</section>

					<button type="button" class="flex min-h-11 w-full items-center gap-2 rounded-xl border border-white/[0.07] bg-white/[0.015] px-3 text-left text-xs text-terminal-dim hover:bg-white/[0.03] hover:text-terminal-fg" onclick={() => (developerOpen = !developerOpen)} aria-expanded={developerOpen}><Code2 class="size-4" /><span class="flex-1">Developer details</span><ChevronDown class="size-4 transition-transform {developerOpen ? 'rotate-180' : ''}" /></button>

					{#if developerOpen}
					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center gap-2"><Pause class="size-4 text-amber-400" /><h2 class="text-sm font-semibold">Task / GoalRun</h2></div>
						<div class="grid grid-cols-2 gap-2 font-mono text-[10px] text-terminal-dim"><span>task {view.task?.id}</span><span>session {view.session?.session_id}</span><span>GoalRun {view.goal_run?.goal_run_id ?? "—"}</span><span>status <b class={statusClass(view.goal_run?.status)}>{statusLabel(view.goal_run?.status)}</b></span><span>work items {view.goal_run?.work_items?.length ?? 0}</span><span>trace {view.session?.trace_id ?? "—"}</span></div>
						<div class="mt-3 flex gap-2">{#if ["running", "pending", "waiting_approval"].includes(view.state?.status)}<button type="button" class="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-rose-500/30 px-3 py-2 text-xs text-rose-300 hover:bg-rose-500/10 disabled:opacity-40" onclick={() => void taskControl("cancel")} disabled={busy !== ""}><CircleX class="size-3.5" />取消</button>{/if}{#if ["failed", "cancelled", "partial_completed"].includes(view.state?.status)}<button type="button" class="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-sky-500/30 px-3 py-2 text-xs text-sky-300 hover:bg-sky-500/10 disabled:opacity-40" onclick={() => void taskControl("resume")} disabled={busy !== ""}><Play class="size-3.5" />恢复</button>{/if}</div>
					</section>
					{/if}

					{#if developerOpen}
					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<h2 class="mb-3 text-sm font-semibold">Governance / audit</h2><p class="font-mono text-[10px] text-terminal-dim">decisions {view.governance?.decisions?.length ?? 0} · side effects {view.governance?.side_effects?.length ?? 0}</p><div class="mt-2 space-y-1">{#each (view.governance?.decisions ?? []).slice(-8) as item (item.event_id)}<p class="break-all font-mono text-[10px] text-terminal-dim"><span class="text-violet-300">audit</span> {json(item.payload)}</p>{/each}</div>
					</section>
					{/if}
					{#if developerOpen}
						<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4"><h2 class="mb-3 text-sm font-semibold">Provider usage</h2><div class="grid grid-cols-2 gap-2 font-mono text-[10px] text-terminal-dim"><span>cost ${Number(view.usage?.cost_usd ?? 0).toFixed(6)}</span><span>records {view.usage?.records?.length ?? 0}</span></div>{#if view.usage?.records?.length}<div class="mt-2 space-y-1">{#each view.usage.records.slice(-8) as record (record.event_id)}<p class="break-all font-mono text-[10px] text-terminal-dim">{json(record)}</p>{/each}</div>{/if}</section>
					{/if}
				</aside>
			</div>
		{:else}<div class="rounded-xl border border-rose-500/30 p-8 text-center text-sm text-rose-300">{error || "Workbench unavailable"}</div>{/if}

		{#if artifact}
			<section class="rounded-xl border border-sky-500/30 bg-sky-500/[0.03] p-4"><div class="flex items-center justify-between"><h2 class="text-sm font-semibold">Artifact: {artifact.name}</h2><button type="button" class="inline-flex min-h-11 items-center rounded-lg px-3 text-xs text-terminal-dim hover:bg-white/5 hover:text-terminal-fg" onclick={() => (artifact = null)}>关闭</button></div><pre class="mt-3 max-h-[520px] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-black/30 p-3 font-mono text-xs text-terminal-dim">{typeof artifact.content === "string" ? artifact.content : formatResult(artifact.content)}</pre></section>
		{/if}
	{#if view && (pendingApprovalCount > 0 || canCancel || canResume)}
		<div class="fixed inset-x-0 bottom-0 z-40 border-t border-white/10 bg-[#0b0b0b]/95 p-3 backdrop-blur md:hidden">
			<div class="mx-auto flex max-w-6xl items-center gap-2">
				{#if pendingApprovalCount > 0}
					<button type="button" class="min-h-11 flex-1 rounded-xl bg-amber-500/15 px-3 text-sm font-medium text-amber-200" onclick={scrollToAttention}>需要确认 {pendingApprovalCount}</button>
				{/if}
				{#if canResume}
					<button type="button" class="min-h-11 flex-1 rounded-xl bg-sky-500/15 px-3 text-sm font-medium text-sky-200 disabled:opacity-40" onclick={() => void taskControl("resume")} disabled={busy !== ""}>继续任务</button>
				{/if}
				{#if canCancel}
					<button type="button" class="min-h-11 rounded-xl border border-rose-500/30 px-3 text-sm text-rose-300 disabled:opacity-40" onclick={() => void taskControl("cancel")} disabled={busy !== ""}>取消</button>
				{/if}
			</div>
		</div>
	{/if}

	</div>
</main>
