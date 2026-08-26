/**
 * sessionStore — multi-session chat persistence (Claude-style).
 *
 * localStorage stores only the session index (sid/title/timestamps/cost). The
 * backend history endpoint is the sole durable message source; hydrate() pulls
 * messages after reload or on another device.
 * Each session auto-titles from its first user message.
 */

import { API_BASE } from "./api";
import type { ChatMessage } from "./chatTypes";

export interface ChatSession {
	sid: string;
	title: string;
	ts: number;
	cost: number;
	messages: ChatMessage[];
}

const LS_KEY = "veya.sessions.v2";
const MAX_SESSIONS = 50;

function load(): ChatSession[] {
	try {
		const raw = localStorage.getItem(LS_KEY);
		if (!raw) return [];
		const parsed = JSON.parse(raw) as ChatSession[];
		return Array.isArray(parsed)
			? parsed.map(({ messages: _messages, ...session }) => ({ ...session, messages: [] }))
			: [];
	} catch {
		return [];
	}
}

function save(list: ChatSession[]) {
	try {
		const index = list.slice(0, MAX_SESSIONS).map(({ messages: _messages, ...session }) => ({
			...session,
			messages: [],
		}));
		localStorage.setItem(LS_KEY, JSON.stringify(index));
	} catch {
		/* storage full — ignore */
	}
}

function newSessionId(): string {
	const bytes = new Uint8Array(16);
	crypto.getRandomValues(bytes);
	const timestamp = BigInt(Date.now());
	for (let i = 5; i >= 0; i -= 1) bytes[i] = Number((timestamp >> BigInt((5 - i) * 8)) & 0xffn);
	bytes[6] = (bytes[6] & 0x0f) | 0x70;
	bytes[8] = (bytes[8] & 0x3f) | 0x80;
	const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
	return `sess_${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

class SessionManager {
	sessions = $state<ChatSession[]>(load());
	activeSid = $state<string>("");

	/** 新建(或切换到)一个会话; 返回 sid */
	newSession(): string {
		const sid = newSessionId();
		this.sessions = [{ sid, title: "新对话", ts: Date.now(), cost: 0, messages: [] }, ...this.sessions];
		this.activeSid = sid;
		this.persist();
		void this.createRemoteSession(sid);
		return sid;
	}

	private async createRemoteSession(sid: string): Promise<void> {
		try {
			const token = typeof localStorage !== "undefined" ? localStorage.getItem("veya.auth.token") : null;
			const headers: Record<string, string> = { "content-type": "application/json" };
			if (token) headers.authorization = `Bearer ${token}`;
			await fetch(`${API_BASE}/api/v1/sessions`, {
				method: "POST",
				headers,
				body: JSON.stringify({ session_id: sid }),
			});
		} catch {
			/* Local cache remains usable while the backend is unavailable. */
		}
	}

	open(sid: string) {
		this.activeSid = sid;
		void this.hydrate(sid); // P3 跨设备: 后台补齐本地缺失的历史 (本地有则无操作)
	}

	/** P3 跨设备: 本地无此会话消息时, 从后端 hydrate (换设备/清缓存后恢复) */
	async hydrate(sid: string): Promise<void> {
		const existing = this.sessions.find((x) => x.sid === sid);
		if (existing && existing.messages.length > 0) return; // 本地已有 → 不覆盖
		try {
			const token = typeof localStorage !== "undefined" ? localStorage.getItem("veya.auth.token") : null;
			const headers: Record<string, string> = {};
			if (token) headers.authorization = `Bearer ${token}`;
			const res = await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(sid)}`, { headers });
			if (!res.ok) return;
			const data = await res.json();
			const msgs = ((data.messages ?? []) as Array<{ role: string; content?: string }>)
				.filter((m) => m.role === "user" || m.role === "assistant")
				.map(
					(m) =>
						({ role: m.role, text: String(m.content ?? ""), status: "done", steps: [] }) as ChatMessage,
				);
			if (msgs.length === 0) return;
			if (existing) {
				existing.messages = msgs;
			} else {
				this.sessions = [
					{ sid, title: msgs[0].text.slice(0, 40) || "恢复的会话", ts: Date.now(), cost: 0, messages: msgs },
					...this.sessions,
				];
			}
			this.persist();
		} catch {
			/* 网络/后端不可用 → 保持本地 */
		}
	}

	/** 登录后: 从后端拉云端会话列表, 合并到本地 (多端同步)。 */
	async syncCloud(): Promise<void> {
		const token = typeof localStorage !== "undefined" ? localStorage.getItem("veya.auth.token") : null;
		if (!token) return;
		try {
			const res = await fetch(`${API_BASE}/api/v1/sessions`, {
				headers: { authorization: `Bearer ${token}` },
			});
			if (!res.ok) return;
			const data = await res.json();
			const cloud = (data.sessions ?? []) as { session_id: string; title?: string; updated_at?: number }[];
			if (cloud.length === 0) return;
			const localSids = new Set(this.sessions.map((s) => s.sid));
			const added: ChatSession[] = cloud
				.filter((s) => s.session_id && !localSids.has(s.session_id))
				.map((s) => ({
					sid: s.session_id,
					title: s.title || "云端会话",
					ts: (s.updated_at ?? Date.now() / 1000) * 1000,
					cost: 0,
					messages: [], // 打开时 hydrate 拉取
				}));
			if (added.length > 0) {
				this.sessions = [...this.sessions, ...added].sort((a, b) => b.ts - a.ts);
				this.persist();
			}
		} catch {
			/* 后端不可用 → 保持本地 */
		}
	}

	/** 确保会话存在 (跨端镜像: 别的设备开始的会话, 本端先建占位再接收流) */
	ensureSession(sid: string, title = "新对话"): void {
		if (this.sessions.find((x) => x.sid === sid)) return;
		this.sessions = [
			{ sid, title, ts: Date.now(), cost: 0, messages: [] },
			...this.sessions,
		];
		this.persist();
	}

	/** 追加消息并自动持久化; 若首条用户消息则生成标题 */
	append(sid: string, msg: ChatMessage) {
		const s = this.sessions.find((x) => x.sid === sid);
		if (!s) return;
		s.messages = [...s.messages, msg];
		if (msg.role === "user" && s.title === "新对话") {
			s.title = msg.text.replace(/\s+/g, " ").slice(0, 40) || "新对话";
		}
		s.ts = Date.now();
		s.cost += msg.cost ?? 0;
		this.persist();
	}

	/** 流式更新最后一条助手消息(原地替换, 不闪屏) */
	patchLast(sid: string, patch: Partial<ChatMessage>) {
		const s = this.sessions.find((x) => x.sid === sid);
		if (!s || s.messages.length === 0) return;
		const last = s.messages[s.messages.length - 1];
		if (last.role !== "assistant") return;
		Object.assign(last, patch);
		this.persist();
	}

	remove(sid: string) {
		this.sessions = this.sessions.filter((s) => s.sid !== sid);
		if (this.activeSid === sid) this.activeSid = "";
		this.persist();
	}

	rename(sid: string, title: string) {
		const s = this.sessions.find((x) => x.sid === sid);
		if (!s) return;
		s.title = title || s.title;
		this.persist();
	}

	persist() {
		save(this.sessions);
	}
}

export const sessionStore = new SessionManager();
