<script lang="ts">
	/**
	 * KanbanPanel — 多 Agent 编排看板 (worktree 隔离 + 依赖链 + auto-commit)。
	 *
	 * 视图: board 列表 + 卡片状态列 (todo/running/done/trash)。
	 * 操作: 创建看板 / 加卡 / 建依赖链 / 启动 / 归档(触发下游) / 审查 diff。
	 * API: POST /api/v1/board (cindy_compat, 全部 action)。
	 */
	import { Columns3, Plus, Play, Trash2, Eye, Link2, RefreshCw } from "lucide-svelte";
	import { API_BASE } from "$lib/api";

	type Card = {
		id: string;
		title: string;
		prompt: string;
		status: string;
		depends_on: string[];
		worktree: string;
		branch: string;
		engine: string;
		commit_sha: string;
		error: string;
	};

	let boards = $state<string[]>([]);
	let current = $state("");
	let repo = $state("");
	let cards = $state<Record<string, Card>>({});
	let error = $state("");
	let busy = $state(false);
	let newBoardOpen = $state(false);
	let newCardOpen = $state(false);
	let cardTitle = $state("");
	let cardPrompt = $state("");
	let cardDepends = $state("");
	let diffResult = $state("");

	const COLUMNS: { key: string; label: string }[] = [
		{ key: "todo", label: "待办" },
		{ key: "running", label: "执行中" },
		{ key: "done", label: "完成" },
		{ key: "trash", label: "归档" },
	];

	async function api(body: Record<string, unknown>): Promise<{ ok: boolean; data: unknown }> {
		try {
			const res = await fetch(`${API_BASE}/api/v1/board`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify(body),
			});
			const text = await res.text();
			let data: unknown = text;
			try { data = JSON.parse(text); } catch { /* raw */ }
			return { ok: res.ok, data };
		} catch (e) {
			return { ok: false, data: String(e) };
		}
	}

	async function refreshBoards() {
		const r = await api({ action: "list" });
		if (r.ok) {
			const b = (r.data as { boards?: { name: string }[] }).boards ?? [];
			boards = b.map((x) => x.name);
			if (current && !boards.includes(current)) current = "";
		}
	}

	async function loadBoard() {
		if (!current) { cards = {}; return; }
		const r = await api({ action: "status", board: current });
		if (r.ok) {
			const data = r.data as { board: string; repo: string; cards: Card[] };
			repo = data.repo;
			cards = Object.fromEntries(data.cards.map((c) => [c.id, c]));
		} else {
			error = String(r.data);
		}
	}

	async function selectBoard(name: string) {
		current = name;
		diffResult = "";
		await loadBoard();
	}

	async function createBoard() {
		if (!current.trim()) return;
		busy = true;
		const r = await api({ action: "create", name: current, repo });
		error = r.ok ? "" : String(r.data);
		busy = false;
		await refreshBoards();
		await loadBoard();
	}

	async function addCard() {
		if (!cardPrompt.trim()) return;
		busy = true;
		const r = await api({
			action: "add_card", board: current, title: cardTitle || cardPrompt.slice(0, 40),
			prompt: cardPrompt,
			depends_on: cardDepends.split(",").map((s) => s.trim()).filter(Boolean),
		});
		error = r.ok ? "" : String(r.data);
		busy = false;
		cardTitle = ""; cardPrompt = ""; cardDepends = "";
		await loadBoard();
	}

	async function linkCards(from: string) {
		const to = prompt("下游卡 id (to, 依赖 from):");
		if (!to?.trim()) return;
		const r = await api({ action: "link", board: current, from_id: from, to_id: to.trim() });
		error = r.ok ? "" : String(r.data);
		await loadBoard();
	}

	async function startCard(id: string) {
		const r = await api({ action: "start", board: current, card_id: id });
		error = r.ok ? "" : String(r.data);
		await loadBoard();
	}

	async function trashCard(id: string) {
		const r = await api({ action: "trash", board: current, card_id: id });
		if (r.ok) {
			const t = (r.data as { triggered?: string[] }).triggered ?? [];
			if (t.length) error = `已触发下游: ${t.join(", ")}`;
			else error = "";
		} else error = String(r.data);
		await loadBoard();
	}

	async function viewDiff(id: string) {
		const r = await api({ action: "diff", board: current, card_id: id });
		diffResult = r.ok
			? JSON.stringify((r.data as { stat?: string }).stat ?? "", null, 1)
			: String(r.data);
	}

	// 运行中轮询刷新 (3s)
	let timer: ReturnType<typeof setInterval> | undefined;
	$effect(() => {
		if (!current) return;
		if (timer) clearInterval(timer);
		timer = setInterval(() => void loadBoard(), 3000);
		return () => { if (timer) clearInterval(timer); };
	});

	// 初始化
	$effect(() => { void refreshBoards(); });

	function cardInColumn(status: string): Card[] {
		return Object.values(cards).filter((c) => c.status === status);
	}
</script>

