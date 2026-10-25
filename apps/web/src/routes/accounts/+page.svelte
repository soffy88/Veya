<script lang="ts">
	/**
	 * /accounts — 多账号窗口 (2026-10-25)。
	 *
	 * 一个页面内开多个窗口, 每个窗口独立登录一个账号, 互不影响。
	 *
	 * 隔离是怎么保证的 (关键, 别改坏):
	 * - 每个窗口的 token 只存在本组件的 $state 内存里, 绝不写 localStorage、
	 *   不写 cookie、不碰 $lib/auth.svelte 的全局登录态。
	 * - 窗口的请求全部用原生 fetch + 显式 Authorization 头, 绝不走 $lib/api
	 *   的 api() (它读全局 token, 且 401 时会 clearAuth() 清掉主登录态)。
	 * - 因此: 窗口 B 登录不会覆盖窗口 A, 窗口退出只退本窗口, 主登录态
	 *   (右上角) 全程不受影响; 刷新页面后窗口登录态消失, 主登录态不变。
	 */
	import { onMount } from "svelte";

	const BASE: string = (import.meta.env.VITE_VEYA_ENDPOINT as string | undefined) ?? "";

	interface PaneMe {
		user_id: string;
		username: string;
		is_owner: boolean;
	}

	interface Pane {
		id: number;
		username: string;
		password: string;
		token: string;
		me: PaneMe | null;
		bots: number | null;
		busy: boolean;
		error: string;
	}

	let nextId = $state(1);
	let panes = $state<Pane[]>([newPane()]);

	function newPane(): Pane {
		return {
			id: nextId++,
			username: "",
			password: "",
			token: "",
			me: null,
			bots: null,
			busy: false,
			error: "",
		};
	}

	function addPane(): void {
		panes = [...panes, newPane()];
	}

	function removePane(id: number): void {
		panes = panes.filter((p) => p.id !== id);
		if (panes.length === 0) panes = [newPane()];
	}

	async function readJson(res: Response): Promise<unknown> {
		const text = await res.text();
		try {
			return text ? JSON.parse(text) : null;
		} catch {
			return text;
		}
	}

	function detailOf(data: unknown, fallback: string): string {
		const d = (data as { detail?: unknown } | null)?.detail;
		return typeof d === "string" ? d : fallback;
	}

	/** 用本窗口 token 拉身份 + 该账号的机器人列表, 证明这个会话真实可用。 */
	async function refreshPane(pane: Pane): Promise<void> {
		if (!pane.token) return;
		pane.busy = true;
		pane.error = "";
		try {
			const meRes = await fetch(`${BASE}/api/v1/auth/me`, {
				headers: { authorization: `Bearer ${pane.token}` },
			});
			const meData = await readJson(meRes);
			if (meRes.status === 401) {
				pane.token = "";
				pane.me = null;
				pane.bots = null;
				pane.error = "登录已失效, 请重新登录";
				return;
			}
			if (!meRes.ok) {
				pane.error = detailOf(meData, `身份验证失败 (${meRes.status})`);
				return;
			}
			pane.me = meData as PaneMe;
			const botsRes = await fetch(`${BASE}/api/v1/bots`, {
				headers: { authorization: `Bearer ${pane.token}` },
			});
			const botsData = await readJson(botsRes);
			if (botsRes.ok) {
				const bots = (botsData as { bots?: unknown[] } | null)?.bots;
				pane.bots = Array.isArray(bots) ? bots.length : null;
			} else {
				pane.bots = null;
			}
		} catch {
			pane.error = "网络请求失败";
		} finally {
			pane.busy = false;
		}
	}

	async function loginPane(pane: Pane): Promise<void> {
		if (!pane.username.trim() || !pane.password) {
			pane.error = "请填写账号和密码";
			return;
		}
		pane.busy = true;
		pane.error = "";
		try {
			const res = await fetch(`${BASE}/api/v1/auth/login`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ username: pane.username.trim(), password: pane.password }),
			});
			const data = await readJson(res);
			if (!res.ok) {
				pane.error = detailOf(data, `登录失败 (${res.status})`);
				return;
			}
			// 只写本窗口内存, 不写全局登录态 — 这就是互不影响的全部保证。
			pane.token = (data as { token: string }).token;
			pane.password = "";
			await refreshPane(pane);
		} catch {
			pane.error = "网络请求失败";
		} finally {
			pane.busy = false;
		}
	}

	async function logoutPane(pane: Pane): Promise<void> {
		const token = pane.token;
		pane.token = "";
		pane.me = null;
		pane.bots = null;
		pane.error = "";
		if (!token) return;
		try {
			await fetch(`${BASE}/api/v1/auth/logout`, {
				method: "POST",
				headers: { authorization: `Bearer ${token}` },
			});
		} catch {
			/* 只清本窗口, 登出网络错误可忽略 */
		}
	}

	onMount(() => {
		// 本页不需要主登录态; 各窗口自己登录。
	});
