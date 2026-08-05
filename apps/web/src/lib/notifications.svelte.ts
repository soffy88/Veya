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
	#es: EventSource | undefined;

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

	connect() {
		if (this.#es) return;
		this.#es = new EventSource("/legacy/notifications/stream");
		this.#es.onmessage = (ev) => {
			let payload: Record<string, unknown>;
			try {
				payload = JSON.parse(ev.data);
			} catch {
				return;
			}
			if (payload.type === "DISMISS") {
				const id = (payload.payload as Record<string, unknown> | undefined)?.id;
				if (typeof id === "string") this.remove(id);
				return;
			}
			this.add(payload as unknown as VeyaNotification);
		};
		this.#es.onerror = () => {
			console.error("[Veya] notification stream lost connection — browser will auto-retry");
		};
	}
}

export const notifyStore = new NotificationManager();
