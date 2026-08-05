<script lang="ts">
	/**
	 * PluginPanel — list / install / toggle / uninstall plugins via the L4 gateway
	 * (POST /api/v1/plugin/manage, see server/routes... capabilities.ts's old "plugin" cap).
	 */
	import { onMount } from "svelte";
	import { Loader2, Package, Trash2, Plus } from "lucide-svelte";
	import { api } from "$lib/api";

	interface Plugin {
		id: string;
		name: string;
		version: string;
		enabled: boolean;
		source: string;
		capabilities: string[];
	}

	let plugins = $state<Plugin[]>([]);
	let loading = $state(true);
	let error = $state("");
	let busy = $state<string | null>(null);

	let newName = $state("");
	let newVersion = $state("1.0.0");
	let newSource = $state("local");
	let installing = $state(false);

	async function refresh() {
		loading = true;
		error = "";
		const res = await api("gateway", "api/v1/plugin/manage", { body: { action: "list" } });
		const data = res.data as { plugins?: Plugin[] };
		if (!res.ok) {
			error = "加载插件列表失败";
		} else {
			plugins = data.plugins ?? [];
		}
		loading = false;
	}

	async function toggle(p: Plugin) {
		busy = p.name;
		await api("gateway", "api/v1/plugin/manage", { body: { action: "toggle", name: p.name } });
		await refresh();
		busy = null;
	}

	async function uninstall(p: Plugin) {
		busy = p.name;
		await api("gateway", "api/v1/plugin/manage", { body: { action: "uninstall", name: p.name } });
		await refresh();
		busy = null;
	}

	async function install() {
		if (!newName.trim()) return;
		installing = true;
		await api("gateway", "api/v1/plugin/manage", {
			body: { action: "install", name: newName.trim(), version: newVersion.trim() || "1.0.0", source: newSource },
		});
		newName = "";
		await refresh();
		installing = false;
	}

	onMount(refresh);
</script>

<div class="flex flex-col gap-4">
	<div class="flex items-end gap-2 rounded-lg border border-terminal-edge bg-terminal-bg p-3">
		<div class="flex min-w-0 flex-1 flex-col gap-1">
			<label class="text-xs text-terminal-dim" for="plugin-name">插件名</label>
			<input id="plugin-name" bind:value={newName} placeholder="例如 dark-theme" class="rounded-lg border border-terminal-edge bg-terminal-panel px-2 py-1.5 text-sm text-terminal-fg outline-none focus:border-sky-500/60" />
		</div>
		<div class="flex w-24 flex-col gap-1">
			<label class="text-xs text-terminal-dim" for="plugin-version">版本</label>
			<input id="plugin-version" bind:value={newVersion} class="rounded-lg border border-terminal-edge bg-terminal-panel px-2 py-1.5 text-sm text-terminal-fg outline-none focus:border-sky-500/60" />
		</div>
		<div class="flex w-28 flex-col gap-1">
			<label class="text-xs text-terminal-dim" for="plugin-source">来源</label>
			<select id="plugin-source" bind:value={newSource} class="rounded-lg border border-terminal-edge bg-terminal-panel px-2 py-1.5 text-sm text-terminal-fg outline-none focus:border-sky-500/60">
				<option value="local">local</option>
				<option value="marketplace">marketplace</option>
			</select>
		</div>
		<button
			type="button"
			onclick={install}
			disabled={installing || !newName.trim()}
			class="flex shrink-0 items-center gap-1.5 rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 px-3 py-1.5 text-sm font-medium text-white transition hover:brightness-110 disabled:opacity-40"
		>
			{#if installing}<Loader2 class="size-4 animate-spin" />{:else}<Plus class="size-4" />{/if}
			安装
		</button>
	</div>

	{#if loading}
		<div class="flex items-center gap-2 text-sm text-terminal-dim"><Loader2 class="size-4 animate-spin" /> 加载中…</div>
	{:else if error}
		<div class="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3 text-sm text-rose-300">{error}</div>
	{:else if plugins.length === 0}
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
