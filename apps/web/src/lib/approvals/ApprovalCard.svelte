<script lang="ts">
	/** One pending approval with the four answers (inbox row and deep-link page). */
	import { ShieldAlert, GraduationCap, CheckCircle } from "lucide-svelte";
	import { actionClass, cardClass, primaryClass, timeLabel } from "$lib/team/ui";
	import { argsPreview, decisionLabel, resolveApproval, type ApprovalDecision, type ApprovalView } from "./api";

	let { approval, onresolved } = $props<{ approval: ApprovalView; onresolved?: (d: ApprovalDecision) => void }>();
	let busy = $state(false);
	let error = $state("");
	const resolved = $derived(approval.status !== "pending");
	const isTeachSkill = $derived(approval.tool === "teach_skill_draft");
	const toolArgs = $derived((approval.tool_args as Record<string, unknown>) ?? {});

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
		{#if isTeachSkill}
			<GraduationCap class="mt-0.5 size-5 shrink-0 text-sky-400" />
			<div class="min-w-0 flex-1">
				<div class="flex items-center gap-2">
					<span class="rounded bg-sky-500/20 px-2 py-0.5 text-xs font-medium text-sky-300">教一次技能草稿审批</span>
					<span class="text-sm font-semibold text-white">{String(toolArgs.name || "技能草稿")}</span>
				</div>
				<div class="mt-1.5 space-y-1 text-xs text-white/70">
					<p>技能ID：<code class="rounded bg-black/40 px-1 py-0.5 font-mono text-[11px] text-sky-300">{String(toolArgs.skill_id || "")}</code></p>
					{#if toolArgs.skill_version_id}
						<p>版本：<code class="rounded bg-black/40 px-1 py-0.5 font-mono text-[11px] text-white/60">{String(toolArgs.skill_version_id)}</code></p>
					{/if}
					<p class="text-emerald-300/90">✓ 批准后将激活入库，可分配给任意 Bot 并在对话中自主调用。</p>
				</div>
				<div class="mt-1 text-[11px] text-white/40">{timeLabel(approval.created_at)}</div>
			</div>
		{:else}
			<ShieldAlert class="mt-0.5 size-4 shrink-0 text-amber-300" />
			<div class="min-w-0 flex-1">
				<div class="text-sm font-medium text-terminal-fg">「{approval.tool}」需要你批准</div>
				{#if argsPreview(approval.tool_args)}
					<pre class="mt-1 whitespace-pre-wrap break-all text-xs text-white/60">{argsPreview(approval.tool_args)}</pre>
				{/if}
				<div class="mt-1 text-[11px] text-white/40">{timeLabel(approval.created_at)}</div>
			</div>
		{/if}
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
