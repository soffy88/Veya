<script lang="ts">
	/**
	 * /muse — Muse 会话 (多账号独立浏览器窗口的管理页)。
	 *
	 * 一个会话 = 宿主 :99 桌面上一扇真实的浏览器窗口, 自带独立的配置
	 * 目录 (cookie 互不干扰)。在这里只能建会话、关会话、删会话;
	 * 登录和使用发生在桌面窗口里 (Easel 桌面 noVNC, 你此前登 X 用的
	 * 就是同一块桌面), 由你本人在窗口里完成, 密码不经过本页。
	 *
	 * 本页调用 /muse-sessions (BFF) 时带主站登录 token, 服务端核验
	 * owner 后才放行; 非 owner 打开本页只能看到提示。
	 */
	import { onMount } from "svelte";
	import { authHeader } from "$lib/auth.svelte";

	interface SessionInfo {
		status: string;
		port: number | null;
		url: string | null;
		pid: number | null;
	}

	let sessions = $state<Record<string, SessionInfo>>({});
	let name = $state("");
	let busy = $state(false);
	let error = $state("");
	let notice = $state("");

	async function call(path: string, init?: RequestInit): Promise<unknown> {
		const res = await fetch(path, {
			...init,
			headers: { "content-type": "application/json", ...authHeader(), ...(init?.headers ?? {}) },
		});
		const text = await res.text();
		let data: unknown = text;
		try {
			data = text ? JSON.parse(text) : null;
		} catch {
			/* raw */
		}
		if (!res.ok) {
			const detail = (data as { message?: string } | null)?.message;
			throw new Error(detail || `请求失败 (${res.status})`);
		}
		return data;
	}

	async function refresh(): Promise<void> {
		busy = true;
		error = "";
		try {
			sessions = (await call("/muse-sessions")) as Record<string, SessionInfo>;
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	async function act(action: string, target: string): Promise<void> {
		busy = true;
		error = "";
		notice = "";
		try {
			const r = (await call("/muse-sessions", {
				method: "POST",
				body: JSON.stringify({ action, name: target }),
			})) as { ok?: boolean; error?: string };
			if (!r.ok) error = r.error ?? "操作失败";
			else notice = `${action} ${target} 已执行`;
			await refresh();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	async function create(): Promise<void> {
		const n = name.trim();
		if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/.test(n)) {
			error = "会话名只允许字母、数字、_、- (1-32 位)";
			return;
		}
		await act("create", n);
		if (!error) name = "";
	}

	onMount(() => {
		void refresh();
	});
</script>

<div class="mx-auto max-w-4xl px-4 py-6">
	<h1 class="text-xl font-semibold text-terminal-fg">Muse 会话</h1>
	<p class="mt-2 text-sm leading-6 text-terminal-dim">
		一个会话就是桌面上一扇独立的浏览器窗口, 每个窗口登录不同的 Muse
		账号、互不影响。建好会话后, 去 Easel 桌面 (与此前登 X 用的同一块
		:99 桌面) 找到对应的窗口, 由你本人在里面登录和使用;
		登录态存在该窗口自己的配置目录里, 关掉再打开还在。
		本页不碰你的密码, 也看不到任何会话的登录态。
	</p>

	<div class="mt-4 flex gap-2">
		<input
			type="text"
			placeholder="新会话名, 如 muse-a"
			autocomplete="off"
			bind:value={name}
			onkeydown={(e) => {
				if (e.key === "Enter") void create();
			}}
			class="w-56 rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-white/30 focus:border-sky-400/50"
		/>
		<button
			type="button"
			disabled={busy}
			onclick={() => void create()}
			class="rounded-lg bg-sky-500/20 px-4 py-2 text-sm text-sky-200 transition hover:bg-sky-500/30 disabled:opacity-50"
		>
			新建会话
		</button>
		<button
			type="button"
			disabled={busy}
			onclick={() => void refresh()}
			class="rounded-lg bg-white/10 px-4 py-2 text-sm text-terminal-fg transition hover:bg-white/20 disabled:opacity-50"
		>
			刷新
		</button>
	</div>

	{#if error}
		<p class="mt-3 text-sm text-red-300">{error}</p>
	{/if}
	{#if notice}
		<p class="mt-3 text-sm text-emerald-300">{notice}</p>
	{/if}

	<div class="mt-4 space-y-3">
		{#each Object.entries(sessions) as [sname, s] (sname)}
			<div class="flex items-center justify-between rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3">
				<div class="text-sm">
					<p class="font-medium text-terminal-fg">
						{sname}
						<span class="ml-2 rounded bg-white/10 px-1.5 py-0.5 text-xs text-terminal-dim">{s.status}</span>
					</p>
					<p class="mt-1 text-xs text-terminal-dim">
						{s.url ?? ""}{s.port ? ` · 本机调试端口 ${s.port} (仅宿主可达)` : ""}
					</p>
				</div>
				<div class="flex gap-2">
					{#if s.status === "running"}
						<button
							type="button"
							disabled={busy}
							onclick={() => void act("close", sname)}
							class="rounded-lg bg-white/10 px-3 py-1.5 text-xs text-terminal-fg transition hover:bg-white/20 disabled:opacity-50"
						>
							关闭窗口
						</button>
					{:else}
						<button
							type="button"
							disabled={busy}
							onclick={() => void act("open", sname)}
							class="rounded-lg bg-sky-500/20 px-3 py-1.5 text-xs text-sky-200 transition hover:bg-sky-500/30 disabled:opacity-50"
						>
							打开窗口
						</button>
					{/if}
					<button
						type="button"
						disabled={busy}
						onclick={() => void act("remove", sname)}
						class="rounded-lg bg-red-500/20 px-3 py-1.5 text-xs text-red-200 transition hover:bg-red-500/30 disabled:opacity-50"
					>
						删除会话
					</button>
				</div>
			</div>
		{:else}
			<p class="text-sm text-terminal-dim">还没有会话, 上面起一个。</p>
		{/each}
	</div>
</div>
