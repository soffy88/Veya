<script lang="ts">
	/**
	 * PluginPanel — browse the marketplace, install/toggle/uninstall plugins,
	 * via the L4 gateway (POST /api/v1/plugin/manage).
	 */
	import { onMount } from "svelte";
	import { Loader2, Package, Trash2, Store, Check } from "lucide-svelte";
	import { api } from "$lib/api";

	interface Plugin {
		id: string;
		name: string;
		version: string;
		enabled: boolean;
		source: string;
		capabilities: string[];
	}

	interface MarketplaceEntry {
		name: string;
		description: string;
		author: string;
		tags: string[];
		version: string;
	}

	let plugins = $state<Plugin[]>([]);
	let marketplace = $state<MarketplaceEntry[]>([]);
	let loading = $state(true);
	let error = $state("");
	let busy = $state<string | null>(null);

	const installedNames = $derived(new Set(plugins.map((p) => p.name)));

	async function refresh() {
		loading = true;
		error = "";
		const res = await api("gateway", "api/v1/plugin/manage", { body: { action: "marketplace" } });
		const data = res.data as { marketplace?: MarketplaceEntry[]; installed?: Plugin[] };
		if (!res.ok) {
			error = "加载插件列表失败";
		} else {
			marketplace = data.marketplace ?? [];
			plugins = data.installed ?? [];
		}
		loading = false;
	}

	async function installFromMarketplace(entry: MarketplaceEntry) {
		busy = entry.name;
		await api("gateway", "api/v1/plugin/manage", {
			body: { action: "install", name: entry.name, version: entry.version, capabilities: [], source: "marketplace" },
		});
		await refresh();
		busy = null;
	}

	async function toggle(p: Plugin) {
		busy = p.name;
		await api("gateway", "api/v1/plugin/manage", { body: { action: "toggle", name: p.name, enabled: !p.enabled } });
		await refresh();
		busy = null;
	}

	async function uninstall(p: Plugin) {
		busy = p.name;
		await api("gateway", "api/v1/plugin/manage", { body: { action: "uninstall", name: p.name } });
		await refresh();
		busy = null;
	}

	onMount(refresh);
</script>

<div class="flex flex-col gap-6">
	<div>
		<div class="mb-2 flex items-center gap-2">
			<Store class="size-4 text-terminal-dim" />
			<h3 class="text-sm font-semibold text-terminal-fg">插件市场</h3>
		</div>
		{#if loading}
			<div class="flex items-center gap-2 text-sm text-terminal-dim"><Loader2 class="size-4 animate-spin" /> 加载中…</div>
		{:else if error}
			<div class="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3 text-sm text-rose-300">{error}</div>
		{:else}
			<ul class="flex flex-col gap-2">
				{#each marketplace as entry (entry.name)}
					<li class="flex items-center gap-3 rounded-lg border border-terminal-edge bg-terminal-bg p-3">
						<div class="min-w-0 flex-1">
							<div class="text-sm font-medium text-terminal-fg">{entry.name} <span class="text-xs text-terminal-dim">v{entry.version}</span></div>
							<div class="truncate text-xs text-terminal-dim">{entry.description}</div>
						</div>
						{#if installedNames.has(entry.name)}
							<span class="flex items-center gap-1 rounded-md border border-emerald-500/40 px-2.5 py-1 text-xs font-medium text-emerald-400">
								<Check class="size-3.5" /> 已安装
							</span>
						{:else}
							<button
								type="button"
								onclick={() => installFromMarketplace(entry)}
								disabled={busy === entry.name}
								class="flex shrink-0 items-center gap-1.5 rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-40"
							>
								{#if busy === entry.name}<Loader2 class="size-3.5 animate-spin" />{:else}安装{/if}
							</button>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>

	<div>
		<h3 class="mb-2 text-sm font-semibold text-terminal-fg">已安装</h3>
		{#if !loading && plugins.length === 0}
			<div class="flex flex-col items-center gap-2 py-10 text-terminal-dim">
				<Package class="size-6 opacity-40" />
				<p class="text-sm">还没有安装任何插件</p>
			</div>
		{:else}
			<ul class="flex flex-col gap-2">
				{#each plugins as p (p.id)}
					<li class="flex items-center gap-3 rounded-lg border border-terminal-edge bg-terminal-bg p-3">
						<div class="min-w-0 flex-1">
							<div class="text-sm font-medium text-terminal-fg">{p.name} <span class="text-xs text-terminal-dim">v{p.version}</span></div>
							<div class="truncate text-xs text-terminal-dim">{p.source} · {p.capabilities?.join(", ") || "—"}</div>
						</div>
						<button
							type="button"
							onclick={() => toggle(p)}
							disabled={busy === p.name}
							class="rounded-md border px-2.5 py-1 text-xs font-medium transition {p.enabled ? 'border-emerald-500/40 text-emerald-400' : 'border-terminal-edge text-terminal-dim'}"
						>
							{p.enabled ? "已启用" : "已停用"}
						</button>
						<button
							type="button"
							onclick={() => uninstall(p)}
							disabled={busy === p.name}
							class="flex size-7 items-center justify-center rounded-md text-terminal-dim transition hover:bg-rose-500/10 hover:text-rose-300"
							aria-label="卸载"
						>
							<Trash2 class="size-4" />
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</div>
