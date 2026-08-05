<script lang="ts">
	/**
	 * AgentChat — 多轮聊天，经 legacy coordinator 驱动（/legacy/prompt +
	 * /legacy/stream/{session_id}），可实时看到 agent 调用了哪些工具
	 * （tool_call 事件，例如 bash/read/write/grep），执行完把结果写回聊天，
	 * 类似 codex 的对话式命令执行。
	 *
	 * 同一 sessionId 贯穿整个会话（coordinator 按 session_id 保留上下文），
	 * EventSource 只开一条，跨多轮复用。
	 */
	import { Bot, Brain, CheckCircle2, CircleAlert, Code2, Loader2, RotateCcw, Send, Terminal, User, Wrench } from "lucide-svelte";
	import { artifactStore } from "$lib/artifacts.svelte";

	interface StepEvent {
		type: string;
		[key: string]: unknown;
	}

	interface Turn {
		id: string;
		role: "user" | "assistant";
		text: string;
		events: StepEvent[];
		status: "streaming" | "done" | "error";
		cost?: number;
		error?: string;
	}

	const QUICK_TASKS = [
		"写一个冒泡排序函数并写好测试",
		"分析 tests/ 目录的测试覆盖情况",
		"跑一下 git status 看看当前改动",
		"重构 veya/tools.py 里重复的代码",
	];

	const PERSONAS: [string, string][] = [
		["build", "build · 完整工具集(bash/read/write/grep…)"],
		["plan", "plan · 只读规划"],
		["research", "research · 检索调研"],
	];

	let input = $state("");
	let persona = $state("build");
	let sessionId = $state("");
	let turns = $state<Turn[]>([]);
	let sending = $state(false);
	let listEl = $state<HTMLDivElement>();

	let es: EventSource | undefined;
	let activeTurnId = "";

	function ensureStream() {
		if (es) return;
		sessionId = crypto.randomUUID();
		es = new EventSource(`/legacy/stream/${sessionId}`);
		es.onmessage = (ev) => {
			if (ev.data === "[DONE]") return;
			let payload: StepEvent;
			try {
				payload = JSON.parse(ev.data);
			} catch {
				return;
			}
			const turn = turns.find((t) => t.id === activeTurnId);
			if (!turn) return;
			turn.events = [...turn.events, payload];
			if (payload.type === "text_delta" && typeof payload.delta === "string") {
				turn.text = payload.delta;
			}
		};
	}

	function newSession() {
		es?.close();
		es = undefined;
		sessionId = "";
		activeTurnId = "";
		turns = [];
	}

	async function send() {
		const text = input.trim();
		if (!text || sending) return;
		ensureStream();
		input = "";
		sending = true;

		turns = [...turns, { id: crypto.randomUUID(), role: "user", text, events: [], status: "done" }];
		const aid = crypto.randomUUID();
		activeTurnId = aid;
		turns = [...turns, { id: aid, role: "assistant", text: "", events: [], status: "streaming" }];

		try {
			const res = await fetch("/legacy/prompt", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ text, session_id: sessionId, persona }),
			});
			const data = await res.json();
			const turn = turns.find((t) => t.id === aid)!;
			if (!res.ok) {
				turn.status = "error";
				turn.error = typeof data === "string" ? data : JSON.stringify(data);
			} else {
				const squads = Array.isArray(data.squads) ? data.squads : [];
				const content = squads
					.map((s: Record<string, unknown>) => {
						const out = s.output;
						if (typeof out === "string") return out;
						if (out && typeof out === "object") return (out as Record<string, unknown>).content ?? s.error ?? "";
						return s.error ?? "";
					})
					.filter(Boolean)
					.join("\n\n");
				turn.text = String(content || turn.text || "(no output)");
				turn.status = data.status === "failed" ? "error" : "done";
				turn.cost = typeof data.cost_usd === "number" ? data.cost_usd : 0;
			}
		} catch (e) {
			const turn = turns.find((t) => t.id === aid)!;
			turn.status = "error";
			turn.error = e instanceof Error ? e.message : String(e);
		} finally {
			sending = false;
		}
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === "Enter" && !e.shiftKey) {
			e.preventDefault();
			send();
		}
	}

	// activity events worth showing inline (tool calls + squad lifecycle) —
	// text_delta / cost_update are folded into the bubble text / footer instead
	function visibleEvents(turn: Turn): StepEvent[] {
		return turn.events.filter((e) => e.type === "tool_call" || e.type === "squad_start" || e.type === "squad_done");
	}

	function stepIcon(ev: StepEvent) {
		switch (ev.type) {
			case "tool_call":
				return { Icon: Wrench, cls: "text-amber-400 bg-amber-400/10 border-amber-400/30" };
			case "squad_start":
				return { Icon: Brain, cls: "text-violet-400 bg-violet-400/10 border-violet-400/30" };
			case "squad_done":
				return { Icon: CheckCircle2, cls: "text-emerald-400 bg-emerald-400/10 border-emerald-400/30" };
			default:
				return { Icon: Terminal, cls: "text-sky-400 bg-sky-400/10 border-sky-400/30" };
		}
	}

	function stepLabel(ev: StepEvent): string {
		switch (ev.type) {
			case "tool_call":
				return `$ ${ev.tool_name}`;
			case "squad_start":
				return `squad · ${ev.role}`;
			case "squad_done":
				return `done · ${ev.role} · ${ev.status}`;
			default:
				return String(ev.type);
		}
	}

	function stepDetail(ev: StepEvent): string {
		if (ev.type === "tool_call" && ev.tool_args != null) {
			try {
				return JSON.stringify(ev.tool_args);
			} catch {
				return "";
			}
		}
		return "";
	}

	$effect(() => {
		void turns.length;
		if (listEl) {
			requestAnimationFrame(() => {
				if (listEl) listEl.scrollTop = listEl.scrollHeight;
			});
		}
	});
