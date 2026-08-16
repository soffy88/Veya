/**
 * notifyStore — global background-task notification channel. One EventSource for the
 * whole app (mounted from +layout.svelte), independent of whichever section is active —
 * "Genesis finished while you were on Kanban" toasts, plus a HITL_REQUIRED variant that
 * doesn't auto-dismiss and exposes approve/reject (server/routes/notifications.py).
 */

export type NotificationType = "INFO" | "SUCCESS" | "ERROR" | "HITL_REQUIRED";

export interface VeyaNotification {
	id: string;
	type: NotificationType;
	title: string;
	content: string;
	payload?: Record<string, unknown>;
}

class NotificationManager {
	list = $state<VeyaNotification[]>([]);
	#abort: AbortController | undefined;
	#active = false;
	/** 跨端同步: type="STREAM" 镜像帧交给此回调 (ChatConsole 注册), 不弹 toast。 */
	streamHandler: ((sessionId: string, event: Record<string, unknown>) => void) | undefined;

	add(notif: VeyaNotification) {
		this.list = [...this.list, notif];
		// HITL_REQUIRED must stay until the user explicitly approves/rejects it
		if (notif.type !== "HITL_REQUIRED") {
			setTimeout(() => this.remove(notif.id), 5000);
		}
	}

	remove(id: string) {
		this.list = this.list.filter((n) => n.id !== id);
	}

	/** 登录后自动重连 (带新 token); 登出后重连为匿名 (只收全局消息)。 */
	connect() {
		if (this.#active) return;
		this.#active = true;
		this.#abort = new AbortController();
		this.#run();
	}

	disconnect() {
		this.#abort?.abort();
		this.#active = false;
	}

	async #run() {
		const base: string = (import.meta.env.VITE_VEYA_ENDPOINT as string | undefined) ?? "";
		while (this.#active) {
			try {
				const token = typeof localStorage !== "undefined" ? localStorage.getItem("veya.auth.token") : null;
				const headers: Record<string, string> = {};
				if (token) headers.authorization = `Bearer ${token}`;
				const res = await fetch(`${base}/legacy/notifications/stream`, {
					headers,
					signal: this.#abort?.signal,
				});
				if (!res.ok || !res.body) throw new Error(`notification stream ${res.status}`);
				const reader = res.body.getReader();
				const decoder = new TextDecoder();
				let buf = "";
				while (this.#active) {
					const { done, value } = await reader.read();
					if (done) break;
					buf += decoder.decode(value, { stream: true });
					let idx;
					while ((idx = buf.indexOf("\n\n")) >= 0) {
						const frame = buf.slice(0, idx);
						buf = buf.slice(idx + 2);
						const m = /^data: (.*)$/m.exec(frame);
						if (!m) continue;
						let payload: Record<string, unknown>;
						try {
							payload = JSON.parse(m[1]);
						} catch {
							continue;
						}
						if (payload.type === "DISMISS") {
							const id = (payload.payload as Record<string, unknown> | undefined)?.id;
							if (typeof id === "string") this.remove(id);
							continue;
						}
						if (payload.type === "STREAM") {
							// 跨端镜像帧: 不弹 toast, 交给 ChatConsole 应用到对应会话。
							const sid = typeof payload.session_id === "string" ? payload.session_id : "";
							const ev = payload.event as Record<string, unknown> | undefined;
							if (sid && ev) this.streamHandler?.(sid, ev);
							continue;
						}
						this.add(payload as unknown as VeyaNotification);
					}
				}
			} catch (e) {
				if (!this.#active) return; // 主动断开
				console.error("[Veya] notification stream lost — retry in 5s", e);
			}
			if (!this.#active) return;
			await new Promise((r) => setTimeout(r, 5000));
		}
	}
}

export const notifyStore = new NotificationManager();
