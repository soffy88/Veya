<script lang="ts">
	/**
	 * Group roles (W11 W-5).
	 *
	 * Reply mode and the admin bot decide who answers a message in a group:
	 * everyone in order, the admin first, or only the bots that were @mentioned.
	 * The values here are the three the backend already accepts, so this sheet
	 * only edits them — no second role model.
	 */
	import { X } from "lucide-svelte";
	import { patchGroupRoles, type GroupView } from "$lib/team/api";
	import { actionClass, inputClass, primaryClass } from "$lib/team/ui";

	let {
		open,
		group,
		memberNames,
		onclose,
		onsaved,
		onunsupported,
	}: {
		open: boolean;
		group: GroupView;
		memberNames: Record<string, string>;
		onclose: () => void;
		onsaved: (group: GroupView) => void;
		onunsupported: () => void;
	} = $props();

	const MODES = [
		{ value: "everyone", label: "人人都回", desc: "每个成员按顺序说一轮" },
		{ value: "admin-first", label: "主 bot 先回", desc: "默认主 bot 回，被 @ 的成员也回" },
		{ value: "mention-only", label: "只回被点的", desc: "只有被 @ 的成员回，否则主 bot 回" },
	] as const;

	let replyMode = $state("everyone");
	let adminBotId = $state("");
	let notice = $state("");
	let busy = $state(false);
	let error = $state("");

	$effect(() => {
		// Re-seed whenever a different group opens the sheet.
		if (!open) return;
		replyMode = String(group.reply_mode ?? "everyone");
		adminBotId = String(group.admin_bot_id ?? group.bot_ids[0] ?? "");
		notice = String(group.notice ?? "");
		error = "";
	});

	async function save(): Promise<void> {
		if (busy) return;
		busy = true;
		error = "";
		try {
			const updated = await patchGroupRoles(group.id, {
				reply_mode: replyMode,
				admin_bot_id: adminBotId || null,
				notice,
			});
			onsaved(updated);
		} catch (err) {
			const message = err instanceof Error ? err.message : String(err);
			// 404 is the flag being off (VEYA_GROUP_ROLES), not a broken form.
			if (/VEYA_GROUP_ROLES|404/.test(message)) {
				onunsupported();
				return;
			}
			error = message;
		} finally {
			busy = false;
		}
	}
</script>

{#if open}
	<div class="fixed inset-0 z-50 flex flex-col justify-end">
		<button class="absolute inset-0 bg-black/60" aria-label="关闭" onclick={onclose}></button>
		<div
			class="relative max-h-[92dvh] space-y-3 overflow-y-auto rounded-t-2xl border-t border-white/10 bg-terminal-panel p-4 pb-[max(1rem,env(safe-area-inset-bottom))]"
			role="dialog"
			aria-modal="true"
			aria-label="群角色设置"
		>
			<div class="flex items-center gap-2">
				<h2 class="flex-1 text-base font-semibold">群角色</h2>
				<button class={actionClass} onclick={onclose} aria-label="取消"><X size={16} /></button>
			</div>

			<div>
				<p class="mb-1.5 text-xs opacity-60">谁来回消息</p>
				<div class="space-y-2">
					{#each MODES as mode (mode.value)}
						<label
							class="flex min-h-11 items-center gap-3 rounded-lg border px-3 py-2 text-sm transition {replyMode ===
							mode.value
								? 'border-sky-500/60 bg-sky-500/10 text-sky-200'
								: 'border-white/10 bg-white/[0.03]'}"
						>
							<input type="radio" name="reply-mode" class="size-4" value={mode.value} bind:group={replyMode} />
							<span class="min-w-0">
								<span class="block">{mode.label}</span>
								<span class="block text-[11px] opacity-50">{mode.desc}</span>
							</span>
						</label>
					{/each}
				</div>
			</div>

			<div>
				<label class="mb-1 block text-xs opacity-60" for="group-admin">主 bot（admin）</label>
				<select id="group-admin" class={inputClass} bind:value={adminBotId}>
					{#each group.bot_ids as botId (botId)}
						<option value={botId}>{memberNames[botId] ?? botId}</option>
					{/each}
				</select>
			</div>

			<div>
				<label class="mb-1 block text-xs opacity-60" for="group-notice">群公告（每个成员都能看到）</label>
				<textarea id="group-notice" class="{inputClass} min-h-20 resize-y py-2" bind:value={notice}></textarea>
			</div>

			{#if error}
				<p class="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</p>
			{/if}

			<button class="{primaryClass} w-full" type="button" onclick={() => void save()} disabled={busy}>
				{busy ? "保存中…" : "保存"}
			</button>
		</div>
	</div>
{/if}
