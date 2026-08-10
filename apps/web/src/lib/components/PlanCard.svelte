<script lang="ts">
	/**
	 * PlanCard — 计划卡片渲染 (PlanBoard 看板 + ChatConsole 活跃计划条复用)。
	 */
	import { CheckCircle2 } from "lucide-svelte";
	import type { PlanSnap } from "$lib/planStore.svelte";

	interface Props {
		plan: {
			plan_id: string;
			objective: string;
			updated_at?: string;
			todos: { id: string; title: string; status: string; depends_on?: string[]; evidence?: { note?: string }[] }[];
			quota?: { action?: string; reason?: string };
		};
		compact?: boolean; // ChatConsole 顶部条: 只显示进度 + 状态机一行
		statusCls?: (s: string) => string;
	}

	let { plan, compact = false }: Props = $props();

	const MARK: Record<string, string> = { done: "✅", in_progress: "▶️", blocked: "⛔", open: "⬜" };
	const STATUS_CLS: Record<string, string> = {
		done: "text-emerald-400 border-emerald-500/30 bg-emerald-500/10",
		in_progress: "text-sky-400 border-sky-500/30 bg-sky-500/10",
		blocked: "text-rose-400 border-rose-500/30 bg-rose-500/10",
		open: "text-white/60 border-white/10 bg-white/5",
	};

	function cls(s: string): string {
		return STATUS_CLS[s] ?? STATUS_CLS.open;
	}

	function depLabel(t: { id: string; title: string; status: string; depends_on?: string[] }): string {
		return (t.depends_on ?? []).join(" → ") || "—";
	}

	function quotaCls(a?: string): string {
		if (a === "deliver") return "text-emerald-400";
		if (a === "repair") return "text-amber-400";
		return "text-white/35";
	}

	const done = $derived(plan.todos.filter((t) => t.status === "done").length);
	const pct = $derived(plan.todos.length ? (done / plan.todos.length) * 100 : 0);
</script>

<div class="rounded-xl border border-white/10 bg-[#0d0d0d]">
	<div class="flex items-center gap-2 px-3 py-2">
		<span class="min-w-0 flex-1 truncate text-[13px] font-semibold text-terminal-fg">
			📋 {plan.objective}
		</span>
		<span class="shrink-0 font-mono text-[10px] text-white/50">{done}/{plan.todos.length}</span>
		<span class="h-1.5 w-16 shrink-0 overflow-hidden rounded-full bg-white/10">
			<span class="block h-full rounded-full bg-sky-500 transition-all" style="width: {pct}%"></span>
		</span>
	</div>

	{#if !compact}
		<div class="border-t border-white/5 px-3 py-2">
			<div class="flex flex-col gap-1.5">
				{#each plan.todos as t (t.id)}
					<div class="flex items-start gap-2 rounded-lg border px-2.5 py-1.5 {cls(t.status)}">
						<span class="shrink-0">{MARK[t.status] ?? "⬜"}</span>
						<div class="min-w-0 flex-1">
							<div class="flex flex-wrap items-center gap-1.5">
								<span class="font-mono text-[10px] text-white/45">[{t.id}]</span>
								<span class="text-[12px] text-terminal-fg">{t.title}</span>
							</div>
							<div class="mt-0.5 font-mono text-[9px] text-white/30">依赖: {depLabel(t)}</div>
							{#if t.evidence && t.evidence.length > 0}
								<div class="mt-0.5 flex flex-col gap-0.5">
									{#each t.evidence as ev (ev.note)}
										<span class="flex items-center gap-1 font-mono text-[9px] text-emerald-400/70">
											<CheckCircle2 class="size-2" /> {ev.note ?? ""}
										</span>
									{/each}
								</div>
							{/if}
						</div>
					</div>
				{/each}
			</div>
			{#if plan.quota}
				<div class="mt-1.5 font-mono text-[9px] {quotaCls(plan.quota.action)}">
					quota: {plan.quota.action ?? "unknown"} · {plan.quota.reason ?? ""}
				</div>
			{/if}
		</div>
	{:else}
		<div class="flex flex-wrap gap-1 border-t border-white/5 px-3 py-1.5">
			{#each plan.todos as t (t.id)}
				<span class="rounded border px-1.5 py-0.5 font-mono text-[9px] {cls(t.status)}">
					{MARK[t.status] ?? "⬜"} {t.id}
				</span>
			{/each}
		</div>
	{/if}
</div>
