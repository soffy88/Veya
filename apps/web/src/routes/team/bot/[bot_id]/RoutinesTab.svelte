<script lang="ts">
	/**
	 * Per-bot routines: standing jobs the bot runs on its own ("every 30m",
	 * "daily 09:00", or a cron expression), created here and run now on demand.
	 *
	 * Like memory, routines come from a sibling branch: this panel degrades to a
	 * sentence rather than a broken form when the endpoints are not deployed.
	 */
	import { onMount } from "svelte";
	import { Clock, Play, Plus, Trash2 } from "lucide-svelte";
	import {
		createRoutine,
		deleteRoutine,
		listRoutines,
		patchRoutine,
		runRoutineNow,
		type RoutineView,
	} from "$lib/team/api";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";

	let { botId, botName }: { botId: string; botName: string } = $props();

	let routines = $state<RoutineView[]>([]);
	let supported = $state(true);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state("");

	let objective = $state("");
	let schedule = $state("every 30m");
	let timezone = $state("Asia/Shanghai");

	async function load(): Promise<void> {
		loading = true;
		try {
			routines = await listRoutines(botId);
			supported = true;
			error = "";
		} catch {
			supported = false;
		} finally {
			loading = false;
		}
	}

	async function submit(): Promise<void> {
		if (!objective.trim() || busy) return;
		busy = true;
		error = "";
		try {
			await createRoutine(botId, {
				objective: objective.trim(),
				schedule: schedule.trim(),
				timezone: timezone.trim(),
				enabled: true,
			});
			objective = "";
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function setEnabled(routine: RoutineView, enabled: boolean): Promise<void> {
		busy = true;
		try {
			await patchRoutine(botId, routine.id, { enabled });
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function runNow(routine: RoutineView): Promise<void> {
		busy = true;
		error = "";
		try {
			await runRoutineNow(botId, routine.id);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function remove(routine: RoutineView): Promise<void> {
		busy = true;
		error = "";
		try {
			await deleteRoutine(botId, routine.id);
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	onMount(load);
</script>

<div class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
	{#if loading}
		<p class="text-sm opacity-60">加载定时任务…</p>
	{:else if !supported}
		<p class="{cardClass} text-sm opacity-70">
			这个部署还没有 routines 功能（端点未上线）。上线后就能让 {botName} 按计划自己干活。
		</p>
	{:else}
		<form
			class="{cardClass} space-y-2"
			onsubmit={(event) => {
				event.preventDefault();
				void submit();
			}}
		>
			<textarea
				class="{inputClass} min-h-20 resize-y py-2"
				placeholder="它每次该做什么（例如：汇总今天仓库的新 issue）"
				bind:value={objective}
			></textarea>
			<div class="grid grid-cols-2 gap-2">
				<input class={inputClass} placeholder="every 30m / daily 09:00 / cron" bind:value={schedule} />
				<input class={inputClass} placeholder="时区" bind:value={timezone} />
			</div>
			<button class={primaryClass} type="submit" disabled={busy || !objective.trim()}>
				<Plus size={16} /> 新建定时任务
			</button>
		</form>

		{#if error}<p class="text-xs text-red-300">{error}</p>{/if}

		{#if routines.length === 0}
			<p class="{cardClass} text-sm opacity-60">还没有定时任务。</p>
		{:else}
			<ul class="space-y-2">
				{#each routines as routine (routine.id)}
					<li class="{cardClass} flex items-start gap-2">
						<Clock size={16} class="mt-0.5 shrink-0 opacity-50" />
						<div class="min-w-0 flex-1">
							<p class="text-sm break-words">{routine.objective}</p>
							<p class="mt-0.5 text-xs opacity-60">
								{routine.schedule} · {routine.timezone} · {routine.enabled ? "启用中" : "已暂停"}
							</p>
						</div>
						<div class="flex shrink-0 gap-1">
							<button
								class={actionClass}
								onclick={() => void setEnabled(routine, !routine.enabled)}
								title={routine.enabled ? "暂停" : "启用"}
							>
								{routine.enabled ? "暂停" : "启用"}
							</button>
							<button class={actionClass} onclick={() => void runNow(routine)} title="立即跑一次">
								<Play size={14} />
							</button>
							<button class={actionClass} onclick={() => void remove(routine)} title="删除">
								<Trash2 size={14} />
							</button>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	{/if}
</div>
