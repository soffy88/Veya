<script lang="ts">
	import { onMount } from "svelte";
	import { ArrowLeft, Check, CircleX, ExternalLink, Eye, Pause, Play, RefreshCw, Shield, UserRound } from "lucide-svelte";
	import { api, formatResult } from "$lib/api";

	type AnyRecord = Record<string, any>;

	let { data: routeData } = $props<{ data: { taskId: string } }>();
	let view = $state<AnyRecord | null>(null);
	let loading = $state(true);
	let busy = $state("");
	let error = $state("");
	let artifact = $state<AnyRecord | null>(null);
	let artifactBusy = $state(false);

	const STATUS_LABEL: Record<string, string> = {
		created: "已创建",
		contract_ready: "契约就绪",
		worktree_ready: "工作树就绪",
		goalrun_created: "GoalRun 已创建",
		pending: "待运行",
		running: "运行中",
		waiting_approval: "等待审批",
		verifying: "验证中",
		finalizing: "收尾中",
		completed: "已完成",
		failed: "失败",
		cancelled: "已取消",
		partial_completed: "部分完成",
		quarantined: "已隔离",
		not_started: "未开始",
	};

	const taskId = $derived(routeData.taskId);

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
			error = result.status === 409 ? "审批已过期，已刷新 canonical 状态。" : `审批失败 (HTTP ${result.status})`;
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
			error = result.status === 409 ? "浏览器控制已过期或当前进程未附着该句柄。" : `浏览器控制失败 (HTTP ${result.status})`;
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
		void loadWorkbench();
		const timer = window.setInterval(() => void loadWorkbench(), 3000);
		return () => window.clearInterval(timer);
	});
</script>

<svelte:head>
	<title>{view?.task?.title ?? "Workbench"} · Veya</title>
</svelte:head>

