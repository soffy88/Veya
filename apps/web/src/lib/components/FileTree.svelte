<script lang="ts">
	/**
	 * FileTree — 工作区文件树 (P3, 借鉴 ccgui)。点击文件 → 注入 @path 到输入框。
	 * 只读浏览 (GET /api/v1/fs/tree), 写入走 hicode。工作区边界在服务端强制。
	 */
	import { ChevronDown, ChevronRight, FileText, Folder, FolderOpen, Loader2, RefreshCw } from "lucide-svelte";
	import { API_BASE } from "$lib/api";

	type Entry = {
		name: string;
		type: "dir" | "file";
		path: string;
		size?: number;
		children?: Entry[];
	};

	interface Props {
		onPick: (path: string) => void; // 点击文件 → 外部注入输入框
	}

	let { onPick }: Props = $props();

	let entries = $state<Entry[]>([]);
	let loading = $state(false);
	let error = $state("");
	let openDirs = $state<Set<string>>(new Set());

	async function load() {
		loading = true;
		error = "";
		try {
			const res = await fetch(`${API_BASE}/api/v1/fs/tree`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = (await res.json()) as { entries: Entry[] };
			entries = data.entries ?? [];
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		void load();
	});

	function toggleDir(p: string) {
		const next = new Set(openDirs);
		if (next.has(p)) next.delete(p);
		else next.add(p);
		openDirs = next;
	}
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="flex shrink-0 items-center gap-2 border-b border-white/5 px-3 py-1.5">
		<Folder class="size-3.5 text-sky-400" />
		<span class="font-mono text-[10px] uppercase tracking-wider text-white/40">工作区文件</span>
		<span class="flex-1"></span>
		<button type="button" onclick={load} disabled={loading} class="rounded p-1 text-white/40 transition hover:bg-white/10 hover:text-white">
			<RefreshCw class="size-3 {loading ? 'animate-spin' : ''}" />
		</button>
	</div>

	<div class="min-h-0 flex-1 overflow-y-auto p-1.5">
		{#if error}
			<p class="px-2 py-2 font-mono text-[11px] text-red-400">{error}</p>
		{:else if loading && entries.length === 0}
			<div class="flex items-center gap-1.5 px-2 py-2 text-xs text-white/40"><Loader2 class="size-3 animate-spin" /> 加载…</div>
		{:else if entries.length === 0}
			<p class="px-2 py-2 font-mono text-[11px] text-white/30">空工作区</p>
		{:else}
			<div class="flex flex-col">
				{#each entries as e (e.path)}
					{#if e.type === "dir"}
						<div class="flex items-center gap-1 rounded px-1 py-0.5 hover:bg-white/5">
							<button type="button" onclick={() => toggleDir(e.path)} class="flex flex-1 items-center gap-1.5 text-left">
								{#if openDirs.has(e.path)}
									<ChevronDown class="size-3 shrink-0 text-white/40" />
								{:else}
									<ChevronRight class="size-3 shrink-0 text-white/40" />
								{/if}
								{#if openDirs.has(e.path)}
									<FolderOpen class="size-3.5 shrink-0 text-amber-400/70" />
								{:else}
									<Folder class="size-3.5 shrink-0 text-amber-400/70" />
								{/if}
								<span class="truncate font-mono text-[11px] text-white/70">{e.name}</span>
							</button>
						</div>
						{#if openDirs.has(e.path) && e.children}
							<div class="ml-3 border-l border-white/5 pl-1.5">
								{#each e.children as c (c.path)}
									{#if c.type === "dir"}
										<div class="flex items-center gap-1 rounded px-1 py-0.5 hover:bg-white/5">
											<button type="button" onclick={() => toggleDir(c.path)} class="flex flex-1 items-center gap-1.5 text-left">
												<ChevronRight class="size-3 shrink-0 text-white/30" />
												<Folder class="size-3.5 shrink-0 text-amber-400/70" />
												<span class="truncate font-mono text-[11px] text-white/70">{c.name}</span>
											</button>
										</div>
									{:else}
										<button
											type="button"
											onclick={() => onPick(c.path)}
											title={`${c.path}${c.size ? ` (${(c.size / 1024).toFixed(0)}KB)` : ""}`}
											class="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-white/5"
										>
											<FileText class="size-3 shrink-0 text-white/35" />
											<span class="truncate font-mono text-[11px] text-white/60 hover:text-white">{c.name}</span>
										</button>
									{/if}
								{/each}
							</div>
						{/if}
					{:else}
						<button
							type="button"
							onclick={() => onPick(e.path)}
							title={`${e.path}${e.size ? ` (${(e.size / 1024).toFixed(0)}KB)` : ""}`}
							class="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-white/5"
						>
							<FileText class="size-3 shrink-0 text-white/35" />
							<span class="truncate font-mono text-[11px] text-white/60 hover:text-white">{e.name}</span>
						</button>
					{/if}
				{/each}
			</div>
		{/if}
	</div>
</div>
