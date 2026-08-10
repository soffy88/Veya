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
		Code2,
		Copy,
		ListTodo,
		Loader2,
		RotateCcw,
		Send,
		Square,
		User,
		Wrench,
	} from "lucide-svelte";
	import MarkdownBlock from "./MarkdownBlock.svelte";
import ModelPicker from "./ModelPicker.svelte";
	import { artifactStore } from "$lib/artifacts.svelte";
	import { sessionStore } from "$lib/sessionStore.svelte";
	import { apiKeyStore } from "$lib/settings.svelte";
	import { API_BASE } from "$lib/api";
	import { planStore } from "$lib/planStore.svelte";
	import PlanCard from "./PlanCard.svelte";
	import FileTree from "./FileTree.svelte";
	import { Folder, Mic, Paperclip } from "lucide-svelte";
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
	let fileTreeOpen = $state(false);
	let dictating = $state(false);
	let dictationError = $state("");
	let recognition: { stop: () => void; lang: string } | null = null;

	// ── 附件上传 (文件/图片) ──────────────────────────────────────────
	interface Attachment {
		name: string;
		content: string;
	}
	let attachments = $state<Attachment[]>([]);
	let images = $state<string[]>([]); // base64 data URI
	let fileInput = $state<HTMLInputElement>();
	let uploadError = $state("");

	const TEXT_EXT = [".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".csv", ".log", ".html", ".css", ".svelte", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".sh", ".pdf", ".docx", ".xlsx"];

	function pickFiles() {
		fileInput?.click();
	}

	async function onFilesChosen(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const files = [...(input.files ?? [])];
		input.value = "";
		uploadError = "";
		const MAX_UPLOAD = 100 * 1024 * 1024; // 100MB
		const MAX_INLINE_TEXT = 100 * 1024; // ≤100KB 直接注入内容
		const MAX_IMG = 5 * 1024 * 1024; // 图片 ≤5MB base64
		for (const f of files) {
			if (f.size > MAX_UPLOAD) {
				uploadError = `文件超过 100MB 上限: ${f.name}`;
				continue;
			}
			if (f.type.startsWith("image/")) {
				if (f.size > MAX_IMG) {
					uploadError = `图片过大 (>5MB): ${f.name} — 请压缩后重试`;
					continue;
				}
				const dataUrl = await new Promise<string>((resolve) => {
					const r = new FileReader();
					r.onload = () => resolve(String(r.result ?? ""));
					r.readAsDataURL(f);
				});
				if (dataUrl) images = [...images, dataUrl];
				continue;
			}
			const ext = "." + (f.name.split(".").pop() ?? "").toLowerCase();
			if (!TEXT_EXT.includes(ext)) {
				uploadError = `不支持的文件类型: ${f.name} — 仅文本类可上传, 其他用文件树 @path`;
				continue;
			}
			if (f.size <= MAX_INLINE_TEXT) {
				// 小文本: 读内容直接注入消息
				const text = await f.text();
				attachments = [...attachments, { name: f.name, content: text }];
			} else {
				// 大文本 (≤100MB): 上传到工作区 → 消息放 @uploads/<path> 引用
				try {
					const fd = new FormData();
					fd.append("file", f);
					const res = await fetch(`${API_BASE}/api/v1/fs/upload`, { method: "POST", body: fd });
					if (!res.ok) throw new Error(`HTTP ${res.status}`);
					const data = (await res.json()) as { path: string };
					attachments = [
						...attachments,
						{ name: `${f.name} (大文件, 已存工作区 @${data.path})`, content: `@${data.path}` },
					];
				} catch (err) {
					uploadError = `大文件上传失败: ${f.name} — ${err instanceof Error ? err.message : String(err)}`;
				}
			}
		}
	}

	function onPaste(e: ClipboardEvent) {
		const items = e.clipboardData?.items;
		if (!items) return;
		for (const it of items) {
			if (it.type.startsWith("image/")) {
				e.preventDefault();
				const f = it.getAsFile();
				if (!f) continue;
				const r = new FileReader();
				r.onload = () => {
					if (typeof r.result === "string") images = [...images, r.result];
				};
				r.readAsDataURL(f);
			}
		}
	}

	function removeImage(i: number) {
		images = images.filter((_, idx) => idx !== i);
	}
	function removeAttachment(i: number) {
		attachments = attachments.filter((_, idx) => idx !== i);
	}

	// ── 语音听写 (Web Speech API, 纯前端) ────────────────────────────
	function toggleDictation() {
		if (dictating) {
			recognition?.stop();
			dictating = false;
			return;
		}
		const SR = (window as unknown as Record<string, unknown>).SpeechRecognition
			?? (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
		if (!SR) {
			dictationError = "浏览器不支持语音听写 (请用 Chrome/Edge)";
			return;
		}
		dictationError = "";
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const rec = new (SR as new () => any)();
		rec.lang = "zh-CN";
		rec.continuous = false;
		rec.interimResults = true;
		rec.onresult = (e: { resultIndex: number; results: { [k: number]: { [k: number]: { transcript: string } }; length: number } }) => {
			let t = "";
			for (let i = e.resultIndex; i < e.results.length; i++) t += e.results[i][0]?.transcript ?? "";
			if (t) input = t;
		};
		rec.onend = () => {
			dictating = false;
			recognition = null;
		};
		rec.onerror = (ev: { error?: string }) => {
			dictating = false;
			dictationError = `语音识别错误: ${ev.error ?? "unknown"}`;
		};
		recognition = rec;
		dictating = true;
		rec.start();
	}
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
	// master_start / master_round (任务开始/思考…) 是过程噪音, 不展示 —
	// 只保留真正有用的执行轨迹: 工具调用/失败/hicode 进度。
	function stepMeta(ev: ToolStep) {
		switch (ev.type) {
			case "tool_call":
				return { Icon: Wrench, cls: "text-amber-400 bg-amber-400/10 border-amber-400/30", label: `$ ${String(ev.tool_name ?? "tool")}` };
			case "tool_error":
				return { Icon: CircleAlert, cls: "text-rose-400 bg-rose-400/10 border-rose-400/30", label: `✗ ${String(ev.tool_name ?? "tool")}` };
			case "hicode_progress": {
				const stage = String(ev.stage ?? "");
				if (stage === "planning")
					return { Icon: Brain, cls: "text-emerald-400 bg-emerald-400/10 border-emerald-400/30", label: "hicode 规划中" };
				if (stage === "executing")
					return { Icon: Code2, cls: "text-emerald-400 bg-emerald-400/10 border-emerald-400/30", label: `hicode: ${String(ev.tool ?? "执行中")}` };
				if (stage === "done")
					return { Icon: CheckCircle2, cls: "text-emerald-400 bg-emerald-400/10 border-emerald-400/30", label: "hicode 完成" };
				return { Icon: Code2, cls: "text-emerald-400 bg-emerald-400/10 border-emerald-400/30", label: "hicode" };
			}
			case "plan_update": {
				const action = String(ev.action ?? "");
				const obj = String(ev.objective ?? "").slice(0, 26);
				return {
					Icon: ListTodo,
					cls: "text-sky-400 bg-sky-400/10 border-sky-400/30",
					label: action === "create" ? `📋 计划: ${obj || "创建"}` : `计划更新: ${obj || action}`,
				};
			}
			default:
				// master_start / master_round (任务开始/思考…) 一律不展示
				return null;
		}
	}

	function stepDetail(ev: ToolStep): string {
		if (ev.type === "hicode_progress" && typeof ev.detail === "string") return ev.detail.slice(0, 200);
		if (ev.type === "plan_update" && Array.isArray(ev.todos)) {
			const mark: Record<string, string> = { done: "✅", in_progress: "▶️", blocked: "⛔", open: "⬜" };
			const lines = (ev.todos as { id?: string; title?: string; status?: string }[]).map(
				(t) => `${mark[t.status ?? "open"] ?? "⬜"} ${t.id ?? "?"}: ${String(t.title ?? "").slice(0, 60)}`
			);
			return lines.join("\n").slice(0, 400);
		}
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
		// 附件: 上传的文本/图片随消息发送
		const pendingFiles = [...attachments];
		const pendingImages = [...images];
		attachments = [];
		images = [];
		const attachPrefix =
			pendingFiles.length > 0
				? "\n\n" + pendingFiles.map((f) => `[附件 ${f.name}]\n${f.content}`).join("\n\n") + "\n\n"
				: "";
		sessionStore.append(sid, {
			role: "user",
			text: text + attachPrefix,
			status: "done",
			steps: [],
			images: pendingImages,
		});
		sessionStore.append(sid, { role: "assistant", text: "", status: "streaming", steps: [] });
		busy = true;

		aborter = new AbortController();
		const signal = aborter.signal;

		try {
			const res = await fetch(`${API_BASE}/api/v1/agent/stream`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					text: text + attachPrefix,
					images: pendingImages,
					session_id: sid,
					provider: apiKeyStore.provider,
					model: apiKeyStore.model.trim() || undefined,
					engine: apiKeyStore.engine,
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
					const accumulated = sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1)?.text ?? "";
					const final = typeof ev.final === "string" && ev.final.trim() ? ev.final : "";
					const text = accumulated || final;
					// P2: 上下文用量 — 主脑 cost 透传
					const cost = typeof ev.cost_usd === "number" ? ev.cost_usd : undefined;
					// 绝不静默空白: 主脑无输出时置 error 态 → 显示重试按钮而非空白气泡
					sessionStore.patchLast(sid, {
						status: text.trim() ? "done" : "error",
						text,
						cost,
						error: text.trim()
							? undefined
							: "主脑未返回任何内容 (模型/网关异常)。请重试或更换模型。",
					});
				} else if (kind === "tool_call" || kind === "tool_error") {
					// 只记录真实执行轨迹 (工具调用/失败); master_start/master_round
					// (任务开始/思考…) 是过程噪音, 不展示
					const last = sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1);
					if (last) last.steps = [...last.steps, ev as ToolStep];
				} else if (kind === "plan_update") {
					// 计划看板事件: 进轨迹徽章 + 更新活跃计划条 (P5)
					const last = sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1);
					if (last) last.steps = [...last.steps, ev as ToolStep];
					const pev = ev as { plan_id?: string; objective?: string; todos?: unknown[] };
					if (pev.plan_id && Array.isArray(pev.todos)) {
						planStore.apply({ plan_id: pev.plan_id, objective: pev.objective ?? "", todos: pev.todos as never[] });
					}
				} else if (kind === "hicode_progress") {
					// hicode 编码执行器实时进度: 跳过 token 统计噪音, 其余进轨迹
					if (ev.stage !== "stats") {
						const last = sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1);
						if (last) last.steps = [...last.steps, ev as ToolStep];
					}
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
			// 流正常结束但一直没收到有效文本 → 同样标记 error, 不留空白气泡
			const lastText = sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1)?.text ?? "";
			sessionStore.patchLast(
				sid,
				lastText.trim()
					? { status: "done" }
					: { status: "error", error: "主脑未返回任何内容 (模型/网关异常)。请重试或更换模型。" },
			);
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
		// 先通知后端真正中断 (hicode 任务 → serve /cancel; 排队中 → 取消),
		// 再断前端 SSE — 不再「只断连接、子进程继续烧 token」。
		const sid = sessionStore.activeSid;
		if (sid) {
			fetch(`${API_BASE}/api/v1/agent/stop`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ session_id: sid }),
			}).catch(() => {});
		}
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

