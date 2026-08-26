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
		ChevronDown,
		CircleAlert,
		Code2,
		Copy,
		ListTodo,
		Loader2,
		Pencil,
		RotateCcw,
		Send,
		Square,
		User,
		Wrench,
	} from "lucide-svelte";
	import MarkdownBlock from "./MarkdownBlock.svelte";
import ModelPicker from "./ModelPicker.svelte";
import CollapsibleText from "./CollapsibleText.svelte";
	import { onMount, onDestroy } from "svelte";
	import { artifactStore } from "$lib/artifacts.svelte";
	import { notifyStore } from "$lib/notifications.svelte";
	import { sessionStore } from "$lib/sessionStore.svelte";
	import { apiKeyStore } from "$lib/settings.svelte";
	import { API_BASE } from "$lib/api";
	import { authHeader } from "$lib/auth.svelte";
	import { planStore } from "$lib/planStore.svelte";
	import { applyDiagramOperations, wrapMxCellsXml, type DiagramOperation } from "$lib/drawioOps";
	import PlanCard from "./PlanCard.svelte";
	import FileTree from "./FileTree.svelte";
	import { Folder, HelpCircle, Mic, Paperclip, Phone, Plus } from "lucide-svelte";
	import VoiceCall from "./VoiceCall.svelte";
	import type { ChatMessage, ToolStep } from "$lib/chatTypes";

	const SUGGESTIONS = [
		"分析当前项目的目录结构，找出 3O 主库里已有的算子",
		"写一个基于 MACD 的风险控制拦截器设计",
		"解释 git rebase 和 merge 的区别",
		"检查 tests/ 目录的测试覆盖情况",
	];

	let input = $state("");
	let busy = $state(false);
	let planMode = $state(false);
	// P1-05: 权限档位选择器 (数据源 GET /api/v1/permission/profiles)
	let permissionProfile = $state("");
	let profileOptions = $state<{ name: string; description: string }[]>([]);
	let profileError = $state("");
	let pendingApproval = $state<{
		request_id: string;
		tool_name: string;
		reason: string;
		tool_args?: unknown;
	} | null>(null);
	// 提问卡片 (OpenMausBot 内化): 主脑执行中提问 → 卡片点选/文字回答 → /agent/answer 回填
	let pendingQuestion = $state<{
		request_id: string;
		question: string;
		options: string[];
	} | null>(null);
	let questionAnswer = $state("");
	let lastUserText = $state("");
	// 跨端同步: 本端正在亲自流式的会话 sid — 忽略自己产生的镜像帧 (避免重复应用)。
	let locallyStreamingSid = $state("");
	let fileTreeOpen = $state(false);
	let dictating = $state(false);
	let dictationError = $state("");
	let recognition: { stop: () => void; lang: string } | null = null;
	let attachMenuOpen = $state(false);
	let voiceCallOpen = $state(false);
	let attachMenuEl = $state<HTMLDivElement | null>(null);

	// 点击外部关闭「+」插入菜单
	$effect(() => {
		if (!attachMenuOpen) return;
		function onDown(e: PointerEvent) {
			if (attachMenuEl && !attachMenuEl.contains(e.target as Node)) attachMenuOpen = false;
		}
		document.addEventListener("pointerdown", onDown, true);
		return () => document.removeEventListener("pointerdown", onDown, true);
	});

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
					// octet-stream 原始 body (非 multipart form → 不触发 SvelteKit CSRF, 二进制安全)
					const res = await fetch(`${API_BASE}/api/v1/fs/upload?name=${encodeURIComponent(f.name)}`, {
						method: "POST",
						headers: { "content-type": "application/octet-stream" },
						body: f,
					});
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

	// 是否贴底 — 用户手动往上滚看历史时不再被拉回底部 (Claude 式), 显示悬浮的
	// 「回到底部」按钮; 点击它或贴底时的新消息/流式增量才继续自动跟随滚动。
	let atBottom = $state(true);
	const BOTTOM_THRESHOLD = 96;

	function handleScroll() {
		if (!listEl) return;
		const distance = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight;
		atBottom = distance < BOTTOM_THRESHOLD;
	}

	function scrollBottom(force = false) {
		if (!listEl || (!force && !atBottom)) return;
		requestAnimationFrame(() => {
			if (listEl) listEl.scrollTop = listEl.scrollHeight;
		});
	}

	function jumpToBottom() {
		atBottom = true;
		scrollBottom(true);
	}

	// 切会话: 强制跳底并重置贴底状态 (不沿用上一个会话的滚动位置判断)。
	$effect(() => {
		void sid;
		atBottom = true;
		scrollBottom(true);
	});

	$effect(() => {
		void messages.length;
		void messages.at(-1)?.text; // 流式增量增长时, 贴底才跟着滚
		void busy;
		scrollBottom();
	});

	function newSession() {
		sessionStore.newSession();
		input = "";
		pendingApproval = null;
		pendingQuestion = null;
		questionAnswer = "";
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
			case "permission_request":
				return {
					Icon: CircleAlert,
					cls: "text-amber-300 bg-amber-400/10 border-amber-400/30",
					label: `批准? ${String(ev.tool_name ?? "tool")}`,
				};
			case "agent_question":
				return {
					Icon: HelpCircle,
					cls: "text-sky-300 bg-sky-400/10 border-sky-400/30",
					label: "❓ 主脑提问",
				};
			case "project_understand_ask": {
				const qs = Array.isArray(ev.questions) ? (ev.questions as unknown[]).length : 0;
				return {
					Icon: CircleAlert,
					cls: "text-sky-300 bg-sky-400/10 border-sky-400/30",
					label: `❓ 需要澄清 (${qs} 个问题)`,
				};
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
		if (ev.type === "agent_question") {
			return String(ev.question ?? "").slice(0, 300);
		}
		if (ev.type === "project_understand_ask") {
			const interp = typeof ev.interpretation === "string" ? ev.interpretation : "";
			const qs = Array.isArray(ev.questions) ? (ev.questions as string[]) : [];
			const lines = [
				...(interp ? [`理解: ${interp}`] : []),
				...qs.map((q, i) => `${i + 1}. ${q}`),
			];
			return lines.join("\n").slice(0, 400);
		}
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

	// 断流不等于任务死了: 后端 chat_task 与 SSE 推流解耦 (client.is_disconnected()
	// 只停消费, 不取消后台任务 — 见 server/chat_stream.py), hicode 等长任务在
	// 网络抖动/HTTP 502 期间仍在后端继续跑。GET /stream/{sid} 能重新接上同一个
	// session 的事件队列 (未消费事件仍排队等着), 不必把已完成的探索工作扔掉
	// 重新发一条新消息。用户反馈 (2026-08-16): 网络抖动不该直接报错停在那里。
	const RECONNECT_ATTEMPTS = 3;
	const RECONNECT_BACKOFF_MS = [1500, 3000, 6000];

	// 应用一个已解析的流事件到目标会话 — SSE 帧与跨端镜像帧共用这套逻辑。
	function applyEvent(targetSid: string, ev: Record<string, unknown>) {
		{
			const kind = String(ev.type ?? ev.event ?? "");
			if (kind === "text_delta" && typeof ev.delta === "string") {
				sessionStore.patchLast(targetSid, {
					text:
						(sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1)?.text ?? "") +
						ev.delta,
				});
			} else if (kind === "master_done") {
				const accumulated =
					sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1)?.text ?? "";
				const final = typeof ev.final === "string" && ev.final.trim() ? ev.final : "";
				const text = accumulated || final;
				// P2: 上下文用量 — 主脑 cost 透传
				const cost = typeof ev.cost_usd === "number" ? ev.cost_usd : undefined;
				// 绝不静默空白: 主脑无输出时置 error 态 → 显示重试按钮而非空白气泡
				sessionStore.patchLast(targetSid, {
					status: text.trim() ? "done" : "error",
					text,
					cost,
					error: text.trim() ? undefined : "主脑未返回任何内容 (模型/网关异常)。请重试或更换模型。",
				});
			} else if (
				kind === "tool_call" ||
				kind === "tool_error" ||
				kind === "project_understand_ask"
			) {
				// 只记录真实执行轨迹 (工具调用/失败/澄清追问); master_start/master_round
				// (任务开始/思考…) 是过程噪音, 不展示。project_understand_ask 是
				// project_ask 澄清早退的结构化事件 (成功的 tool 结果本身不流给前端,
				// 见 server/project_ask.py 里的注释), 不靠模型转述原文渲染澄清卡片。
				const last = sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1);
				if (last) last.steps = [...last.steps, ev as ToolStep];
			} else if (kind === "plan_update") {
				// 计划看板事件: 进轨迹徽章 + 更新活跃计划条 (P5)
				const last = sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1);
				if (last) last.steps = [...last.steps, ev as ToolStep];
				const pev = ev as { plan_id?: string; objective?: string; todos?: unknown[] };
				if (pev.plan_id && Array.isArray(pev.todos)) {
					planStore.apply({
						plan_id: pev.plan_id,
						objective: pev.objective ?? "",
						todos: pev.todos as never[],
					});
				}
			} else if (kind === "diagram_result") {
				// display_diagram 成功: 后端把模型写的裸 mxCell 片段原样透传过来,
				// 这里包成完整 mxfile 文档后, 拼进当前流式消息文本, 复用既有
				// <veya-artifact> 正则解析 + ArtifactRenderer 渲染管线。
				const xml = typeof ev.xml === "string" ? ev.xml : "";
				const title = (typeof ev.title === "string" && ev.title.trim()) || "Diagram";
				if (xml.trim()) {
					const fullXml = wrapMxCellsXml(xml);
					const current =
						sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1)?.text ?? "";
					sessionStore.patchLast(targetSid, {
						text:
							current +
							`\n\n<veya-artifact type="drawio" title="${title.replace(/"/g, "'")}">${fullXml}</veya-artifact>\n\n`,
					});
				}
			} else if (kind === "diagram_edit") {
				// edit_diagram 成功: 后端只透传结构化操作, 实际 XML 变更在这里做 —
				// 从会话历史里回溯最近一次画的图当编辑基底 (MasterAgent 是跨会话
				// 共享单例, 不适合按会话缓存"当前图表"这种可变状态)。
				const ops = Array.isArray(ev.operations) ? (ev.operations as DiagramOperation[]) : [];
				const msgs = sessionStore.sessions.find((s) => s.sid === targetSid)?.messages ?? [];
				let baseXml = "";
				let baseTitle = "Diagram";
				for (let i = msgs.length - 1; i >= 0; i--) {
					if (msgs[i].role !== "assistant") continue;
					const { artifacts } = artifactStore.parseArtifactsFromText(msgs[i].text);
					const drawio = [...artifacts].reverse().find((a) => a.type === "drawio");
					if (drawio) {
						baseXml = drawio.code;
						baseTitle = drawio.title;
						break;
					}
				}
				if (!baseXml) {
					console.warn("[diagram_edit] 本会话还没有可编辑的图表, 忽略此次编辑");
				} else if (ops.length > 0) {
					const { xml: newXml, errors } = applyDiagramOperations(baseXml, ops);
					if (errors.length > 0) console.warn("[diagram_edit] 部分操作未生效:", errors);
					const current =
						sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1)?.text ?? "";
					sessionStore.patchLast(targetSid, {
						text:
							current +
							`\n\n<veya-artifact type="drawio" title="${baseTitle.replace(/"/g, "'")}">${newXml}</veya-artifact>\n\n`,
					});
				}
			} else if (kind === "hicode_progress") {
				// hicode 编码执行器实时进度: 跳过 token 统计噪音, 其余进轨迹
				if (ev.stage !== "stats") {
					const last = sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1);
					if (last) last.steps = [...last.steps, ev as ToolStep];
				}
			} else if (kind === "permission_request") {
				pendingApproval = {
					request_id: String(ev.request_id ?? ""),
					tool_name: String(ev.tool_name ?? "tool"),
					reason: String(ev.reason ?? "需要你批准"),
					tool_args: ev.tool_args,
				};
				const last = sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1);
				if (last) last.steps = [...last.steps, ev as ToolStep];
			} else if (kind === "agent_question") {
				// 提问卡片 (OpenMausBot 内化): 弹出卡片 → 用户回答 → /agent/answer 回填
				pendingQuestion = {
					request_id: String(ev.request_id ?? ""),
					question: String(ev.question ?? ""),
					options: Array.isArray(ev.options) ? (ev.options as unknown[]).map(String) : [],
				};
				questionAnswer = "";
				const last = sessionStore.sessions.find((s) => s.sid === targetSid)?.messages.at(-1);
				if (last) last.steps = [...last.steps, ev as ToolStep];
			} else if (kind === "error") {
				throw new Error(typeof ev.error === "string" ? ev.error : "agent error");
			}
		}
	}

	function makeFrameHandler(targetSid: string) {
		return (data: string) => {
			if (data === "[DONE]") return;
			let ev: Record<string, unknown>;
			try {
				ev = JSON.parse(data);
			} catch {
				return;
			}
			applyEvent(targetSid, ev);
		};
	}

	// 跨端镜像: 别的设备执行时, 本端常驻通知通道收到逐帧镜像 → 应用到会话。
	function applyMirrorEvent(mirrorSid: string, ev: Record<string, unknown>) {
		// 本端正在亲自流式这个会话 → 自己的回显, 跳过 (本端 pumpSse 已应用)。
		if (mirrorSid === locallyStreamingSid) return;
		const kind = String(ev.type ?? ev.event ?? "");
		sessionStore.ensureSession(mirrorSid);
		if (kind === "user_prompt") {
			// 别的设备发起的一轮: 渲染用户气泡 + 助手占位, 后续 delta 才有落点。
			const s = sessionStore.sessions.find((x) => x.sid === mirrorSid);
			const lastMsg = s?.messages.at(-1);
			// 幂等: 同一轮镜像可能重放, 已有正在流式的助手占位就不重复建。
			if (!(lastMsg && lastMsg.role === "assistant" && lastMsg.status === "streaming")) {
				sessionStore.append(mirrorSid, {
					role: "user",
					text: typeof ev.text === "string" ? ev.text : "",
					status: "done",
					steps: [],
				});
				sessionStore.append(mirrorSid, { role: "assistant", text: "", status: "streaming", steps: [] });
			}
			return;
		}
		// 兜底: 缺助手占位 (镜像中途接入) → 补一个, 保证 delta/步骤有落点。
		const s = sessionStore.sessions.find((x) => x.sid === mirrorSid);
		const last = s?.messages.at(-1);
		if (!(last && last.role === "assistant")) {
			sessionStore.append(mirrorSid, { role: "assistant", text: "", status: "streaming", steps: [] });
		}
		applyEvent(mirrorSid, ev);
	}

	onMount(() => {
		notifyStore.streamHandler = applyMirrorEvent;
		// P1-05: 加载权限档位列表与当前档位
		void loadProfiles();
	});
	onDestroy(() => {
		if (notifyStore.streamHandler === applyMirrorEvent) notifyStore.streamHandler = undefined;
	});

	async function pumpSse(res: Response, onFrame: (data: string) => void) {
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
					if (line.startsWith("data:")) onFrame(line.slice(5).trim());
				}
			}
		}
	}

	// 重新接上同一 session 的事件队列 (不重发消息, 不重启后端任务) —
	// 后端任务仍在跑就续上剩余事件, 已经跑完就立刻收到收尾事件。
	async function reconnectStream(targetSid: string, signal?: AbortSignal): Promise<boolean> {
		try {
			// 先探活: 后台任务已经跑完/取消的话, 重连只会挂在一个空队列上
			// 永远等不到新事件 (心跳 ping 不停但没有真数据) — 不值得重连。
			const statusRes = await fetch(
				`${API_BASE}/api/v1/agent/stream_status?session_id=${encodeURIComponent(targetSid)}`,
				{ headers: { ...authHeader() }, signal },
			);
			const statusJson = await statusRes.json().catch(() => ({}));
			if (!statusRes.ok || !statusJson?.active) return false;
			const res = await fetch(`${API_BASE}/stream/${targetSid}`, {
				headers: { ...authHeader() },
				signal,
			});
			await pumpSse(res, makeFrameHandler(targetSid));
			return true;
		} catch {
			return false;
		}
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
		locallyStreamingSid = sid; // 本端亲自流式 → 忽略同 sid 的镜像回显

		aborter = new AbortController();
		const signal = aborter.signal;

		try {
			const res = await fetch(`${API_BASE}/api/v1/agent/stream`, {
				method: "POST",
				headers: { "content-type": "application/json", ...authHeader() },
				body: JSON.stringify({
					text: text + attachPrefix,
					images: pendingImages,
					session_id: sid,
					provider: apiKeyStore.provider,
					model: apiKeyStore.model.trim() || undefined,
					engine: apiKeyStore.engine,
					config: apiKeyStore.asConfig(),
					agent_mode: planMode ? "plan" : "agent",
					require_approval: !planMode,
				}),
				signal,
			});
			await pumpSse(res, makeFrameHandler(sid));
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
			if (aborted) {
				sessionStore.patchLast(sid, { status: "stopped" });
			} else {
				// 断流不代表任务死了 (后端任务与推流解耦, 仍在跑) — 先原地重连
				// 同一 session 的事件队列, 续上剩余进度, 不立刻甩错误给用户。
				let recovered = false;
				for (let i = 0; i < RECONNECT_ATTEMPTS && !aborter?.signal.aborted; i++) {
					await new Promise((r) => setTimeout(r, RECONNECT_BACKOFF_MS[i]));
					if (await reconnectStream(sid, aborter?.signal)) {
						recovered = true;
						break;
					}
				}
				if (!recovered) {
					const lastStatus = sessionStore.sessions.find((s) => s.sid === sid)?.messages.at(-1)
						?.status;
					if (lastStatus !== "done" && lastStatus !== "error") {
						sessionStore.patchLast(sid, {
							status: "error",
							error: `网络连接反复失败, 已重试 ${RECONNECT_ATTEMPTS} 次仍未恢复: ${
								e instanceof Error ? e.message : String(e)
							}`,
						});
					}
				}
			}
		} finally {
			busy = false;
			aborter = null;
			locallyStreamingSid = "";
		}
	}

	async function resolveApproval(approved: boolean) {
		const req = pendingApproval;
		if (!req) return;
		pendingApproval = null;
		await fetch(`${API_BASE}/api/v1/agent/approval`, {
			method: "POST",
			headers: { "content-type": "application/json", ...authHeader() },
			body: JSON.stringify({ request_id: req.request_id, approved }),
		}).catch(() => {});
	}

	async function resolveQuestion(answer: string) {
		const q = pendingQuestion;
		if (!q) return;
		pendingQuestion = null;
		questionAnswer = "";
		await fetch(`${API_BASE}/api/v1/agent/answer`, {
			method: "POST",
			headers: { "content-type": "application/json", ...authHeader() },
			body: JSON.stringify({ request_id: q.request_id, answer }),
		}).catch(() => {});
	}

	// ── P1-05: 权限档位选择器 ────────────────────────────────────────
	async function loadProfiles() {
		try {
			const [optsRes, curRes] = await Promise.all([
				fetch(`${API_BASE}/api/v1/permission/profiles`, { headers: { ...authHeader() } }),
				fetch(`${API_BASE}/api/v1/permission/profile`, { headers: { ...authHeader() } }),
			]);
			if (optsRes.ok) {
				const data = (await optsRes.json()) as { profiles?: { name: string; description: string }[] };
				profileOptions = data.profiles ?? [];
			}
			if (curRes.ok) {
				const data = (await curRes.json()) as { profile?: string };
				if (data.profile) permissionProfile = data.profile;
			}
		} catch {
			profileError = "档位加载失败";
		}
	}

	async function setPermissionProfile(profile: string) {
		const prev = permissionProfile;
		permissionProfile = profile;
		profileError = "";
		try {
			const res = await fetch(`${API_BASE}/api/v1/permission/profile`, {
				method: "POST",
				headers: { "content-type": "application/json", ...authHeader() },
				body: JSON.stringify({ profile }),
			});
			if (!res.ok) {
				permissionProfile = prev;
				const txt = await res.text();
				profileError = `档位切换失败 (${res.status}): ${txt.slice(0, 120)}`;
			}
		} catch (e) {
			permissionProfile = prev;
			profileError = `档位切换失败: ${String(e)}`;
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
		pendingQuestion = null; // 中断后提问作废 (后端超时会自动按默认假设继续)
		aborter?.abort();
	}

	async function retryLast() {
		if (busy || !lastUserText) return;
		// 先探探后台任务是不是还活着 (之前的探索/工具调用别浪费) — 活着就
		// 续接同一个 session 的事件流, 不是从头发一条新消息重新跑一遍。
		// 只有确认后台任务已经不在了才退化成全新一轮。
		const targetSid = sid;
		if (targetSid) {
			busy = true;
			try {
				if (await reconnectStream(targetSid)) return;
			} finally {
				busy = false;
			}
		}
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

	// ── 消息内联编辑 (Claude 式: 悬浮铅笔图标 → 就地改文字 → 重新发送,
	// 截断该消息及其后所有内容, 不支持分支历史) ──────────────────────
	let editingIndex = $state<number | null>(null);
	let editingText = $state("");
	let editTextareaEl = $state<HTMLTextAreaElement>();

	function startEdit(i: number) {
		if (busy) return;
		const msg = messages[i];
		if (!msg || msg.role !== "user") return;
		editingIndex = i;
		editingText = msg.text;
		requestAnimationFrame(() => {
			editTextareaEl?.focus();
			if (editTextareaEl) {
				editTextareaEl.style.height = "auto";
				editTextareaEl.style.height = Math.min(editTextareaEl.scrollHeight, 300) + "px";
			}
		});
	}

	function cancelEdit() {
		editingIndex = null;
		editingText = "";
	}

	function confirmEdit() {
		const i = editingIndex;
		const text = editingText.trim();
		if (i === null || !text) return;
		const s = sessionStore.sessions.find((x) => x.sid === sid);
		if (s) {
			s.messages = s.messages.slice(0, i); // 截断该消息及其后续(含已收到的回答)
			sessionStore.persist();
		}
		editingIndex = null;
		editingText = "";
		void send(text);
	}

	function onEditTextareaInput() {
		if (!editTextareaEl) return;
		editTextareaEl.style.height = "auto";
		editTextareaEl.style.height = Math.min(editTextareaEl.scrollHeight, 300) + "px";
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
		{#if pendingApproval}
			<div class="mb-2 flex items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2">
				<span class="min-w-0 flex-1 font-mono text-[11px] text-amber-200">
					{pendingApproval.reason}
				</span>
				<button
					type="button"
					onclick={() => void resolveApproval(true)}
					class="rounded-lg bg-emerald-500/90 px-2.5 py-1 font-mono text-[11px] text-black hover:bg-emerald-400"
				>批准</button>
				<button
					type="button"
					onclick={() => void resolveApproval(false)}
					class="rounded-lg bg-white/10 px-2.5 py-1 font-mono text-[11px] text-white/80 hover:bg-white/20"
				>拒绝</button>
			</div>
		{/if}
		{#if pendingQuestion}
			<div class="mb-2 rounded-xl border border-sky-500/30 bg-sky-500/10 px-3 py-2.5">
				<div class="flex items-start gap-2">
					<span class="mt-0.5 flex size-5 shrink-0 items-center justify-center">
						<HelpCircle class="size-4 text-sky-300" />
					</span>
					<div class="min-w-0 flex-1">
						<div class="font-mono text-[10px] uppercase tracking-wider text-sky-400/80">主脑提问</div>
						<div class="mt-1 text-[13px] leading-relaxed text-terminal-fg">{pendingQuestion.question}</div>
						{#if pendingQuestion.options.length > 0}
							<div class="mt-2 flex flex-wrap gap-1.5">
								{#each pendingQuestion.options as opt (opt)}
									<button
										type="button"
										onclick={() => void resolveQuestion(opt)}
										class="rounded-lg border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 font-mono text-[11px] text-sky-200 transition hover:bg-sky-500/25"
									>{opt}</button>
								{/each}
							</div>
						{/if}
						<div class="mt-2 flex items-center gap-2">
							<input
								bind:value={questionAnswer}
								placeholder="输入回答…"
								onkeydown={(e) => {
									if (e.key === "Enter" && !e.isComposing && questionAnswer.trim()) {
										e.preventDefault();
										void resolveQuestion(questionAnswer.trim());
									}
								}}
								class="min-w-0 flex-1 rounded-lg border border-white/15 bg-black/30 px-2.5 py-1.5 font-mono text-[12px] text-terminal-fg outline-none placeholder:text-white/30 focus:border-sky-500/50"
							/>
							<button
								type="button"
									onclick={() => void resolveQuestion(questionAnswer.trim())}
									disabled={!questionAnswer.trim()}
									class="rounded-lg bg-sky-500/90 px-2.5 py-1.5 font-mono text-[11px] text-black transition hover:bg-sky-400 disabled:opacity-30"
							>回答</button>
						</div>
						<div class="mt-1.5 font-mono text-[10px] text-white/30">
							不回答的话主脑会在 5 分钟后按默认假设继续，不会卡住
						</div>
					</div>
				</div>
			</div>
		{/if}
		<div class="flex items-end gap-2 rounded-2xl border border-white/10 bg-[#0d0d0d] p-2 transition focus-within:border-white/25 focus-within:shadow-[0_0_0_3px_rgba(255,255,255,0.05)]">
			<div class="relative" bind:this={attachMenuEl}>
				<button
					type="button"
					onclick={() => (attachMenuOpen = !attachMenuOpen)}
					title="插入…"
					class="mb-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-white/50 transition hover:border-sky-500/40 hover:text-white {attachMenuOpen ||
					fileTreeOpen ||
					dictating
						? 'border-sky-500/40 text-sky-400'
						: ''}"
				>
					<Plus class="size-4 transition-transform {attachMenuOpen ? 'rotate-45' : ''}" />
				</button>
				{#if attachMenuOpen}
					<div class="absolute bottom-10 left-0 z-50 w-52 overflow-hidden rounded-xl border border-terminal-edge bg-terminal-panel shadow-2xl">
						<button
							type="button"
							onclick={() => {
								fileTreeOpen = !fileTreeOpen;
								attachMenuOpen = false;
							}}
							title="点击文件注入 @path"
							class="flex w-full items-center gap-2 px-3 py-2 text-left font-mono text-[11px] text-white/70 transition hover:bg-white/10 hover:text-white"
						>
							<Folder class="size-3.5" /> 工作区文件
						</button>
						<button
							type="button"
							onclick={() => {
								toggleDictation();
								attachMenuOpen = false;
							}}
							title="语音听写"
							class="flex w-full items-center gap-2 px-3 py-2 text-left font-mono text-[11px] transition hover:bg-white/10 {dictating
								? 'text-rose-400'
								: 'text-white/70 hover:text-white'}"
						>
							<Mic class="size-3.5" /> 语音听写
						</button>
						<button
							type="button"
							onclick={() => {
								pickFiles();
								attachMenuOpen = false;
							}}
							title="文本类直接读, 图片随消息发送"
							class="flex w-full items-center gap-2 px-3 py-2 text-left font-mono text-[11px] text-white/70 transition hover:bg-white/10 hover:text-white"
						>
							<Paperclip class="size-3.5" /> 上传文件/图片
						</button>
						<button
							type="button"
							onclick={() => {
								ensureSession();
								voiceCallOpen = true;
								attachMenuOpen = false;
							}}
							title="连续可打断的实时语音对话"
							class="flex w-full items-center gap-2 px-3 py-2 text-left font-mono text-[11px] text-white/70 transition hover:bg-white/10 hover:text-white"
						>
							<Phone class="size-3.5" /> 语音通话
						</button>
					</div>
				{/if}
			</div>
			<input bind:this={fileInput} type="file" multiple class="hidden" onchange={onFilesChosen} />
			<textarea
				bind:this={textareaEl}
				bind:value={input}
				rows="1"
				placeholder={planMode ? "计划模式：只读探索，不会改文件…" : "问 Veya 任何事…"}
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
			<button
				type="button"
				onclick={() => (planMode = !planMode)}
				title="计划模式：只读探索，确认后再切回执行"
				class="rounded-md border px-1.5 py-0.5 transition {planMode
					? 'border-sky-500/50 bg-sky-500/15 text-sky-300'
					: 'border-white/10 text-white/35 hover:text-white/70'}"
			>{planMode ? "计划" : "执行"}</button>
			<span class="flex-1"></span>
			{#if profileOptions.length > 0}
				<label
					title="权限档位 (P1-05 / P3-01): READ_ONLY=全禁写, DEVELOPMENT=本地写放行, PRODUCTION=写与执行需批准"
					class="flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] text-terminal-dim"
				>
					<select
						class="bg-transparent text-terminal-fg outline-none"
						bind:value={permissionProfile}
						onchange={() => void setPermissionProfile(permissionProfile)}
						disabled={busy}
					>
						{#each profileOptions as opt (opt.name)}
							<option value={opt.name}>{opt.name}</option>
						{/each}
					</select>
				</label>
			{/if}
			{#if profileError}
				<span class="font-mono text-[10px] text-rose-400">{profileError}</span>
			{/if}
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
		<div class="flex min-h-0 flex-1 flex-col">
		<div class="relative min-h-0 flex-1">
		<div bind:this={listEl} onscroll={handleScroll} class="absolute inset-0 overflow-y-auto px-4 pb-8 pt-6 md:px-6">
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
								<div class="group/user flex flex-col items-end">
									{#if editingIndex === i}
										<div class="w-full rounded-2xl border border-sky-500/40 bg-[#0d0d0d] p-2">
											<textarea
												bind:this={editTextareaEl}
												bind:value={editingText}
												rows="1"
												oninput={onEditTextareaInput}
												onkeydown={(e) => {
													if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
														e.preventDefault();
														confirmEdit();
													} else if (e.key === "Escape") {
														e.preventDefault();
														cancelEdit();
													}
												}}
												class="max-h-[300px] w-full resize-none bg-transparent px-2 py-1.5 text-[15px] text-terminal-fg outline-none"
											></textarea>
											<div class="mt-1.5 flex items-center justify-end gap-2">
												<button
													type="button"
													onclick={cancelEdit}
													class="rounded-lg px-3 py-1.5 font-mono text-xs text-white/50 transition hover:text-white/80"
												>取消</button>
												<button
													type="button"
													onclick={confirmEdit}
													disabled={!editingText.trim()}
													class="rounded-lg bg-white px-3 py-1.5 font-mono text-xs text-black transition hover:bg-white/85 disabled:opacity-30"
												>发送</button>
											</div>
										</div>
									{:else}
										<div class="w-fit max-w-full rounded-2xl rounded-tr-sm bg-white/10 px-4 py-2.5 text-[15px] leading-relaxed text-terminal-fg">
											{#if msg.images && msg.images.length > 0}
												<div class="mb-2 flex flex-wrap gap-1.5">
													{#each msg.images as img (img)}
														<img src={img} alt="图片附件" class="max-h-32 rounded-lg border border-white/15" />
													{/each}
												</div>
											{/if}
											<CollapsibleText maxHeight={160}>
												{#snippet children()}
													{msg.text}
												{/snippet}
											</CollapsibleText>
										</div>
										<div class="mt-1 flex items-center gap-0.5 opacity-0 transition group-hover/user:opacity-100">
											<button
												type="button"
												onclick={() => copyMessage(msg.text)}
												title="复制"
												class="flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[11px] text-white/40 transition hover:text-white/80"
											>
												<Copy class="size-3" />
											</button>
											<button
												type="button"
												onclick={() => startEdit(i)}
												disabled={busy}
												title="编辑并重新发送"
												class="flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[11px] text-white/40 transition hover:text-white/80 disabled:pointer-events-none disabled:opacity-30"
											>
												<Pencil class="size-3" />
											</button>
										</div>
									{/if}
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
								执行中
							</div>
										{:else if parsed.pureText.trim()}
											{#if msg.status === "streaming"}
												<MarkdownBlock content={parsed.pureText.replace(/\[ARTIFACT_PLACEHOLDER:[^\]]+\]/g, "")} />
											{:else}
												<CollapsibleText maxHeight={360}>
													{#snippet children()}
														<MarkdownBlock content={parsed.pureText.replace(/\[ARTIFACT_PLACEHOLDER:[^\]]+\]/g, "")} />
													{/snippet}
												</CollapsibleText>
											{/if}
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
		{#if !atBottom}
			<button
				type="button"
				onclick={jumpToBottom}
				title="回到底部"
				class="absolute bottom-2 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1 rounded-full border border-white/10 bg-[#141414] px-3 py-1.5 font-mono text-[11px] text-white/70 shadow-lg transition hover:border-white/25 hover:text-white"
			>
				<ChevronDown class="size-3.5" />
				回到底部
			</button>
		{/if}
		</div>
		<!-- 输入框固定贴底, 与页面一体(无分隔线/无独立背景) -->
		<div class="shrink-0 px-4 pb-4 pt-1 md:px-6">
			{@render composer()}
		</div>
		</div>
	{/if}
</div>

{#if voiceCallOpen && sid}
	<VoiceCall {sid} onClose={() => (voiceCallOpen = false)} />
{/if}

<style>
	@keyframes blink {
		50% {
			opacity: 0;
		}
	}
</style>
