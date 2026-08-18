<script lang="ts">
	/**
	 * CollapsibleText — auto-collapses long input/output (Claude-style):
	 * content taller than maxHeight fades out + shows "展开全文", click to
	 * expand; can collapse back. Uses a mask-image fade so it works over
	 * any background (user bubble vs assistant transparent).
	 */
	import { ChevronDown, ChevronUp } from "lucide-svelte";
	import type { Snippet } from "svelte";

	interface Props {
		maxHeight?: number;
		children: Snippet;
	}

	let { maxHeight = 220, children }: Props = $props();

	let contentEl = $state<HTMLDivElement>();
	let expanded = $state(false);
	let overflowing = $state(false);

	$effect(() => {
		const el = contentEl;
		if (!el) return;
		const check = () => {
			overflowing = el.scrollHeight > maxHeight + 4;
		};
		check();
		const ro = new ResizeObserver(check);
		ro.observe(el);
		return () => ro.disconnect();
	});
</script>

<div class="relative">
	<div
		bind:this={contentEl}
		class="overflow-hidden"
		style={!expanded && overflowing
			? `max-height:${maxHeight}px; -webkit-mask-image:linear-gradient(to bottom, black calc(100% - 36px), transparent 100%); mask-image:linear-gradient(to bottom, black calc(100% - 36px), transparent 100%);`
			: ""}
	>
		{@render children()}
	</div>
	{#if overflowing}
		<button
			type="button"
			onclick={() => (expanded = !expanded)}
			class="mt-1 flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[11px] text-white/40 transition hover:bg-white/5 hover:text-white/80"
		>
			{#if expanded}
				<ChevronUp class="size-3" /> 收起
			{:else}
				<ChevronDown class="size-3" /> 展开全文
			{/if}
		</button>
	{/if}
</div>
