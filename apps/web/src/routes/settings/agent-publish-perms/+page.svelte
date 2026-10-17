<script lang="ts">
	/** Owner-only UI for durable TDX agent publication permissions. */
	import { onMount } from "svelte";
	import { Check, RefreshCw, ShieldCheck } from "lucide-svelte";
	import { api } from "$lib/api";
	import { listBots } from "$lib/team/api";

	type Action = "push" | "merge" | "tag" | "deploy";
	type Policy = Record<Action, boolean>;
	type PermissionResponse = { enabled: boolean; permissions: Record<string, Policy> };
	const actions: Action[] = ["push", "merge", "tag", "deploy"];
	const labels: Record<Action, string> = {
		push: "Push to remote",
		merge: "Merge branches / PRs",
		tag: "Create Git tags",
		deploy: "Deploy / restart services",
	};
	const denyAll = (): Policy => ({ push: false, merge: false, tag: false, deploy: false });

	let policies = $state<Record<string, Policy>>({});
	let agentNames = $state<Record<string, string>>({});
	let agentIdInput = $state("");
	let loading = $state(true);
	let busyAgent = $state("");
	let error = $state("");
	let notice = $state("");
	let enabled = $state(false);

	function responseMessage(data: unknown, status: number): string {
		if (data && typeof data === "object" && "detail" in data) {
			const detail = (data as { detail?: unknown }).detail;
			if (typeof detail === "string") return detail;
		}
		if (typeof data === "string" && data) return data;
		if (status === 404) return "This feature is disabled by VEYA_TDX_AGENT_PUBLISH_PERMS.";
		if (status === 403 || status === 401) return "Sign in with the configured Veya owner account to manage these permissions.";
		return `Request failed (HTTP ${status}).`;
	}

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			const result = await api("gateway", "api/v1/tdx/publish-perms", { method: "GET" });
			if (!result.ok) {
				enabled = false;
				error = responseMessage(result.data, result.status);
				return;
			}
			const data = result.data as PermissionResponse;
			if (!data || !data.permissions || typeof data.permissions !== "object") {
				enabled = false;
				error = "Permission response did not include a valid policy map.";
				return;
			}
			enabled = data.enabled === true;
			const savedPolicies = Object.fromEntries(
				Object.entries(data.permissions).map(([id, policy]) => [id, { ...denyAll(), ...policy }]),
			) as Record<string, Policy>;
			try {
				const { bots } = await listBots();
				const nextNames: Record<string, string> = {};
				const knownPolicies: Record<string, Policy> = {};
				for (const bot of bots) {
					nextNames[bot.bot_id] = bot.name || bot.bot_id;
					if (bot.bot_id !== "default") knownPolicies[bot.bot_id] = savedPolicies[bot.bot_id] ?? denyAll();
				}
				agentNames = nextNames;
				policies = { ...knownPolicies, ...savedPolicies };
			} catch {
				// Keep persisted IDs visible if the optional bot list endpoint is unavailable.
				agentNames = {};
				policies = savedPolicies;
			}
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			loading = false;
		}
	}

	async function save(agentId: string, nextPolicy: Policy): Promise<void> {
		busyAgent = agentId;
		error = "";
		notice = "";
		try {
			const result = await api("gateway", `api/v1/tdx/publish-perms/${encodeURIComponent(agentId)}`, {
				method: "PUT",
				body: nextPolicy,
			});
			if (!result.ok) {
				error = responseMessage(result.data, result.status);
				return;
			}
			const data = result.data as { permissions?: Policy };
			policies = { ...policies, [agentId]: { ...denyAll(), ...(data.permissions ?? nextPolicy) } };
			notice = `Saved durable permissions for ${agentId}.`;
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			busyAgent = "";
		}
	}

	async function addAgent(): Promise<void> {
		const agentId = agentIdInput.trim();
		if (!agentId || busyAgent) return;
		if (agentId === "default") {
			error = "The default policy is read-only and always denies by default.";
			return;
		}
		await save(agentId, policies[agentId] ?? denyAll());
		if (!error) agentIdInput = "";
	}

	async function change(agentId: string, action: Action, checked: boolean): Promise<void> {
		if (busyAgent) return;
		const nextPolicy = { ...(policies[agentId] ?? denyAll()), [action]: checked };
		policies = { ...policies, [agentId]: nextPolicy };
		await save(agentId, nextPolicy);
	}

	onMount(() => {
		void load();
	});
</script>

<svelte:head>
	<title>Agent Publish Permissions · Veya</title>
</svelte:head>

