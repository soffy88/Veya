import { execFile } from "node:child_process";
import { error, json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";

/**
 * Muse 会话管理 (BFF, 不走 /api/*)。
 *
 * 注意: 生产环境 Caddy 会拦截 /api/* 直转后端网关, 所以本路由故意放在
 * /muse-sessions (不在 /api/* 下), 由 SvelteKit Node 服务自己处理, 经
 * Caddy 的 catch-all 转到 :3105 才能到达。
 *
 * 安全门 (必须, 否则任何公网访客都能开关你的浏览器):
 * - 调用方必须带主站登录 token (Authorization: Bearer), 且经后端
 *   /api/v1/auth/me 核验为 owner, 否则 403。
 * - 会话名只允许字母数字/_/- (防注入); 执行用 execFile (无 shell)。
 * - CDP 端口只绑 127.0.0.1 (见 muse-sessions.py), 公网够不着。
 * - profile 目录含登录态 cookie, 0700, 绝不经本路由外传。
 * - Muse 会话默认走本机代理 (127.0.0.1:7890): 直连到不了 auth.muse.ai,
 *   代理路 0.4 秒通；代理只转发 CONNECT, 登录仍是 HTTPS 加密直达 Muse。
 *   proxy 参数只允许 127.0.0.1:端口。
 */

const MANAGER = "/data/soffy/projects/veya/apps/web/scripts/muse-sessions.py";
const GATEWAY = (process.env.VEYA_GATEWAY_PUBLIC ?? "http://127.0.0.1:8767").replace(/\/+$/, "");
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

function runManager(args: string[]): Promise<unknown> {
	return new Promise((resolve, reject) => {
		execFile("python3", [MANAGER, ...args], { timeout: 60000 }, (err, stdout, stderr) => {
			if (err) {
				reject(new Error((stderr || err.message).slice(0, 300)));
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

async function requireOwner(request: Request): Promise<void> {
	const authz = request.headers.get("authorization");
	if (!authz) error(401, "请先登录主站");
	let me: Response;
	try {
		me = await fetch(`${GATEWAY}/api/v1/auth/me`, { headers: { authorization: authz } });
	} catch {
		error(502, "无法连接后端鉴权");
	}
	if (!me.ok) error(401, "登录已失效");
	const data = (await me.json()) as { is_owner?: boolean };
	if (!data.is_owner) error(403, "只有 owner 可以管理 Muse 会话");
}

export const GET: RequestHandler = async ({ request }) => {
	await requireOwner(request);
	return json(await runManager(["list"]));
};

export const POST: RequestHandler = async ({ request }) => {
	await requireOwner(request);
	let body: { action?: unknown; name?: unknown; url?: unknown; proxy?: unknown };
	try {
		body = await request.json();
	} catch {
		error(400, "请求体必须是 JSON");
	}
	const action = body.action;
	const name = body.name;
	if (action !== "create" && action !== "close" && action !== "remove" && action !== "open") {
		error(400, "action 只能是 create/close/remove/open");
	}
	if (typeof name !== "string" || !NAME_RE.test(name)) {
		error(400, "会话名只允许字母、数字、_、- (1-32 位)");
	}
	const args = [action as string, name];
	if (action === "create" && typeof body.url === "string" && body.url.startsWith("https://")) {
		args.push("--url", body.url);
	}
	if ((action === "create" || action === "open") && typeof body.proxy === "string") {
		if (!/^127\.0\.0\.1:\d{1,5}$/.test(body.proxy) || Number(body.proxy.split(":")[1]) > 65535) {
			error(400, "proxy 只允许 127.0.0.1:端口");
		}
		args.push("--proxy", body.proxy);
	}
	try {
		return json(await runManager(args));
	} catch (e) {
		error(500, (e as Error).message);
	}
};
