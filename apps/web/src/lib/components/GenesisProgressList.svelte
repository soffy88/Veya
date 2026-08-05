<script module lang="ts">
	export type ElementStatus = "pending" | "running" | "success" | "failed";
</script>

<script lang="ts">
	/**
	 * GenesisProgressList — Phase 3: 每个 3O 元素的锻造进度（pending → running → done/failed），
	 * 由 FlowConsole 根据 genesis_element_start / genesis_element_done SSE 事件驱动更新。
	 */
	import { CheckCircle2, CircleAlert, Circle, Hammer, Loader2 } from "lucide-svelte";

	interface ProgressElement {
		layer: string;
		name: string;
		status: ElementStatus;
		error?: string;
		response?: string;
	}

	interface Props {
		elements: ProgressElement[];
	}

	let { elements }: Props = $props();

	function statusStyle(status: ElementStatus) {
		switch (status) {
			case "running":
				return { Icon: Loader2, cls: "text-status-running bg-status-running/10 border-status-running/30", spin: true };
			case "success":
				return { Icon: CheckCircle2, cls: "text-status-success bg-status-success/10 border-status-success/30", spin: false };
			case "failed":
				return { Icon: CircleAlert, cls: "text-status-error bg-status-error/10 border-status-error/30", spin: false };
			default:
				return { Icon: Circle, cls: "text-status-pending bg-status-pending/10 border-terminal-edge", spin: false };
		}
	}
</script>

<div class="flex w-full max-w-3xl flex-col gap-2 rounded-xl border border-terminal-edge bg-terminal-panel p-4">
	<div class="mb-1 flex items-center gap-2 border-b border-terminal-edge pb-2">
		<Hammer class="size-4 text-amber-400" />
		<h3 class="font-mono text-sm font-semibold text-terminal-fg">Genesis 施工进度</h3>
	</div>

	<ol class="flex flex-col gap-1.5">
		{#each elements as el, i (i)}
			{@const s = statusStyle(el.status)}
			{@const Icon = s.Icon}
			<li class="flex items-start gap-2.5">
				<span class={`mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-md border ${s.cls}`}>
					<Icon class="size-3.5 {s.spin ? 'animate-spin' : ''}" />
				</span>
				<div class="min-w-0 flex-1 font-mono text-[12px] leading-relaxed">
					<span class="font-semibold text-terminal-fg">{el.layer}</span>
					<span class="text-terminal-dim">/{el.name}</span>
					{#if el.status === "failed" && el.error}
						<div class="mt-0.5 break-words text-[11px] text-rose-300">{el.error}</div>
					{:else if el.status === "success" && el.response}
						<div class="mt-0.5 break-words text-[11px] text-terminal-dim">{el.response}</div>
					{/if}
				</div>
			</li>
		{/each}
	</ol>
</div>
