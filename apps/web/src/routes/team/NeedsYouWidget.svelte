<script lang="ts">
	/**
	 * TDX "Needs You" widget component.
	 * Only renders when VEYA_TDX_NEEDS_YOU is enabled on the server.
	 */
	import { onMount } from "svelte";
	import { AlertCircle, CheckCircle, Clock, ExternalLink } from "lucide-svelte";

	let needsYou = $state<{ enabled: boolean; count: number; items: any[] } | null>(null);
	let loading = $state(true);

	onMount(async () => {
		try {
			const res = await fetch("/api/v1/tdx/needs-you", { credentials: "include" });
			if (res.ok) {
				needsYou = await res.json();
			}
		} catch {
			// Flag disabled or error
		} finally {
			loading = false;
		}
	});
</script>

{#if needsYou && needsYou.enabled && needsYou.items.length > 0}
	<div class="mb-6 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-amber-900 dark:border-amber-400/30 dark:bg-amber-950/20 dark:text-amber-200">
		<div class="flex items-center justify-between mb-3">
			<div class="flex items-center gap-2 font-semibold">
				<AlertCircle class="h-5 w-5 text-amber-600 dark:text-amber-400" />
				<span>等你决定 / Needs You ({needsYou.count})</span>
			</div>
			<span class="text-xs opacity-75">Clean-room TDX priority queue</span>
		</div>
		<div class="space-y-2">
			{#each needsYou.items as item}
				<div class="flex items-center justify-between rounded-lg bg-white/60 p-3 shadow-sm backdrop-blur dark:bg-zinc-900/60">
					<div class="flex items-center gap-3">
						<span class="rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-300">
							{item.kind}
						</span>
						<div>
							<div class="text-sm font-medium">{item.title}</div>
							<div class="flex items-center gap-2 text-xs opacity-60">
								<span>Source: {item.source}</span>
								<span>•</span>
								<span class="flex items-center gap-1"><Clock class="h-3 w-3" /> {item.age_s}s ago</span>
							</div>
						</div>
					</div>
					<div>
						<a
							href={item.link}
							class="inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-medium text-white shadow hover:bg-amber-700 dark:bg-amber-500 dark:hover:bg-amber-600"
						>
							Review <ExternalLink class="h-3 w-3" />
						</a>
					</div>
				</div>
			{/each}
		</div>
	</div>
{/if}
