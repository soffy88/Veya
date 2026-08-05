<script lang="ts">
	/**
	 * ChatConsole — human ⇄ Master Brain conversation (Claude/Codex-grade).
	 *
	 * - Streaming SSE over POST /api/v1/agent/stream (compat bridge → master brain)
	 * - Full Markdown rendering + syntax-highlighted code blocks
	 * - Collapsible tool-call traces, stop button, ↑ edit last prompt, retry on error
	 * - Sessions persisted via sessionStore (localStorage)
	 *
	 * Genesis 施工 (FlowConsole) is intentionally NOT here: it is the
	 * master-model ↔ Genesis channel, not a human conversation surface.
	 */
	import {
		Bot,
		Brain,
		CheckCircle2,
		CircleAlert,
		Copy,
		Loader2,
		RotateCcw,
		Send,
		Square,
		Terminal,
		User,
		Wrench,
	} from "lucide-svelte";
	import MarkdownBlock from "./MarkdownBlock.svelte";
	import { sessionStore } from "$lib/sessionStore.svelte";
	import { apiKeyStore } from "$lib/settings.svelte";
	import type { ChatMessage, ToolStep } from "$lib/chatTypes";

	const SUGGESTIONS = [
		"分析当前项目的目录结构，找出 3O 主库里已有的算子",
		"写一个基于 MACD 的风险控制拦截器设计",
		"解释 git rebase 和 merge 的区别",
		"检查 tests/ 目录的测试覆盖情况",
	];

	let input = $state("");
	let busy = $state(false);
	let lastUserText = $state("");
	let listEl = $state<HTMLDivElement>();
	let textareaEl = $state<HTMLTextAreaElement>();
	let aborter: AbortController | null = null;
	let expandedSteps = $state<Set<number>>(new Set());

	const sid = $derived(sessionStore.activeSid);
	const messages = $derived(
		sessionStore.sessions.find((s) => s.sid === sid)?.messages ?? [],
	);

	function scrollBottom() {
		if (!listEl) return;
		requestAnimationFrame(() => {
			if (listEl) listEl.scrollTop = listEl.scrollHeight;
		});
	}

	$effect(() => {
		void messages.length;
		void busy;
		scrollBottom();
	});

	function newSession() {
		sessionStore.newSession();
		input = "";
	}

	function toggleStep(i: number) {
		const next = new Set(expandedSteps);
		if (next.has(i)) next.delete(i);
		else next.add(i);
		expandedSteps = next;
	}

	// ── 工具轨迹图标/文案 ───────────────────────────────────────────
	function stepMeta(ev: ToolStep) {
		switch (ev.type) {
			case "tool_call":
				return { Icon: Wrench, cls: "text-amber-400 bg-amber-400/10 border-amber-400/30", label: `$ ${String(ev.tool_name ?? "tool")}` };
			case "tool_error":
				return { Icon: CircleAlert, cls: "text-rose-400 bg-rose-400/10 border-rose-400/30", label: `✗ ${String(ev.tool_name ?? "tool")}` };
			case "master_round":
				return { Icon: Brain, cls: "text-violet-400 bg-violet-400/10 border-violet-400/30", label: "思考…" };
			case "master_start":
				return { Icon: Terminal, cls: "text-sky-400 bg-sky-400/10 border-sky-400/30", label: "任务开始" };
			default:
				return { Icon: Terminal, cls: "text-sky-400 bg-sky-400/10 border-sky-400/30", label: String(ev.type) };
		}
	}

	function stepDetail(ev: ToolStep): string {
		if (ev.type === "tool_call" && ev.tool_args != null) {
			try {
				return JSON.stringify(ev.tool_args).slice(0, 200);
			} catch {
				return "";
			}
		}
		if (typeof ev.error === "string") return ev.error.slice(0, 200);
		return "";
	}

	// ── 发送/流式 ────────────────────────────────────────────────────
	function ensureSession() {
		if (!sessionStore.activeSid) sessionStore.newSession();
	}

	async function send(overrideText?: string) {
		const text = (overrideText ?? input).trim();
		if (!text || busy) return;
		ensureSession();
		input = "";
		lastUserText = text;

		const sid = sessionStore.activeSid;
		sessionStore.append(sid, { role: "user", text, status: "done", steps: [] });
		sessionStore.append(sid, { role: "assistant", text: "", status: "streaming", steps: [] });
		busy = true;

		aborter = new AbortController();
		const signal = aborter.signal;

		try {
			const res = await fetch("/api/v1/agent/stream", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					text,
					session_id: sid,
					provider: apiKeyStore.provider,
					model: apiKeyStore.model.trim() || undefined,
					config: apiKeyStore.asConfig(),
				}),
				signal,
			});
			if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			let buf = "";

			const handleFrame = (data: string) => {
				if (data === "[DONE]") return;
				let ev: Record<string, unknown>;
				try {
					ev = JSON.parse(data);
				} catch {
					return;
				}
				const kind = String(ev.type ?? ev.event ?? "");
				if (kind === "text_delta" && typeof ev.delta === "string") {
					sessionStore.patchLast(sid, { text: (sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1)?.text ?? "") + ev.delta });
				} else if (kind === "master_done") {
					sessionStore.patchLast(sid, {
						status: "done",
						text: (sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1)?.text ?? "") || (typeof ev.final === "string" ? ev.final : ""),
					});
				} else if (kind === "tool_call" || kind === "tool_error" || kind === "master_round" || kind === "master_start") {
					const last = sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1);
					if (last) last.steps = [...last.steps, ev as ToolStep];
				} else if (kind === "error") {
					throw new Error(typeof ev.error === "string" ? ev.error : "agent error");
				}
			};

			while (true) {
				const { done, value } = await reader.read();
				if (done) break;
				buf += decoder.decode(value, { stream: true });
				let idx;
				while ((idx = buf.indexOf("\n\n")) >= 0) {
					const frame = buf.slice(0, idx);
					buf = buf.slice(idx + 2);
					for (const line of frame.split("\n")) {
						if (line.startsWith("data:")) handleFrame(line.slice(5).trim());
					}
				}
			}
			sessionStore.patchLast(sid, { status: "done" });
		} catch (e) {
			const aborted = aborter?.signal.aborted;
			sessionStore.patchLast(sid, {
				status: aborted ? "stopped" : "error",
				error: aborted ? undefined : e instanceof Error ? e.message : String(e),
			});
		} finally {
			busy = false;
			aborter = null;
		}
	}

	function stop() {
		aborter?.abort();
	}

	function retryLast() {
		if (busy || !lastUserText) return;
		void send(lastUserText);
	}

	function editLastPrompt() {
		if (busy) return;
		const last = messages.at(-1);
		const lastUser = [...messages].reverse().find((m) => m.role === "user");
		if (!lastUser) return;
		input = lastUser.text;
		lastUserText = "";
		// 移除最后一条用户消息及其后续(未完成的助手消息)
		const idx = messages.indexOf(lastUser);
		const s = sessionStore.sessions.find((x) => x.sid === sid);
		if (s) {
			s.messages = s.messages.slice(0, idx);
			sessionStore.persist();
		}
		void last;
		textareaEl?.focus();
	}

	function copyMessage(text: string) {
		void navigator.clipboard?.writeText(text);
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
			e.preventDefault();
			void send();
		} else if (e.key === "ArrowUp" && !input.trim()) {
			e.preventDefault();
			editLastPrompt();
		} else if (e.key === "Escape" && busy) {
			e.preventDefault();
			stop();
		}
	}

	function onTextareaInput() {
		if (!textareaEl) return;
		textareaEl.style.height = "auto";
		textareaEl.style.height = Math.min(textareaEl.scrollHeight, 200) + "px";
	}
