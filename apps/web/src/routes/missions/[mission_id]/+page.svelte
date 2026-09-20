<script lang="ts">
	/**
	 * /missions/[mission_id] — one mission: state, evidence, review timeline, live events.
	 *
	 * Everything shown comes from the canonical backend. `executed` is not success,
	 * an INTERRUPTED report is not a failed mission, and ACCEPTED/DONE appear only
	 * when the mission itself says so. Loading is GET-only; only the explicit
	 * controls below can dispatch work, and Retry goes through canonical continue.
	 */
	import { onDestroy, onMount } from "svelte";
	import {
		AlertTriangle,
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
		relativeTime,
		reportHeadline,
		statusLabel,
		statusTone,
		supervisionModeLabel,
	} from "$lib/supervision/format";
	import type { MissionEvent, SupervisionMode, SupervisorReview } from "$lib/supervision/types";

	let { data }: { data: { missionId: string } } = $props();
	const missionId = $derived(data.missionId);
	const store = createMissionDetailStore(missionId);

	onMount(() => {
		void store.load();
		store.subscribe(); // read-only event stream
	});
	onDestroy(() => store.dispose());

	// Owner-only escalation codes (everything else is self-handled by the runtime).
	const OWNER_CODES = new Set([
		"OWNER_CREDENTIAL_REQUIRED",
		"IRREVERSIBLE_EXTERNAL_ACTION",
		"POLICY_CONFIRMATION_REQUIRED",
		"PRODUCTION_DESTRUCTIVE_ACTION",
		"RESOURCE_OWNER_INPUT_REQUIRED",
	]);

	const mission = $derived(store.state.inspect?.mission ?? null);
	const report = $derived(store.state.report ?? store.state.inspect?.latest_report ?? null);
	const supervisor = $derived(
		store.state.inspect?.current_supervisor ?? mission?.supervision_mode ?? "",
	);
	const executor = $derived.by(() => {
		const policy = mission?.policies?.execution_policy ?? {};
		return executorLabel(String(policy.assignee_hint ?? ""));
	});
	const switchEvents = $derived(store.state.events.filter((e) => e.topic === "SUPERVISOR_SELECTED" || e.topic === "SUPERVISION_MODE_CHANGED"));
	const ownerEscalations = $derived(
		store.state.escalations.filter((e) => OWNER_CODES.has(String(e.code ?? e.escalation_code ?? ""))),
	);
	const jev = $derived((report?.jev_decisions ?? []) as Array<Record<string, unknown>>);
	const reviews = $derived(storyByIteration(store.state.reviews, store.state.events));

	interface TimelineEntry {
		review: SupervisorReview;
		iteration: number;
		events: MissionEvent[];
	}

	function storyByIteration(reviews: SupervisorReview[], events: MissionEvent[]): TimelineEntry[] {
		return reviews.map((review) => ({
			review,
			iteration: Number(review.iteration ?? 0),
			events: events.filter(
				(event) => Number(event.iteration ?? -1) === Number(review.iteration ?? 0),
			),
		}));
	}

	async function switchMode(mode: SupervisionMode): Promise<void> {
		await store.switchMode(mode);
	}
</script>

