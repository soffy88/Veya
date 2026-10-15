<script lang="ts">
	/**
	 * XRTPanels — External Runtimes management panel for Veya web UI.
	 * Lists runtime availability, status badge, execution dispatch and output streaming.
	 */
	import { onMount } from "svelte";
	import { Bot, CheckCircle2, CircleAlert, Loader2, Play, RefreshCw, Terminal, XCircle } from "lucide-svelte";
	import { API_BASE } from "$lib/api";

	interface RuntimeInfo {
		name: string;
		display_name: string;
		state: "ready" | "installed_not_logged_in" | "not_installed" | "disabled";
		installed: boolean;
		version: string | null;
		logged_in: boolean;
		path: string | null;
	}

	let runtimes = $state<RuntimeInfo[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let selectedRuntime = $state<string>("claude_code");
	let taskText = $state("");
	let dispatching = $state(false);
	let executionResult = $state<any>(null);

	async function fetchRuntimes() {
		loading = true;
		error = null;
		try {
			const res = await fetch(`${API_BASE}/api/v1/xrt/runtimes`, {
				headers: { "content-type": "application/json" },
			});
			if (!res.ok) {
				if (res.status === 404) {
					error = "外部运行时 (XRT) 未启用 (VEYA_XRT_RUNTIMES=0)";
				} else if (res.status === 403) {
					error = "需要 Owner 权限访问外部运行时管理";
				} else {
					error = `加载失败 (HTTP ${res.status})`;
				}
				runtimes = [];
				return;
			}
			const data = await res.json();
			runtimes = data.runtimes ?? [];
		} catch (e: any) {
			error = e?.message ?? "网络错误";
		} finally {
			loading = false;
		}
	}

	async function dispatchTask() {
		if (!taskText.trim() || dispatching) return;
		dispatching = true;
		executionResult = null;
		try {
			const res = await fetch(`${API_BASE}/api/v1/xrt/dispatch`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ runtime: selectedRuntime, task: taskText.trim() }),
			});
			const data = await res.json();
			if (!res.ok) {
				throw new Error(data.detail ?? `HTTP ${res.status}`);
			}
			executionResult = data;
		} catch (e: any) {
			executionResult = { error: e?.message ?? "分派失败" };
		} finally {
			dispatching = false;
		}
	}

	onMount(() => {
		void fetchRuntimes();
	});

	function badgeClass(state: string) {
		switch (state) {
			case "ready":
				return "bg-emerald-500/10 text-emerald-400 border-emerald-500/30";
			case "installed_not_logged_in":
				return "bg-amber-500/10 text-amber-400 border-amber-500/30";
			case "not_installed":
				return "bg-zinc-500/10 text-zinc-400 border-zinc-500/30";
			case "disabled":
				return "bg-rose-500/10 text-rose-400 border-rose-500/30";
			default:
				return "bg-zinc-500/10 text-zinc-400 border-zinc-500/30";
		}
	}

	function badgeText(state: string) {
		switch (state) {
			case "ready":
				return "就绪";
			case "installed_not_logged_in":
				return "未登录";
			case "not_installed":
				return "未安装";
			case "disabled":
				return "已禁用";
			default:
				return state;
		}
	}
</script>

