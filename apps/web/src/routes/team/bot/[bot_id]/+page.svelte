<script lang="ts">
	/**
	 * /team/bot/[bot_id] — one bot: chat, its memory, its routines, its config.
	 *
	 * The tabs exist because a phone screen cannot hold a chat *and* a settings
	 * form; the address stays stable (/team/bot/<id>) so a push notification's
	 * click lands here.
	 */
	import { onMount } from "svelte";
	import { ArrowLeft, MessageSquare, Brain, Clock, Settings } from "lucide-svelte";
	import { getBot, type BotView } from "$lib/team/api";
	import PushToggle from "$lib/team/PushToggle.svelte";
	import ChatTab from "./ChatTab.svelte";
	import MemoryTab from "./MemoryTab.svelte";
	import RoutinesTab from "./RoutinesTab.svelte";
	import SettingsTab from "./SettingsTab.svelte";
	import { actionClass } from "$lib/team/ui";

	let { data } = $props<{ data: { botId: string } }>();
	const botId = $derived(data.botId);

	let bot = $state<BotView | null>(null);
	let loading = $state(true);
	let error = $state("");
	let tab = $state<"chat" | "memory" | "routines" | "settings">("chat");

	// Chef's chat must answer inline (its proposal only rides on the sync
	// response); every other bot defers to the background path.
	const mode = $derived<"background" | "inline">(bot?.bot_id === "chef" ? "inline" : "background");
	const botName = $derived(bot?.name || bot?.bot_id || botId);

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			bot = await getBot(botId);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	const TABS = [
		{ id: "chat", label: "聊天", Icon: MessageSquare },
		{ id: "memory", label: "记忆", Icon: Brain },
		{ id: "routines", label: "定时", Icon: Clock },
		{ id: "settings", label: "设置", Icon: Settings },
	] as const;

	onMount(load);
</script>

<div class="flex h-dvh flex-col overflow-hidden">
	<header class="flex shrink-0 items-center gap-2 border-b border-white/[0.07] px-3 py-2">
		<a href="/team" class={actionClass} title="返回团队"><ArrowLeft size={18} /></a>
		<div class="min-w-0 flex-1">
			<p class="truncate text-sm font-medium">{loading ? "加载中…" : botName}</p>
			{#if bot?.model}<p class="truncate text-[10px] opacity-50">{bot.model}</p>{/if}
		</div>
		<PushToggle />
	</header>

	{#if error}
		<p class="bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</p>
	{/if}

	{#if bot}
		<div class="flex min-h-0 flex-1 flex-col">
			{#if tab === "chat"}
				<ChatTab {botId} {botName} {mode} />
			{:else if tab === "memory"}
				<MemoryTab {botId} />
			{:else if tab === "routines"}
				<RoutinesTab {botId} {botName} />
			{:else}
				<SettingsTab {bot} onSaved={() => void load()} />
			{/if}
		</div>
	{/if}

	<nav class="flex shrink-0 border-t border-white/[0.07] pb-[max(0.25rem,env(safe-area-inset-bottom))]">
		{#each TABS as item (item.id)}
			<button
				class="flex min-h-14 flex-1 items-center justify-center gap-1 text-xs transition {tab ===
				item.id
					? 'text-sky-300'
					: 'opacity-50'}"
				onclick={() => (tab = item.id)}
				aria-current={tab === item.id}
			>
				<item.Icon size={18} />
				{item.label}
			</button>
		{/each}
	</nav>
</div>
