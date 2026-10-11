<script lang="ts">
	/**
	 * 1:1 chat with one bot (the /team bot page's default tab).
	 *
	 * Sends with `?async=1` and then polls: a bot turn can run for minutes, and
	 * holding a phone's request open for that long is how it loses the tab. The
	 * reply is only "there" once the transcript carries a bot message with the
	 * task id the 202 handed back, so that is exactly what the poll waits for.
	 *
	 * While the turn runs, the tab also reads the turn's own workbench
	 * projection (`GET /api/v1/workbench/{task_id}`) — the redacted canonical
	 * timeline of the task the turn opened — and renders its tool calls through
	 * the same `WorkProcess` the console uses, plus its pending side-effect
	 * approvals as cards the user can answer. Both come from the projection the
	 * workbench page already renders; nothing here starts, records or re-orders
	 * anything.
	 *
	 * Chef is the exception (`inline`): a Chef turn's whole product is the
	 * confirmable proposal, which only the synchronous response carries, so its
	 * chat must wait for the turn instead of deferring it.
	 */
	import { onDestroy, onMount } from "svelte";
	import { tick } from "svelte";
	import { Check, CornerUpLeft, RotateCw, Send } from "lucide-svelte";
	import WorkProcess from "$lib/components/WorkProcess.svelte";
	import {
		acceptProposal,
		answerCheckpoint,
		listBotMessages,
		sendBotCorrection,
		sendBotMessage,
		type BotMessage,
		type PendingPushTask,
		type TurnReply,
	} from "$lib/team/api";
	import {
		fetchTaskLive,
		resolveTaskApproval,
		type PendingApproval,
	} from "$lib/team/live";
	import { latestBotMessage, markRead } from "$lib/team/unread";
	import { turnStore } from "$lib/team/turns.svelte";
	import { actionClass, inputClass, primaryClass } from "$lib/team/ui";
	import type { ToolStep } from "$lib/chatTypes";

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
	let pendingSessionId = "";
	let proposal = $state<{ id: string; bots: Array<Record<string, unknown>> } | null>(null);
	let accepting = $state(false);
	let accepted = $state<string[]>([]);
	let correctionNote = $state("");
	// W5: the id of the card whose verdict is in flight, so the buttons cannot
	// be pressed twice while the answer is being recorded.
	let answering = $state("");
	// W-2: live tool steps and the turn's pending side-effect approvals.
	let liveSteps = $state<ToolStep[]>([]);
	let approvals = $state<PendingApproval[]>([]);
	let answeringApproval = $state("");
	/** The last thing the user asked, so a failed turn can be retried as-is. */
	let lastAttempt = $state("");

	// W4: while a turn is in flight this box corrects it instead of queuing a
	// second turn. The backend lands it through the item-1 steer, so there is
	// one path, not a parallel control channel.
	const turnRunning = $derived(pendingTaskId !== "");

	function cardIdOf(message: BotMessage): string {
		const meta = message.meta ?? {};
		return meta.kind === "finish_checkpoint" ? (meta.card_id ?? "") : "";
	}

	async function scrollToEnd(): Promise<void> {
		await tick();
		if (thread) thread.scrollTop = thread.scrollHeight;
	}

	async function load(): Promise<void> {
		try {
			messages = await listBotMessages(botId);
			error = "";
			markRead(botId);
			// A turn that finished before this tab opened still has its steps on
			// the record: read the last task's projection once so reopening a chat
			// shows what the bot did, not just what it said.
			const lastTaskId = latestBotMessage(messages)?.task_id ?? "";
			if (lastTaskId && lastTaskId !== pendingTaskId) {
				pendingTaskId = lastTaskId;
				await pollLive();
				pendingTaskId = "";
			}
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

	function resetTurn(): void {
		pendingTaskId = "";
		pendingSessionId = "";
		liveSteps = [];
		approvals = [];
		turnStore.clear(botId);
	}

	/**
	 * One live read of the running turn: its tool steps and whatever it is
	 * waiting for. A projection that cannot answer (task not visible yet, a
	 * flaky minute) leaves the panel as it was instead of blanking it.
	 */
	async function pollLive(): Promise<void> {
		if (!pendingTaskId) return;
		const live = await fetchTaskLive(pendingTaskId);
		if (!live || !pendingTaskId) return;
		liveSteps = live.steps;
		// Approvals are only actionable while a turn is actually running: a card
		// left over from a finished turn would be a button that does nothing.
		if (turnRunning) {
			approvals = live.approvals;
			turnStore.setApprovals(botId, live.approvals);
		}
		if (!live.running) {
			// The projection says the task is done; the transcript poll is what
			// confirms the reply landed, and it is already running.
			return;
		}
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
		if (landed) {
			// A turn that produced no visible reply is a failure the user can see,
			// never a silent empty bubble.
			const reply = fresh.filter((item) => item.role === "bot" && item.task_id === pendingTaskId).at(-1);
			if (reply && !String(reply.text ?? "").trim()) {
				error = `${botName} 这一轮没有给出回复。可以重试，或去任务详情看原因。`;
			}
			resetTurn();
			stopPolling();
		}
		markRead(botId);
		await scrollToEnd();
	}

	async function answerSideEffect(approval: PendingApproval, approved: boolean): Promise<void> {
		if (!pendingTaskId || answeringApproval) return;
		answeringApproval = approval.request_id;
		error = "";
		try {
			await resolveTaskApproval(pendingTaskId, approval.request_id, approved);
			approvals = approvals.filter((item) => item.request_id !== approval.request_id);
			await pollLive();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			answeringApproval = "";
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

	async function submitCorrection(): Promise<void> {
		const text = draft.trim();
		if (!text || sending) return;
		draft = "";
		sending = true;
		error = "";
		try {
			const result = await sendBotCorrection(botId, text);
			correctionNote = result.action === "steer" ? "已发送到当前这一轮" : `已${result.action === "queue" ? "排队" : "处理"}`;
			messages = [
				...messages,
				{ id: `local-${Date.now()}`, role: "user", text, ts: Date.now() / 1000 },
			];
			await scrollToEnd();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			sending = false;
		}
	}

	async function answerCard(
		message: BotMessage,
		verdict: "accept" | "send_back",
	): Promise<void> {
		const cardId = cardIdOf(message);
		if (!cardId || answering !== "") return;
		if (verdict === "send_back" && !draft.trim()) {
			// A card sent back without a note is not a correction; say so
			// instead of silently doing nothing.
			correctionNote = "退回需要写点什么 — 先在上面输入框里说明。";
			return;
		}
		const note = verdict === "send_back" ? draft.trim() : "";
		answering = cardId;
		error = "";
		try {
			await answerCheckpoint(botId, cardId, verdict, note);
			correctionNote = verdict === "accept" ? "已接受这张检查卡。" : "已退回，并把它作为纠正送回这一轮。";
			if (verdict === "send_back") {
				draft = "";
				messages = [
					...messages,
					{ id: `local-${Date.now()}`, role: "user", text: note, ts: Date.now() / 1000 },
				];
				await scrollToEnd();
			}
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			answering = "";
		}
	}

	async function send(text: string): Promise<void> {
		const trimmed = text.trim();
		if (!trimmed || sending) return;
		draft = "";
		sending = true;
		error = "";
		proposal = null;
		lastAttempt = trimmed;
		messages = [...messages, { id: `local-${Date.now()}`, role: "user", text: trimmed, ts: Date.now() / 1000 }];
		await scrollToEnd();
		try {
			const result = await sendBotMessage(botId, trimmed, { background: mode === "background" });
			const pending = result as PendingPushTask;
			if (pending.task_id) {
				pendingTaskId = pending.task_id;
				pendingSessionId = pending.session_id ?? "";
				turnStore.start(botId, { taskId: pending.task_id, sessionId: pendingSessionId });
				stopPolling();
				timer = setInterval(() => {
					void poll();
					void pollLive();
				}, 2500);
				await poll();
				await pollLive();
			} else {
				const inline = result as TurnReply;
				if (inline.proposal) {
					proposal = { id: inline.proposal.id, bots: inline.proposal.bots ?? [] };
				}
				if (inline.task_id) {
					// A synchronous Chef turn still opened a task: its steps are
					// already on the record and worth showing.
					pendingTaskId = inline.task_id;
					await pollLive();
					pendingTaskId = "";
				}
				await load();
			}
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			sending = false;
		}
	}

	async function submit(): Promise<void> {
		if (turnRunning) {
			await submitCorrection();
			return;
		}
		await send(draft);
	}

	onMount(load);
	onDestroy(() => {
		stopPolling();
		resetTurn();
	});
</script>

<div class="flex min-h-0 flex-1 flex-col">
	<div bind:this={thread} class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
		{#if loading}
			<p class="text-sm opacity-60">加载对话…</p>
		{:else if messages.length === 0}
			<p class="text-sm opacity-60">和 {botName} 说第一句话吧。</p>
		{/if}
		{#each messages as message (message.id)}
			{#if cardIdOf(message)}
				{@const cardId = cardIdOf(message)}
				<div class="flex justify-start">
					<div class="max-w-[85%] rounded-2xl border border-amber-500/30 bg-amber-500/[0.07] px-3.5 py-2.5 text-sm whitespace-pre-wrap break-words text-terminal-fg">
						{message.text}
						<div class="mt-2 flex gap-2">
							<button class={primaryClass} onclick={() => void answerCard(message, "accept")} disabled={answering !== ""}>
								<Check size={14} /> 接受
							</button>
							<button class={actionClass} onclick={() => void answerCard(message, "send_back")} disabled={answering !== ""}>
								<CornerUpLeft size={14} /> 退回并说明
							</button>
							{#if answering === cardId}
								<span class="self-center text-xs opacity-60">处理中…</span>
							{/if}
						</div>
					</div>
				</div>
			{:else}
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
			{/if}
		{/each}
		{#if sending}
			<div class="flex justify-start">
				<div class="rounded-2xl bg-white/[0.05] px-3.5 py-2.5 text-sm opacity-60">{botName} 正在思考…</div>
			</div>
		{/if}
	</div>

	<!-- W-2: what the turn is actually doing, and what it is waiting for. -->
	{#if turnRunning || liveSteps.length > 0 || approvals.length > 0}
		<div class="space-y-2 border-t border-white/[0.05] px-4 pt-2">
			{#if liveSteps.length > 0}
				<WorkProcess steps={liveSteps} streaming={turnRunning} />
			{/if}
			{#each approvals as approval (approval.request_id)}
				<div class="rounded-xl border border-amber-500/30 bg-amber-500/[0.07] p-3">
					<p class="text-sm font-medium text-amber-100">需要你批准：{approval.tool_name}</p>
					<p class="mt-0.5 text-xs opacity-70">{approval.reason}</p>
					<div class="mt-2 flex gap-2">
						<button
							class={primaryClass}
							onclick={() => void answerSideEffect(approval, true)}
							disabled={answeringApproval !== ""}
						>
							<Check size={14} /> 批准
						</button>
						<button
							class={actionClass}
							onclick={() => void answerSideEffect(approval, false)}
							disabled={answeringApproval !== ""}
						>
							拒绝
						</button>
						{#if answeringApproval === approval.request_id}
							<span class="self-center text-xs opacity-60">处理中…</span>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	{/if}

	{#if error}
		<div class="border-t border-red-500/30 bg-red-500/10 px-4 py-2">
			<p class="text-xs text-red-300">{error}</p>
			{#if lastAttempt && !sending}
				<button class="{actionClass} mt-2" onclick={() => void send(lastAttempt)}>
					<RotateCw size={14} /> 重试这一句
				</button>
			{/if}
		</div>
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
			placeholder={turnRunning ? "纠正当前这一轮…" : "发消息…"}
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
	{#if turnRunning}
		<p class="border-t border-white/[0.05] px-4 py-1.5 text-[11px] text-terminal-dim">
			这一轮还在跑 — 现在发送的内容会作为纠正送进去，而不是新开一轮。{correctionNote}
		</p>
	{/if}
</div>