<div class="flex h-full flex-col gap-4 p-6">
	<div class="flex items-center justify-between">
		<h2 class="text-lg font-medium text-terminal-fg">看板 · 多 Agent 编排</h2>
		<div class="flex items-center gap-2">
			<select
				bind:value={current}
				onchange={() => void selectBoard(current)}
				class="rounded-lg border border-white/10 bg-white/5 px-2 py-1.5 text-sm text-terminal-fg"
			>
				<option value="">— 选择看板 —</option>
				{#each boards as b (b)}
					<option value={b}>{b}</option>
				{/each}
			</select>
			<button
				type="button"
				onclick={() => { newBoardOpen = !newBoardOpen; }}
				class="flex items-center gap-1.5 rounded-lg bg-white/10 px-3 py-1.5 text-sm text-terminal-fg transition hover:bg-white/20"
			>
				<Plus class="size-4" /> 新建看板
			</button>
			<button
				type="button"
				onclick={() => void loadBoard()}
				title="刷新"
				class="flex items-center gap-1 rounded-lg bg-white/5 px-2 py-1.5 text-xs text-terminal-dim hover:text-terminal-fg"
			>
				<RefreshCw class="size-3.5" />
			</button>
		</div>
	</div>

	{#if newBoardOpen}
		<div class="flex items-center gap-2 rounded-xl border border-white/10 bg-white/5 p-3">
			<input bind:value={current} placeholder="看板名 (如 sprint1)" class="flex-1 rounded-lg bg-transparent px-2 py-1.5 text-sm text-terminal-fg outline-none placeholder:text-white/30" />
			<input bind:value={repo} placeholder="git 仓库路径 (worktree 源)" class="flex-1 rounded-lg bg-transparent px-2 py-1.5 text-sm text-terminal-fg outline-none placeholder:text-white/30" />
			<button type="button" onclick={() => void createBoard()} disabled={busy} class="rounded-lg bg-white text-black px-3 py-1.5 text-sm disabled:opacity-40">
				创建
			</button>
		</div>
	{/if}

	{#if error}
		<div class="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div>
	{/if}

	{#if current}
		{#if Object.keys(cards).length === 0}
			<div class="rounded-xl border border-dashed border-white/15 p-6 text-center text-sm text-white/40">
				看板为空 — 添加第一张任务卡
			</div>
		{:else}
			<div class="grid min-h-0 flex-1 grid-cols-4 gap-3">
				{#each COLUMNS as col (col.key)}
					<div class="flex min-h-0 flex-col rounded-xl border border-white/10 bg-white/[0.03]">
						<div class="border-b border-white/10 px-3 py-2 text-xs font-medium text-terminal-dim">
							{col.label} · {cardInColumn(col.key).length}
						</div>
						<div class="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
							{#each cardInColumn(col.key) as card (card.id)}
								<div class="rounded-lg border border-white/10 bg-white/5 p-2.5">
									<div class="text-sm font-medium text-terminal-fg">{card.title}</div>
									{#if card.engine}<div class="mt-0.5 font-mono text-[10px] text-white/35">engine={card.engine}{card.branch ? ` · ${card.branch}` : ""}</div>{/if}
									{#if card.commit_sha}<div class="mt-0.5 font-mono text-[10px] text-emerald-400/70">commit {card.commit_sha.slice(0, 8)}</div>{/if}
									{#if card.error}<div class="mt-0.5 text-[11px] text-red-300/80">{card.error.slice(0, 80)}</div>{/if}
									<div class="mt-2 flex flex-wrap gap-1">
										{#if card.status === "todo"}
											<button type="button" onclick={() => void startCard(card.id)} title="启动 (worktree 隔离执行)" class="flex items-center gap-1 rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-300 hover:bg-emerald-500/25">
												<Play class="size-3" /> 启动
											</button>
										{:else if card.status === "done"}
											<button type="button" onclick={() => void trashCard(card.id)} title="归档 (触发下游依赖)" class="flex items-center gap-1 rounded bg-white/10 px-1.5 py-0.5 text-[10px] text-terminal-fg hover:bg-white/20">
												<Trash2 class="size-3" /> 归档
											</button>
										{/if}
										{#if card.status === "todo" && card.depends_on.length}
											<button type="button" onclick={() => void linkCards(card.id)} title="建依赖链" class="flex items-center gap-1 rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] text-sky-300 hover:bg-sky-500/25">
												<Link2 class="size-3" /> 依赖
											</button>
										{/if}
										<button type="button" onclick={() => void viewDiff(card.id)} title="审查 diff" class="flex items-center gap-1 rounded bg-white/10 px-1.5 py-0.5 text-[10px] text-terminal-fg hover:bg-white/20">
											<Eye class="size-3" /> diff
										</button>
									</div>
								</div>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		{/if}

		<div class="flex items-center gap-2">
			<button
				type="button"
				onclick={() => { newCardOpen = !newCardOpen; }}
				class="flex items-center gap-1.5 rounded-lg bg-white/10 px-3 py-1.5 text-sm text-terminal-fg hover:bg-white/20"
			>
				<Plus class="size-4" /> 加卡
			</button>
			{#if newCardOpen}
				<input bind:value={cardTitle} placeholder="标题" class="w-40 rounded-lg bg-transparent px-2 py-1.5 text-sm text-terminal-fg outline-none placeholder:text-white/30" />
				<input bind:value={cardPrompt} placeholder="任务描述 (prompt)" class="min-w-64 flex-1 rounded-lg bg-transparent px-2 py-1.5 text-sm text-terminal-fg outline-none placeholder:text-white/30" />
				<input bind:value={cardDepends} placeholder="依赖卡 id (逗号分隔, 可选)" class="w-48 rounded-lg bg-transparent px-2 py-1.5 text-sm text-terminal-fg outline-none placeholder:text-white/30" />
				<button type="button" onclick={() => void addCard()} disabled={busy} class="rounded-lg bg-white text-black px-3 py-1.5 text-sm disabled:opacity-40">添加</button>
			{/if}
		</div>
	{/if}

	{#if diffResult}
		<pre class="max-h-40 overflow-auto rounded-xl border border-white/10 bg-black/40 p-3 font-mono text-xs text-terminal-fg">{diffResult}</pre>
	{/if}
</div>
