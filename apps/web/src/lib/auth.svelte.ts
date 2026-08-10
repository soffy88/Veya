/**
 * Veya Auth — 用户注册/登录/登出 + token 管理 (localStorage)。
 *
 * 未登录时所有 API 以 anonymous 身份工作 (行为不变);
 * 登录后 api() 自动携带 Authorization: Bearer <token>, 数据按用户隔离。
 *
 * Svelte 5 限制: 模块级 $state 导出不可重新赋值 → 用单对象导出, 只 mutate 属性。
 */

const LS_TOKEN = "veya.auth.token";
const LS_USER = "veya.auth.user";

function loadUser(): { user_id: string; username: string } | null {
	if (typeof localStorage === "undefined") return null;
	try {
		const raw = localStorage.getItem(LS_USER);
		return raw ? JSON.parse(raw) : null;
	} catch {
		return null;
	}
}

function loadToken(): string {
	if (typeof localStorage === "undefined") return "";
	return localStorage.getItem(LS_TOKEN) ?? "";
}

/** 登录态单对象 (组件内用 $derived(auth.user && auth.token) 派生响应式)。 */
export const auth = $state<{ token: string; user: { user_id: string; username: string } | null }>({
	token: loadToken(),
	user: loadUser(),
});

export function authHeader(): Record<string, string> {
	return auth.token ? { authorization: `Bearer ${auth.token}` } : {};
}

function persist(user: { user_id: string; username: string } | null, token: string) {
	auth.user = user;
	auth.token = token;
	if (typeof localStorage === "undefined") return;
	if (token) localStorage.setItem(LS_TOKEN, token);
	else localStorage.removeItem(LS_TOKEN);
	if (user) localStorage.setItem(LS_USER, JSON.stringify(user));
	else localStorage.removeItem(LS_USER);
}

async function post(path: string, body: unknown) {
	const base: string = (import.meta.env.VITE_VEYA_ENDPOINT as string | undefined) ?? "";
	const res = await fetch(`${base}/api/v1/auth/${path}`, {
		method: "POST",
		headers: { "content-type": "application/json" },
		body: JSON.stringify(body),
	});
	const text = await res.text();
	let data: unknown = text;
	try {
		data = text ? JSON.parse(text) : null;
	} catch {
		/* raw */
	}
	if (!res.ok) {
		const detail = (data as { detail?: string } | null)?.detail;
		throw new Error(detail || `请求失败 (${res.status})`);
	}
	return data as { user_id: string; username: string; token: string };
}

export async function register(username: string, password: string) {
	const r = await post("register", { username, password });
	persist({ user_id: r.user_id, username: r.username }, r.token);
}

export async function login(username: string, password: string) {
	const r = await post("login", { username, password });
	persist({ user_id: r.user_id, username: r.username }, r.token);
}

export async function logout() {
	try {
		await fetch(`${(import.meta.env.VITE_VEYA_ENDPOINT as string | undefined) ?? ""}/api/v1/auth/logout`, {
			method: "POST",
			headers: authHeader(),
		});
	} catch {
		/* 忽略登出网络错误 */
	}
	persist(null, "");
}
