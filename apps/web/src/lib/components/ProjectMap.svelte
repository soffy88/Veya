<script lang="ts">
	/**
	 * ProjectMap — 项目图谱 (借鉴 ccgui Project Map)。复用 codebase memory 的
	 * AST 图谱: 文件节点 + import 依赖边。渲染为依赖浏览器 (节点列表 + 展开边)。
	 */
	import { Network, RefreshCw, Loader2, CircleAlert, FileText, ArrowRight } from "lucide-svelte";
	import { API_BASE } from "$lib/api";

	type GraphNode = { id: string; type: string; deps: number; dependents: number };
	type GraphEdge = { src: string; dst: string; weight: number };

	let nodes = $state<GraphNode[]>([]);
	let edges = $state<GraphEdge[]>([]);
	let loading = $state(false);
	let error = $state("");
	let selected = $state<string | null>(null);
	let search = $state("");

	async function load() {
		loading = true;
		error = "";
		try {
			const res = await fetch(`${API_BASE}/api/v1/graph/files?limit=300`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = (await res.json()) as { nodes: GraphNode[]; edges: GraphEdge[] };
			nodes = data.nodes ?? [];
			edges = data.edges ?? [];
			selected = null;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		void load();
	});

	const filtered = $derived.by(() => {
		if (!search.trim()) return nodes;
		const q = search.toLowerCase();
		return nodes.filter((n) => n.id.toLowerCase().includes(q));
	});

	const selectedNode = $derived(selected ? nodes.find((n) => n.id === selected) : null);
	const outEdges = $derived(selected ? edges.filter((e) => e.src === selected) : []);
	const inEdges = $derived(selected ? edges.filter((e) => e.dst === selected) : []);

	function short(p: string): string {
		return p.split("/").slice(-2).join("/");
	}
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="flex shrink-0 items-center gap-2 border-b border-white/5 px-4 py-2">
		<Network class="size-4 text-violet-400" />
		<span class="font-mono text-[11px] uppercase tracking-wider text-white/40">项目图谱</span>
		<input
			bind:value={search}
			placeholder="搜索文件…"
			class="ml-2 w-48 rounded-lg border border-white/10 bg-[#0d0d0d] px-2.5 py-1 text-xs text-terminal-fg outline-none placeholder:text-white/25 focus:border-violet-500/40"
		/>
		<span class="flex-1"></span>
		<span class="font-mono text-[10px] text-white/30">{nodes.length} 文件 · {edges.length} 依赖边</span>
		<button type="button" onclick={load} disabled={loading} class="flex items-center gap-1.5 rounded-lg border border-white/10 px-2.5 py-1 font-mono text-xs text-white/60 transition hover:border-violet-500/40 hover:text-white disabled:opacity-40">
			<RefreshCw class="size-3.5 {loading ? 'animate-spin' : ''}" /> 刷新
		</button>
	</div>

	<div class="flex min-h-0 flex-1 flex-col gap-0 md:flex-row">
		<!-- 文件节点列表 -->
		<div class="min-h-0 flex-1 overflow-y-auto border-r border-white/5 p-2">
			{#if error}
				<div class="flex items-center gap-2 rounded-lg bg-red-500/10 p-3 text-sm text-red-400"><CircleAlert class="size-4" /> {error}</div>
			{:else if loading && nodes.length === 0}
				<div class="flex items-center gap-2 px-2 py-3 text-sm text-white/40"><Loader2 class="size-4 animate-spin" /> 构建图谱…</div>
			{:else if nodes.length === 0}
				<div class="flex flex-col items-center justify-center gap-2 py-16 text-center">
					<Network class="size-8 text-white/20" />
					<p class="text-sm text-white/40">暂无图谱数据</p>
					<p class="max-w-sm text-xs text-white/25">需 codebase 索引就绪（mcp_codebase 网关）。索引后刷新即可看到文件依赖图。</p>
				</div>
			{:else}
				<div class="flex flex-col gap-0.5">
					{#each filtered as n (n.id)}
						<button
							type="button"
							onclick={() => (selected = n.id)}
							class="flex items-center gap-2 rounded-lg px-2 py-1.5 text-left transition {selected === n.id ? 'bg-violet-500/15 border border-violet-500/30' : 'hover:bg-white/5 border border-transparent'}"
						>
							<FileText class="size-3.5 shrink-0 text-white/40" />
							<span class="min-w-0 flex-1 truncate font-mono text-[11px] text-white/75">{n.id}</span>
							<span class="shrink-0 font-mono text-[9px] text-white/30" title="依赖 → 被依赖">→{n.deps} ←{n.dependents}</span>
						</button>
					{/each}
					{#if filtered.length === 0}
						<p class="px-2 py-3 font-mono text-[11px] text-white/30">无匹配</p>
					{/if}
				</div>
			{/if}
		</div>

		<!-- 选中节点的依赖详情 -->
		<div class="min-h-0 w-full shrink-0 overflow-y-auto p-3 md:w-96">
			{#if selectedNode}
				<div class="flex items-center gap-2">
					<FileText class="size-4 text-violet-400" />
					<span class="truncate font-mono text-[12px] font-semibold text-terminal-fg">{selectedNode.id}</span>
				</div>
				<div class="mt-0.5 font-mono text-[10px] text-white/35">
					{selectedNode.deps} 依赖 · {selectedNode.dependents} 被依赖
				</div>

				<div class="mt-3 font-mono text-[10px] uppercase tracking-wider text-white/30">依赖 (imports)</div>
				<div class="mt-1 flex flex-col gap-1">
					{#if outEdges.length === 0}
						<p class="font-mono text-[10px] text-white/25">无</p>
					{:else}
						{#each outEdges as e (e.src + e.dst)}
							<div class="flex items-center gap-1.5 rounded border border-white/5 bg-white/[0.03] px-2 py-1">
								<ArrowRight class="size-3 shrink-0 text-violet-400/70" />
								<span class="truncate font-mono text-[10px] text-white/60">{short(e.dst)}</span>
							</div>
						{/each}
					{/if}
				</div>

				<div class="mt-3 font-mono text-[10px] uppercase tracking-wider text-white/30">被依赖 (dependents)</div>
				<div class="mt-1 flex flex-col gap-1">
					{#if inEdges.length === 0}
						<p class="font-mono text-[10px] text-white/25">无</p>
					{:else}
						{#each inEdges as e (e.src + e.dst)}
							<div class="flex items-center gap-1.5 rounded border border-white/5 bg-white/[0.03] px-2 py-1">
								<ArrowRight class="size-3 shrink-0 rotate-180 text-amber-400/70" />
								<span class="truncate font-mono text-[10px] text-white/60">{short(e.src)}</span>
							</div>
						{/each}
					{/if}
				</div>
			{:else}
				<div class="flex h-full flex-col items-center justify-center gap-2 text-center text-white/25">
					<Network class="size-6" />
					<p class="text-xs">点击左侧文件查看依赖关系</p>
				</div>
			{/if}
		</div>
	</div>
</div>
