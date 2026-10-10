<script lang="ts">
	/**
	 * 1:1 chat with one bot (the /team bot page's default tab).
	 *
	 * Sends with `?async=1` and then polls: a bot turn can run for minutes, and
	 * holding a phone's request open for that long is how it loses the tab. The
	 * reply is only "there" once the transcript carries a bot message with the
	 * task id the 202 handed back, so that is exactly what the poll waits for.
	 *
	 * Chef is the exception (`inline`): a Chef turn's whole product is the
	 * confirmable proposal, which only the synchronous response carries, so its
	 * chat must wait for the turn instead of deferring it.
	 */
	import { onDestroy, onMount } from "svelte";
	import { tick } from "svelte";
	import { Check, Send } from "lucide-svelte";
	import {
		acceptProposal,
		listBotMessages,
		sendBotMessage,
		type BotMessage,
		type PendingPushTask,
		type TurnReply,
	} from "$lib/team/api";
	import { actionClass, inputClass, primaryClass } from "$lib/team/ui";

	let { botId, botName, mode }: { botId: string; botName: string; mode: "background" | "inline" } =
		$props();

	let messages = $state<BotMessage[]>([]);
	let draft = $state("");
	let loading = $state(true);
	let sending = $state(false);
	let error = $state("");
	let thread: HTMLDivElement | undefined = $state();
	let timer: ReturnType<typeof setInterval> | null = null;
	let pendingTaskId = "";
	let proposal = $state<{ id: string; bots: Array<Record<string, unknown>> } | null>(null);
	let accepting = $state(false);
	let accepted = $state<string[]>([]);

	async function scrollToEnd(): Promise<void> {
		await tick();
		if (thread) thread.scrollTop = thread.scrollHeight;
	}

	async function load(): Promise<void> {
		try {
			messages = await listBotMessages(botId);
			error = "";
			await scrollToEnd();
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
			fresh = await listBotMessages(botId);
		} catch {
			return; // a flaky minute must not resurrect an error banner over a live turn
		}
		const landed = fresh.some((item) => item.role === "bot" && item.task_id === pendingTaskId);
		messages = fresh;
		await scrollToEnd();
		if (landed) {
			pendingTaskId = "";
			stopPolling();
		}
	}

	async function accept(): Promise<void> {
		if (!proposal || accepting) return;
		accepting = true;
		error = "";
		try {
			await acceptProposal(proposal.id);
			accepted = (proposal.bots ?? []).map((bot) => String((bot as { bot_id?: string }).bot_id ?? ""));
			error = "";
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			accepting = false;
		}
	}

	async function submit(): Promise<void> {
		const text = draft.trim();
		if (!text || sending) return;
		draft = "";
		sending = true;
		error = "";
		proposal = null;
		messages = [...messages, { id: `local-${Date.now()}`, role: "user", text, ts: Date.now() / 1000 }];
		await scrollToEnd();
		try {
			const result = await sendBotMessage(botId, text, { background: mode === "background" });
			const pending = result as PendingPushTask;
			if (pending.task_id) {
				pendingTaskId = pending.task_id;
				stopPolling();
				timer = setInterval(() => void poll(), 2500);
				await poll();
			} else {
				const inline = result as TurnReply;
				if (inline.proposal) {
					proposal = { id: inline.proposal.id, bots: inline.proposal.bots ?? [] };
				}
				await load();
			}
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			sending = false;
		}
	}

	onMount(load);
	onDestroy(stopPolling);
</script>

<div class="flex min-h-0 flex-1 flex-col">
	<div bind:this={thread} class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
		{#if loading}
			<p class="text-sm opacity-60">加载对话…</p>
		{:else if messages.length === 0}
			<p class="text-sm opacity-60">和 {botName} 说第一句话吧。</p>
		{/if}
		{#each messages as message (message.id)}
			<div class="flex {message.role === 'user' ? 'justify-end' : 'justify-start'}">
				<div
					class="max-w-[85%] rounded-2xl px-3.5 py-2.5 text-sm whitespace-pre-wrap break-words {message.role ===
					'user'
						? 'bg-gradient-to-br from-sky-500/90 to-violet-600/90 text-white'
						: 'bg-white/[0.05] text-terminal-fg'}"
				>
					{message.text}
				</div>
			</div>
		{/each}
		{#if sending}
			<div class="flex justify-start">
				<div class="rounded-2xl bg-white/[0.05] px-3.5 py-2.5 text-sm opacity-60">{botName} 正在思考…</div>
			</div>
		{/if}
	</div>

	{#if error}
		<p class="border-t border-red-500/30 bg-red-500/10 px-4 py-2 text-xs text-red-300">{error}</p>
	{/if}

	{#if proposal}
		<div class="mx-4 mb-2 rounded-xl border border-sky-500/40 bg-sky-500/10 p-3 text-sm">
			<p class="font-medium text-sky-200">Chef 提议组建团队</p>
			<ul class="mt-1 space-y-0.5 text-xs opacity-80">
				{#each proposal.bots as bot (String((bot as { bot_id?: string }).bot_id ?? ""))}
					<li>
						{String((bot as { name?: string }).name ?? (bot as { bot_id?: string }).bot_id ?? "")} — {String(
							(bot as { description?: string }).description ?? "",
						)}
					</li>
				{/each}
			</ul>
			<div class="mt-2 flex gap-2">
				<button class={primaryClass} onclick={() => void accept()} disabled={accepting}>
					<Check size={16} /> 确认创建
				</button>
				{#if accepted.length > 0}
					<span class="self-center text-xs text-emerald-300">已创建：{accepted.join(", ")}</span>
				{/if}
			</div>
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
			class="{inputClass} max-h-32 min-h-11 resize-none py-2.5"
			placeholder="发消息…"
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