</script>

<div class="flex min-h-0 flex-1 flex-col gap-3">
	<!-- chat log -->
	<div bind:this={listEl} class="flex min-h-[420px] flex-1 flex-col gap-3 overflow-y-auto rounded-xl border border-terminal-edge bg-terminal-panel p-3">
		{#if turns.length === 0}
			<div class="flex h-full flex-col items-center justify-center gap-2 text-terminal-dim">
				<Bot class="size-8 opacity-40" />
				<p class="text-sm">跟 agent 聊天，直接让它跑命令</p>
				<p class="text-xs opacity-70">POST /legacy/prompt · GET /legacy/stream/{"{session}"}</p>
			</div>
		{:else}
			{#each turns as turn (turn.id)}
			{@const parsed = artifactStore.parseArtifactsFromText(turn.text)}
			<div class="flex items-start gap-2.5 {turn.role === 'user' ? 'flex-row-reverse' : ''}">
					<span
						class="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-md border {turn.role === 'user'
							? 'border-sky-500/40 bg-sky-500/10 text-sky-300'
							: 'border-violet-500/40 bg-violet-500/10 text-violet-300'}"
					>
						{#if turn.role === "user"}
							<User class="size-3.5" />
						{:else}
							<Bot class="size-3.5" />
						{/if}
					</span>

					<div class="max-w-[85%] min-w-0 rounded-xl border border-terminal-edge bg-terminal-bg px-3 py-2 {turn.role === 'user' ? 'bg-sky-500/5' : ''}">
						{#if turn.role === "assistant" && visibleEvents(turn).length > 0}
							<ol class="mb-2 flex flex-col gap-1 border-b border-terminal-edge/60 pb-2">
								{#each visibleEvents(turn) as ev, i (i)}
									{@const b = stepIcon(ev)}
									{@const Icon = b.Icon}
									<li class="flex items-start gap-2">
										<span class={`mt-0.5 flex size-5 shrink-0 items-center justify-center rounded border ${b.cls}`}>
											<Icon class="size-3" />
										</span>
										<div class="min-w-0 flex-1 font-mono text-[11px] leading-relaxed">
											<span class="font-semibold text-terminal-fg">{stepLabel(ev)}</span>
											{#if stepDetail(ev)}
												<span class="ml-1 break-words text-terminal-dim">{stepDetail(ev)}</span>
											{/if}
										</div>
									</li>
								{/each}
							</ol>
						{/if}

						<p class="whitespace-pre-wrap break-words font-mono text-[13px] leading-relaxed text-terminal-fg">
							{parsed.pureText}
						</p>

						{#if turn.role === "assistant" && parsed.artifacts.length > 0}
							<div class="mt-2 flex flex-col gap-2">
								{#each parsed.artifacts as art (art.id)}
									<button
										type="button"
										onclick={() => artifactStore.setActive(art)}
										class="group flex items-center gap-3 rounded-lg border border-emerald-900/50 bg-emerald-950/30 p-2.5 text-left transition hover:bg-emerald-900/40"
									>
										<span class="flex size-8 shrink-0 items-center justify-center rounded-md bg-emerald-900/50 group-hover:bg-emerald-800/80">
											<Code2 class="size-4 text-emerald-400" />
										</span>
										<span class="min-w-0">
											<span class="block text-sm font-bold text-emerald-100">{art.title || "React Artifact"}</span>
											<span class="block text-xs text-emerald-500/70">Click to run in Sandbox</span>
										</span>
									</button>
								{/each}
							</div>
						{/if}

						{#if turn.status === "streaming"}
							<div class="mt-1.5 flex items-center gap-1.5 text-terminal-dim">
								<Loader2 class="size-3 animate-spin" />
								<span class="font-mono text-[10px]">运行中…</span>
							</div>
						{:else if turn.status === "error"}
							<div class="mt-1.5 flex items-start gap-1.5 text-rose-300">
								<CircleAlert class="mt-0.5 size-3 shrink-0" />
								<span class="font-mono text-[10px] break-words">{turn.error}</span>
							</div>
						{:else if turn.role === "assistant" && turn.cost !== undefined}
							<div class="mt-1.5 font-mono text-[10px] text-terminal-dim">cost ${turn.cost.toFixed(6)}</div>
						{/if}
					</div>
				</div>
			{/each}
		{/if}
	</div>

	<!-- composer -->
	<div class="flex flex-col gap-2 rounded-xl border border-terminal-edge bg-terminal-panel p-3">
		<div class="flex items-end gap-3">
			<div class="min-w-0 flex-1">
				<label for="chat-input" class="mb-1 block font-mono text-[11px] text-terminal-dim">
					message — 直接说想让 agent 做什么，会实时跑工具（bash/read/write/grep…）
				</label>
				<textarea
					id="chat-input"
					bind:value={input}
					onkeydown={onKeydown}
					rows="2"
					placeholder="例如：帮我跑一下 pytest，看看有没有失败的用例"
					class="w-full resize-none rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 font-mono text-[13px] text-terminal-fg outline-none transition placeholder:text-terminal-dim/60 focus:border-sky-500/60"
				></textarea>
			</div>
			<div class="flex shrink-0 flex-col items-stretch gap-2 pb-0.5">
				<select
					bind:value={persona}
					class="rounded-lg border border-terminal-edge bg-terminal-bg px-2 py-1.5 font-mono text-[11px] text-terminal-dim outline-none focus:border-sky-500/60"
				>
					{#each PERSONAS as [value, label] (value)}
						<option {value}>{label}</option>
					{/each}
				</select>
				<button
					type="button"
					onclick={send}
					disabled={!input.trim() || sending}
					class="flex items-center justify-center gap-1.5 rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 px-4 py-2 font-mono text-xs font-semibold text-white transition hover:brightness-110 disabled:opacity-40"
				>
					{#if sending}
						<Loader2 class="size-3.5 animate-spin" />
					{:else}
						<Send class="size-3.5" />
					{/if}
					send
				</button>
			</div>
		</div>

		<!-- quick tasks -->
		<div class="flex flex-wrap items-center gap-1.5">
			{#each QUICK_TASKS as t (t)}
				<button
					type="button"
					onclick={() => (input = t)}
					class="rounded-full border border-terminal-edge px-2.5 py-1 font-mono text-[10px] text-terminal-dim transition hover:border-sky-500/40 hover:text-sky-300"
				>
					{t.length > 22 ? t.slice(0, 22) + "…" : t}
				</button>
			{/each}
			<span class="flex-1"></span>
			{#if sessionId}
				<span class="font-mono text-[10px] text-terminal-dim/70">session {sessionId.slice(0, 8)}</span>
			{/if}
			{#if turns.length > 0}
				<button
					type="button"
					onclick={newSession}
					class="flex items-center gap-1 font-mono text-[10px] text-terminal-dim transition hover:text-terminal-fg"
				>
					<RotateCcw class="size-3" /> new session
				</button>
			{/if}
		</div>
	</div>
</div>