<div class="mx-auto w-full max-w-5xl space-y-6 p-6">
	<header class="flex flex-wrap items-start justify-between gap-4">
		<div class="min-w-0">
			<div class="flex items-center gap-2">
				<span class="badge" data-tone={statusTone(mission?.status)}>{statusLabel(mission?.status)}</span>
				<span class="text-xs opacity-70">{missionId}</span>
			</div>
			<h1 class="mt-2 text-xl font-semibold">{mission?.goal ?? "…"}</h1>
		</div>
		<div class="flex flex-wrap items-center gap-2">
			<button class="btn" onclick={() => void store.load()} disabled={store.state.loading}>
				<RefreshCw size={16} class={store.state.loading ? "animate-spin" : ""} /> 刷新
			</button>
			<button class="btn btn-primary" onclick={() => void store.start()} disabled={store.runInFlight}>
				{#if store.runInFlight}<Loader2 size={16} class="animate-spin" />{:else}<Play size={16} />{/if} Start
			</button>
			<button class="btn" onclick={() => void store.retry()}>
				<RotateCcw size={16} /> Retry
			</button>
			<button class="btn" onclick={() => void store.cancel()}>
				<Square size={16} /> Cancel
			</button>
		</div>
	</header>

	{#if store.state.error}
		<div class="card border-red-500/40 p-4 text-sm text-red-400">{store.state.error}</div>
	{/if}

	<!-- A. Current state -->
	<section class="card p-5">
		<h2 class="mb-3 text-sm font-semibold uppercase tracking-wide opacity-70">当前状态</h2>
		<dl class="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
			<div><dt class="opacity-60">监督模式</dt><dd>{supervisionModeLabel(mission?.supervision_mode)}</dd></div>
			<div><dt class="opacity-60">当前监督者</dt><dd>{supervisor || "—"}</dd></div>
			<div><dt class="opacity-60">当前执行器</dt><dd>{executor}</dd></div>
			<div><dt class="opacity-60">iteration</dt><dd>{String(mission?.authority?.iteration ?? report?.iteration ?? 0)}</dd></div>
			<div><dt class="opacity-60">goalrun_id</dt><dd>{report?.goalrun_id ?? "—"}</dd></div>
			<div><dt class="opacity-60">execution_id</dt><dd>{String(mission?.authority?.execution_id ?? "—")}</dd></div>
			<div><dt class="opacity-60">updated_at</dt><dd>{formatTime(mission?.updated_at)}</dd></div>
		</dl>

		{#if mission?.supervision_mode === "auto"}
			<div class="mt-4 rounded border p-3 text-sm">
				<div class="font-medium">AUTO 决策</div>
				<div class="mt-1 opacity-80">
					selected supervisor：<b>{supervisor || "—"}</b>
					{#if switchEvents.length}
						· reason={String(switchEvents[switchEvents.length - 1].reason_code ?? switchEvents[switchEvents.length - 1].mode ?? "—")}
					{/if}
				</div>
				<ul class="mt-2 space-y-1 text-xs opacity-70">
					{#each switchEvents as event, index (index)}
						<li>{formatTime(event.ts)} · {eventLabel(event)} · {JSON.stringify(event).slice(0, 160)}</li>
					{/each}
					{#if !switchEvents.length}<li>尚无切换记录</li>{/if}
				</ul>
			</div>
		{/if}

		<div class="mt-4 flex flex-wrap items-center gap-2 text-xs">
			<span class="opacity-60">切换监督模式：</span>
			{#each ["auto", "external", "internal"] as m (m)}
				<button class="btn" onclick={() => void switchMode(m as SupervisionMode)}>{supervisionModeLabel(m)}</button>
			{/each}
		</div>
	</section>

	<!-- External waiting / owner escalation -->
	{#if mission?.status === "WAITING_EXTERNAL_SUPERVISOR"}
		<section class="card border-amber-500/40 p-5">
			<h2 class="flex items-center gap-2 text-sm font-semibold text-amber-400">
				<AlertTriangle size={16} /> 等待 ChatGPT 审查
			</h2>
			<p class="mt-2 text-sm opacity-80">
				本轮执行已结束，等待外部监督者提交审查（CONTINUE / REVISE / RETRY / ACCEPT）。
				执行证据见下方 ExecutionReport。
			</p>
		</section>
	{/if}

	{#if mission?.status === "WAITING_OWNER" || ownerEscalations.length}
		<section class="card border-red-500/40 p-5">
			<h2 class="flex items-center gap-2 text-sm font-semibold text-red-400">
				<ShieldAlert size={16} /> 需要你处理
			</h2>
			<ul class="mt-2 space-y-1 text-sm">
				{#each ownerEscalations as escalation, index (index)}
					<li>{String(escalation.code ?? escalation.escalation_code ?? escalation.topic)} · {String(escalation.reason ?? "")}</li>
				{/each}
				{#if !ownerEscalations.length}<li>任务停在 WAITING_OWNER，等待 owner 输入。</li>{/if}
			</ul>
		</section>
	{/if}

	<!-- B. ExecutionReport -->
	<section class="card p-5">
		<h2 class="mb-1 text-sm font-semibold uppercase tracking-wide opacity-70">ExecutionReport</h2>
		<p class="mb-3 text-xs opacity-60">{reportHeadline(report)}</p>
		{#if !report}
			<div class="text-sm opacity-70">尚无执行报告。</div>
		{:else}
			<div class="grid gap-4 md:grid-cols-2">
				<div>
					<h3 class="text-xs font-medium opacity-70">executor_summary</h3>
					<pre class="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-black/20 p-2 text-xs">{report.executor_summary}</pre>
				</div>
				<div>
					<h3 class="text-xs font-medium opacity-70">proposed_next_action</h3>
					<div class="mt-1 text-sm">{report.proposed_next_action ?? "—"}</div>
					<h3 class="mt-3 text-xs font-medium opacity-70">git_diff_summary</h3>
					<pre class="mt-1 max-h-24 overflow-auto rounded bg-black/20 p-2 text-xs">{JSON.stringify(report.git_diff_summary, null, 2)}</pre>
				</div>
			</div>

			<div class="mt-4 grid gap-4 md:grid-cols-2">
				<div>
					<h3 class="text-xs font-medium opacity-70">changes（{report.changes.length}）</h3>
					<ul class="mt-1 space-y-1 text-xs">
						{#each report.changes as change, index (index)}
							<li>{String(change.path ?? change.task_id ?? JSON.stringify(change)).slice(0, 160)}</li>
						{/each}
						{#if !report.changes.length}<li class="opacity-60">—</li>{/if}
					</ul>
				</div>
				<div>
					<h3 class="text-xs font-medium opacity-70">artifacts（{report.artifacts.length}）</h3>
					<ul class="mt-1 space-y-1 text-xs">
						{#each report.artifacts as artifact, index (index)}
							{@const path = artifactPath(artifact)}
							<li class="flex items-center gap-2">
								<FileText size={12} />
								{#if artifact.verified === true || artifact.exists === true}
									<span title="verified on disk">{path}</span>
								{:else}
									<span class="opacity-60" title="未经校验（后端未标记 verified）">{path}（未校验）</span>
								{/if}
							</li>
						{/each}
						{#if !report.artifacts.length}<li class="opacity-60">—</li>{/if}
					</ul>
				</div>
				<div>
					<h3 class="text-xs font-medium opacity-70">tests（{report.tests.length}）</h3>
					<ul class="mt-1 space-y-1 text-xs">
						{#each report.tests as test, index (index)}
							<li>{JSON.stringify(test).slice(0, 160)}</li>
						{/each}
						{#if !report.tests.length}<li class="opacity-60">—</li>{/if}
					</ul>
				</div>
				<div>
					<h3 class="text-xs font-medium opacity-70">runtime_evidence（{report.runtime_evidence.length}）</h3>
					<ul class="mt-1 space-y-1 text-xs">
						{#each report.runtime_evidence as item, index (index)}
							<li>{JSON.stringify(item).slice(0, 160)}</li>
						{/each}
						{#if !report.runtime_evidence.length}<li class="opacity-60">—</li>{/if}
					</ul>
				</div>
				<div>
					<h3 class="text-xs font-medium text-red-400">failures（{report.failures.length}）</h3>
					<ul class="mt-1 space-y-1 text-xs">
						{#each report.failures as failure, index (index)}
							<li>{JSON.stringify(failure).slice(0, 160)}</li>
						{/each}
						{#if !report.failures.length}<li class="opacity-60">—</li>{/if}
					</ul>
				</div>
				<div>
					<h3 class="text-xs font-medium text-amber-400">blocked_items（{report.blocked_items.length}）</h3>
					<ul class="mt-1 space-y-1 text-xs">
						{#each report.blocked_items as item, index (index)}
							<li>{JSON.stringify(item).slice(0, 160)}</li>
						{/each}
						{#if !report.blocked_items.length}<li class="opacity-60">—</li>{/if}
					</ul>
				</div>
			</div>

			<!-- Jev decisions (structured, never a chat) -->
			<div class="mt-4">
				<h3 class="text-xs font-medium opacity-70">jev_decisions（{jev.length}）</h3>
				{#if !jev.length}
					<div class="mt-1 text-xs opacity-60">本轮无 Jev 决策。</div>
				{:else}
					<table class="mt-1 w-full text-left text-xs">
						<thead class="opacity-60"><tr><th>question</th><th>answer</th><th>confidence</th><th>decision</th></tr></thead>
						<tbody>
							{#each jev as decision, index (index)}
								{@const answers = (decision.answers ?? {}) as Record<string, Record<string, unknown>>}
								{#each Object.entries(answers) as [question, answer] (question)}
									<tr>
										<td>{question}</td>
										<td>{String(answer.choice ?? answer.score ?? answer.noul ?? "—")}</td>
										<td>{answer.confidence == null ? "—" : Number(answer.confidence).toFixed(2)}</td>
										<td>{String(decision.decision ?? decision.action ?? "advisory")}</td>
									</tr>
								{/each}
							{/each}
						</tbody>
					</table>
				{/if}
			</div>
		{/if}
	</section>

	<!-- C. Review timeline -->
	<section class="card p-5">
		<h2 class="mb-3 text-sm font-semibold uppercase tracking-wide opacity-70">Review Timeline</h2>
		{#if !reviews.length}
			<div class="text-sm opacity-70">尚无审查记录。</div>
		{:else}
			<ol class="space-y-3">
				{#each reviews as entry (String(entry.iteration))}
					{@const review = entry.review}
					<li class="rounded border p-3 text-sm">
						<div class="flex items-center justify-between">
							<span class="font-medium">#{String(entry.iteration)} · {String(review.supervisor)} · {String(review.decision)}</span>
							<span class="text-xs opacity-60">{formatTime(Number(review.created_at))}</span>
						</div>
						{#if review.reason}<div class="mt-1 text-xs opacity-80">reason：{String(review.reason)}</div>{/if}
						{#if review.next_task}<div class="text-xs opacity-80">next_task：{String(review.next_task)}</div>{/if}
						{#if review.constraints_delta?.length}
							<div class="text-xs opacity-80">constraints_delta：{JSON.stringify(review.constraints_delta)}</div>
						{/if}
						{#if review.acceptance_delta?.length}
							<div class="text-xs opacity-80">acceptance_delta：{JSON.stringify(review.acceptance_delta)}</div>
						{/if}
						<ul class="mt-2 space-y-0.5 text-xs opacity-60">
							{#each entry.events as event, index (index)}
								<li>{formatTime(Number(event.ts))} · {eventLabel(event)}</li>
							{/each}
						</ul>
					</li>
				{/each}
			</ol>
		{/if}
	</section>

	<!-- D. Live events -->
	<section class="card p-5">
		<h2 class="mb-3 flex items-center justify-between text-sm font-semibold uppercase tracking-wide opacity-70">
			<span>Live Events（{store.state.events.length}）</span>
			{#if store.streamError}<span class="text-xs font-normal text-amber-400">流中断，已回落轮询：{store.streamError}</span>{/if}
		</h2>
		<ul class="max-h-72 space-y-1 overflow-auto text-xs">
			{#each store.state.events as event, index (index)}
				<li><span class="opacity-50">{formatTime(Number(event.ts))}</span> · {eventLabel(event)}</li>
			{/each}
			{#if !store.state.events.length}<li class="opacity-60">尚无事件</li>{/if}
		</ul>
		<div class="mt-1 text-xs opacity-50">最后更新：{relativeTime(Date.now() / 1000)}</div>
	</section>
</div>