</script>

<div class="flex min-h-0 flex-1 flex-col overflow-hidden">
	<!-- ── 消息流 ─────────────────────────────────────────────────── -->
	<div bind:this={listEl} class="flex-1 overflow-y-auto px-4 py-6 md:px-6">
		<div class="mx-auto flex max-w-3xl flex-col gap-6">
			{#if messages.length === 0}
				<div class="flex flex-col items-center gap-6 py-16">
					<div class="flex size-14 items-center justify-center rounded-2xl bg-gradient-to-br from-sky-500 to-violet-600 font-mono text-lg font-bold text-white">
						V
					</div>
					<div class="text-center">
						<h2 class="text-lg font-semibold text-terminal-fg">和 Veya 主脑对话</h2>
						<p class="mt-1 text-sm text-terminal-dim">直接描述任务，主脑会实时调用工具并流式返回结果</p>
					</div>
					<div class="flex w-full max-w-md flex-col gap-2">
						{#each SUGGESTIONS as s (s)}
							<button
								type="button"
								onclick={() => void send(s)}
								class="rounded-xl border border-terminal-edge bg-terminal-panel px-4 py-3 text-left text-sm text-terminal-dim transition hover:border-sky-500/40 hover:text-terminal-fg"
							>
								{s}
							</button>
						{/each}
					</div>
				</div>
			{:else}
				{#each messages as msg, i (i)}
					<div class="flex items-start gap-3 {msg.role === 'user' ? 'flex-row-reverse' : ''}">
						<span
							class="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg border {msg.role === 'user'
								? 'border-sky-500/40 bg-sky-500/10 text-sky-300'
								: 'border-violet-500/40 bg-violet-500/10 text-violet-300'}"
						>
							{#if msg.role === "user"}
								<User class="size-4" />
							{:else}
								<Bot class="size-4" />
							{/if}
						</span>

						<div class="min-w-0 max-w-[85%] flex-1 {msg.role === 'user' ? 'max-w-[70%]' : ''}">
							{#if msg.role === "user"}
								<div class="w-fit max-w-full rounded-2xl rounded-tr-sm bg-terminal-panel px-4 py-2.5 text-sm text-terminal-fg">
									{msg.text}
								</div>
							{:else}
								<div class="flex flex-col gap-2">
									{#if msg.steps.length > 0}
										<div class="flex flex-col gap-1">
											{#each msg.steps as ev, si (si)}
												{@const meta = stepMeta(ev)}
												{@const Icon = meta.Icon}
												<button
													type="button"
													onclick={() => toggleStep(si)}
													class="flex w-fit max-w-full items-center gap-2 rounded-lg border px-2.5 py-1.5 text-left font-mono text-[11px] {meta.cls}"
												>
													<Icon class="size-3.5 shrink-0" />
													<span class="font-semibold">{meta.label}</span>
													{#if stepDetail(ev)}
														<span class="max-w-[300px] truncate opacity-80">
															{expandedSteps.has(si) ? stepDetail(ev) : stepDetail(ev).slice(0, 60) + "…"}
														</span>
													{/if}
												</button>
											{/each}
										</div>
									{/if}

									<div class="rounded-2xl rounded-tl-sm border border-terminal-edge bg-terminal-panel px-4 py-3">
										{#if msg.status === "streaming" && !msg.text}
											<div class="flex items-center gap-2 text-sm text-terminal-dim">
												<Loader2 class="size-4 animate-spin text-sky-400" />
												正在思考…
											</div>
										{:else if msg.text}
											<MarkdownBlock content={msg.text} />
										{/if}

										{#if msg.status === "streaming" && msg.text}
											<span class="mt-1 inline-block size-2 bg-sky-400 align-middle [animation:blink_1s_steps(1)_infinite]"></span>
										{/if}

										{#if msg.status === "error"}
											<div class="mt-2 flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/5 p-2.5 text-sm text-rose-300">
												<CircleAlert class="mt-0.5 size-4 shrink-0" />
												<span class="min-w-0 break-words">{msg.error}</span>
											</div>
											<div class="mt-2 flex gap-2">
												<button
													type="button"
													onclick={retryLast}
													class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-3 py-1.5 font-mono text-xs text-terminal-dim transition hover:border-sky-500/40 hover:text-terminal-fg"
												>
													<RotateCcw class="size-3.5" />
													重试
												</button>
												<button
													type="button"
													onclick={() => copyMessage(msg.error ?? "")}
													class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-3 py-1.5 font-mono text-xs text-terminal-dim transition hover:border-sky-500/40 hover:text-terminal-fg"
												>
													<Copy class="size-3.5" />
													复制
												</button>
											</div>
										{:else if msg.status === "stopped"}
											<div class="mt-2 font-mono text-xs text-amber-400">已停止</div>
										{:else if msg.status === "done" && msg.text}
											<div class="mt-2 flex items-center gap-2">
												<button
													type="button"
													onclick={() => copyMessage(msg.text)}
													class="flex items-center gap-1 rounded-md px-2 py-1 font-mono text-[11px] text-terminal-dim transition hover:bg-white/5 hover:text-terminal-fg"
													title="复制回答"
												>
													<Copy class="size-3" />
													复制
												</button>
												{#if typeof msg.cost === "number" && msg.cost > 0}
													<span class="ml-auto font-mono text-[10px] text-terminal-dim/70">cost ${msg.cost.toFixed(6)}</span>
												{/if}
											</div>
										{/if}
									</div>
								</div>
							{/if}
						</div>
					</div>
				{/each}
			{/if}
		</div>
	</div>

	<!-- ── composer ────────────────────────────────────────────────── -->
	<div class="shrink-0 border-t border-terminal-edge bg-terminal-panel/60 px-4 py-4 md:px-6">
		<div class="mx-auto flex max-w-3xl flex-col gap-2">
			<div class="flex items-end gap-2 rounded-2xl border border-terminal-edge bg-terminal-bg p-2 transition focus-within:border-sky-500/60 focus-within:shadow-[0_0_0_3px_rgba(56,189,248,0.1)]">
				<textarea
					bind:this={textareaEl}
					bind:value={input}
					rows="1"
					placeholder="告诉主脑你想做什么…（Enter 发送 · Shift+Enter 换行 · ↑ 编辑上一条）"
					disabled={busy}
					onkeydown={onKeydown}
					oninput={onTextareaInput}
					class="max-h-[200px] min-w-0 flex-1 resize-none bg-transparent px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 disabled:opacity-50"
				></textarea>
				{#if busy}
					<button
						type="button"
						onclick={stop}
						title="停止生成 (Esc)"
						class="mb-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-rose-500/20 text-rose-400 transition hover:bg-rose-500/30"
					>
						<Square class="size-4 fill-current" />
					</button>
				{:else}
					<button
						type="button"
						onclick={() => void send()}
						disabled={!input.trim()}
						title="发送 (Enter)"
						class="mb-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-r from-sky-500 to-violet-600 text-white transition hover:brightness-110 disabled:opacity-40"
					>
						<Send class="size-4" />
					</button>
				{/if}
			</div>
			<div class="flex items-center gap-3 px-1 font-mono text-[11px] text-terminal-dim/70">
				<span class="flex items-center gap-1.5">
					<span class="size-1.5 rounded-full {apiKeyStore.api_key ? 'bg-emerald-500' : 'bg-amber-500'}"></span>
					{apiKeyStore.current.label} · {apiKeyStore.model.trim() || apiKeyStore.current.defaultModel || "未设置模型"}
				</span>
				<span class="flex-1"></span>
				<span class="hidden md:inline"><kbd class="rounded border border-terminal-edge px-1">Enter</kbd> 发送</span>
				<span class="hidden md:inline"><kbd class="rounded border border-terminal-edge px-1">Shift+Enter</kbd> 换行</span>
				<span class="hidden md:inline"><kbd class="rounded border border-terminal-edge px-1">↑</kbd> 编辑</span>
				{#if busy}
					<span class="hidden md:inline"><kbd class="rounded border border-terminal-edge px-1">Esc</kbd> 停止</span>
				{/if}
			</div>
		</div>
	</div>
</div>

<style>
	@keyframes blink {
		50% {
			opacity: 0;
		}
	}
</style>
