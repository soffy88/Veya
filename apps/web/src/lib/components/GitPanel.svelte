<script lang="ts">
	/**
	 * GitPanel — 工作区 Git 面板 (P4, 借鉴 ccgui)。deny-by-default: 只读 status/diff;
	 * commit 需显式 message + 确认。AI 提交信息: 可选调主脑生成 (经 /api/v1/agent/run?engine=master 太重, 用简单生成端点)。
	 */
	import { GitBranch, RefreshCw, GitCommitHorizontal, CircleAlert, Loader2, FileText } from "lucide-svelte";
	import { API_BASE } from "$lib/api";

	type GitStatus = {
		branch: string;
		ahead: string;
		behind: string;
		files: { path: string; index: string; worktree: string }[];
		dirty: boolean;
		error?: string;
	};

	let status = $state<GitStatus | null>(null);
	let diff = $state("");
	let loading = $state(false);
	let error = $state("");
	let commitMsg = $state("");
	let committing = $state(false);
	let aiMsgLoading = $state(false);
	let picked = $state<string[]>([]);

	async function refresh() {
		loading = true;
		error = "";
		try {
			const res = await fetch(`${API_BASE}/api/v1/git/status`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			status = (await res.json()) as GitStatus;
			diff = "";
			picked = [];
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		void refresh();
	});

	async function loadDiff() {
		const q = picked.length === 1 ? `?path=${encodeURIComponent(picked[0])}` : "?stat=true";
		try {
			const res = await fetch(`${API_BASE}/api/v1/git/diff${q}`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			diff = ((await res.json()) as { diff: string }).diff;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function aiMessage() {
		aiMsgLoading = true;
		try {
			const res = await fetch(`${API_BASE}/api/v1/agent/run`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					text: "根据当前 git 变更写一条简洁的中文 commit message (一句话, 含类型前缀如 feat/fix/docs)。",
					engine: "master",
				}),
			});
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = (await res.json()) as { result?: string };
			commitMsg = String(data.result ?? "").trim().replace(/^["'“”]+|["'“”]+$/g, "").slice(0, 200);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			aiMsgLoading = false;
		}
	}

	async function commit() {
		if (!commitMsg.trim() || committing) return;
		committing = true;
		error = "";
		try {
			const res = await fetch(`${API_BASE}/api/v1/git/commit`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ message: commitMsg, files: picked }),
			});
			if (!res.ok) {
				const detail = (await res.json().catch(() => ({}))) as { detail?: string };
				throw new Error(detail.detail ?? `HTTP ${res.status}`);
			}
			const data = (await res.json()) as { sha: string };
			commitMsg = "";
			await refresh();
			error = `✅ committed ${data.sha}`;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			committing = false;
		}
	}

	function toggleFile(p: string) {
		picked = picked.includes(p) ? picked.filter((x) => x !== p) : [...picked, p];
	}

	function idxLabel(c: string): string {
		const m: Record<string, string> = { M: "modified", A: "added", D: "deleted", R: "renamed", "??": "untracked" };
		return m[c] ?? c;
	}
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="flex shrink-0 items-center gap-2 border-b border-white/5 px-4 py-2">
		<GitBranch class="size-4 text-emerald-400" />
		<span class="font-mono text-[11px] uppercase tracking-wider text-white/40">Git</span>
		{#if status?.branch}
			<span class="rounded bg-emerald-500/10 px-2 py-0.5 font-mono text-[11px] text-emerald-400">{status.branch}</span>
			{#if status.ahead !== "0" || status.behind !== "0"}
				<span class="font-mono text-[10px] text-white/40">↑{status.ahead} ↓{status.behind}</span>
			{/if}
		{/if}
		<span class="flex-1"></span>
		<button type="button" onclick={refresh} disabled={loading} class="flex items-center gap-1.5 rounded-lg border border-white/10 px-2.5 py-1 font-mono text-xs text-white/60 transition hover:border-emerald-500/40 hover:text-white disabled:opacity-40">
			<RefreshCw class="size-3.5 {loading ? 'animate-spin' : ''}" /> 刷新
		</button>
	</div>

	<div class="min-h-0 flex-1 overflow-y-auto p-4">
		{#if error && !status}
			<div class="flex items-center gap-2 rounded-lg bg-red-500/10 p-3 text-sm text-red-400"><CircleAlert class="size-4" /> {error}</div>
		{:else if status?.error}
			<div class="rounded-lg bg-amber-500/10 p-3 text-sm text-amber-400">{status.error}</div>
		{:else if !status?.dirty}
			<div class="flex flex-col items-center justify-center gap-2 py-16 text-center">
				<GitCommitHorizontal class="size-8 text-white/20" />
				<p class="text-sm text-white/40">工作区干净</p>
			</div>
		{:else}
			<!-- 变更文件 -->
			<div class="flex flex-col gap-1.5">
				{#each status?.files ?? [] as f (f.path)}
					<label class="flex items-center gap-2 rounded-lg border border-white/10 px-3 py-2 hover:border-emerald-500/30 {picked.includes(f.path) ? 'border-emerald-500/40 bg-emerald-500/5' : ''}">
						<input type="checkbox" checked={picked.includes(f.path)} onchange={() => toggleFile(f.path)} class="accent-emerald-500" />
						<span class="min-w-0 flex-1">
							<span class="block truncate font-mono text-[12px] text-terminal-fg">{f.path}</span>
							<span class="font-mono text-[10px] text-white/35">{idxLabel(f.index)}{f.worktree !== " " ? ` + ${idxLabel(f.worktree)}` : ""}</span>
						</span>
						<button type="button" onclick={() => toggleFile(f.path)} class="rounded p-1 text-white/30 hover:text-white" title="查看 diff">
							<FileText class="size-3.5" />
						</button>
					</label>
				{/each}
			</div>

			<!-- diff 预览 -->
			{#if diff}
				<pre class="mt-3 max-h-64 overflow-auto rounded-lg border border-white/10 bg-black/40 p-3 font-mono text-[11px] leading-relaxed text-white/70">{diff}</pre>
			{/if}

			<!-- commit -->
			<div class="mt-3 flex flex-col gap-2 rounded-lg border border-white/10 p-3">
				<div class="flex items-center gap-2">
					<input
						bind:value={commitMsg}
						placeholder="commit message (必填)"
						class="min-w-0 flex-1 rounded-lg border border-white/10 bg-[#0d0d0d] px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-white/25 focus:border-emerald-500/40"
					/>
					<button
						type="button"
						onclick={aiMessage}
						disabled={aiMsgLoading}
						title="让主脑根据变更写提交信息"
						class="flex shrink-0 items-center gap-1.5 rounded-lg border border-white/10 px-2.5 py-2 font-mono text-xs text-white/60 transition hover:border-emerald-500/40 hover:text-white disabled:opacity-40"
					>
						{#if aiMsgLoading}<Loader2 class="size-3.5 animate-spin" />{:else}✨{/if} AI 提交信息
					</button>
				</div>
				<div class="flex items-center gap-2">
					<button
						type="button"
						onclick={commit}
						disabled={!commitMsg.trim() || committing}
						class="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-30"
					>
						{#if committing}<Loader2 class="size-4 animate-spin" />{:else}<GitCommitHorizontal class="size-4" />{/if}
						提交
					</button>
					{#if error && status}<span class="font-mono text-[11px] text-white/35">{error}</span>{/if}
				</div>
			</div>
		{/if}
	</div>
</div>
