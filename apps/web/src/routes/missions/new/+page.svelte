<script lang="ts">
	/**
	 * /missions/new — three-step create: goal → 执行方式 → 开始.
	 *
	 * The mission is created *then* started (canonical create + run) and only
	 * navigates on success; both steps are guarded against double submits, so a
	 * double click can never create or dispatch twice.
	 */
	import { goto } from "$app/navigation";
	import { Loader2, Play } from "lucide-svelte";
	import { createMission, runMission } from "$lib/supervision/api";
	import type { ExecutorKind, SupervisionMode } from "$lib/supervision/types";

	let goal = $state("");
	let mode = $state<SupervisionMode>("auto");
	let executor = $state<ExecutorKind | "">("");
	let workspace = $state("");
	let acceptance = $state("");
	let constraints = $state("");
	let advanced = $state(false);
	let busy = $state(false);
	let error = $state("");

	const MODES: Array<{ value: SupervisionMode; label: string; hint: string }> = [
		{ value: "auto", label: "自动", hint: "由 Veya 决定监督者（可运行时切换）" },
		{ value: "external", label: "ChatGPT 监督", hint: "每轮等你/ChatGPT 审查后再继续" },
		{ value: "internal", label: "Veya 自主", hint: "Veya 自主设计、执行、审查、返工" },
	];

	const EXECUTORS: Array<{ value: ExecutorKind | ""; label: string }> = [
		{ value: "", label: "自动" },
		{ value: "hicode", label: "Hicode" },
		{ value: "dsh", label: "DSH" },
	];

	function lines(value: string): string[] {
		return value
			.split("\n")
			.map((line) => line.trim())
			.filter(Boolean);
	}

	async function submit(): Promise<void> {
		if (busy) return; // double-submit guard
		if (!goal.trim()) {
			error = "请填写任务目标";
			return;
		}
		busy = true;
		error = "";
		const created = await createMission({
			goal: goal.trim(),
			supervision_mode: mode,
			executor,
			workspace: workspace.trim(),
			acceptance_criteria: lines(acceptance),
			constraints: lines(constraints),
		});
		if (!created.ok || !created.data) {
			error = created.error ?? "创建失败";
			busy = false;
			return; // never navigate on failure
		}
		const missionId = created.data.mission.mission_id;
		const started = await runMission(missionId);
		if (!started.ok) {
			// The mission exists; go to it and surface the honest error there.
			await goto(`/missions/${missionId}`);
			return;
		}
		await goto(`/missions/${missionId}`);
	}
</script>

<div class="mx-auto w-full max-w-2xl p-6">
	<h1 class="mb-1 text-2xl font-semibold">新建任务</h1>
	<p class="mb-6 text-sm opacity-70">写目标、选执行方式，其余交给 Veya 的监督运行时。</p>

	<div class="card space-y-5 p-5">
		<label class="block">
			<span class="mb-1 block text-sm font-medium">任务目标 *</span>
			<textarea
				class="input w-full"
				rows="3"
				bind:value={goal}
				placeholder="例如：给 /data/soffy/projects/veya 增加一个导出 CSV 的接口并跑通测试"
			></textarea>
		</label>

		<fieldset>
			<legend class="mb-2 text-sm font-medium">执行方式</legend>
			<div class="space-y-2">
				{#each MODES as option (option.value)}
					<label class="flex cursor-pointer items-start gap-3 rounded border p-3">
						<input type="radio" name="mode" value={option.value} bind:group={mode} />
						<span>
							<span class="block text-sm">{option.label}</span>
							<span class="block text-xs opacity-70">{option.hint}</span>
						</span>
					</label>
				{/each}
			</div>
		</fieldset>

		<label class="block">
			<span class="mb-1 block text-sm font-medium">执行器</span>
			<select class="input w-full" bind:value={executor}>
				{#each EXECUTORS as option (option.value)}
					<option value={option.value}>{option.label}</option>
				{/each}
			</select>
		</label>

		<button class="text-xs underline opacity-70" onclick={() => (advanced = !advanced)}>
			{advanced ? "收起高级设置" : "高级设置"}
		</button>

		{#if advanced}
			<div class="space-y-4 border-t pt-4">
				<label class="block">
					<span class="mb-1 block text-sm font-medium">workspace</span>
					<input class="input w-full" bind:value={workspace} placeholder="/data/soffy/projects/<repo>" />
					<span class="mt-1 block text-xs opacity-70">
						留空使用服务端默认授权 workspace；未授权的路径会被后端拒绝（403）。
					</span>
				</label>
				<label class="block">
					<span class="mb-1 block text-sm font-medium">验收标准（每行一条）</span>
					<textarea class="input w-full" rows="2" bind:value={acceptance}></textarea>
				</label>
				<label class="block">
					<span class="mb-1 block text-sm font-medium">约束条件（每行一条）</span>
					<textarea class="input w-full" rows="2" bind:value={constraints}></textarea>
				</label>
			</div>
		{/if}

		{#if error}
			<div class="rounded border border-red-500/40 p-3 text-sm text-red-400">{error}</div>
		{/if}

		<button class="btn btn-primary w-full" onclick={() => void submit()} disabled={busy}>
			{#if busy}
				<Loader2 size={16} class="animate-spin" /> 正在创建并启动…
			{:else}
				<Play size={16} /> 开始
			{/if}
		</button>
	</div>
</div>
