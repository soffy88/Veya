<script lang="ts">
	/**
	 * /team — the bot team's front door (W11 W-1/W-3/W-6).
	 *
	 * This is where a first-time user creates a bot, so the page leads with that
	 * and only then with the list. Every bot card carries what a phone user
	 * needs to pick one: whether it is busy, what it last said, and whether
	 * there is anything unread.
	 */
	import { onMount } from "svelte";
	import { ArrowRight, ChefHat, MessageSquare, Plus, RefreshCw, Trash2, Users } from "lucide-svelte";
	import {
		createGroup,
		deleteBot,
		listBotMessages,
		listBots,
		listGroups,
		type BotMessage,
		type BotView,
		type GroupView,
	} from "$lib/team/api";
	import { hasUnread, latestBotMessage, markRead } from "$lib/team/unread";
import { actionClass, cardClass, firstLine, inputClass, primaryClass } from "$lib/team/ui";
import { turnStore } from "$lib/team/turns.svelte";
import NewBotSheet from "./NewBotSheet.svelte";

	let bots = $state<BotView[]>([]);
	let groups = $state<GroupView[]>([]);
	let chefId = $state("chef");
	let loading = $state(true);
	let error = $state("");

	/** bot_id → the newest thing it said, for the card's second line. */
	let lastSaid = $state<Record<string, string>>({});
	let unread = $state<Record<string, boolean>>({});

	let showNewBot = $state(false);
	let confirmDeleteId = $state("");

	let showNewGroup = $state(false);
	let groupName = $state("");
	let groupMembers = $state<string[]>([]);
	let busy = $state(false);
	let formError = $state("");

	/** A brand-new account has neither a bot nor a group yet. */
	const firstRun = $derived(!loading && bots.length === 0 && groups.length === 0);

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			const [botList, groupList] = await Promise.all([listBots(), listGroups()]);
			bots = botList.bots ?? [];
			chefId = botList.chef_bot_id || "chef";
			groups = groupList;
			await loadSummaries();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	/**
	 * One read per bot for its last line and unread dot. A team is a handful of
	 * bots, and both facts come off the same transcript, so this is one request
	 * per bot rather than a second endpoint to keep in sync.
	 */
	async function loadSummaries(): Promise<void> {
		const said: Record<string, string> = {};
		const unreadMap: Record<string, boolean> = {};
		await Promise.all(
			(bots ?? []).map(async (bot) => {
				try {
					const messages: BotMessage[] = await listBotMessages(bot.bot_id);
					const last = latestBotMessage(messages);
					said[bot.bot_id] = last ? firstLine(last.text) : "";
					unreadMap[bot.bot_id] = hasUnread(bot.bot_id, messages);
				} catch {
					// A bot whose transcript cannot be read still gets a card; only
					// its second line is missing.
				}
			}),
		);
		lastSaid = said;
		unread = unreadMap;
	}

	function openBot(botId: string): void {
		markRead(botId);
		location.assign(`/team/bot/${botId}`);
	}

	async function remove(bot: BotView): Promise<void> {
		busy = true;
		formError = "";
		try {
			await deleteBot(bot.bot_id);
			confirmDeleteId = "";
			await load();
		} catch (err) {
			formError = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function submitGroup(): Promise<void> {
		if (!groupName.trim() || groupMembers.length === 0) return;
		busy = true;
		formError = "";
		try {
			const group = await createGroup(groupName.trim(), groupMembers);
			groupName = "";
			groupMembers = [];
			showNewGroup = false;
			await load();
			// The round is where a group exists; land the user in it.
			location.assign(`/team/group/${group.id}`);
		} catch (err) {
			formError = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	function toggleMember(botId: string): void {
		groupMembers = groupMembers.includes(botId)
			? groupMembers.filter((id) => id !== botId)
			: [...groupMembers, botId];
	}

	function phaseLabel(botId: string): string {
		const phase = turnStore.phase(botId);
		if (phase === "awaiting_approval") return "待批准";
		if (phase === "running") return "运行中";
		return "";
	}

	onMount(load);
</script>

<div class="mx-auto w-full max-w-3xl p-4 pb-24">
	<header class="mb-5 flex items-center justify-between gap-3">
		<div class="min-w-0">
			<h1 class="text-xl font-semibold tracking-tight">Bot 团队</h1>
			<p class="text-xs opacity-60">起 bot、1:1 聊、群聊，跑完会推送到手机。</p>
		</div>
		<button class={actionClass} onclick={() => void load()} disabled={loading}>
			<RefreshCw size={16} class={loading ? "animate-spin" : ""} /> 刷新
		</button>
	</header>

	{#if error}
		<div class="{cardClass} mb-4 border-red-500/40 text-sm text-red-300">{error}</div>
	{/if}

	{#if firstRun}
		<!-- W-6: the first thing a new account sees is what to do first. -->
		<div class="{cardClass} mb-6 border-sky-500/30 bg-sky-500/[0.06]">
			<h2 class="text-sm font-semibold text-sky-200">从第一个 Bot 开始</h2>
			<p class="mt-1 text-xs opacity-70">
				给它一个名字和一句话职责，选一种起点，就能直接聊天。它会记住你的事、按计划自己干活，需要动到文件或外部服务时会先问你。
			</p>
			<button class="{primaryClass} mt-3" onclick={() => (showNewBot = true)}>
				<Plus size={16} /> 新建 Bot
			</button>
		</div>
	{/if}

	<!-- Chef: the one bot that proposes a team instead of answering questions. -->
	<a
		href="/team/bot/{chefId}"
		class="{cardClass} mb-5 flex items-center gap-3 transition hover:border-sky-500/40"
	>
		<span class="flex size-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 to-violet-600">
			<ChefHat size={18} class="text-white" />
		</span>
		<span class="min-w-0 flex-1">
			<span class="block text-sm font-medium">Chef</span>
			<span class="block truncate text-xs opacity-60">说想要一个团队，它提方案，你确认后才创建</span>
		</span>
		<ArrowRight size={16} class="shrink-0 opacity-40" />
	</a>

	<section class="mb-6">
		<div class="mb-2 flex items-center justify-between">
			<h2 class="text-sm font-medium opacity-80">机器人</h2>
			<button class={actionClass} onclick={() => (showNewBot = true)}>
				<Plus size={16} /> 新建
			</button>
		</div>

		{#if loading && bots.length === 0}
			<p class="{cardClass} text-sm opacity-60">加载中…</p>
		{:else if bots.length === 0}
			<p class="{cardClass} text-sm opacity-60">还没有机器人。</p>
		{:else}
			<ul class="space-y-2">
				{#each bots as bot (bot.bot_id)}
					{@const phase = phaseLabel(bot.bot_id)}
					<li class="{cardClass} flex items-start gap-3">
						<button class="min-w-0 flex-1 text-left" onclick={() => openBot(bot.bot_id)}>
							<span class="flex items-center gap-2">
								<MessageSquare size={16} class="shrink-0 opacity-50" />
								<span class="min-w-0 flex-1 truncate text-sm font-medium">{bot.name || bot.bot_id}</span>
								{#if unread[bot.bot_id]}
									<span class="size-2 shrink-0 rounded-full bg-sky-400" title="有新消息"></span>
								{/if}
							</span>
							{#if phase}
								<span
									class="mt-1 inline-block rounded-full px-2 py-0.5 text-[10px] {phase === '待批准'
										? 'bg-amber-500/15 text-amber-200'
										: 'bg-sky-500/15 text-sky-200'}"
								>
									{phase}
								</span>
							{/if}
							{#if lastSaid[bot.bot_id]}
								<span class="mt-1 block truncate text-xs opacity-60">{lastSaid[bot.bot_id]}</span>
							{:else if bot.description}
								<span class="mt-1 block truncate text-xs opacity-60">{bot.description}</span>
							{/if}
							{#if bot.model}
								<span class="mt-1 block text-[10px] opacity-40">{bot.model}</span>
							{/if}
						</button>
						{#if confirmDeleteId === bot.bot_id}
							<button
								class="{actionClass} shrink-0 !border-red-500/40 !text-red-300"
								onclick={() => void remove(bot)}
								disabled={busy}
							>
								确认删除
							</button>
						{:else}
							<button
								class="{actionClass} shrink-0"
								onclick={() => (confirmDeleteId = bot.bot_id)}
								title="删除这个机器人"
							>
								<Trash2 size={14} />
							</button>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<section>
		<div class="mb-2 flex items-center justify-between">
			<h2 class="text-sm font-medium opacity-80">群聊</h2>
			<button class={actionClass} onclick={() => (showNewGroup = !showNewGroup)}>
				<Plus size={16} /> 新建
			</button>
		</div>

		{#if showNewGroup}
			<form
				class="{cardClass} mb-3 space-y-3"
				onsubmit={(event) => {
					event.preventDefault();
					void submitGroup();
				}}
			>
				<input class={inputClass} placeholder="群名" bind:value={groupName} autocomplete="off" />
				<fieldset>
					<legend class="mb-1 text-xs opacity-60">成员（1–6 个）</legend>
					<div class="flex flex-wrap gap-2">
						{#each bots as bot (bot.bot_id)}
							<label
								class="inline-flex min-h-11 items-center gap-2 rounded-lg border px-3 text-sm transition {groupMembers.includes(
									bot.bot_id,
								)
									? 'border-sky-500/60 bg-sky-500/10 text-sky-200'
									: 'border-white/10 bg-white/[0.03]'}"
							>
								<input
									type="checkbox"
									class="size-4"
									checked={groupMembers.includes(bot.bot_id)}
									onchange={() => toggleMember(bot.bot_id)}
								/>
								{bot.name || bot.bot_id}
							</label>
						{/each}
					</div>
				</fieldset>
				{#if formError}<p class="text-xs text-red-300">{formError}</p>{/if}
				<div class="flex gap-2">
					<button class={primaryClass} type="submit" disabled={busy || !groupName.trim() || groupMembers.length === 0}>
						创建并进入
					</button>
					<button class={actionClass} type="button" onclick={() => (showNewGroup = false)}>取消</button>
				</div>
			</form>
		{/if}

		{#if groups.length === 0}
			<p class="{cardClass} text-sm opacity-60">还没有群聊。选 2 个以上机器人即可开始。</p>
		{:else}
			<ul class="space-y-2">
				{#each groups as group (group.id)}
					<li>
						<a href="/team/group/{group.id}" class="{cardClass} flex items-center gap-3 transition hover:border-sky-500/40">
							<Users size={16} class="shrink-0 opacity-50" />
							<span class="min-w-0 flex-1 truncate text-sm font-medium">{group.name}</span>
							<span class="shrink-0 text-[10px] opacity-50">{group.bot_ids.length} 个成员</span>
						</a>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
</div>

<NewBotSheet
	open={showNewBot}
	takenIds={bots.map((bot) => bot.bot_id)}
	onclose={() => (showNewBot = false)}
	oncreated={(botId) => {
		showNewBot = false;
		openBot(botId);
	}}
/>
