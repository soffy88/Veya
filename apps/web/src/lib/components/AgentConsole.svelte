<script lang="ts">
	/**
	 * AgentConsole — 单引擎 Agent 工作台 (Dashboard 四块之一)。
	 *
	 * 每块独立绑定一个引擎 (claude / codex / grok / pi), 通过
	 * /api/v1/agent/stream + engine 参数跑独立 SSE 会话, 可同时分别给任务。
	 *
	 * 视觉变体 (variant) 复刻对应工具: claude 紫 / codex 蓝 / grok 青绿 / pi TUI。
	 * 工具轨迹: claude/grok 流式事件含 tool_call (Anthropic wire format) 显示徽章;
	 * codex/pi 纯文本流, 无工具轨迹。
	 */
	import { Bot, CheckCircle2, CircleAlert, Copy, Loader2, RotateCcw, Send, Square, User, Wrench, Trash2 } from "lucide-svelte";
	import MarkdownBlock from "./MarkdownBlock.svelte";
	import { API_BASE } from "$lib/api";

	export interface AgentMsg {
		role: "user" | "assistant";
		text: string;
		status: "streaming" | "done" | "error";
		steps: { tool?: string; error?: string }[];
		error?: string;
	}

	interface Props {
		engine: string;
		label: string;
		accent: string; // tailwind accent class, e.g. "violet"
		variant?: "tui" | "default";
		promptPrefix?: string; // e.g. "❯" for Claude
	}

	let { engine, label, accent, variant = "default", promptPrefix = "" }: Props = $props();

	const STORAGE_KEY = $derived(`veya.agent.${engine}`);

	function load(): AgentMsg[] {
		try {
			const raw = localStorage.getItem(STORAGE_KEY);
			if (raw) return JSON.parse(raw) as AgentMsg[];
		} catch {
			/* ignore */
		}
		return [];
	}

	let msgs = $state<AgentMsg[]>(load());
	let input = $state("");
	let busy = $state(false);
	let listEl = $state<HTMLDivElement>();
	let aborter: AbortController | null = null;

	// accent 映射 (tailwind 动态类必须完整写出)
	const ACCENTS: Record<string, { text: string; chip: string; ring: string; dot: string }> = {
		violet: { text: "text-violet-400", chip: "bg-violet-400/10 border-violet-400/30", ring: "focus-within:border-violet-500/60", dot: "bg-violet-400" },
		blue: { text: "text-blue-400", chip: "bg-blue-400/10 border-blue-400/30", ring: "focus-within:border-blue-500/60", dot: "bg-blue-400" },
		teal: { text: "text-teal-400", chip: "bg-teal-400/10 border-teal-400/30", ring: "focus-within:border-teal-500/60", dot: "bg-teal-400" },
		emerald: { text: "text-emerald-400", chip: "bg-emerald-400/10 border-emerald-400/30", ring: "focus-within:border-emerald-500/60", dot: "bg-emerald-400" },
		amber: { text: "text-amber-400", chip: "bg-amber-400/10 border-amber-400/30", ring: "focus-within:border-amber-500/60", dot: "bg-amber-400" },
	};
	const A = $derived(ACCENTS[accent] ?? ACCENTS.violet);

	function persist() {
		try {
			localStorage.setItem(STORAGE_KEY, JSON.stringify(msgs.slice(-60)));
		} catch {
			/* ignore */
		}
	}

	$effect(() => {
		persist();
	});

	$effect(() => {
		void msgs.length;
		void busy;
		if (listEl) listEl.scrollTop = listEl.scrollHeight;
	});

	function scrollBottom() {
		if (!listEl) return;
		requestAnimationFrame(() => {
			if (listEl) listEl.scrollTop = listEl.scrollHeight;
		});
	}

	async function send() {
		const text = input.trim();
		if (!text || busy) return;
		input = "";
		msgs = [...msgs, { role: "user", text, status: "done", steps: [] }];
		msgs = [...msgs, { role: "assistant", text: "", status: "streaming", steps: [] }];
		busy = true;
		aborter = new AbortController();
		const signal = aborter.signal;
		const sid = `agent-${engine}-${Date.now().toString(36)}`;

		try {
			const res = await fetch(`${API_BASE}/api/v1/agent/stream`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ text, engine, session_id: sid }),
				signal,
			});
			if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			let buf = "";

			while (true) {
				const { done, value } = await reader.read();
				if (done) break;
				buf += decoder.decode(value, { stream: true });
				let idx;
				while ((idx = buf.indexOf("\n\n")) >= 0) {
					const frame = buf.slice(0, idx);
					buf = buf.slice(idx + 2);
					for (const line of frame.split("\n")) {
						if (!line.startsWith("data:")) continue;
						const data = line.slice(5).trim();
						if (!data || data === "[DONE]") continue;
						let ev: Record<string, unknown>;
						try {
							ev = JSON.parse(data);
						} catch {
							continue;
						}
						const kind = String(ev.type ?? "");
						if (kind === "text_delta" && typeof ev.delta === "string") {
							const last = msgs.at(-1);
							if (last) last.text += ev.delta;
							scrollBottom();
						} else if (kind === "tool_call") {
							const last = msgs.at(-1);
							if (last)
								last.steps = [
									...last.steps,
									{ tool: String(ev.tool_name ?? "tool") },
								];
						} else if (kind === "engine_done") {
							const last = msgs.at(-1);
							if (last) last.status = "done";
						} else if (kind === "engine_error") {
							throw new Error(typeof ev.error === "string" ? ev.error : "engine error");
						}
					}
				}
			}
			const last = msgs.at(-1);
			if (last && last.status === "streaming") last.status = "done";
		} catch (e) {
			const aborted = aborter?.signal.aborted;
			const last = msgs.at(-1);
			if (last) {
				last.status = aborted ? "done" : "error";
				last.error = aborted ? undefined : e instanceof Error ? e.message : String(e);
			}
		} finally {
			busy = false;
			aborter = null;
		}
	}

	function stop() {
		aborter?.abort();
	}

	function clearAll() {
		msgs = [];
		localStorage.removeItem(STORAGE_KEY);
	}

	function copyText(t: string) {
		void navigator.clipboard?.writeText(t);
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
			e.preventDefault();
			void send();
		} else if (e.key === "Escape" && busy) {
			e.preventDefault();
			stop();
		}
	}