</script>

<div class="mx-auto max-w-6xl px-4 py-6">
	<h1 class="text-xl font-semibold text-terminal-fg">多账号窗口</h1>
	<p class="mt-2 text-sm leading-6 text-terminal-dim">
		一个页面内开多个窗口, 每个窗口独立登录一个账号, 互不影响。
		窗口的登录凭证只存在本页内存里, 不写浏览器存储、不碰右上角的主登录态;
		刷新页面后窗口登录态消失, 主登录态不变。退出只退本窗口。
	</p>

	<div class="mt-4">
		<button
			type="button"
			onclick={addPane}
			class="rounded-lg bg-sky-500/20 px-4 py-2 text-sm text-sky-200 transition hover:bg-sky-500/30"
		>
			+ 添加窗口
		</button>
	</div>

	<div class="mt-4 grid gap-4 md:grid-cols-2">
		{#each panes as pane, i (pane.id)}
			<div class="rounded-xl border border-white/10 bg-white/[0.03] p-4">
				<div class="flex items-center justify-between">
					<span class="text-sm font-medium text-terminal-fg">窗口 {i + 1}</span>
					<button
						type="button"
						aria-label="关闭窗口"
						onclick={() => removePane(pane.id)}
						class="rounded-md px-2 py-1 text-xs text-terminal-dim transition hover:bg-white/10 hover:text-terminal-fg"
					>
						关闭
					</button>
				</div>

				{#if !pane.token}
					<div class="mt-3 space-y-2">
						<input
							type="text"
							placeholder="账号"
							autocomplete="off"
							bind:value={pane.username}
							class="w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-white/30 focus:border-sky-400/50"
						/>
						<input
							type="password"
							placeholder="密码"
							autocomplete="off"
							bind:value={pane.password}
							onkeydown={(e) => {
								if (e.key === "Enter") void loginPane(pane);
							}}
							class="w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-white/30 focus:border-sky-400/50"
						/>
						<button
							type="button"
							disabled={pane.busy}
							onclick={() => void loginPane(pane)}
							class="rounded-lg bg-sky-500/20 px-4 py-2 text-sm text-sky-200 transition hover:bg-sky-500/30 disabled:opacity-50"
						>
							{pane.busy ? "登录中…" : "登录本窗口"}
						</button>
					</div>
				{:else}
					<div class="mt-3 space-y-1 text-sm">
						<p class="text-terminal-fg">
							已登录: <span class="font-medium">{pane.me?.username ?? pane.username}</span>
							{#if pane.me?.is_owner}
								<span class="ml-2 rounded bg-amber-400/20 px-1.5 py-0.5 text-xs text-amber-200">owner</span>
							{/if}
						</p>
						<p class="text-terminal-dim">
							该账号机器人: {pane.bots === null ? "—" : `${pane.bots} 个`}
						</p>
						<div class="flex gap-2 pt-2">
							<button
								type="button"
								disabled={pane.busy}
								onclick={() => void refreshPane(pane)}
								class="rounded-lg bg-white/10 px-3 py-1.5 text-xs text-terminal-fg transition hover:bg-white/20 disabled:opacity-50"
							>
								刷新身份
							</button>
							<button
								type="button"
								onclick={() => void logoutPane(pane)}
								class="rounded-lg bg-red-500/20 px-3 py-1.5 text-xs text-red-200 transition hover:bg-red-500/30"
							>
								退出本窗口
							</button>
						</div>
					</div>
				{/if}

				{#if pane.error}
					<p class="mt-2 text-xs text-red-300">{pane.error}</p>
				{/if}
			</div>
		{/each}
	</div>
</div>
