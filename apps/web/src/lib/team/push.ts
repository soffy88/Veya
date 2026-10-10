/**
 * Browser-side Web Push for the bot team (Lorca P5).
 *
 * The server half is `server/bot_team/push.py`; this is the other half —
 * asking iOS/Android to accept a subscription for this device and registering
 * it with the backend, so a turn that finishes while the tab is closed still
 * reaches the phone.
 *
 * Everything here degrades: no Notification API (iOS outside a standalone
 * install), permission denied, or a deployment without the `veya[push]` extra
 * all end in a reason string the UI can show, never in an exception the page
 * has to guess about.
 */

import { api } from "$lib/api";

export type PushReadiness =
	| { ready: false; reason: string }
	| { ready: true; publicKey: string; serverEnabled: boolean };

export interface PushState {
	/** Browser permission (default / granted / denied / unsupported). */
	permission: string;
	/** A subscription this browser already holds, if any. */
	subscribed: boolean;
	/** Can this deployment deliver (i.e. is `pywebpush` installed there)? */
	serverEnabled: boolean;
	/** Last server answer about subscriptions, human readable. */
	detail: string;
}

/** The payload of GET /api/v1/push/status from the backend. */
interface PushStatusInfo {
	enabled: boolean;
	sender: string;
	subscriptions: number;
	hint?: string | null;
}

/** applicationServerKey wants raw bytes; VAPID keys are base64url. */
function urlBase64ToUint8Array(value: string): Uint8Array {
	const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
	const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
	const out = new Uint8Array(raw.length);
	for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
	return out;
}

function describe(): PushState {
	const permission =
		typeof Notification === "undefined" ? "unsupported" : Notification.permission;
	return { permission, subscribed: false, serverEnabled: false, detail: "" };
}

/**
 * Why push cannot be turned on, or the VAPID key to subscribe with plus whether
 * the server can actually deliver (it needs the `veya[push]` extra).
 */
export async function pushReadiness(): Promise<PushReadiness> {
	if (typeof Notification === "undefined" || !("serviceWorker" in navigator)) {
		return { ready: false, reason: "此浏览器不支持 Web Push（需 iOS/Android 已安装到主屏）" };
	}
	const key = await api("gateway", "api/v1/push/public_key", { method: "GET" });
	if (!key.ok || !key.data) {
		return { ready: false, reason: `后端未提供推送公钥 (HTTP ${key.status})` };
	}
	const publicKey = (key.data as { public_key?: string }).public_key;
	if (!publicKey) return { ready: false, reason: "后端推送公钥格式不正确" };
	const status = await api("gateway", "api/v1/push/status", { method: "GET" });
	const serverEnabled = status.ok ? Boolean((status.data as PushStatusInfo).enabled) : false;
	return { ready: true, publicKey, serverEnabled };
}

/** Ask the browser for permission and register this device with the backend. */
export async function enablePush(): Promise<PushState> {
	const base = describe();
	if (base.permission === "unsupported") {
		return { ...base, detail: "此浏览器不支持 Web Push" };
	}
	const readiness = await pushReadiness();
	if (!readiness.ready) return { ...base, detail: readiness.reason };
	let permission = base.permission;
	if (permission === "default") {
		permission = await Notification.requestPermission();
	}
	if (permission !== "granted") {
		return { ...base, permission, detail: "通知权限未授予，无法接收推送" };
	}

	const registration = await navigator.serviceWorker.ready;
	const existing = await registration.pushManager.getSubscription();
	const subscription = existing ?? (await registration.pushManager.subscribe({
		userVisibleOnly: true,
		applicationServerKey: urlBase64ToUint8Array(readiness.publicKey) as BufferSource,
	}));
	const raw = subscription.toJSON();
	const keys = raw.keys ?? {};
	const saved = await api("gateway", "api/v1/push/subscriptions", {
		method: "POST",
		body: {
			endpoint: subscription.endpoint,
			keys: { p256dh: keys.p256dh ?? "", auth: keys.auth ?? "" },
		},
	});
	if (!saved.ok) {
		const detail = typeof saved.data === "object" && saved.data ? String((saved.data as { detail?: string }).detail ?? "") : "";
		return { permission, subscribed: false, serverEnabled: false, detail: detail || `订阅保存失败 (HTTP ${saved.status})` };
	}
	return {
		permission,
		subscribed: true,
		serverEnabled: readiness.serverEnabled,
		detail: readiness.serverEnabled ? "已开启推送" : "已开启订阅，但后端未装 pywebpush，暂时不会真发推送",
	};
}

/** Drop both the browser subscription and the backend record. */
export async function disablePush(): Promise<PushState> {
	const base = describe();
	if (base.permission === "unsupported" || !("serviceWorker" in navigator)) {
		return { ...base, detail: "此浏览器不支持 Web Push" };
	}
	const registration = await navigator.serviceWorker.ready;
	const subscription = await registration.pushManager.getSubscription();
	if (subscription) {
		const endpoint = subscription.endpoint;
		await subscription.unsubscribe();
		await api("gateway", "api/v1/push/subscriptions", {
			method: "DELETE",
			query: { endpoint },
		});
	}
	return { ...base, detail: "已关闭推送" };
}

/** Read current state without asking the user for anything. */
export async function pushState(): Promise<PushState> {
	const base = describe();
	if (base.permission === "unsupported" || !("serviceWorker" in navigator)) {
		return { ...base, detail: "此浏览器不支持 Web Push" };
	}
		try {
			const registration = await navigator.serviceWorker.ready;
			const subscription = await registration.pushManager.getSubscription();
			const status = await api("gateway", "api/v1/push/status", { method: "GET" });
			return {
				permission: base.permission,
				subscribed: Boolean(subscription),
				serverEnabled: status.ok ? Boolean((status.data as PushStatusInfo).enabled) : false,
				detail: status.ok ? String((status.data as PushStatusInfo).hint ?? "") : "",
			};
		} catch {
			return { ...base, detail: "推送状态读取失败" };
		}
}
