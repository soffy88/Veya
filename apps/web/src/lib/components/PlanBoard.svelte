<script lang="ts">
	/**
	 * PlanBoard — 计划看板 (状态内核控制面可视化, P1)。
	 *
	 * 展示所有计划 (plan_todo JSON): objective + 进度条 + todos 状态机
	 * (依赖箭头 + 证据链) + 控制面状态行 (quota / claim lease / spends)。
	 * 数据源: GET /api/v1/plan/list + /api/v1/plan/{id}。主脑零改动。
	 */
	import { ListTodo, RefreshCw, ChevronDown, ChevronRight, CheckCircle2, Loader2, CircleAlert } from "lucide-svelte";
	import { API_BASE } from "$lib/api";

	type Todo = {
		id: string;
		title: string;
		status: "open" | "in_progress" | "done" | "blocked";
		depends_on: string[];
		assignee?: string;
		claim?: { claimed_by?: string; lease_until?: number; claimed_at?: string };
		evidence?: { at?: string; note?: string }[];
	};

	type Plan = {
		plan_id: string;
		objective: string;
		updated_at: string;
		progress: { done: number; total: number };
		todos: Todo[];
		spends?: number;
		quota?: { should_run?: boolean | null; action?: string; reason?: string };
	};

	const MARK: Record<string, string> = { done: "✅", in_progress: "▶️", blocked: "⛔", open: "⬜" };
	const STATUS_CLS: Record<string, string> = {
		done: "text-emerald-400 border-emerald-500/30 bg-emerald-500/10",
		in_progress: "text-sky-400 border-sky-500/30 bg-sky-500/10",
		blocked: "text-rose-400 border-rose-500/30 bg-rose-500/10",
		open: "text-white/60 border-white/10 bg-white/5",
	};

	let plans = $state<Plan[]>([]);
	let loading = $state(false);
	let error = $state("");
	let expanded = $state<Set<string>>(new Set());

	async function refresh() {
		loading = true;
		error = "";
		try {
			const res = await fetch(`${API_BASE}/api/v1/plan/list`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = (await res.json()) as { plans: Plan[] };
			plans = data.plans ?? [];
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		void refresh();
	});

	function toggle(id: string) {
		const next = new Set(expanded);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		expanded = next;
	}

	function quotaLabel(q?: Plan["quota"]): { text: string; cls: string } {
		const action = q?.action ?? "unknown";
		if (action === "deliver") return { text: `quota: deliver · ${q?.reason ?? ""}`, cls: "text-emerald-400" };
		if (action === "repair") return { text: `quota: repair · ${q?.reason ?? ""}`, cls: "text-amber-400" };
		if (action === "wait") return { text: `quota: wait · ${q?.reason ?? ""}`, cls: "text-white/35" };
		return { text: `quota: ${action}`, cls: "text-white/35" };
	}

	function leaseInfo(t: Todo): string {
		const c = t.claim;
		if (!c) return "";
		if (c.lease_until) {
			const left = Math.max(0, Math.round((c.lease_until - Date.now() / 1000) / 60));
			return `🔒 ${c.claimed_by ?? "?"} 持有 ${left}min`;
		}
		return `🔒 ${c.claimed_by ?? "?"}`;
	}

	function depLabel(p: Plan, t: Todo): string {
		return (t.depends_on ?? [])
			.map((d) => {
				const dep = p.todos.find((x) => x.id === d);
				return dep ? `${d}(${dep.status === "done" ? "✓" : "…"})` : d;
			})
			.join(" → ") || "—";
	}
</script>

<div class="flex h-full min-h-0 flex-col">
	<!-- 头部 -->
	<div class="flex shrink-0 items-center gap-2 border-b border-white/5 px-4 py-2">
		<ListTodo class="size-4 text-sky-400" />
		<span class="font-mono text-[11px] uppercase tracking-wider text-white/40">计划看板 · 状态内核</span>
		<span class="flex-1"></span>
		<button
			type="button"
			onclick={refresh}
			disabled={loading}
			class="flex items-center gap-1.5 rounded-lg border border-white/10 px-2.5 py-1 font-mono text-xs text-white/60 transition hover:border-sky-500/40 hover:text-white disabled:opacity-40"
		>
			<RefreshCw class="size-3.5 {loading ? 'animate-spin' : ''}" />
			刷新
		</button>
	</div>

	<!-- 主体 -->
	<div class="min-h-0 flex-1 overflow-y-auto p-4">
		{#if error}
			<div class="flex items-center gap-2 rounded-lg bg-red-500/10 p-3 text-sm text-red-400">
				<CircleAlert class="size-4" /> {error}
			</div>
		{:else if loading && plans.length === 0}
			<div class="flex items-center gap-2 text-sm text-white/40"><Loader2 class="size-4 animate-spin" /> 加载计划…</div>
		{:else if plans.length === 0}
			<div class="flex flex-col items-center justify-center gap-2 py-16 text-center">
				<ListTodo class="size-8 text-white/20" />
				<p class="text-sm text-white/40">暂无计划</p>
				<p class="max-w-sm text-xs text-white/25">
					对主脑说「规划一个 XX 任务」— 模型会自主调用 create_plan 建计划，然后在这里实时看到进度。
				</p>
			</div>
		{:else}
			<div class="flex flex-col gap-4">
				{#each plans as p (p.plan_id)}
					{@const q = quotaLabel(p.quota)}
					<div class="rounded-xl border border-white/10 bg-[#0d0d0d]">
						<button
							type="button"
							onclick={() => toggle(p.plan_id)}
							class="flex w-full items-center gap-2 px-4 py-3 text-left"
						>
							{#if expanded.has(p.plan_id)}
								<ChevronDown class="size-4 shrink-0 text-white/40" />
							{:else}
								<ChevronRight class="size-4 shrink-0 text-white/40" />
							{/if}
							<span class="min-w-0 flex-1">
								<span class="block truncate text-sm font-semibold text-terminal-fg">📋 {p.objective}</span>
								<span class="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-white/35">
									<span>{p.plan_id.slice(0, 8)}</span>
									<span>{p.updated_at}</span>
									<span class={q.cls}>{q.text.slice(0, 60)}</span>
								</span>
							</span>
							<span class="flex shrink-0 items-center gap-2">
								<span class="font-mono text-[10px] text-white/50">
									{p.progress.done}/{p.progress.total}
								</span>
								<span class="h-1.5 w-20 overflow-hidden rounded-full bg-white/10">
									<span
										class="block h-full rounded-full bg-sky-500 transition-all"
										style="width: {p.progress.total ? (p.progress.done / p.progress.total) * 100 : 0}%"
									></span>
								</span>
							</span>
						</button>

						{#if expanded.has(p.plan_id)}
							<div class="border-t border-white/5 px-4 py-3">
								<div class="flex flex-col gap-2">
									{#each p.todos as t, i (t.id)}
										<div class="flex items-start gap-2 rounded-lg border px-3 py-2 {STATUS_CLS[t.status] ?? STATUS_CLS.open}">
											<span class="shrink-0">{MARK[t.status] ?? "⬜"}</span>
											<div class="min-w-0 flex-1">
												<div class="flex flex-wrap items-center gap-2">
													<span class="font-mono text-[11px] text-white/50">[{t.id}]</span>
													<span class="text-[13px] text-terminal-fg">{t.title}</span>
													{#if t.assignee}
														<span class="rounded bg-white/10 px-1.5 py-0.5 font-mono text-[9px] text-white/50">→ {t.assignee}</span>
													{/if}
													{#if leaseInfo(t)}
														<span class="rounded bg-amber-500/10 px-1.5 py-0.5 font-mono text-[9px] text-amber-400">{leaseInfo(t)}</span>
													{/if}
												</div>
												<div class="mt-0.5 font-mono text-[10px] text-white/35">
													依赖: {depLabel(p, t)}
												</div>
												{#if t.evidence && t.evidence.length > 0}
													<div class="mt-1 flex flex-col gap-0.5">
														{#each t.evidence as ev (ev.at)}
															<span class="flex items-center gap-1 font-mono text-[10px] text-emerald-400/70">
																<CheckCircle2 class="size-2.5" /> {ev.note ?? ""}
															</span>
														{/each}
													</div>
												{/if}
											</div>
										</div>
									{/each}
								</div>
								{#if (p.spends ?? 0) > 0}
									<div class="mt-2 font-mono text-[10px] text-white/30">已 spend: {p.spends} 笔（有效推进记账）</div>
								{/if}
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>
