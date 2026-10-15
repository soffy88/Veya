<script lang="ts">
	/** Owner queue projected from the canonical approvals DB and event log. */
	import { onMount } from "svelte";
	import { AlertCircle, Clock, ExternalLink } from "lucide-svelte";
	import { resolveApproval, type ApprovalDecision } from "$lib/approvals/api";
	import { api } from "$lib/api";

	type NeedItem = {
		id: string;
		kind: "approval" | "blocked" | string;
		title: string;
		source: string;
		link: string;
		age_s?: number;
		request_id?: string;
		actions?: ApprovalDecision[];
	};
	type NeedsYouResponse = { enabled: boolean; count: number; items: NeedItem[] };

	let needsYou = $state<NeedsYouResponse | null>(null);
	let busyId = $state("");
	let error = $state("");

	async function load(): Promise<void> {
		try {
			const response = await api("gateway", "api/v1/tdx/needs-you", { method: "GET" });
			if (!response.ok) {
				needsYou = null;
				return;
			}
			needsYou = response.data as NeedsYouResponse;
		} catch {
			needsYou = null;
		}
	}

	async function decide(item: NeedItem, decision: ApprovalDecision): Promise<void> {
		if (!item.request_id || busyId) return;
		busyId = item.id;
		error = "";
		try {
			await resolveApproval(item.request_id, decision, "Resolved from Needs You");
			await load();
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			busyId = "";
		}
	}

	onMount(() => {
		void load();
	});
</script>

{#if needsYou?.enabled && needsYou.items.length > 0}
	<section class="mb-6 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-amber-900 dark:border-amber-400/30 dark:bg-amber-950/20 dark:text-amber-200">
		<div class="mb-3 flex items-center justify-between">
			<div class="flex items-center gap-2 font-semibold">
				<AlertCircle class="h-5 w-5 text-amber-600 dark:text-amber-400" />
				<span>等你决定 / Needs You ({needsYou.count})</span>
			</div>
			<span class="text-xs opacity-75">Canonical approvals and blocked tasks</span>
		</div>
		{#if error}
			<p class="mb-3 text-sm text-red-700 dark:text-red-300">{error}</p>
		{/if}
		<div class="space-y-2">
			{#each needsYou.items as item (item.id)}
				<div class="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-white/60 p-3 shadow-sm backdrop-blur dark:bg-zinc-900/60">
					<div class="flex min-w-0 items-start gap-3">
						<span class="rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-300">{item.kind}</span>
						<div>
							<div class="text-sm font-medium">{item.title}</div>
							<div class="mt-1 flex items-center gap-2 text-xs opacity-60">
								<span>Source: {item.source}</span>
								{#if typeof item.age_s === "number"}
									<span>•</span>
									<span class="flex items-center gap-1"><Clock class="h-3 w-3" /> {item.age_s}s ago</span>
								{/if}
							</div>
						</div>
					</div>
					<div class="flex flex-wrap items-center gap-1.5">
						{#if item.kind === "approval" && item.request_id}
							<button class="rounded-md border border-amber-600/40 px-2 py-1 text-xs hover:bg-amber-500/10 disabled:opacity-50" disabled={busyId !== ""} onclick={() => decide(item, "once")}>Allow once</button>
							<button class="rounded-md border border-amber-600/40 px-2 py-1 text-xs hover:bg-amber-500/10 disabled:opacity-50" disabled={busyId !== ""} onclick={() => decide(item, "session")}>Session</button>
							<button class="rounded-md border border-amber-600/40 px-2 py-1 text-xs hover:bg-amber-500/10 disabled:opacity-50" disabled={busyId !== ""} onclick={() => decide(item, "always")}>Always</button>
							<button class="rounded-md border border-red-600/40 px-2 py-1 text-xs text-red-700 hover:bg-red-500/10 disabled:opacity-50 dark:text-red-300" disabled={busyId !== ""} onclick={() => decide(item, "deny")}>Deny</button>
						{:else}
							<a href={item.link} class="inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-medium text-white shadow hover:bg-amber-700 dark:bg-amber-500 dark:hover:bg-amber-600">Review <ExternalLink class="h-3 w-3" /></a>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	</section>
{/if}