{#snippet composer()}
	<div class="mx-auto w-full max-w-2xl">
		{#if dictationError}
			<div class="mb-1.5 rounded-lg border border-amber-500/20 bg-amber-500/10 px-2.5 py-1 font-mono text-[10px] text-amber-400">{dictationError}</div>
		{/if}
		{#if uploadError}
			<div class="mb-1.5 rounded-lg border border-amber-500/20 bg-amber-500/10 px-2.5 py-1 font-mono text-[10px] text-amber-400">{uploadError}</div>
		{/if}
		{#if images.length > 0 || attachments.length > 0}
			<div class="mb-1.5 flex flex-wrap gap-1.5">
				{#each images as img, i (img)}
					<div class="relative">
						<img src={img} alt="附件图片" class="h-14 w-14 rounded-lg border border-white/15 object-cover" />
						<button type="button" onclick={() => removeImage(i)} class="absolute -right-1.5 -top-1.5 flex size-4 items-center justify-center rounded-full bg-rose-500 text-[10px] text-white">×</button>
					</div>
				{/each}
				{#each attachments as a, i (a.name + i)}
					<span class="flex items-center gap-1 rounded-lg border border-sky-500/30 bg-sky-500/10 px-2 py-1 font-mono text-[10px] text-sky-300">
						📎 {a.name}
						<button type="button" onclick={() => removeAttachment(i)} class="text-white/50 hover:text-white">×</button>
					</span>
				{/each}
			</div>
		{/if}
		{#if dictating}
			<div class="mb-1.5 flex items-center gap-1.5 rounded-lg border border-rose-500/20 bg-rose-500/10 px-2.5 py-1 font-mono text-[10px] text-rose-400">
				<span class="size-1.5 animate-pulse rounded-full bg-rose-400"></span>
				正在听… (再次点击麦克风停止)
			</div>
		{/if}
		{#if fileTreeOpen}
			<div class="mb-2 h-56 overflow-hidden rounded-xl border border-white/10 bg-[#0d0d0d]">
				<FileTree
					onPick={(p) => {
						input = input + (input ? " " : "") + "@" + p;
						fileTreeOpen = false;
						if (textareaEl) textareaEl.focus();
					}}
				/>
			</div>
		{/if}
		<div class="flex items-end gap-2 rounded-2xl border border-white/10 bg-[#0d0d0d] p-2 transition focus-within:border-white/25 focus-within:shadow-[0_0_0_3px_rgba(255,255,255,0.05)]">
			<button
				type="button"
				onclick={() => (fileTreeOpen = !fileTreeOpen)}
				title="工作区文件 (点击文件注入 @path)"
				class="mb-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-white/50 transition hover:border-sky-500/40 hover:text-white {fileTreeOpen
					? 'border-sky-500/40 text-sky-400'
					: ''}"
			>
				<Folder class="size-4" />
			</button>
			<button
				type="button"
				onclick={toggleDictation}
				title="语音听写"
				class="mb-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg border border-white/10 transition {dictating
					? 'border-rose-500/50 bg-rose-500/10 text-rose-400'
					: 'text-white/50 hover:border-sky-500/40 hover:text-white'}"
			>
				<Mic class="size-4" />
			</button>
			<button
				type="button"
				onclick={pickFiles}
				title="上传文件/图片 (文本类直接读, 图片随消息发送)"
				class="mb-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-white/50 transition hover:border-sky-500/40 hover:text-white"
			>
				<Paperclip class="size-4" />
			</button>
			<input bind:this={fileInput} type="file" multiple class="hidden" onchange={onFilesChosen} />
			<textarea
				bind:this={textareaEl}
				bind:value={input}
				rows="1"
				placeholder="问 Veya 任何事…"
				disabled={busy}
				onkeydown={onKeydown}
				oninput={onTextareaInput}
				class="max-h-[200px] min-w-0 flex-1 resize-none bg-transparent px-3 py-2.5 text-[15px] text-terminal-fg outline-none placeholder:text-white/30 disabled:opacity-50"
			></textarea>
			{#if busy}
				<button
					type="button"
					onclick={stop}
					title="停止生成 (Esc)"
					class="mb-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-white/10 text-white transition hover:bg-white/20"
				>
					<Square class="size-4 fill-current" />
				</button>
			{:else}
				<button
					type="button"
					onclick={() => void send()}
					disabled={!input.trim()}
					title="发送 (Enter)"
					class="mb-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-white text-black transition hover:bg-white/85 disabled:opacity-30"
				>
					<Send class="size-4" />
				</button>
			{/if}
		</div>
		<div class="mt-1.5 flex items-center gap-3 px-1 font-mono text-[10px] text-white/25">
			<span class="flex items-center gap-1.5">
				<span class="size-1.5 rounded-full {apiKeyStore.api_key ? 'bg-emerald-500' : 'bg-amber-500'}"></span>
				<ModelPicker />
			</span>
			<span class="flex-1"></span>
			<span class="hidden md:inline">Enter 发送 · Shift+Enter 换行 · ↑ 编辑{ busy ? " · Esc 停止" : "" }</span>
		</div>
	</div>
{/snippet}

<div class="flex min-h-0 flex-1 flex-col">
	{#if messages.length === 0}
		<!-- 空状态: 整个页面垂直居中, 输入框在窗口中间 (Claude 式) -->
		<div class="flex min-h-0 flex-1 flex-col items-center justify-center gap-8 px-6 pb-10">
			<div class="flex flex-col items-center gap-4">
				<div class="flex size-12 items-center justify-center rounded-2xl bg-white font-mono text-lg font-bold text-black">V</div>
				<div class="text-center">
					<h2 class="text-xl font-medium text-terminal-fg">和 Veya 主脑对话</h2>
					<p class="mt-1 text-sm text-white/40">直接描述任务，主脑会实时调用工具并流式返回结果</p>
				</div>
				<div class="flex flex-wrap items-center justify-center gap-2">
					{#each SUGGESTIONS as s (s)}
						<button
							type="button"
							onclick={() => void send(s)}
							class="rounded-full border border-white/10 px-4 py-2 text-sm text-white/60 transition hover:border-white/30 hover:text-white"
						>
							{s}
						</button>
					{/each}
				</div>
			</div>
			{@render composer()}
		</div>
	{:else}
		<!-- 聊天中: 内容向上滚动, 输入框固定在窗口最下不动 (Claude 式) -->
		<div bind:this={listEl} class="min-h-0 flex-1 overflow-y-auto px-4 pb-8 pt-6 md:px-6">
			<div class="mx-auto flex max-w-2xl flex-col gap-7">
				{#if planStore.active()}
					{@const activePlan = planStore.active()}
					{#if activePlan}
						<PlanCard plan={activePlan} compact />
					{/if}
				{/if}
				{#each messages as msg, i (i)}
					<div class="flex items-start gap-3 {msg.role === 'user' ? 'flex-row-reverse' : ''}">
						<span
							class="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full {msg.role === 'user'
								? 'bg-white/10 text-white'
								: 'bg-white text-black'}"
						>
							{#if msg.role === "user"}
								<User class="size-3.5" />
							{:else}
								<Bot class="size-3.5" />
							{/if}
						</span>

						<div class="min-w-0 flex-1 {msg.role === 'user' ? 'max-w-[70%]' : 'max-w-[85%]'}">
							{#if msg.role === "user"}
								<div class="w-fit max-w-full rounded-2xl rounded-tr-sm bg-white/10 px-4 py-2.5 text-[15px] leading-relaxed text-terminal-fg">
									{#if msg.images && msg.images.length > 0}
										<div class="mb-2 flex flex-wrap gap-1.5">
											{#each msg.images as img (img)}
												<img src={img} alt="图片附件" class="max-h-32 rounded-lg border border-white/15" />
											{/each}
										</div>
									{/if}
									{msg.text}
								</div>
							{:else}
								{@const parsed = artifactStore.parseArtifactsFromText(msg.text)}
								<div class="flex flex-col gap-2">
									{#if msg.steps.length > 0}
										<div class="flex flex-col gap-1">
											{#each msg.steps as ev, si (si)}
												{@const meta = stepMeta(ev)}
												{#if meta}
													{@const Icon = meta.Icon}
													<button
														type="button"
														onclick={() => toggleStep(si)}
														class="flex w-fit max-w-full items-center gap-2 rounded-lg border px-2.5 py-1 font-mono text-[11px] {meta.cls}"
													>
														<Icon class="size-3.5 shrink-0" />
														<span class="font-semibold">{meta.label}</span>
														{#if stepDetail(ev)}
															<span class="max-w-[280px] truncate opacity-80">
																{expandedSteps.has(si) ? stepDetail(ev) : stepDetail(ev).slice(0, 60) + "…"}
															</span>
														{/if}
													</button>
												{/if}
											{/each}
										</div>
									{/if}

									<div class="text-[15px] leading-relaxed text-terminal-fg">
										{#if msg.status === "streaming" && !msg.text}
											<div class="flex items-center gap-2 text-sm text-white/40">
												<Loader2 class="size-4 animate-spin text-white/60" />
												正在思考…
											</div>
										{:else if parsed.pureText.trim()}
											<MarkdownBlock content={parsed.pureText.replace(/\[ARTIFACT_PLACEHOLDER:[^\]]+\]/g, "")} />
										{/if}

										{#if msg.status === "streaming" && msg.text}
											<span class="mt-1 inline-block size-2 rounded-full bg-white align-middle [animation:blink_1s_steps(1)_infinite]"></span>
										{/if}

										{#if msg.status === "error"}
											<div class="mt-2 flex items-start gap-2 rounded-lg bg-red-500/10 p-2.5 text-sm text-red-400">
												<CircleAlert class="mt-0.5 size-4 shrink-0" />
												<span class="min-w-0 break-words">{msg.error}</span>
											</div>
											<div class="mt-2 flex gap-2">
												<button
													type="button"
													onclick={retryLast}
													class="flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 font-mono text-xs text-white/60 transition hover:border-white/30 hover:text-white"
												>
													<RotateCcw class="size-3.5" />
													重试
												</button>
											</div>
										{:else if msg.status === "stopped"}
											<div class="mt-2 font-mono text-xs text-white/40">已停止</div>
										{:else if msg.status === "done" && msg.text}
											<div class="mt-2 flex items-center gap-2">
												<button
													type="button"
													onclick={() => copyMessage(msg.text)}
													class="flex items-center gap-1 rounded-md px-2 py-1 font-mono text-[11px] text-white/30 transition hover:bg-white/5 hover:text-white/70"
													title="复制回答"
												>
													<Copy class="size-3" />
												</button>
												{#if msg.cost}
													<span class="rounded-md px-2 py-1 font-mono text-[11px] text-white/25" title="本次回答成本">
														${msg.cost.toFixed(4)}
													</span>
												{/if}
											</div>
										{/if}
									</div>

									{#if parsed.artifacts.length > 0}
										<div class="flex flex-col gap-2">
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
								</div>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		</div>
		<!-- 输入框固定贴底, 与页面一体(无分隔线/无独立背景) -->
		<div class="shrink-0 px-4 pb-4 pt-1 md:px-6">
			{@render composer()}
		</div>
	{/if}
</div>

<style>
	@keyframes blink {
		50% {
			opacity: 0;
		}
	}
</style>
