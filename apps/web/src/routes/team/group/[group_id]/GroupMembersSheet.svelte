<script lang="ts">
	/**
	 * Group members: who is in this room right now.
	 *
	 * A room stays put; members come and go. Every change PATCHes the full
	 * member list, and the room plus its transcript are untouched — the next
	 * round simply offers a turn to whoever is a member then. The sheet stays
	 * open while members change, so several people can join or leave in one
	 * visit.
	 */
	import { X } from "lucide-svelte";
	import { patchGroupMembers, type GroupView } from "$lib/team/api";
	import { actionClass, inputClass, primaryClass } from "$lib/team/ui";

	let {
		open,
		group,
		memberNames,
		onclose,
		onsaved,
	}: {
		open: boolean;
		group: GroupView;
		memberNames: Record<string, string>;
		onclose: () => void;
		onsaved: (group: GroupView) => void;
	} = $props();

	let addId = $state("");
	let busy = $state(false);
	let error = $state("");

	let candidates = $derived(
		Object.keys(memberNames).filter((id) => !group.bot_ids.includes(id)),
	);

	const currentMode = $derived(group.reply_mode ?? "everyone");

	async function apply(next: string[], mode?: string): Promise<void> {
		if (busy) return;
		busy = true;
		error = "";
		try {
			onsaved(await patchGroupMembers(group.id, next, mode));
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function add(): Promise<void> {
		if (!addId) return;
		await apply([...group.bot_ids, addId]);
		addId = "";
	}

	async function remove(botId: string): Promise<void> {
		if (group.bot_ids.length <= 1) {
			error = "群里至少要留 1 个人";
			return;
		}
		await apply(group.bot_ids.filter((id) => id !== botId));
	}
</script>

{#if open}
	<div class="fixed inset-0 z-50 flex flex-col justify-end">
		<button class="absolute inset-0 bg-black/60" aria-label="关闭" onclick={onclose}></button>
		<div
			class="relative max-h-[92dvh] space-y-3 overflow-y-auto rounded-t-2xl border-t border-white/10 bg-terminal-panel p-4 pb-[max(1rem,env(safe-area-inset-bottom))]"
			role="dialog"
			aria-modal="true"
			aria-label="群成员设置"
		>
			<div class="flex items-center gap-2">
				<h2 class="flex-1 text-base font-semibold">群成员（{group.bot_ids.length} 人）</h2>
				<button class={actionClass} onclick={onclose} aria-label="取消"><X size={16} /></button>
			</div>

			<div>
				<label class="mb-1 block text-xs opacity-60" for="group-reply-mode">发言方式</label>
				<select
					id="group-reply-mode"
					class={inputClass}
					value={currentMode}
					disabled={busy}
					onchange={(e) => void apply(group.bot_ids, (e.currentTarget as HTMLSelectElement).value)}
				>
					<option value="everyone">大家都说</option>
					<option value="admin-first">队长先说</option>
					<option value="mention-only">被点名才说</option>
					<option value="lead">领队分工（领队分工→成员回报→领队综合）</option>
				</select>
				<p class="mt-1 text-xs opacity-60">领队分工：房主先分工，成员各自回报，最后房主综合成结论。</p>
			</div>

			<div>
				<p class="mb-1.5 text-xs opacity-60">现在房里的人，点移出就离开（房间和聊天记录不动）</p>
				<div class="space-y-2">
					{#each group.bot_ids as botId (botId)}
						<div class="flex min-h-11 items-center gap-3 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-sm">
							<span class="min-w-0 flex-1">{memberNames[botId] ?? botId}</span>
							<button
								class="rounded-lg bg-white/10 px-3 py-1.5 text-xs transition hover:bg-white/20 disabled:opacity-50"
								type="button"
								disabled={busy || group.bot_ids.length <= 1}
								onclick={() => void remove(botId)}
							>
								移出
							</button>
						</div>
					{/each}
				</div>
			</div>

			<div>
				<label class="mb-1 block text-xs opacity-60" for="group-add-member">加一个人进来</label>
				{#if candidates.length === 0}
					<p class="text-xs opacity-60">没有可加入的人了，先去团队页新建机器人。</p>
				{:else}
					<div class="flex gap-2">
						<select id="group-add-member" class="{inputClass} min-w-0 flex-1" bind:value={addId}>
							<option value="">选一个人…</option>
							{#each candidates as botId (botId)}
								<option value={botId}>{memberNames[botId] ?? botId}</option>
							{/each}
						</select>
						<button
							class={primaryClass}
							type="button"
							disabled={busy || !addId}
							onclick={() => void add()}
						>
							加入
						</button>
					</div>
				{/if}
			</div>

			{#if error}
				<p class="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</p>
			{/if}
		</div>
	</div>
{/if}
