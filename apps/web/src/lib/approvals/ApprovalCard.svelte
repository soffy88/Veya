<script lang="ts">
	/** One pending approval with the four answers (inbox row and deep-link page). */
	import { ShieldAlert } from "lucide-svelte";
	import { actionClass, cardClass, primaryClass, timeLabel } from "$lib/team/ui";
	import { argsPreview, decisionLabel, resolveApproval, type ApprovalDecision, type ApprovalView } from "./api";

	let { approval, onresolved } = $props<{ approval: ApprovalView; onresolved?: (d: ApprovalDecision) => void }>();
	let busy = $state(false);
	let error = $state("");
	const resolved = $derived(approval.status !== "pending");

	async function answer(decision: ApprovalDecision): Promise<void> {
		busy = true;
		error = "";
		try {
			await resolveApproval(approval.request_id, decision);
			onresolved?.(decision);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

<div class={cardClass}>
	<div class="flex items-start gap-2">
		<ShieldAlert class="mt-0.5 size-4 shrink-0 text-amber-300" />
		<div class="min-w-0 flex-1">
			<div class="text-sm font-medium text-terminal-fg">「{approval.tool}」需要你批准</div>
			{#if argsPreview(approval.tool_args)}
				<pre class="mt-1 whitespace-pre-wrap break-all text-xs text-white/60">{argsPreview(approval.tool_args)}</pre>
			{/if}
			<div class="mt-1 text-[11px] text-white/40">{timeLabel(approval.created_at)}</div>
		</div>
	</div>
	{#if resolved}
		<div class="mt-2 text-xs text-white/60">已处理：{approval.decision ?? approval.status}</div>
	{:else}
		<div class="mt-3 grid grid-cols-2 gap-2">
			<button type="button" class={primaryClass} disabled={busy} onclick={() => void answer("once")}>{decisionLabel.once}</button>
			<button type="button" class={actionClass} disabled={busy} onclick={() => void answer("session")}>{decisionLabel.session}</button>
			<button type="button" class={actionClass} disabled={busy} onclick={() => void answer("always")}>{decisionLabel.always}</button>
			<button type="button" class={actionClass} disabled={busy} onclick={() => void answer("deny")}>{decisionLabel.deny}</button>
		</div>
	{/if}
	{#if error}<div class="mt-2 text-xs text-rose-300">{error}</div>{/if}
</div>