<main class="min-h-dvh overflow-y-auto bg-[#080808] px-4 py-5 text-terminal-fg md:px-8">
	<div class="mx-auto max-w-[1600px] space-y-4">
		<header class="flex flex-wrap items-center gap-3 border-b border-white/10 pb-4">
			<a href="/" class="inline-flex items-center gap-1.5 rounded-lg border border-terminal-edge px-2.5 py-1.5 text-xs text-terminal-dim hover:text-terminal-fg"><ArrowLeft class="size-3.5" /> 返回</a>
			<div class="min-w-0 flex-1">
				<p class="font-mono text-[10px] uppercase tracking-[0.2em] text-sky-400/80">Unified Veya Bot Workbench</p>
				<h1 class="truncate text-lg font-semibold">{view?.task?.title ?? taskId}</h1>
				<p class="truncate font-mono text-[11px] text-terminal-dim">{view?.task?.objective ?? "读取 canonical task state…"}</p>
			</div>
			{#if view}
				<span class="font-mono text-xs {statusClass(view.state?.status)}">● {statusLabel(view.state?.status)}</span>
				<span class="font-mono text-[10px] text-terminal-dim">v {view.state?.version}</span>
			{/if}
			<button type="button" class="rounded-lg border border-terminal-edge p-2 text-terminal-dim hover:text-terminal-fg disabled:opacity-40" onclick={() => void loadWorkbench()} disabled={loading} title="刷新 canonical state"><RefreshCw class="size-4 {loading ? 'animate-spin' : ''}" /></button>
		</header>

		{#if error}<div class="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 font-mono text-xs text-rose-300">{error}</div>{/if}
		{#if loading && !view}<div class="rounded-xl border border-terminal-edge p-8 text-center font-mono text-xs text-terminal-dim">读取 canonical state…</div>
		{:else if view}
			<div class="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(360px,0.65fr)]">
				<section class="space-y-4">
					<div class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center gap-2"><UserRound class="size-4 text-sky-400" /><h2 class="text-sm font-semibold">Conversation</h2><span class="font-mono text-[10px] text-terminal-dim">canonical messages</span></div>
						{#if view.conversation?.length}
							<div class="space-y-3">{#each view.conversation as message (message.event_id)}<article class="rounded-lg border border-white/5 p-3 {message.role === 'user' ? 'bg-sky-400/[0.04]' : 'bg-white/[0.02]'}"><div class="mb-1 flex items-center justify-between font-mono text-[10px] text-terminal-dim"><span>{message.role === "user" ? "USER" : "MASTERAGENT"}</span><span>{formatTime(message.ts)}</span></div><p class="whitespace-pre-wrap text-sm leading-6">{message.content}</p></article>{/each}</div>
						{:else}<p class="font-mono text-xs text-terminal-dim">暂无 canonical conversation event。</p>{/if}
					</div>

					<div class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center gap-2"><Eye class="size-4 text-violet-400" /><h2 class="text-sm font-semibold">Real event timeline</h2><span class="font-mono text-[10px] text-terminal-dim">{view.state?.event_count ?? 0} events · EventStore</span></div>
						{#if view.timeline?.length}<div class="max-h-[520px] space-y-1.5 overflow-y-auto pr-1">{#each view.timeline as event (event.event_id)}<div class="grid grid-cols-[130px_1fr] gap-3 rounded-md border border-white/[0.04] px-2.5 py-2 font-mono text-[10px]"><span class="text-sky-300">{event.topic}</span><span class="min-w-0 text-terminal-dim"><span>{formatTime(event.ts)}</span>{#if event.actor}<span class="ml-2 opacity-60">{event.actor}</span>{/if}{#if event.payload && Object.keys(event.payload).length}<span class="ml-2 break-all opacity-70">{json(event.payload)}</span>{/if}</span></div>{/each}</div>
						{:else}<p class="font-mono text-xs text-terminal-dim">暂无 event；页面不会生成伪 activity。</p>{/if}
					</div>
				</section>

				<aside class="space-y-4">
					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center gap-2"><Pause class="size-4 text-amber-400" /><h2 class="text-sm font-semibold">Task / GoalRun</h2></div>
						<div class="grid grid-cols-2 gap-2 font-mono text-[10px] text-terminal-dim"><span>task {view.task?.id}</span><span>session {view.session?.session_id}</span><span>GoalRun {view.goal_run?.goal_run_id ?? "—"}</span><span>status <b class={statusClass(view.goal_run?.status)}>{statusLabel(view.goal_run?.status)}</b></span><span>work items {view.goal_run?.work_items?.length ?? 0}</span><span>trace {view.session?.trace_id ?? "—"}</span></div>
						<div class="mt-3 flex gap-2">{#if ["running", "pending", "waiting_approval"].includes(view.state?.status)}<button type="button" class="inline-flex items-center gap-1.5 rounded-md border border-rose-500/30 px-2.5 py-1.5 text-xs text-rose-300 hover:bg-rose-500/10 disabled:opacity-40" onclick={() => void taskControl("cancel")} disabled={busy !== ""}><CircleX class="size-3.5" />取消</button>{/if}{#if ["failed", "cancelled", "partial_completed"].includes(view.state?.status)}<button type="button" class="inline-flex items-center gap-1.5 rounded-md border border-sky-500/30 px-2.5 py-1.5 text-xs text-sky-300 hover:bg-sky-500/10 disabled:opacity-40" onclick={() => void taskControl("resume")} disabled={busy !== ""}><Play class="size-3.5" />恢复</button>{/if}</div>
					</section>

					<section class="rounded-xl border border-amber-500/25 bg-amber-500/[0.03] p-4">
						<div class="mb-3 flex items-center gap-2"><Shield class="size-4 text-amber-400" /><h2 class="text-sm font-semibold">Approval queue</h2><span class="font-mono text-[10px] text-terminal-dim">PR-09 existing mechanism</span></div>
						{#if view.approvals?.pending?.length}{#each view.approvals.pending as item (item.request_id)}<div class="mb-2 rounded-lg border border-amber-500/20 p-3"><div class="flex items-center justify-between text-xs"><span class="font-mono text-amber-200">{item.tool_name}</span><span class="font-mono text-[10px] text-terminal-dim">{item.request_id}</span></div><p class="mt-1 text-xs text-terminal-dim">{item.reason}</p><p class="mt-2 break-all font-mono text-[10px] text-terminal-dim">{json(item.tool_args)}</p><div class="mt-2 flex gap-2"><button type="button" class="inline-flex items-center gap-1 rounded-md bg-emerald-500/15 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-40" onclick={() => void approval(item.request_id, true)} disabled={busy !== ""}><Check class="size-3.5" />批准</button><button type="button" class="inline-flex items-center gap-1 rounded-md bg-rose-500/15 px-2 py-1 text-xs text-rose-300 hover:bg-rose-500/25 disabled:opacity-40" onclick={() => void approval(item.request_id, false)} disabled={busy !== ""}><CircleX class="size-3.5" />拒绝</button></div></div>{/each}{:else}<p class="font-mono text-xs text-terminal-dim">当前没有待审批请求。</p>{/if}
					</section>

					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<div class="mb-3 flex items-center gap-2"><Eye class="size-4 text-cyan-400" /><h2 class="text-sm font-semibold">Computer / Browser</h2></div>
						<div class="space-y-1 font-mono text-[10px] text-terminal-dim"><p>computer: {view.computer?.status ?? "not_observed"} · {view.computer?.computer_id ?? "—"}</p><p>browser: {view.browser?.status ?? "unknown"}</p><p>url: {view.browser?.current_url ?? "—"}</p><p>control: <span class={view.browser?.control_state === "HUMAN_CONTROL" ? "text-amber-300" : "text-sky-300"}>{view.browser?.control_state ?? "unknown"}</span></p></div>
						{#if view.browser?.session_id}<div class="mt-3 flex gap-2">{#if view.browser?.control_state === "HUMAN_CONTROL"}<button type="button" class="rounded-md border border-sky-500/30 px-2.5 py-1.5 text-xs text-sky-300 hover:bg-sky-500/10 disabled:opacity-40" onclick={() => void browserControl("return_control")} disabled={busy !== ""}>交还 Agent</button>{:else}<button type="button" class="rounded-md border border-amber-500/30 px-2.5 py-1.5 text-xs text-amber-300 hover:bg-amber-500/10 disabled:opacity-40" onclick={() => void browserControl("takeover")} disabled={busy !== ""}>接管浏览器</button>{/if}</div>{:else}<p class="mt-3 font-mono text-[10px] text-terminal-dim">当前任务没有 canonical browser handle。</p>{/if}
						{#if view.browser?.snapshot}<pre class="mt-3 max-h-48 overflow-auto rounded-md bg-black/30 p-2 font-mono text-[10px] text-terminal-dim">{json(view.browser.snapshot)}</pre>{/if}
					</section>

					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<h2 class="mb-3 text-sm font-semibold">Governance / audit</h2><p class="font-mono text-[10px] text-terminal-dim">decisions {view.governance?.decisions?.length ?? 0} · side effects {view.governance?.side_effects?.length ?? 0}</p><div class="mt-2 space-y-1">{#each (view.governance?.decisions ?? []).slice(-8) as item (item.event_id)}<p class="break-all font-mono text-[10px] text-terminal-dim"><span class="text-violet-300">audit</span> {json(item.payload)}</p>{/each}</div>
					</section>

					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
						<h2 class="mb-3 text-sm font-semibold">Verification / artifacts</h2><div class="grid grid-cols-2 gap-2 font-mono text-[10px] text-terminal-dim"><span>acceptance {view.verification?.acceptance_passed === true ? "PASS" : view.verification?.acceptance_passed === false ? "FAIL" : "—"}</span><span>report {view.verification?.verification_report_id ?? "—"}</span><span>sensors {view.verification?.sensor_summary?.passed ?? "—"}/{view.verification?.sensor_summary?.total ?? "—"}</span></div><p class="mt-2 text-[10px] text-terminal-dim">changed: {view.verification?.changed_files?.join(", ") || "—"}</p>{#if view.artifacts?.length}<div class="mt-3 space-y-1">{#each view.artifacts as item (item.name)}<button type="button" class="flex w-full items-center justify-between rounded-md border border-white/5 px-2 py-1.5 text-left font-mono text-[10px] text-sky-300 hover:bg-white/5 disabled:opacity-40" onclick={() => void openArtifact(item.name)} disabled={!item.available || artifactBusy}><span>{item.name}</span><ExternalLink class="size-3" /></button>{/each}</div>{:else}<p class="mt-3 font-mono text-[10px] text-terminal-dim">暂无 task-scoped artifacts。</p>{/if}
					</section>

					<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4"><h2 class="mb-3 text-sm font-semibold">Provider usage</h2><div class="grid grid-cols-2 gap-2 font-mono text-[10px] text-terminal-dim"><span>cost ${Number(view.usage?.cost_usd ?? 0).toFixed(6)}</span><span>records {view.usage?.records?.length ?? 0}</span></div>{#if view.usage?.records?.length}<div class="mt-2 space-y-1">{#each view.usage.records.slice(-8) as record (record.event_id)}<p class="break-all font-mono text-[10px] text-terminal-dim">{json(record)}</p>{/each}</div>{/if}</section>
				</aside>
			</div>
		{:else}<div class="rounded-xl border border-rose-500/30 p-8 text-center text-sm text-rose-300">{error || "Workbench unavailable"}</div>{/if}

		{#if artifact}
			<section class="rounded-xl border border-sky-500/30 bg-sky-500/[0.03] p-4"><div class="flex items-center justify-between"><h2 class="text-sm font-semibold">Artifact: {artifact.name}</h2><button type="button" class="text-xs text-terminal-dim hover:text-terminal-fg" onclick={() => (artifact = null)}>关闭</button></div><pre class="mt-3 max-h-[520px] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-black/30 p-3 font-mono text-xs text-terminal-dim">{typeof artifact.content === "string" ? artifact.content : formatResult(artifact.content)}</pre></section>
		{/if}
	</div>
</main>
