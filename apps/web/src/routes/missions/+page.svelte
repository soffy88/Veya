<script lang="ts">
	/**
	 * /missions — Mission list (read-only view over the canonical store).
	 *
	 * Loading only calls `store.load()` (GET). Nothing on this page can start an
	 * execution; refresh simply re-reads the same missions.
	 */
	import { onMount } from "svelte";
	import { Plus, RefreshCw, ArrowRight } from "lucide-svelte";
	import { createMissionListStore } from "$lib/supervision/store.svelte";
	import type { Mission } from "$lib/supervision/types";
	import {
		statusLabel,
		statusTone,
		supervisionModeLabel,
		executorLabel,
		relativeTime,
	} from "$lib/supervision/format";

	const store = createMissionListStore();

	onMount(() => {
		void store.load();
	});

	function executorOf(mission: Mission): string {
		const execution = mission.policies?.execution_policy ?? {};
		return executorLabel(String(execution.assignee_hint ?? ""));
	}

	function blockingReason(mission: Mission): string {
		const authority = (mission.authority ?? {}) as Record<string, unknown>;
		return String(authority.block_reason ?? authority.blocking_reason ?? "");
	}
</script>

<div class="mx-auto w-full max-w-5xl p-6">
	<header class="mb-6 flex items-center justify-between">
		<div>
			<h1 class="text-2xl font-semibold">Work 历史</h1>
			<p class="text-sm opacity-70">查看长期工作的真实状态、阻塞原因和结果。</p>
		</div>
		<div class="flex items-center gap-2">
			<button class="btn" onclick={() => void store.load()} disabled={store.loading}>
				<RefreshCw size={16} class={store.loading ? "animate-spin" : ""} /> 刷新
			</button>
			<a class="btn btn-primary" href="/missions/new"><Plus size={16} /> 新建 Work</a>
		</div>
	</header>

	{#if store.error}
		<div class="card border-red-500/40 p-4 text-sm text-red-400">{store.error}</div>
	{:else if store.loading && !store.missions.length}
		<div class="card p-6 text-sm opacity-70">加载中…</div>
	{:else if !store.missions.length}
		<div class="card p-6 text-sm opacity-70">还没有 Work。点击「新建 Work」开始。</div>
	{:else}
		<ul class="space-y-3">
			{#each store.missions as mission (mission.mission_id)}
				<li class="card p-4">
					<div class="flex items-start justify-between gap-4">
						<div class="min-w-0">
							<div class="truncate font-medium">{mission.goal}</div>
							<div class="mt-1 flex flex-wrap items-center gap-2 text-xs opacity-70">
								<span class="badge" data-tone={statusTone(mission.status)}>
									{statusLabel(mission.status)}
								</span>




								<span>更新：{relativeTime(mission.updated_at)}</span>
							</div>
							<details class="mt-2">
								<summary class="cursor-pointer text-xs opacity-50">Developer details</summary>
								<div class="mt-1 grid gap-1 text-xs opacity-60">
									<span>supervision_mode: {mission.supervision_mode}</span>
									<span>active_supervisor: {String(mission.authority?.active_supervisor ?? mission.supervision_mode)}</span>
									<span>iteration: {String(mission.authority?.iteration ?? 0)}</span>
									<span>updated_at: {String(mission.updated_at)}</span>
								</div>
							</details>
							{#if blockingReason(mission)}
								<div class="mt-2 text-xs text-amber-400">受阻：{blockingReason(mission)}</div>
							{/if}
						</div>
						<a class="btn shrink-0" href={`/missions/${mission.mission_id}`}>
							打开 Work <ArrowRight size={14} />
						</a>
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</div>
