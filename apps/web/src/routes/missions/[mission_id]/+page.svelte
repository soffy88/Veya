<script lang="ts">
	import { onDestroy, onMount, untrack } from "svelte";
	import {
		AlertTriangle,
		ArrowLeft,
		CheckCircle2,
		Code2,
		FileText,
		Loader2,
		Play,
		RefreshCw,
		RotateCcw,
		ShieldAlert,
		Square,
	} from "lucide-svelte";
	import { createMissionDetailStore } from "$lib/supervision/store.svelte";
	import {
		artifactPath,
		eventLabel,
		executorLabel,
		formatTime,
		reportHeadline,
		statusLabel,
		statusTone,
		supervisionModeLabel,
	} from "$lib/supervision/format";
	import type { SupervisionMode } from "$lib/supervision/types";

	let { data }: { data: { missionId: string } } = $props();
	const missionId = untrack(() => data.missionId);
	const store = createMissionDetailStore(missionId);

	onMount(() => {
		void store.load();
		store.subscribe();
	});
	onDestroy(() => store.dispose());

	const OWNER_CODES = new Set([
		"OWNER_CREDENTIAL_REQUIRED",
		"IRREVERSIBLE_EXTERNAL_ACTION",
		"POLICY_CONFIRMATION_REQUIRED",
		"PRODUCTION_DESTRUCTIVE_ACTION",
		"RESOURCE_OWNER_INPUT_REQUIRED",
	]);

	const mission = $derived(store.state.inspect?.mission ?? null);
	const report = $derived(store.state.report ?? store.state.inspect?.latest_report ?? null);
	const supervisor = $derived(store.state.inspect?.current_supervisor ?? mission?.supervision_mode ?? "");
	const executor = $derived.by(() => {
		const policy = mission?.policies?.execution_policy ?? {};
		return executorLabel(String(policy.assignee_hint ?? ""));
	});
	const ownerEscalations = $derived(
		store.state.escalations.filter((event) =>
			OWNER_CODES.has(String(event.code ?? event.escalation_code ?? "")),
		),
	);
	const reviews = $derived(store.state.reviews);
	const events = $derived(store.state.events);

	async function switchMode(mode: SupervisionMode): Promise<void> {
		await store.switchMode(mode);
	}
</script>

<svelte:head>
	<title>{mission?.goal ?? "Work"} · Veya</title>
</svelte:head>

