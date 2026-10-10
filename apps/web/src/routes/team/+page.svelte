<script lang="ts">
	/**
	 * /team — the bot team's front door.
	 *
	 * Bot list + create/edit, group list + create, and the Chef entry point.
	 * Everything is a link into a page that owns one thing, because this list has
	 * to stay readable on a phone.
	 */
	import { onMount } from "svelte";
	import { ArrowRight, ChefHat, MessageSquare, Plus, RefreshCw, Users } from "lucide-svelte";
	import { createBot, createGroup, listBots, listGroups, type BotView, type GroupView } from "$lib/team/api";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";

	let bots = $state<BotView[]>([]);
	let groups = $state<GroupView[]>([]);
	let chefId = $state("chef");
	let loading = $state(true);
	let error = $state("");

	let showNewBot = $state(false);
	let newBotId = $state("");
	let newBotName = $state("");
	let newBotDescription = $state("");
	let busy = $state(false);
	let formError = $state("");

	let showNewGroup = $state(false);
	let groupName = $state("");
	let groupMembers = $state<string[]>([]);

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			const [botList, groupList] = await Promise.all([listBots(), listGroups()]);
			bots = botList.bots ?? [];
			chefId = botList.chef_bot_id || "chef";
			groups = groupList;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	async function submitBot(): Promise<void> {
		if (!newBotId.trim()) return;
		busy = true;
		formError = "";
		try {
			await createBot({
				bot_id: newBotId.trim(),
				name: newBotName.trim() || newBotId.trim(),
				description: newBotDescription.trim(),
			});
			newBotId = "";
			newBotName = "";
			newBotDescription = "";
			showNewBot = false;
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
			<button class={actionClass} onclick={() => (showNewBot = !showNewBot)}>
				<Plus size={16} /> 新建
			</button>
		</div>

		{#if showNewBot}
			<form
				class="{cardClass} mb-3 space-y-2"
				onsubmit={(event) => {
					event.preventDefault();
					void submitBot();
				}}
			>
				<input
					class={inputClass}
					placeholder="bot id（小写字母/数字/._-）"
					bind:value={newBotId}
					autocomplete="off"
				/>
				<input class={inputClass} placeholder="名字（可选）" bind:value={newBotName} autocomplete="off" />
				<textarea
					class="{inputClass} min-h-20 py-2"
					placeholder="它负责什么（可选）"
					bind:value={newBotDescription}
				></textarea>
				{#if formError}<p class="text-xs text-red-300">{formError}</p>{/if}
				<div class="flex gap-2">
					<button class={primaryClass} type="submit" disabled={busy || !newBotId.trim()}>创建</button>
					<button class={actionClass} type="button" onclick={() => (showNewBot = false)}>取消</button>
				</div>
			</form>
		{/if}

		{#if loading && bots.length === 0}
			<p class="{cardClass} text-sm opacity-60">加载中…</p>
		{:else if bots.length === 0}
			<p class="{cardClass} text-sm opacity-60">还没有机器人。</p>
		{:else}
			<ul class="space-y-2">
				{#each bots as bot (bot.bot_id)}
					<li>
						<a href="/team/bot/{bot.bot_id}" class="{cardClass} flex items-center gap-3 transition hover:border-sky-500/40">
							<MessageSquare size={16} class="shrink-0 opacity-50" />
							<span class="min-w-0 flex-1">
								<span class="block truncate text-sm font-medium">{bot.name || bot.bot_id}</span>
								{#if bot.description}
									<span class="block truncate text-xs opacity-60">{bot.description}</span>
								{/if}
							</span>
							{#if bot.model}<span class="shrink-0 text-[10px] opacity-50">{bot.model}</span>{/if}
						</a>
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