<div class="flex h-full flex-col gap-6 p-6 text-terminal-fg">
	<div class="flex items-center justify-between border-b border-terminal-edge pb-4">
		<div>
			<h1 class="text-xl font-bold tracking-tight">外部编码客户端 (External Runtimes)</h1>
			<p class="text-sm text-terminal-dim">统一纳管本机已登录的第三方编码 Agent CLI（Devin、Claude Code、Codex、OpenCode 等）</p>
		</div>
		<button
			type="button"
			onclick={fetchRuntimes}
			disabled={loading}
			class="flex items-center gap-2 rounded-lg border border-terminal-edge bg-white/5 px-3 py-1.5 text-xs font-medium text-terminal-fg transition hover:bg-white/10 disabled:opacity-50"
		>
			<RefreshCw class={`size-3.5 ${loading ? "animate-spin" : ""}`} />
			刷新状态
		</button>
	</div>

	{#if error}
		<div class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-300">
			<div class="flex items-center gap-2 font-semibold">
				<CircleAlert class="size-4" />
				提示
			</div>
			<p class="mt-1">{error}</p>
			<p class="mt-2 text-xs text-rose-300/80">
				提示：外部运行时需要设置环境变量 <code class="rounded bg-black/30 px-1 py-0.5">VEYA_XRT_RUNTIMES=1</code> 方可启用。
			</p>
		</div>
	{/if}

	<div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
		<!-- Runtime List Card -->
		<div class="rounded-xl border border-terminal-edge bg-terminal-card p-5 lg:col-span-1">
			<h2 class="text-sm font-semibold text-terminal-fg">客户端实例状态</h2>
			<div class="mt-4 flex flex-col gap-3">
				{#if loading && runtimes.length === 0}
					<div class="flex items-center justify-center py-8 text-terminal-dim">
						<Loader2 class="size-5 animate-spin" />
					</div>
				{:else if runtimes.length === 0}
					<p class="text-xs text-terminal-dim">无可用运行时，或 XRT 系统未开启。</p>
				{:else}
					{#each runtimes as rt}
						<button
							type="button"
							onclick={() => { selectedRuntime = rt.name; }}
							class={`flex items-center justify-between rounded-lg border p-3 text-left transition ${selectedRuntime === rt.name ? "border-violet-500/60 bg-violet-500/10" : "border-terminal-edge bg-white/2 hover:bg-white/5"}`}
						>
							<div>
								<div class="font-medium text-terminal-fg">{rt.display_name}</div>
								<div class="text-[11px] text-terminal-dim">{rt.path ?? "未在 PATH 中找到"}</div>
								{#if rt.version}
									<div class="mt-0.5 text-[10px] text-violet-400 font-mono">{rt.version}</div>
								{/if}
							</div>
							<span class={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${badgeClass(rt.state)}`}>
								{badgeText(rt.state)}
							</span>
						</button>
					{/each}
				{/if}
			</div>
		</div>

		<!-- Dispatch Task Card -->
		<div class="flex flex-col rounded-xl border border-terminal-edge bg-terminal-card p-5 lg:col-span-2">
			<h2 class="text-sm font-semibold text-terminal-fg">隔离 Worktree 任务派发</h2>
			<p class="mt-1 text-xs text-terminal-dim">
				当前选中: <span class="font-semibold text-violet-400">{selectedRuntime}</span>
			</p>

			<div class="mt-4 flex flex-col gap-4 flex-1">
				<div>
					<label for="task-input" class="block text-xs font-medium text-terminal-dim">任务说明 / Prompt</label>
					<textarea
						id="task-input"
						bind:value={taskText}
						rows="4"
						placeholder="输入派发给该客户端的编码任务指令..."
						class="mt-1.5 w-full rounded-lg border border-terminal-edge bg-terminal-bg p-3 text-sm text-terminal-fg placeholder:text-terminal-dim focus:border-violet-500 focus:outline-none"
					></textarea>
				</div>

				<div class="flex items-center justify-between">
					<span class="text-[11px] text-terminal-dim">非交互模式运行，自动产出 Worktree Git Diff 与产物。</span>
					<button
						type="button"
						onclick={dispatchTask}
						disabled={dispatching || !taskText.trim()}
						class="flex items-center gap-2 rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white transition hover:bg-violet-500 disabled:opacity-50"
					>
						{#if dispatching}
							<Loader2 class="size-3.5 animate-spin" />
							正在执行...
						{:else}
							<Play class="size-3.5" />
							派发任务
						{/if}
					</button>
				</div>

				{#if executionResult}
					<div class="mt-4 flex flex-col gap-2 rounded-lg border border-terminal-edge bg-terminal-bg p-4">
						<div class="flex items-center justify-between border-b border-terminal-edge pb-2">
							<span class="text-xs font-semibold text-terminal-fg">执行结果 ({executionResult.execution_id ?? "N/A"})</span>
							<span class={`rounded px-1.5 py-0.5 text-[10px] font-bold ${executionResult.status === "COMPLETED" ? "bg-emerald-500/10 text-emerald-400" : "bg-rose-500/10 text-rose-400"}`}>
								{executionResult.status ?? "ERROR"} (Exit: {executionResult.exit_code ?? -1})
							</span>
						</div>
						{#if executionResult.error}
							<p class="text-xs text-rose-400">{executionResult.error}</p>
						{:else}
							<div class="grid grid-cols-1 gap-2 md:grid-cols-2">
								<div>
									<span class="text-[10px] text-terminal-dim">Stdout 输出尾部:</span>
									<pre class="mt-1 max-h-40 overflow-auto rounded bg-black/40 p-2 text-[11px] font-mono text-terminal-fg">{executionResult.stdout || "(无输出)"}</pre>
								</div>
								<div>
									<span class="text-[10px] text-terminal-dim">Worktree Git Diff:</span>
									<pre class="mt-1 max-h-40 overflow-auto rounded bg-black/40 p-2 text-[11px] font-mono text-emerald-300">{executionResult.git_diff || "(无代码变更)"}</pre>
								</div>
							</div>
						{/if}
					</div>
				{/if}
			</div>
		</div>
	</div>
</div>
