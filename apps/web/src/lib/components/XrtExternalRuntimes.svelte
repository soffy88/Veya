<script lang="ts">
	import { onDestroy, onMount } from "svelte";
	import { api } from "$lib/api";

	type RuntimeState = "ready" | "installed_not_logged_in" | "not_installed" | "disabled";
	interface RuntimeInfo {
		name: string;
		display_name: string;
		state: RuntimeState;
		installed: boolean;
		version: string | null;
		logged_in: boolean;
	}
	interface ExecutionInfo {
		execution_id: string;
		runtime_name: string;
		status: string;
		exit_code: number | null;
		stdout: string;
		stderr: string;
		git_diff: string;
		worktree_path: string;
	}

	let visible = $state(false);
	let runtimes = $state<RuntimeInfo[]>([]);
	let selectedRuntime = $state("");
	let taskText = $state("");
	let loading = $state(false);
	let error = $state("");
	let execution = $state<ExecutionInfo | null>(null);
	let destroyed = false;

	const stateLabels: Record<RuntimeState, string> = {
		ready: "就绪",
		installed_not_logged_in: "未登录",
		not_installed: "未安装",
		disabled: "已禁用"
	};

	async function loadRuntimes() {
		const result = await api("gateway", "api/v1/xrt/runtimes", { method: "GET" });
		if (!result.ok) return;
		const data = result.data as { enabled?: boolean; runtimes?: RuntimeInfo[] };
		if (!data.enabled) return;
		visible = true;
		runtimes = data.runtimes ?? [];
		if (!selectedRuntime) {
			selectedRuntime = runtimes.find((item) => item.state === "ready")?.name ??
				runtimes.find((item) => item.state !== "disabled")?.name ?? "";
		}
	}

	async function pollStatus(id: string) {
		while (!destroyed) {
			await new Promise((resolve) => setTimeout(resolve, 750));
			if (destroyed) return;
			const result = await api("gateway", `api/v1/xrt/status/${encodeURIComponent(id)}`, { method: "GET" });
			if (!result.ok) {
				error = `读取执行状态失败 (HTTP ${result.status})`;
				return;
			}
			execution = result.data as ExecutionInfo;
			if (execution.status !== "RUNNING") return;
		}
	}

	async function dispatchTask() {
		if (!taskText.trim() || !selectedRuntime || loading) return;
		loading = true;
		error = "";
		execution = null;
		try {
			const result = await api("gateway", "api/v1/xrt/dispatch", {
				method: "POST",
				body: { runtime: selectedRuntime, task: taskText.trim() }
			});
			if (!result.ok) {
				const detail = (result.data as { detail?: string } | null)?.detail;
				throw new Error(detail ?? `请求失败 (HTTP ${result.status})`);
			}
			execution = result.data as ExecutionInfo;
			if (execution.status === "RUNNING") await pollStatus(execution.execution_id);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	async function cancelExecution() {
		if (!execution || execution.status !== "RUNNING") return;
		const result = await api("gateway", `api/v1/xrt/cancel/${encodeURIComponent(execution.execution_id)}`, { method: "POST" });
		if (!result.ok) {
			error = `取消失败 (HTTP ${result.status})`;
			return;
		}
		execution = { ...execution, status: "CANCELLED" };
	}

	onMount(() => { void loadRuntimes().catch(() => undefined); });
	onDestroy(() => { destroyed = true; });
</script>

{#if visible}
	<section class="rounded-xl border border-white/10 bg-slate-950/50 p-4 space-y-4" aria-label="外部客户端运行时">
		<div class="flex items-start justify-between gap-3">
			<div>
				<h2 class="text-base font-semibold text-terminal-fg">外部客户端</h2>
				<p class="mt-1 text-xs text-white/50">使用本机已有的 CLI 登录；任务在独立 worktree 中执行。</p>
			</div>
			<button class="rounded-lg border border-white/15 px-3 py-1.5 text-xs text-terminal-fg" type="button" onclick={() => void loadRuntimes()}>刷新</button>
		</div>

		<div class="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
			{#each runtimes as runtime (runtime.name)}
				<div class="rounded-lg border border-white/10 bg-white/[0.03] p-3">
					<div class="flex items-center justify-between gap-2">
						<span class="text-sm text-terminal-fg">{runtime.display_name}</span>
						<span class="rounded-full bg-white/10 px-2 py-0.5 text-[10px] text-white/70">{stateLabels[runtime.state]}</span>
					</div>
					<div class="mt-1 truncate text-[11px] text-white/40">{runtime.version ?? runtime.name}</div>
				</div>
			{/each}
		</div>

		<div class="space-y-2">
			<label class="block text-xs text-white/60" for="xrt-runtime-select">运行时</label>
			<select id="xrt-runtime-select" class="w-full rounded-lg border border-white/15 bg-slate-900 px-3 py-2 text-sm text-terminal-fg" bind:value={selectedRuntime}>
				{#each runtimes.filter((item) => item.state === "ready") as runtime (runtime.name)}
					<option value={runtime.name}>{runtime.display_name}</option>
				{/each}
			</select>
			<label class="block text-xs text-white/60" for="xrt-task-text">任务</label>
			<textarea id="xrt-task-text" class="min-h-24 w-full rounded-lg border border-white/15 bg-slate-900 px-3 py-2 text-sm text-terminal-fg" bind:value={taskText} placeholder="描述需要执行的代码任务…"></textarea>
			<div class="flex flex-wrap items-center gap-2">
				<button class="rounded-lg bg-sky-600 px-4 py-2 text-sm text-white disabled:opacity-40" type="button" disabled={loading || !taskText.trim() || !selectedRuntime || !runtimes.some((item) => item.name === selectedRuntime && item.state === "ready")} onclick={() => void dispatchTask()}>{loading ? "执行中…" : "派发任务"}</button>
				{#if execution?.status === "RUNNING"}
					<button class="rounded-lg border border-rose-400/40 px-3 py-2 text-sm text-rose-200" type="button" onclick={() => void cancelExecution()}>取消</button>
				{/if}
			</div>
			{#if error}<p class="text-sm text-rose-300" role="alert">{error}</p>{/if}
		</div>

		{#if execution}
			<div class="space-y-2 rounded-lg border border-white/10 p-3">
				<div class="text-xs text-white/60">{execution.execution_id} · {execution.status} · exit {execution.exit_code ?? "—"}</div>
				{#if execution.stdout}<pre class="max-h-48 overflow-auto whitespace-pre-wrap text-xs text-emerald-200">{execution.stdout}</pre>{/if}
				{#if execution.stderr}<pre class="max-h-32 overflow-auto whitespace-pre-wrap text-xs text-rose-200">{execution.stderr}</pre>{/if}
				{#if execution.git_diff}<details><summary class="cursor-pointer text-xs text-sky-200">查看 worktree diff</summary><pre class="mt-2 max-h-80 overflow-auto whitespace-pre-wrap text-xs text-white/70">{execution.git_diff}</pre></details>{/if}
				{#if execution.worktree_path}<p class="break-all text-[11px] text-white/40">{execution.worktree_path}</p>{/if}
			</div>
		{/if}
	</section>
{/if}
