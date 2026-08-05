<script lang="ts">
	/**
	 * AutomationPanel — list / create / toggle / delete cron schedules via the L4 gateway
	 * (POST /api/v1/scheduler, see old capabilities.ts "scheduler" cap).
	 */
	import { onMount } from "svelte";
	import { Loader2, Clock, Trash2, Plus } from "lucide-svelte";
	import { api } from "$lib/api";

	interface Schedule {
		id: string;
		name: string;
		enabled: boolean;
		phase: string;
		run_count: number;
	}

	let schedules = $state<Schedule[]>([]);
	let loading = $state(true);
	let error = $state("");
	let busy = $state<string | null>(null);

	let newName = $state("");
	let newPrompt = $state("");
	let newCron = $state("0 9 * * *");
	let creating = $state(false);

	async function refresh() {
		loading = true;
		error = "";
		const res = await api("gateway", "api/v1/scheduler", { body: { action: "list" } });
		const data = res.data as { schedules?: Schedule[] };
		if (!res.ok) {
			error = "加载定时任务失败";
		} else {
			schedules = data.schedules ?? [];
		}
		loading = false;
	}

	async function toggle(s: Schedule) {
		busy = s.id;
		await api("gateway", "api/v1/scheduler", { body: { action: "toggle", id: s.id, enabled: !s.enabled } });
		await refresh();
		busy = null;
	}

	async function del(s: Schedule) {
		busy = s.id;
		await api("gateway", "api/v1/scheduler", { body: { action: "delete", id: s.id } });
		await refresh();
		busy = null;
	}

	async function create() {
		if (!newName.trim() || !newPrompt.trim()) return;
		creating = true;
		await api("gateway", "api/v1/scheduler", {
			body: { action: "create", name: newName.trim(), prompt: newPrompt.trim(), cron: newCron.trim() },
		});
		newName = "";
		newPrompt = "";
		await refresh();
		creating = false;
	}

	onMount(refresh);
</script>

<div class="flex flex-col gap-4">
	<div class="flex flex-col gap-2 rounded-lg border border-terminal-edge bg-terminal-bg p-3">
		<div class="flex gap-2">
			<div class="flex min-w-0 flex-1 flex-col gap-1">
				<label class="text-xs text-terminal-dim" for="sched-name">任务名</label>
				<input id="sched-name" bind:value={newName} placeholder="例如 日报" class="rounded-lg border border-terminal-edge bg-terminal-panel px-2 py-1.5 text-sm text-terminal-fg outline-none focus:border-sky-500/60" />
			</div>
			<div class="flex w-36 flex-col gap-1">
				<label class="text-xs text-terminal-dim" for="sched-cron">cron 表达式</label>
				<input id="sched-cron" bind:value={newCron} class="rounded-lg border border-terminal-edge bg-terminal-panel px-2 py-1.5 text-sm text-terminal-fg outline-none focus:border-sky-500/60" />
			</div>
		</div>
		<div class="flex flex-col gap-1">
			<label class="text-xs text-terminal-dim" for="sched-prompt">任务内容</label>
			<textarea id="sched-prompt" bind:value={newPrompt} rows="2" placeholder="到点后要执行的任务描述" class="resize-none rounded-lg border border-terminal-edge bg-terminal-panel px-2 py-1.5 text-sm text-terminal-fg outline-none focus:border-sky-500/60"></textarea>
		</div>
		<button
			type="button"
			onclick={create}
			disabled={creating || !newName.trim() || !newPrompt.trim()}
			class="flex w-fit items-center gap-1.5 rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 px-3 py-1.5 text-sm font-medium text-white transition hover:brightness-110 disabled:opacity-40"
		>
			{#if creating}<Loader2 class="size-4 animate-spin" />{:else}<Plus class="size-4" />{/if}
			新建定时任务
		</button>
	</div>

	{#if loading}
		<div class="flex items-center gap-2 text-sm text-terminal-dim"><Loader2 class="size-4 animate-spin" /> 加载中…</div>
	{:else if error}
		<div class="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3 text-sm text-rose-300">{error}</div>
	{:else if schedules.length === 0}
		<div class="flex flex-col items-center gap-2 py-10 text-terminal-dim">
			<Clock class="size-6 opacity-40" />
			<p class="text-sm">还没有定时任务</p>
		</div>
	{:else}
		<ul class="flex flex-col gap-2">
			{#each schedules as s (s.id)}
				<li class="flex items-center gap-3 rounded-lg border border-terminal-edge bg-terminal-bg p-3">
					<div class="min-w-0 flex-1">
						<div class="text-sm font-medium text-terminal-fg">{s.name}</div>
						<div class="text-xs text-terminal-dim">{s.phase} · 已运行 {s.run_count} 次</div>
					</div>
					<button
						type="button"
						onclick={() => toggle(s)}
						disabled={busy === s.id}
						class="rounded-md border px-2.5 py-1 text-xs font-medium transition {s.enabled ? 'border-emerald-500/40 text-emerald-400' : 'border-terminal-edge text-terminal-dim'}"
					>
						{s.enabled ? "已启用" : "已停用"}
					</button>
					<button
						type="button"
						onclick={() => del(s)}
						disabled={busy === s.id}
						class="flex size-7 items-center justify-center rounded-md text-terminal-dim transition hover:bg-rose-500/10 hover:text-rose-300"
						aria-label="删除"
					>
						<Trash2 class="size-4" />
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>