</script>

<div class="flex h-full min-h-0 flex-col bg-[#0d0d0d]">
	<!-- 标题栏: 引擎名 + 状态点 + 清空 -->
	<div class="flex shrink-0 items-center gap-2 border-b border-white/5 px-3 py-2">
		<span class="size-1.5 rounded-full {busy ? 'animate-pulse ' + A.dot : 'bg-white/20'}"></span>
		<span class="font-mono text-[11px] font-bold uppercase tracking-wider {A.text}">{label}</span>
		<span class="flex-1"></span>
		<button type="button" title="清空本引擎会话" onclick={clearAll} class="rounded-md p-1 text-white/30 transition hover:bg-white/10 hover:text-white/70">
			<Trash2 class="size-3.5" />
		</button>
	</div>

	<!-- 消息区 -->
	<div bind:this={listEl} class="min-h-0 flex-1 overflow-y-auto px-3 py-3">
		{#if msgs.length === 0}
			<div class="flex h-full flex-col items-center justify-center gap-2 text-center">
				<div class="flex size-9 items-center justify-center rounded-xl {A.chip}">
					<Bot class="size-4 {A.text}" />
				</div>
				<p class="text-xs text-white/35">
					与 <span class="{A.text}">{label}</span> 对话
				</p>
				<p class="font-mono text-[10px] text-white/20">独立会话 · 可并行执行</p>
			</div>
		{:else}
			<div class="flex flex-col gap-4">
				{#each msgs as m, i (i)}
					<div class="flex items-start gap-2 {m.role === 'user' ? 'flex-row-reverse' : ''}">
						<span class="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full {m.role === 'user' ? 'bg-white/10 text-white' : A.chip + ' ' + A.text}">
							{#if m.role === "user"}
								<User class="size-3" />
							{:else}
								<Bot class="size-3" />
							{/if}
						</span>
						<div class="min-w-0 flex-1">
							{#if m.role === "user"}
								<div class="w-fit max-w-full rounded-xl rounded-tr-sm bg-white/10 px-3 py-1.5 text-[13px] leading-relaxed text-terminal-fg">
									{m.text}
								</div>
							{:else}
								<div class="flex flex-col gap-1.5">
									{#if m.steps.length > 0}
										<div class="flex flex-wrap gap-1">
											{#each m.steps as st, si (si)}
												<span class="flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[10px] {A.chip}">
													<Wrench class="size-2.5" />
													{st.tool ?? "tool"}
												</span>
											{/each}
										</div>
									{/if}
									<div class="text-[13px] leading-relaxed text-terminal-fg">
										{#if m.status === "streaming" && !m.text}
											<div class="flex items-center gap-1.5 text-xs text-white/40">
												<Loader2 class="size-3 animate-spin" />
												正在执行…
											</div>
										{:else if m.text.trim()}
											<MarkdownBlock content={m.text} />
										{/if}
										{#if m.status === "streaming" && m.text}
											<span class="ml-0.5 inline-block size-1.5 rounded-full bg-white align-middle [animation:blink_1s_steps(1)_infinite]"></span>
										{/if}
										{#if m.status === "error"}
											<div class="mt-1 flex items-start gap-1.5 rounded-md bg-red-500/10 p-2 text-xs text-red-400">
												<CircleAlert class="mt-0.5 size-3.5 shrink-0" />
												<span class="min-w-0 break-words">{m.error}</span>
											</div>
										{:else if m.status === "done" && m.text}
											<button type="button" onclick={() => copyText(m.text)} class="mt-1 flex items-center gap-1 rounded p-1 text-[10px] text-white/25 transition hover:bg-white/5 hover:text-white/60">
												<Copy class="size-3" /> 复制
											</button>
										{/if}
									</div>
								</div>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<!-- 输入区 (pi 变体带 TUI 状态栏) -->
	<div class="shrink-0 border-t border-white/5 px-2 py-2">
		{#if variant === "tui"}
			<div class="mb-1 flex items-center gap-2 px-1 font-mono text-[9px] text-white/25">
				<span class="{busy ? 'animate-pulse ' + A.dot : ''} size-1 rounded-full inline-block"></span>
				<span>{label} · {busy ? "RUNNING" : "READY"}</span>
				<span class="flex-1"></span>
				<span>Enter 发送 · Esc 停止</span>
			</div>
		{/if}
		<div class="flex items-end gap-1.5 rounded-lg border border-white/10 bg-[#0d0d0d] px-2 py-1.5 transition {A.ring}">
			{#if promptPrefix}
				<span class="pb-1 font-mono text-xs {A.text}">{promptPrefix}</span>
			{/if}
			<textarea
				bind:value={input}
				rows="1"
				placeholder="给 {label} 下任务…"
				disabled={busy}
				onkeydown={onKeydown}
				class="max-h-28 min-w-0 flex-1 resize-none bg-transparent px-1 py-1 text-[13px] text-terminal-fg outline-none placeholder:text-white/25 disabled:opacity-50"
			></textarea>
			{#if busy}
				<button type="button" title="停止 (Esc)" onclick={stop} class="mb-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg bg-white/10 text-white transition hover:bg-white/20">
					<Square class="size-3.5 fill-current" />
				</button>
			{:else}
				<button type="button" title="发送 (Enter)" onclick={() => void send()} disabled={!input.trim()} class="mb-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg {A.chip} text-white transition hover:opacity-85 disabled:opacity-25">
					<Send class="size-3.5" />
				</button>
			{/if}
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
