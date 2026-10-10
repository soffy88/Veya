/// <reference types="@sveltejs/kit" />
/// <reference lib="webworker" />

/**
 * Veya Team service worker: offline shell + Web Push.
 *
 * Two jobs, deliberately narrow:
 *
 * 1. **Offline shell** — a phone that opened /team once can reopen it with no
 *    network and still see the chrome (bot list, groups, last transcript).
 * 2. **Push** — ``server/bot_team/push.py`` delivers ``{title, body, url}``
 *    when a bot turn / group round / routine finishes; the notification click
 *    opens exactly that chat.
 *
 * Never intercepted: ``/api/*``. A cached API answer is a lie about the current
 * state of a bot team, and POST/PATCH/DELETE are never cacheable anyway. When
 * the app is offline those requests fail with the normal network error, which
 * the UI already renders (see ``$lib/api.ts``).
 */

import { build, files, version } from "$service-worker";

/** Versioned, so a deploy drops the previous shell instead of layering on it. */
const CACHE = `veya-shell-${version}`;

/** The route a cold install starts from; also the navigation fallback. */
const APP_SHELL = "/team";

/** Static files come back from SvelteKit without their leading slash. */
const withSlash = (path: string): string => (path.startsWith("/") ? path : `/${path}`);

const SHELL_ASSETS = [APP_SHELL, "/", "/manifest.webmanifest", "/icons/icon.svg"];
const PRECACHE = [...build, ...files.map(withSlash), ...SHELL_ASSETS];

self.addEventListener("install", (event) => {
	event.waitUntil(
		(async () => {
			const cache = await caches.open(CACHE);
			// addAll rejects wholesale if one entry 404s; a missing entry must not
			// stop the rest of the shell from being cached.
			await Promise.all(
				PRECACHE.map((url) => cache.add(new Request(url, { cache: "reload" })).catch(() => undefined)),
			);
		})(),
	);
});

self.addEventListener("activate", (event) => {
	event.waitUntil(
		(async () => {
			const names = await caches.keys();
			await Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name)));
			await self.clients.claim();
		})(),
	);
});

self.addEventListener("fetch", (event) => {
	const request = event.request;
	if (request.method !== "GET") return;

	const url = new URL(request.url);
	if (url.origin !== self.location.origin) return;
	// The app's own data plane stays live (see the module docstring).
	if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/legacy/")) return;

	if (request.mode === "navigate") {
		// Network first: an online visit must show today's team, not yesterday's
		// shell. Offline, fall back to the once-cached shell.
		event.respondWith(
			(async () => {
				try {
					return await fetch(request);
				} catch {
					const cache = await caches.open(CACHE);
					return (
						(await cache.match(APP_SHELL)) ??
						(await cache.match("/")) ??
						new Response("offline", { status: 503, headers: { "content-type": "text/plain" } })
					);
				}
			})(),
		);
		return;
	}

	// Hashed build assets are immutable: cache first, and only fill on a miss.
	if (url.pathname.startsWith("/_app/")) {
		event.respondWith(
			(async () => {
				const cache = await caches.open(CACHE);
				const hit = await cache.match(request);
				if (hit) return hit;
				const response = await fetch(request);
				if (response.ok) cache.put(request, response.clone());
				return response;
			})(),
		);
	}
});

// ── Web Push ─────────────────────────────────────────────────────────────

interface PushData {
	title?: string;
	body?: string;
	url?: string;
	tag?: string;
	kind?: string;
}

function payloadOf(event: ExtendableEvent): PushData {
	// A malformed payload must still show something: a silent drop is a
	// notification the user never gets and nobody notices.
	try {
		const text = (event as PushEvent).data?.text() ?? "";
		const parsed = text ? JSON.parse(text) : {};
		return typeof parsed === "object" && parsed ? (parsed as PushData) : {};
	} catch {
		return {};
	}
}

self.addEventListener("push", (event) => {
	if (!(event instanceof PushEvent)) return;
	const data = payloadOf(event);
	event.waitUntil(
		self.registration.showNotification(data.title || "Veya", {
			body: data.body || "",
			tag: data.tag || "veya-team",
			icon: "/icons/icon-192.png",
			badge: "/icons/icon-192.png",
			data: { url: data.url || APP_SHELL },
		}),
	);
});

self.addEventListener("notificationclick", (event) => {
	event.notification.close();
	const target = String(event.notification.data?.url || APP_SHELL);
	event.waitUntil(
		(async () => {
			const absolute = new URL(target, self.location.origin).href;
			const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
			// Reuse the tab that is already running the app, then point it at the
			// chat the notification came from.
			for (const client of clients) {
				if (new URL(client.url).origin !== self.location.origin) continue;
				await (client as WindowClient).navigate?.(absolute);
				return client.focus();
			}
			return self.clients.openWindow(absolute);
		})(),
	);
});
