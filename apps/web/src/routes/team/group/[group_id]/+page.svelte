<script lang="ts">
	/**
	 * /team/group/[group_id] — one group chat.
	 *
	 * Same deferral as the bot chat: a round sends each member in sequence, so it
	 * is posted with `?async=1` and the transcript is polled until this round's
	 * messages land. Replies that chose to PASS are not in the transcript at all
	 * (the backend hides them), which is why nothing here re-renders them.
	 */
	import { onDestroy, onMount } from "svelte";
	import { tick } from "svelte";
	import { ArrowLeft, AtSign, Send, Settings2, Users } from "lucide-svelte";
	import {
		listBots,
		listGroupMessages,
		listGroups,
		sendGroupMessage,
		type BotMessage,
		type GroupView,
	} from "$lib/team/api";
	import PushToggle from "$lib/team/PushToggle.svelte";
	import { actionClass, inputClass, primaryClass } from "$lib/team/ui";
	import GroupRolesSheet from "./GroupRolesSheet.svelte";

	let { data } = $props<{ data: { groupId: string } }>();

	let group = $state<GroupView | null>(null);
	let bots = $state<Record<string, string>>({});
	let messages = $state<BotMessage[]>([]);
	let draft = $state("");
	let loading = $state(true);
	let sending = $state(false);
	let error = $state("");
	let thread: HTMLDivElement | undefined = $state();
	let timer: ReturnType<typeof setInterval> | null = null;
	let expectedCount = 0;
	// W-5: who answers is a group setting; the sheet edits it through the same
	// roles endpoint the group already has.
	let showRoles = $state(false);
	let rolesSupported = $state(true);
	let composer: HTMLTextAreaElement | undefined = $state();
	const groupId = $derived(data.groupId);

	async function load(): Promise<void> {
		try {
			const list = await listGroupMessages(groupId);
			messages = list;
			error = "";
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		}
	}

	async function loadGroup(): Promise<void> {
		loading = true;
		try {
			// Groups have no per-id endpoint in this build, so the id is resolved
			// from the collection once and the name kept for the header.
			const groups = await listGroups();
			group = groups.find((item) => item.id === groupId) ?? null;
			const list = await listBots();
			bots = Object.fromEntries((list.bots ?? []).map((bot) => [bot.bot_id, bot.name || bot.bot_id]));
			if (!group) {
				error = "群聊不存在或已被删除";
			} else {
				await load();
			}
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	function stopPolling(): void {
		if (timer !== null) clearInterval(timer);
		timer = null;
	}

	async function poll(): Promise<void> {
		let fresh: BotMessage[];
		try {
			fresh = await listGroupMessages(groupId);
		} catch {
			return;
		}
		messages = fresh;
		await tick();
		if (thread) thread.scrollTop = thread.scrollHeight;
		// One round appends up to one bot message per member; when the transcript
		// has grown by at least that, the round is over.
		if (fresh.length >= expectedCount) {
			stopPolling();
			sending = false;
		}
	}

	async function submit(): Promise<void> {
		const text = draft.trim();
		if (!text || sending) return;
		draft = "";
		sending = true;
		error = "";
		messages = [...messages, { id: `local-${Date.now()}`, role: "user", text, ts: Date.now() / 1000 }];
		await tick();
		if (thread) thread.scrollTop = thread.scrollHeight;
		expectedCount = messages.length + (group?.bot_ids.length ?? 1);
		try {
			await sendGroupMessage(groupId, text, { background: true });
			stopPolling();
			timer = setInterval(() => void poll(), 2500);
			await poll();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
			sending = false;
		}
	}

	/** W-5: append "@bot " to the draft — the backend matches @bot ids verbatim. */
	function mention(botId: string): void {
		const token = `@${botId} `;
		if (draft.includes(token)) return;
		draft = `${draft.trimEnd()} ${token}`.trim();
		composer?.focus();
	}

	onMount(loadGroup);
	onDestroy(stopPolling);
</script>

<div class="flex h-dvh flex-col overflow-hidden">
	<header class="flex shrink-0 items-center gap-2 border-b border-white/[0.07] px-3 py-2">
		<a href="/team" class={actionClass} title="返回团队"><ArrowLeft size={18} /></a>
		<Users size={18} class="shrink-0 opacity-50" />
		<div class="min-w-0 flex-1">
			<p class="truncate text-sm font-medium">{loading ? "加载中…" : (group?.name ?? groupId)}</p>
			{#if group}
				<p class="truncate text-[10px] opacity-50">
					{group.bot_ids.map((id) => bots[id] ?? id).join("、")}
					{#if group.reply_mode === "admin-first"} · 主 bot 先回{/if}
					{#if group.reply_mode === "mention-only"} · 只回被点的{/if}
				</p>
			{/if}
		</div>
		{#if group}
			<button
				class={actionClass}
				onclick={() => (showRoles = true)}
				title="群角色"
				aria-label="群角色设置"
			>
				<Settings2 size={16} />
			</button>
		{/if}
		<PushToggle />
	</header>

	{#if error}
		<p class="bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</p>
	{/if}
	{#if !rolesSupported}
		<p class="bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
			这个部署还没有群角色功能（VEYA_GROUP_ROLES 未开启）— 现在每个成员都会按顺序回消息。
		</p>
	{/if}

	<div bind:this={thread} class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
		{#if loading}
			<p class="text-sm opacity-60">加载群聊…</p>
		{:else if messages.length === 0}
			<p class="text-sm opacity-60">在这个群里说第一句话。</p>
		{/if}
		{#each messages as message (message.id)}
			{#if message.role === "user"}
				<div class="flex justify-end">
					<div class="max-w-[85%] rounded-2xl bg-gradient-to-br from-sky-500/90 to-violet-600/90 px-3.5 py-2.5 text-sm whitespace-pre-wrap break-words text-white">
						{message.text}
					</div>
				</div>
			{:else}
				<div class="flex justify-start gap-2">
					<span
						class="mt-1 size-7 shrink-0 rounded-lg bg-white/[0.06] text-center text-[11px] leading-7 opacity-70"
						title={message.bot_id ?? ""}
					>
						{(bots[String(message.bot_id ?? "")] ?? "bot").slice(0, 2)}
					</span>
					<div class="max-w-[78%] min-w-0">
						<p class="text-[10px] opacity-50">{bots[String(message.bot_id ?? "")] ?? message.bot_id}</p>
						<div class="rounded-2xl bg-white/[0.05] px-3.5 py-2.5 text-sm whitespace-pre-wrap break-words">
							{message.text}
						</div>
					</div>
				</div>
			{/if}
		{/each}
		{#if sending}
			<p class="text-center text-xs opacity-50">群里正在轮流说…</p>
		{/if}
	</div>

	{#if group && group.bot_ids.length > 1}
		<!-- W-5: @mention decides who answers; the chips insert the exact token. -->
		<div class="flex shrink-0 flex-wrap items-center gap-1.5 border-t border-white/[0.05] px-3 pt-2">
			<AtSign size={12} class="opacity-40" />
			{#each group.bot_ids as botId (botId)}
				<button
					class="min-h-11 rounded-full border border-white/10 bg-white/[0.04] px-3 text-xs"
					onclick={() => mention(botId)}
				>
					@{bots[botId] ?? botId}
				</button>
			{/each}
		</div>
	{/if}

	<form
		class="flex shrink-0 items-end gap-2 border-t border-white/[0.07] p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]"
		onsubmit={(event) => {
			event.preventDefault();
			void submit();
		}}
	>
		<textarea
			bind:this={composer}
			class="{inputClass} max-h-32 min-h-11 resize-none py-2.5"
			placeholder={group?.notice ? group.notice : "发给整个群…"}
			bind:value={draft}
			rows="1"
			onkeydown={(event) => {
				if (event.key === "Enter" && !event.shiftKey) {
					event.preventDefault();
					void submit();
				}
			}}
		></textarea>
		<button class={primaryClass} type="submit" disabled={sending || !draft.trim()}>
			<Send size={16} />
		</button>
	</form>
</div>

{#if group}
	<GroupRolesSheet
		open={showRoles}
		{group}
		memberNames={bots}
		onclose={() => (showRoles = false)}
		onsaved={(updated) => {
			group = updated;
			showRoles = false;
			rolesSupported = true;
		}}
		onunsupported={() => {
			showRoles = false;
			rolesSupported = false;
		}}
	/>
{/if}