<main class="min-h-dvh overflow-y-auto bg-[#080808] px-4 py-5 text-terminal-fg md:px-8">
	<div class="mx-auto max-w-5xl space-y-5">
		<header class="flex flex-wrap items-start gap-3 border-b border-white/[0.07] pb-5">
			<a href="/?view=tasks" class="mt-0.5 inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-terminal-edge px-3 text-sm text-terminal-dim hover:text-terminal-fg">
				<ArrowLeft class="size-4" /> Work
			</a>
			<div class="min-w-0 flex-1">
				<div class="mb-1 flex items-center gap-2">
					<span class="badge" data-tone={statusTone(mission?.status)}>{statusLabel(mission?.status)}</span>
					<span class="text-xs text-terminal-dim">Work</span>
				</div>
				<h1 class="text-xl font-semibold leading-tight">{mission?.goal ?? "正在读取工作目标…"}</h1>
			</div>
			<div class="flex flex-wrap items-center gap-2">
				<button class="btn min-h-10" onclick={() => void store.load()} disabled={store.state.loading}>
					<RefreshCw size={16} class={store.state.loading ? "animate-spin" : ""} /> 刷新
				</button>
				<button class="btn btn-primary min-h-10" title="Start" onclick={() => void store.start()} disabled={store.runInFlight}>
					{#if store.runInFlight}<Loader2 size={16} class="animate-spin" />{:else}<Play size={16} />{/if}
					开始 / 继续
				</button>
				<button class="btn min-h-10" title="Retry" onclick={() => void store.retry()}>
					<RotateCcw size={16} /> 重试
				</button>
				<button class="btn min-h-10" title="Cancel" onclick={() => void store.cancel()}>
					<Square size={16} /> 取消
				</button>
			</div>
		</header>

		{#if store.state.error}
			<div class="rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-300">{store.state.error}</div>
		{/if}

		<section class="rounded-2xl border border-white/[0.07] bg-white/[0.02] p-5">
			<div class="grid gap-4 sm:grid-cols-3">
				<div>
					<div class="text-xs text-terminal-dim">运行方式</div>
					<div class="mt-1 text-sm">{supervisionModeLabel(mission?.supervision_mode)}</div>
				</div>
				<div>
					<div class="text-xs text-terminal-dim">当前状态</div>
					<div class="mt-1 text-sm">{statusLabel(mission?.status)}</div>
				</div>
				<div>
					<div class="text-xs text-terminal-dim">最后更新</div>
					<div class="mt-1 text-sm">{formatTime(mission?.updated_at)}</div>
				</div>
			</div>

			<details class="mt-4 border-t border-white/[0.06] pt-3">
				<summary class="cursor-pointer text-xs text-terminal-dim hover:text-terminal-fg">调整运行方式</summary>
				<div class="mt-3 flex flex-wrap gap-2">
					{#each ["auto", "external", "internal"] as mode (mode)}
						<button class="btn" onclick={() => void switchMode(mode as SupervisionMode)}>
							{supervisionModeLabel(mode)}
						</button>
					{/each}
				</div>
			</details>
		</section>

		{#if mission?.status === "WAITING_EXTERNAL_SUPERVISOR"}
			<section class="rounded-2xl border border-amber-500/30 bg-amber-500/[0.05] p-5">
				<h2 class="flex items-center gap-2 text-sm font-semibold text-amber-300">
					<AlertTriangle size={16} /> 等待 ChatGPT 审查
				</h2>
				<p class="mt-2 text-sm leading-6 text-terminal-dim">
					本轮执行已经结束，正在等待外部监督结果。Veya 会在审查完成后继续。
				</p>
			</section>
		{/if}

		{#if mission?.status === "WAITING_OWNER" || ownerEscalations.length}
			<section class="rounded-2xl border border-red-500/30 bg-red-500/[0.04] p-5">
				<h2 class="flex items-center gap-2 text-sm font-semibold text-red-300">
					<ShieldAlert size={16} /> 需要你处理
				</h2>
				<ul class="mt-3 space-y-2 text-sm text-terminal-dim">
					{#each ownerEscalations as escalation, index (index)}
						<li>{String(escalation.reason ?? escalation.code ?? escalation.escalation_code ?? escalation.topic)}</li>
					{/each}
					{#if !ownerEscalations.length}<li>这项工作正在等待你的输入。</li>{/if}
				</ul>
			</section>
		{/if}

		<section class="rounded-2xl border border-white/[0.07] bg-white/[0.02] p-5">
			<div class="mb-4 flex items-start justify-between gap-3">
				<div>
					<h2 class="text-sm font-semibold">结果</h2>
					<p class="mt-1 text-xs text-terminal-dim">{reportHeadline(report)}</p>
				</div>
				{#if mission?.status === "DONE" || mission?.status === "ACCEPTED"}
					<span class="inline-flex items-center gap-1 text-xs text-emerald-300"><CheckCircle2 class="size-3.5" /> 已完成</span>
				{/if}
			</div>

			{#if !report}
				<p class="text-sm text-terminal-dim">运行后，摘要、文件和验证结果会显示在这里。</p>
			{:else}
				{#if report.executor_summary}
					<div class="rounded-xl border border-white/[0.06] bg-black/10 p-4">
						<div class="text-xs text-terminal-dim">执行摘要</div>
						<p class="mt-2 whitespace-pre-wrap text-sm leading-6">{report.executor_summary}</p>
					</div>
				{/if}

				<div class="mt-4 grid gap-4 md:grid-cols-2">
					<div>
						<h3 class="mb-2 text-xs font-medium text-terminal-dim">文件与产物</h3>
						{#if report.artifacts.length}
							<ul class="space-y-1.5">
								{#each report.artifacts as artifact, index (index)}
									{@const path = artifactPath(artifact)}
									<li class="flex items-center gap-2 rounded-lg border border-white/[0.05] px-3 py-2 text-sm">
										<FileText size={14} class="shrink-0 text-sky-300" />
										<span class="min-w-0 flex-1 truncate">{path}</span>
										{#if artifact.verified === true || artifact.exists === true}
											<span class="text-[11px] text-emerald-300">已验证</span>
										{:else}
											<span class="text-[11px] text-terminal-dim">未校验</span>
										{/if}
									</li>
								{/each}
							</ul>
						{:else}
							<p class="text-sm text-terminal-dim">暂无产物。</p>
						{/if}
					</div>

					<div>
						<h3 class="mb-2 text-xs font-medium text-terminal-dim">验证</h3>
						<div class="space-y-2 text-sm">
							<div class="flex justify-between gap-3"><span class="text-terminal-dim">测试记录</span><span>{report.tests.length}</span></div>
							<div class="flex justify-between gap-3"><span class="text-terminal-dim">失败项</span><span class={report.failures.length ? "text-rose-300" : "text-emerald-300"}>{report.failures.length}</span></div>
							<div class="flex justify-between gap-3"><span class="text-terminal-dim">阻塞项</span><span class={report.blocked_items.length ? "text-amber-300" : "text-terminal-fg"}>{report.blocked_items.length}</span></div>
						</div>
					</div>
				</div>

				{#if report.proposed_next_action}
					<div class="mt-4 rounded-xl border border-white/[0.06] px-4 py-3 text-sm">
						<span class="text-terminal-dim">下一步：</span>{report.proposed_next_action}
					</div>
				{/if}
			{/if}
		</section>

		<section class="rounded-2xl border border-white/[0.07] bg-white/[0.02] p-5">
			<h2 class="mb-3 text-sm font-semibold">审查历史 · Review Timeline</h2>
			{#if !reviews.length}
				<p class="text-sm text-terminal-dim">暂无审查记录。</p>
			{:else}
				<ol class="space-y-2">
					{#each [...reviews].reverse() as review, index (index)}
						<li class="rounded-xl border border-white/[0.05] p-3">
							<div class="flex flex-wrap items-center justify-between gap-2">
								<span class="text-sm font-medium">{String(review.decision)}</span>
								<span class="text-xs text-terminal-dim">{formatTime(Number(review.created_at))}</span>
							</div>
							{#if review.reason}<p class="mt-1 text-xs leading-5 text-terminal-dim">{String(review.reason)}</p>{/if}
							{#if review.next_task}<p class="mt-1 text-xs text-terminal-dim">下一步：{String(review.next_task)}</p>{/if}
						</li>
					{/each}
				</ol>
			{/if}
		</section>

		<details class="rounded-2xl border border-white/[0.07] bg-white/[0.015]">
			<summary class="flex cursor-pointer items-center gap-2 px-4 py-3 text-xs text-terminal-dim hover:text-terminal-fg">
				<Code2 class="size-4" /> Developer details
			</summary>
			<div class="space-y-4 border-t border-white/[0.06] p-4">
				<div class="grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
					<div><span class="text-terminal-dim">mission_id</span><div class="mt-1 break-all font-mono">{missionId}</div></div>
					<div><span class="text-terminal-dim">supervisor</span><div class="mt-1 font-mono">{supervisor || "—"}</div></div>
					<div><span class="text-terminal-dim">executor</span><div class="mt-1 font-mono">{executor}</div></div>
					<div><span class="text-terminal-dim">iteration</span><div class="mt-1 font-mono">{String(mission?.authority?.iteration ?? report?.iteration ?? 0)}</div></div>
					<div><span class="text-terminal-dim">goalrun_id</span><div class="mt-1 break-all font-mono">{report?.goalrun_id ?? "—"}</div></div>
					<div><span class="text-terminal-dim">execution_id</span><div class="mt-1 break-all font-mono">{String(mission?.authority?.execution_id ?? "—")}</div></div>
				</div>

				{#if mission?.supervision_mode === "auto"}
					<div class="rounded-xl border border-white/[0.06] p-3 text-xs text-terminal-dim">
						<div class="font-medium text-terminal-fg">AUTO route</div>
						<div class="mt-1">selected supervisor: {supervisor || "—"}</div>
						<div class="mt-1 font-mono">SUPERVISOR_SELECTED</div>
					</div>
				{/if}

				{#if report}
					<div class="grid gap-4 md:grid-cols-2">
						<div>
							<h3 class="mb-2 text-xs font-medium text-terminal-dim">ExecutionReport</h3>
							<pre class="max-h-52 overflow-auto rounded-lg bg-black/20 p-3 font-mono text-[11px] text-terminal-dim">{JSON.stringify({
								changes: report.changes,
								tests: report.tests,
								artifacts: report.artifacts,
								runtime_evidence: report.runtime_evidence,
								failures: report.failures,
								blocked_items: report.blocked_items,
								git_diff_summary: report.git_diff_summary,
								proposed_next_action: report.proposed_next_action,
							}, null, 2)}</pre>
						</div>
						<div class="overflow-auto">
							<h3 class="mb-2 text-xs font-medium text-terminal-dim">jev_decisions</h3>
							<table class="w-full min-w-[420px] text-left text-xs">
								<thead class="text-terminal-dim"><tr><th>question</th><th>answer</th><th>confidence</th><th>decision</th></tr></thead>
								<tbody>
									{#each report.jev_decisions as decision, index (index)}
										<tr class="border-t border-white/[0.05]"><td>{String(decision.question ?? decision.kind ?? "—")}</td><td>{String(decision.choice ?? decision.score ?? decision.noul ?? "—")}</td><td>{decision.confidence == null ? "—" : Number(decision.confidence).toFixed(2)}</td><td>{String(decision.decision ?? decision.action ?? "advisory")}</td></tr>
									{/each}
								</tbody>
							</table>
						</div>
					</div>
				{/if}

				<div>
					<div class="mb-2 flex items-center justify-between gap-2">
						<h3 class="text-xs font-medium text-terminal-dim">Live Events ({events.length})</h3>
						{#if store.streamError}<span class="text-[11px] text-amber-300">{store.streamError}</span>{/if}
					</div>
					<ul class="max-h-64 space-y-1 overflow-auto font-mono text-[11px] text-terminal-dim">
						{#each events as event, index (index)}
							<li>{formatTime(Number(event.ts))} · {eventLabel(event)}</li>
						{/each}
						{#if !events.length}<li>暂无事件</li>{/if}
					</ul>
				</div>
			</div>
		</details>
	</div>
</main>
