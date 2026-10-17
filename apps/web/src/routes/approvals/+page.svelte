<script lang="ts">
	/**
	 * /approvals — the approvals inbox (NANO-W6): pending tool approvals with
	 * once / session / always / deny, plus the "always allow" grants with revoke.
	 * A push notification for one approval opens /approvals/<id> instead.
	 */
	import { onMount } from "svelte";
	import { RefreshCw, Trash2, GraduationCap } from "lucide-svelte";
	import ApprovalCard from "$lib/approvals/ApprovalCard.svelte";
	import { GatesDisabledError, listApprovals, listGrants, revokeGrant, type ApprovalView, type GrantView } from "$lib/approvals/api";
	import { api } from "$lib/api";
	import { actionClass, cardClass, timeLabel } from "$lib/team/ui";

	let approvals = $state<ApprovalView[]>([]);
	let grants = $state<GrantView[]>([]);
	let skillDrafts = $state<Array<{ skill_id: string; name: string; description: string; status: string }>>([]);
	let loading = $state(true);
	let disabled = $state(false);
	let error = $state("");

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			const [apprList, grantList] = await Promise.all([listApprovals("pending"), listGrants()]);
			approvals = apprList;
			grants = grantList;
			disabled = false;
		} catch (err) {
			disabled = err instanceof GatesDisabledError;
			error = disabled ? "" : err instanceof Error ? err.message : String(err);
		}

		try {
			const draftRes = await api("gateway", "api/v1/skills/teach/drafts", { method: "GET" });
			if (draftRes.ok && draftRes.data) {
				skillDrafts = ((draftRes.data as { drafts?: Array<{ skill_id: string; name: string; description: string; status: string }> }).drafts ?? []);
			}
		} catch {
			skillDrafts = [];
		} finally {
			loading = false;
		}
	}

	async function revoke(id: number): Promise<void> {
		try {
			await revokeGrant(id);
			grants = grants.filter((g) => g.id !== id);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		}
	}

	onMount(() => void load());
</script>

<div class="mx-auto flex max-w-2xl flex-col gap-4 p-4">
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-3">
			<a href="/" class="text-xs text-terminal-dim hover:text-terminal-fg">← 返回控制台</a>
			<span class="opacity-20 text-xs">/</span>
			<h1 class="text-lg font-semibold text-terminal-fg">审批</h1>
		</div>
		<button type="button" class={actionClass} onclick={() => void load()} aria-label="刷新"><RefreshCw class="size-4" /></button>
	</div>

	{#if loading}
		<div class="text-sm text-white/50">加载中…</div>
	{:else if disabled}
		<div class={cardClass}><span class="text-sm text-white/70">审批闸门未开启。设置 VEYA_APPROVAL_GATES=1 后，危险操作会在这里等你批准。</span></div>
	{:else}
		{#if error}<div class="text-sm text-rose-300">{error}</div>{/if}
		<section class="flex flex-col gap-2">
			<h2 class="text-sm font-medium text-white/70">待处理（{approvals.length}）</h2>
			{#each approvals as approval (approval.request_id)}
				<ApprovalCard {approval} onresolved={() => void load()} />
			{:else}
				<div class="text-sm text-white/50">没有待处理的审批。</div>
			{/each}
		</section>

		{#if skillDrafts.length > 0}
			<section class="flex flex-col gap-2">
				<h2 class="flex items-center gap-1.5 text-sm font-medium text-sky-300">
					<GraduationCap size={15} />
					<span>教一次技能草稿（{skillDrafts.length}）</span>
				</h2>
				<div class="space-y-2">
					{#each skillDrafts as draft (draft.skill_id)}
						<div class="{cardClass} flex items-center justify-between">
							<div class="min-w-0 flex-1">
								<div class="flex items-center gap-2">
									<span class="text-sm font-medium text-terminal-fg">{draft.name}</span>
									<span class="rounded bg-sky-950 px-1.5 py-0.5 text-[10px] text-sky-400 border border-sky-800">{draft.status}</span>
								</div>
								<div class="mt-0.5 text-xs text-white/60 truncate">{draft.description}</div>
							</div>
						</div>
					{/each}
				</div>
			</section>
		{/if}
		<section class="flex flex-col gap-2">
			<h2 class="text-sm font-medium text-white/70">总是允许（{grants.length}）</h2>
			{#each grants as grant (grant.id)}
				<div class="{cardClass} flex items-center gap-2">
					<div class="min-w-0 flex-1">
						<div class="text-sm text-terminal-fg">{grant.tool}</div>
						<div class="text-[11px] text-white/40">{grant.scope_pattern || "当前账户"} · {timeLabel(grant.created_at)}</div>
					</div>
					<button type="button" class={actionClass} onclick={() => void revoke(grant.id)} aria-label="撤销"><Trash2 class="size-4" />撤销</button>
				</div>
			{:else}
				<div class="text-sm text-white/50">还没有“总是允许”的工具。</div>
			{/each}
		</section>
	{/if}
</div>
