// Custom frontend server: SvelteKit handler + /windows/stream relay.
//
// The windows page needs a WebSocket relay between the user's browser and
// each session's loopback CDP port. adapter-node's generated server cannot
// host it, but its handler can be reused here, so the relay rides the same
// :3105 process and the same public path (no extra service, no Caddy change).
//
// Security (CDP = full control of a logged-in browser, so this is the gate):
// - Every stream connection must first send {type:'auth', token}; the relay
//   verifies the token against the backend /me and requires is_owner.
// - Attach accepts only a session NAME, never a raw port: the relay resolves
//   the port from the manager's own list output, so a client cannot probe
//   arbitrary loopback ports. CDP ports stay loopback-only.
// - Tokens are never logged. Frame data is never logged.
import http from "node:http";
import { execFile } from "node:child_process";
import { handler } from "./build/handler.js";
import { WebSocketServer, WebSocket } from "ws";

const PORT = Number(process.env.PORT ?? 3000);
const MANAGER = "/data/soffy/projects/veya/apps/web/scripts/muse-sessions.py";
const GATEWAY = (process.env.VEYA_GATEWAY_PUBLIC ?? "http://127.0.0.1:8767").replace(/\/+$/, "");
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

function runManager(args) {
	return new Promise((resolve, reject) => {
		execFile("python3", [MANAGER, ...args], { timeout: 30000 }, (err, stdout, stderr) => {
			if (err) {
				reject(new Error((stderr || err.message).slice(0, 200)));
				return;
			}
			try {
				resolve(JSON.parse(stdout));
			} catch {
				reject(new Error("manager 输出不是 JSON"));
			}
		});
	});
}

async function requireOwner(token) {
	if (!token) return false;
	try {
		const me = await fetch(`${GATEWAY}/api/v1/auth/me`, {
			headers: { authorization: `Bearer ${token}` },
		});
		if (!me.ok) return false;
		const data = await me.json();
		return data.is_owner === true;
	} catch {
		return false;
	}
}

const server = http.createServer(handler);
const wss = new WebSocketServer({ server, path: "/windows/stream" });

wss.on("connection", (ws) => {
	let authed = false;
	let cdp = null;
	let session = null;

	const send = (obj) => {
		try {
			if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
		} catch {
			/* closed */
		}
	};
	const closeCdp = () => {
		try {
			cdp?.send(JSON.stringify({ id: 9999, method: "Page.stopScreencast" }));
		} catch {
			/* ignore */
		}
		try {
			cdp?.close();
		} catch {
			/* ignore */
		}
		cdp = null;
	};

	ws.on("message", async (raw) => {
		let msg;
		try {
			msg = JSON.parse(raw.toString());
		} catch {
			return;
		}
		try {
			if (msg.type === "auth") {
				authed = await requireOwner(msg.token);
				send(authed ? { type: "ready" } : { type: "error", message: "未登录或不是 owner" });
				if (!authed) ws.close();
				return;
			}
			if (!authed) {
				send({ type: "error", message: "请先登录" });
				return;
			}
			if (msg.type === "attach") {
				if (typeof msg.session !== "string" || !NAME_RE.test(msg.session)) {
					send({ type: "error", message: "会话名不合法" });
					return;
				}
				const list = await runManager(["list"]);
				const entry = list[msg.session];
				if (!entry || entry.status !== "running" || !entry.port) {
					send({ type: "error", message: "会话未运行" });
					return;
				}
				const pages = await fetch(`http://127.0.0.1:${entry.port}/json`).then((r) => r.json());
				const page = pages.find((p) => p.type === "page");
				if (!page?.webSocketDebuggerUrl) {
					send({ type: "error", message: "找不到窗口页面" });
					return;
				}
				closeCdp();
				session = msg.session;
				cdp = new WebSocket(page.webSocketDebuggerUrl);
				cdp.on("open", () => {
					cdp.send(JSON.stringify({ id: 1, method: "Page.enable" }));
					cdp.send(
						JSON.stringify({
							id: 2,
							method: "Page.startScreencast",
							params: { format: "jpeg", quality: 70, maxWidth: 1280, maxHeight: 900 },
						}),
					);
					send({ type: "attached", session });
				});
				cdp.on("message", (data) => {
					let m;
					try {
						m = JSON.parse(data.toString());
					} catch {
						return;
					}
					if (m.method === "Page.screencastFrame") {
						send({
							type: "frame",
							data: m.params.data,
							width: m.params.metadata.deviceWidth,
							height: m.params.metadata.deviceHeight,
						});
						try {
							cdp.send(
								JSON.stringify({
									id: 3,
									method: "Page.screencastFrameAck",
									params: { sessionId: m.params.sessionId },
								}),
							);
						} catch {
							/* ignore */
						}
					}
				});
				cdp.on("close", () => {
					if (session) send({ type: "detached", session });
				});
				cdp.on("error", () => {});
				return;
			}
			if (msg.type === "detach") {
				closeCdp();
				session = null;
				return;
			}
			if (!cdp || cdp.readyState !== WebSocket.OPEN) return;
			if (msg.type === "mouse") {
				cdp.send(
					JSON.stringify({
						id: 10,
						method: "Input.dispatchMouseEvent",
						params: {
							type: msg.kind,
							x: msg.x,
							y: msg.y,
							button: msg.button ?? "none",
							clickCount: msg.clickCount ?? 0,
						},
					}),
				);
				return;
			}
			if (msg.type === "wheel") {
				cdp.send(
					JSON.stringify({
						id: 11,
						method: "Input.dispatchMouseEvent",
						params: { type: "mouseWheel", x: msg.x, y: msg.y, deltaX: msg.deltaX, deltaY: msg.deltaY },
					}),
				);
				return;
			}
			if (msg.type === "insertText") {
				cdp.send(
					JSON.stringify({
						id: 13,
						method: "Input.insertText",
						params: { text: msg.text ?? "" },
					}),
				);
				return;
			}
			if (msg.type === "key") {
				const params = { type: msg.kind, key: msg.key, code: msg.code };
				if (msg.text) {
					params.text = msg.text;
					params.unmodifiedText = msg.text;
				}
				cdp.send(JSON.stringify({ id: 12, method: "Input.dispatchKeyEvent", params }));
				return;
			}
		} catch (e) {
			send({ type: "error", message: String(e?.message ?? e).slice(0, 200) });
		}
	});

	ws.on("close", closeCdp);
	ws.on("error", closeCdp);
});

server.listen(PORT, () => console.log(`frontend + windows relay on :${PORT}`));
