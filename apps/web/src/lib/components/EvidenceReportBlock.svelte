<script lang="ts">
	import { parseEvidenceReport } from "$lib/evidenceReport";
	import { ShieldCheck, Database, HelpCircle, FastForward } from "lucide-svelte";

	let { text }: { text: string } = $props();

	const report = $derived(parseEvidenceReport(text));
</script>

{#if report.hasEvidence}
	<div class="space-y-3">
		{#if report.mainText}
			<div class="whitespace-pre-wrap break-words">{report.mainText}</div>
		{/if}

		<div class="mt-2 rounded-xl border border-sky-500/20 bg-sky-950/20 p-3 text-xs space-y-2.5">
			<div class="flex items-center gap-1.5 font-medium text-sky-300">
				<ShieldCheck size={15} class="text-sky-400 shrink-0" />
				<span>三段证据报告 (Evidence Report)</span>
			</div>

			{#if report.sources}
				<div class="rounded-lg border border-cyan-500/20 bg-cyan-950/30 p-2.5 space-y-1">
					<div class="flex items-center gap-1.5 font-medium text-cyan-300">
						<Database size={13} class="shrink-0" />
						<span class="rounded bg-cyan-500/20 px-1 py-0.5 text-[10px]">来源 · Sources</span>
					</div>
					<div class="whitespace-pre-wrap break-words text-cyan-100/90 pl-4">{report.sources}</div>
				</div>
			{/if}

			{#if report.guesses}
				<div class="rounded-lg border border-amber-500/20 bg-amber-950/30 p-2.5 space-y-1">
					<div class="flex items-center gap-1.5 font-medium text-amber-300">
						<HelpCircle size={13} class="shrink-0" />
						<span class="rounded bg-amber-500/20 px-1 py-0.5 text-[10px]">猜测 · Guesses</span>
					</div>
					<div class="whitespace-pre-wrap break-words text-amber-100/90 pl-4">{report.guesses}</div>
				</div>
			{/if}

			{#if report.skips}
				<div class="rounded-lg border border-purple-500/20 bg-purple-950/30 p-2.5 space-y-1">
					<div class="flex items-center gap-1.5 font-medium text-purple-300">
						<FastForward size={13} class="shrink-0" />
						<span class="rounded bg-purple-500/20 px-1 py-0.5 text-[10px]">跳过 · Skips</span>
					</div>
					<div class="whitespace-pre-wrap break-words text-purple-100/90 pl-4">{report.skips}</div>
				</div>
			{/if}
		</div>
	</div>
{:else}
	<div class="whitespace-pre-wrap break-words">{text}</div>
{/if}
