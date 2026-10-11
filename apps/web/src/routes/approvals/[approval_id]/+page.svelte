<script lang="ts">
	/** /approvals/[id] — where an approval push lands: answer it, then back to the inbox. */
	import { onMount } from "svelte";
	import { ArrowLeft } from "lucide-svelte";
	import ApprovalCard from "$lib/approvals/ApprovalCard.svelte";
	import { GatesDisabledError, getApproval, type ApprovalView } from "$lib/approvals/api";
	import { actionClass } from "$lib/team/ui";

	let { data } = $props<{ data: { approvalId: string } }>();
	let approval = $state<ApprovalView | null>(null);
	let loading = $state(true);
	let message = $state("");

	async function load(): Promise<void> {
		loading = true;
		message = "";
		try {
			approval = await getApproval(data.approvalId);
			if (!approval) message = "找不到这条审批，可能已过期。";
		} catch (err) {
			message = err instanceof GatesDisabledError ? "审批闸门未开启。" : err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	onMount(() => void load());
</script>

<div class="mx-auto flex max-w-2xl flex-col gap-4 p-4">
	<a href="/approvals" class="{actionClass} self-start"><ArrowLeft class="size-4" />全部审批</a>
	{#if loading}
		<div class="text-sm text-white/50">加载中…</div>
	{:else if approval}
		<ApprovalCard {approval} onresolved={() => void load()} />
	{:else}
		<div class="text-sm text-white/60">{message}</div>
	{/if}
</div>
