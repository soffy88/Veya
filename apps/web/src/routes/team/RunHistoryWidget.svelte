<script lang="ts">
	/** Read-only UI for facts recorded in the existing canonical event store. */
	import { onMount } from "svelte";
	import { Clock3, GitCompare, History } from "lucide-svelte";
	import { api } from "$lib/api";

	type RunRow = {
		task_id: string;
		status?: string;
		plan?: string;
		model?: string;
		duration_s?: number;
		round_count?: number;
		evidence?: unknown;
		diff_summary?: unknown;
		token_usage?: unknown;
	};
	let runs = $state<RunRow[]>([]);
	let enabled = $state(false);
	let compareText = $state("");

	onMount(async () => {
		try {
			const response = await api("gateway", "api/v1/tdx/runs", { method: "GET", query: { limit: 30 } });
			if (!response.ok) return;
			const data = response.data as { enabled?: boolean; runs?: RunRow[] };
			enabled = data.enabled === true;
			runs = Array.isArray(data.runs) ? data.runs : [];
		} catch {
			// Flag off or event store unavailable: avoid displaying invented records.
		}
	});

	async function compare(run: RunRow): Promise<void> {
		try {
			const response = await api("gateway", `api/v1/tdx/runs/${encodeURIComponent(run.task_id)}/rounds`, { method: "GET" });
			if (!response.ok) {
				compareText = "No canonical round data is available.";
				return;
			}
			const data = response.data as { rounds?: Array<{ round: number }> };
			const rounds = data.rounds ?? [];
			if (rounds.length < 2) {
				compareText = `Task ${run.task_id}: ${rounds.length} recorded round; a comparison needs two actual rounds.`;
				return;
			}
			const result = await api("gateway", `api/v1/tdx/runs/${encodeURIComponent(run.task_id)}/compare`, {
				method: "GET",
				query: { a: rounds[0].round, b: rounds[rounds.length - 1].round },
			});
			if (!result.ok) {
				compareText = "Comparison data could not be read.";
				return;
			}
			compareText = JSON.stringify((result.data as { diff_comparison?: unknown }).diff_comparison, null, 2);
		} catch (cause) {
			compareText = cause instanceof Error ? cause.message : String(cause);
		}
	}
</script>

{#if enabled && runs.length > 0}
	<section class="mb-6 rounded-xl border border-zinc-200 p-4 dark:border-zinc-700">
		<div class="mb-3 flex items-center gap-2 font-semibold">
			<History class="h-5 w-5" />
			<span>Run History</span>
			<span class="text-xs font-normal opacity-60">({runs.length} recorded)</span>
		</div>
		<div class="space-y-2">
			{#each runs as run (run.task_id)}
				<div class="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-zinc-50 p-3 dark:bg-zinc-900">
					<div class="min-w-0">
						<div class="font-mono text-xs">{run.task_id}</div>
						{#if run.plan}<div class="mt-1 text-sm">{run.plan}</div>{/if}
						<div class="mt-1 flex flex-wrap gap-3 text-xs opacity-70">
							{#if run.status}<span>Status: {run.status}</span>{/if}
							{#if run.model}<span>Model: {run.model}</span>{/if}
							{#if typeof run.duration_s === "number"}<span class="inline-flex items-center gap-1"><Clock3 class="h-3 w-3" /> {run.duration_s}s</span>{/if}
							{#if typeof run.round_count === "number"}<span>{run.round_count} rounds</span>{/if}
						</div>
					</div>
					<button class="inline-flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs hover:bg-zinc-100 dark:hover:bg-zinc-800" onclick={() => compare(run)}><GitCompare class="h-3.5 w-3.5" /> Compare recorded rounds</button>
				</div>
			{/each}
		</div>
		{#if compareText}<pre class="mt-3 max-h-48 overflow-auto rounded-lg bg-zinc-50 p-3 text-xs dark:bg-zinc-900">{compareText}</pre>{/if}
	</section>
{/if}
