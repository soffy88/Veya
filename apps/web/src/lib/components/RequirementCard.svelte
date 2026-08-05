<script lang="ts">
	/**
	 * RequirementCard — Phase 1 结果：Coordinator 研究后提出的结构化需求文档，
	 * 等待用户确认后才会进入 Phase 2（映射 3O 蓝图）/ Phase 3（Genesis 施工）。
	 */
	import { ArrowRight, Check, FileText, Loader2, X } from "lucide-svelte";

	interface RequirementDoc {
		title: string;
		context_analysis: string;
		core_features: string[];
	}

	interface Props {
		doc: RequirementDoc;
		approving?: boolean;
		onApprove: () => void;
		onReject?: () => void;
	}

	let { doc, approving = false, onApprove, onReject }: Props = $props();
</script>

<div class="flex w-full max-w-3xl flex-col rounded-xl border border-emerald-500/30 bg-terminal-panel p-4 shadow-lg">
	<div class="mb-3 flex items-center gap-2 border-b border-terminal-edge pb-3">
		<FileText class="size-4 text-emerald-400" />
		<h3 class="font-mono text-sm font-semibold text-terminal-fg">{doc.title}</h3>
		<span class="ml-auto rounded-full border border-terminal-edge px-2 py-0.5 font-mono text-[10px] text-terminal-dim">待确认</span>
	</div>

	<p class="mb-3 whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-terminal-dim">
		{doc.context_analysis}
	</p>

	<ul class="mb-4 flex flex-col gap-1.5">
		{#each doc.core_features as feature, i (i)}
			<li class="flex items-start gap-2 font-mono text-[12px] text-terminal-fg">
				<Check class="mt-0.5 size-3.5 shrink-0 text-emerald-500" />
				<span>{feature}</span>
			</li>
		{/each}
	</ul>

	<div class="flex items-center justify-end gap-2 border-t border-terminal-edge pt-3">
		{#if onReject}
			<button
				type="button"
				onclick={onReject}
				disabled={approving}
				class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-3 py-1.5 font-mono text-xs text-terminal-dim transition hover:text-terminal-fg disabled:opacity-40"
			>
				<X class="size-3.5" /> 拒绝
			</button>
		{/if}
		<button
			type="button"
			onclick={onApprove}
			disabled={approving}
			class="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-1.5 font-mono text-xs font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-50"
		>
			{#if approving}
				<Loader2 class="size-3.5 animate-spin" /> 映射 3O 蓝图中…
			{:else}
				确认需求，交由 Genesis 研发 <ArrowRight class="size-3.5" />
			{/if}
		</button>
	</div>
</div>