<div class="mx-auto w-full max-w-4xl space-y-5 p-4 pb-24">
	<header class="flex flex-wrap items-start justify-between gap-3">
		<div class="flex items-center gap-3">
			<a href="/" class="text-xs text-terminal-dim hover:text-terminal-fg">← 返回控制台</a>
			<span class="opacity-20 text-xs">/</span>
			<div>
				<h1 class="flex items-center gap-2 text-xl font-semibold tracking-tight"><ShieldCheck class="h-5 w-5" /> Agent publish permissions</h1>
				<p class="mt-1 text-sm opacity-70">Owner-only, persisted permissions checked before tools execute. Unset agents are denied when the feature flag is enabled.</p>
			</div>
		</div>
		<button class="inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:hover:bg-zinc-800" onclick={() => void load()} disabled={loading || busyAgent !== ""}><RefreshCw class={loading ? "h-4 w-4 animate-spin" : "h-4 w-4"} size={16} /> Refresh</button>
	</header>

	{#if error}
		<div role="alert" class="rounded-lg border border-red-500/40 bg-red-500/5 p-3 text-sm text-red-700 dark:text-red-300">{error}</div>
	{/if}
	{#if notice}
		<div class="flex items-center gap-2 rounded-lg border border-emerald-500/30 p-3 text-sm text-emerald-700 dark:text-emerald-300"><Check size={16} /> {notice}</div>
	{/if}

	{#if loading}
		<p class="text-sm opacity-70">Loading canonical policy state…</p>
	{:else if enabled}
		<section class="rounded-xl border p-4 dark:border-zinc-700">
			<h2 class="mb-2 font-semibold">Add agent</h2>
			<p class="mb-3 text-sm opacity-70">New entries start with all publication actions denied.</p>
			<form class="flex flex-wrap gap-2" onsubmit={(event) => { event.preventDefault(); void addAgent(); }}>
				<input class="min-w-0 flex-1 rounded-lg border bg-transparent px-3 py-2 text-sm" aria-label="Agent ID" placeholder="stable-agent-id" bind:value={agentIdInput} autocomplete="off" />
				<button type="submit" class="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900" disabled={!agentIdInput.trim() || busyAgent !== ""}>Add with deny-all</button>
			</form>
		</section>

		<section class="overflow-hidden rounded-xl border dark:border-zinc-700">
			<div class="border-b p-4 dark:border-zinc-700">
				<h2 class="font-semibold">Default policy</h2>
				<p class="mt-1 text-sm opacity-70">Read-only fallback for any agent without an explicit policy.</p>
			</div>
			<div class="grid grid-cols-2 gap-3 p-4 sm:grid-cols-4">
				{#each actions as action}
					<div class="rounded-lg bg-zinc-50 p-3 dark:bg-zinc-900"><div class="text-xs opacity-70">{labels[action]}</div><div class="mt-1 font-semibold">{policies.default?.[action] ? "Allowed" : "Denied"}</div></div>
				{/each}
			</div>
		</section>

		<section class="space-y-3">
			<h2 class="font-semibold">Agent policies</h2>
			{#each Object.entries(policies).filter(([agentId]) => agentId !== "default") as entry (entry[0])}
				{@const agentId = entry[0]}
				{@const policy = entry[1]}
				<div class="rounded-xl border p-4 dark:border-zinc-700">
					<div class="mb-3 flex flex-wrap items-center justify-between gap-2">
						<div><code class="text-sm">{agentId}</code>{#if agentNames[agentId]}<div class="mt-1 text-xs opacity-65">{agentNames[agentId]}</div>{/if}</div>
						{#if busyAgent === agentId}<span class="text-xs opacity-60">Saving…</span>{/if}
					</div>
					<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
						{#each actions as action}
							<label class="flex cursor-pointer items-center justify-between gap-3 rounded-lg bg-zinc-50 p-3 dark:bg-zinc-900">
								<span class="text-sm">{labels[action]}</span>
								<input type="checkbox" checked={policy[action]} disabled={busyAgent !== ""} onchange={(event) => void change(agentId, action, event.currentTarget.checked)} />
							</label>
						{/each}
					</div>
				</div>
			{/each}
			{#if Object.keys(policies).filter((agentId) => agentId !== "default").length === 0}
				<p class="rounded-lg border border-dashed p-6 text-center text-sm opacity-70">No agent-specific permissions have been saved yet.</p>
			{/if}
		</section>
	{:else}
		<section class="rounded-xl border border-dashed p-5 dark:border-zinc-700">
			<h2 class="font-semibold">Permissions unavailable</h2>
			<p class="mt-2 text-sm opacity-70">This page only works when the feature flag is enabled and the signed-in account is the configured owner. No local fallback state is shown.</p>
		</section>
	{/if}
</div>
