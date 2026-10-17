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
	import { Check, CornerUpLeft, RotateCw, Send, GraduationCap, Sparkles, X } from "lucide-svelte";
	import WorkProcess from "$lib/components/WorkProcess.svelte";
	import EvidenceReportBlock from "$lib/components/EvidenceReportBlock.svelte";
	import { api } from "$lib/api";
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

	// Teach skill draft state (GBP2)
	let teachOpen = $state(false);
	let teachSkillName = $state("");
	let teachSkillDesc = $state("");
	let teachTrajectory = $state<Array<{ role: string; text: string }>>([]);
	let teachBusy = $state(false);
	let teachSuccessNotice = $state("");
	let teachError = $state("");

	function openTeachModal(targetMsg: BotMessage): void {
		teachError = "";
		teachSuccessNotice = "";
		const idx = messages.findIndex((m) => m.id === targetMsg.id);
		const userMsg = idx > 0 ? messages.slice(0, idx).reverse().find((m) => m.role === "user") : null;
		teachSkillName = `${botId}_skill_${Date.now().toString().slice(-4)}`;
		teachSkillDesc = (targetMsg.text || "").slice(0, 120);
		teachTrajectory = [
			...(userMsg ? [{ role: "user", text: userMsg.text }] : []),
			{ role: "bot", text: targetMsg.text },
		];
		teachOpen = true;
	}

	async function submitTeachDraft(): Promise<void> {
		if (!teachSkillName.trim()) return;
		teachBusy = true;
		teachError = "";
		teachSuccessNotice = "";
		try {
			const res = await api("gateway", "api/v1/skills/teach/draft", {
				method: "POST",
				body: {
					name: teachSkillName.trim(),
					description: teachSkillDesc.trim(),
					trajectory: teachTrajectory,
					scope_type: "user",
				},
			});
			if (!res.ok) {
				const detail = (res.data as { detail?: string } | null)?.detail || `提交失败 (${res.status})`;
				throw new Error(detail);
			}
			teachSuccessNotice = "技能草稿已提交！请前往「审批」页面批准该草稿以正式激活技能。";
			setTimeout(() => {
				teachOpen = false;
				teachSuccessNotice = "";
			}, 3000);
		} catch (err) {
			teachError = err instanceof Error ? err.message : String(err);
		} finally {
			teachBusy = false;
		}
	}

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
						class="max-w-[85%] rounded-2xl px-3.5 py-2.5 text-sm {message.role ===
						'user'
							? 'bg-gradient-to-br from-sky-500/90 to-violet-600/90 text-white'
							: 'bg-white/[0.05] text-terminal-fg'}"
					>
						{#if message.role === "user"}
							<div class="whitespace-pre-wrap break-words">{message.text}</div>
						{:else}
							<EvidenceReportBlock text={message.text} />
							<div class="mt-2 flex items-center justify-between border-t border-white/5 pt-2 text-[11px] text-white/40">
								<span>{botName}</span>
								<button
									type="button"
									class="inline-flex items-center gap-1 rounded px-2 py-0.5 text-sky-300 transition hover:bg-sky-500/10 hover:text-sky-200"
									title="将此轮对话沉淀为技能"
									onclick={() => openTeachModal(message)}
								>
									<GraduationCap size={13} />
									<span>教一次</span>
								</button>
							</div>
						{/if}
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

	{#if teachOpen}
		<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
			<div class="w-full max-w-lg rounded-2xl border border-white/10 bg-[#121214] p-5 shadow-2xl">
				<div class="flex items-center justify-between border-b border-white/10 pb-3">
					<div class="flex items-center gap-2 font-medium text-white">
						<GraduationCap size={18} class="text-sky-400" />
						<span>教一次：将对话沉淀为技能草稿</span>
					</div>
					<button
						type="button"
						class="text-white/40 hover:text-white"
						onclick={() => (teachOpen = false)}
						aria-label="关闭"
					>
						<X size={18} />
					</button>
				</div>

				<form
					class="mt-4 space-y-3"
					onsubmit={(e) => {
						e.preventDefault();
						void submitTeachDraft();
					}}
				>
					<div>
						<label class="block text-xs font-medium text-white/70 mb-1" for="teach-skill-name">技能名称（标识符）</label>
						<input
							id="teach-skill-name"
							class="{inputClass} text-xs font-mono"
							bind:value={teachSkillName}
							placeholder="例如：auto_summarizer"
							required
						/>
					</div>

					<div>
						<label class="block text-xs font-medium text-white/70 mb-1" for="teach-skill-desc">技能说明 / 目标描述</label>
						<textarea
							id="teach-skill-desc"
							class="{inputClass} min-h-20 text-xs py-2"
							bind:value={teachSkillDesc}
							placeholder="描述该技能的作用与触发条件…"
							required
						></textarea>
					</div>

					<div>
						<label class="block text-xs font-medium text-white/70 mb-1">轨迹快照（前序问答）</label>
						<pre class="max-h-28 overflow-y-auto rounded-lg bg-black/40 p-2 text-[10px] text-white/60 font-mono whitespace-pre-wrap">{JSON.stringify(teachTrajectory, null, 2)}</pre>
					</div>

					<p class="text-[11px] text-white/40">
						提交后将生成带有触发校验规则的技能草稿，并自动发起审批。审批通过后正式入库。
					</p>

					{#if teachSuccessNotice}
						<div class="rounded-lg bg-emerald-950/40 border border-emerald-500/30 p-2.5 text-xs text-emerald-300">
							{teachSuccessNotice}
							<div class="mt-1">
								<a href="/approvals" class="underline text-sky-300 hover:text-sky-200">立即前往审批页面 →</a>
							</div>
						</div>
					{/if}

					{#if teachError}
						<p class="text-xs text-rose-400">{teachError}</p>
					{/if}

					<div class="flex justify-end gap-2 pt-2 border-t border-white/5">
						<button
							type="button"
							class={actionClass}
							onclick={() => (teachOpen = false)}
							disabled={teachBusy}
						>
							取消
						</button>
						<button
							type="submit"
							class={primaryClass}
							disabled={teachBusy || !teachSkillName.trim()}
						>
							{teachBusy ? "提交中…" : "提交技能草稿"}
						</button>
					</div>
				</form>
			</div>
		</div>
	{/if}
</div>
