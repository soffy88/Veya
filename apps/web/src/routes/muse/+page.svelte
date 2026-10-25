<script lang="ts">
	/**
	 * /muse — 窗口：一个页面，多个窗口。
	 *
	 * 每扇窗口都是一台独立的浏览器（宿主 :99 桌面上有界面 Chromium，自带
	 * 配置目录，cookie 互不干扰），画面实时传进本页，点画面、打字都直接
	 * 在窗口里生效。Muse、Hark 这类网站都能登，不用再去别的桌面找窗口。
	 * 登录由你本人在窗口里完成，本页不碰你的密码。
	 *
	 * 窗口画面走 /windows/stream 中继（与前端同源同进程）：连接时先验
	 * owner，挂接只认会话名（中继自己查管理器的端口，客户端够不着 CDP）。
	 * 新窗口默认走本机代理（Muse 这类站需要）；某个站走代理打不开时，
	 * 建窗口时取消勾选再试。
	 */
	import { onDestroy, onMount } from "svelte";
	import { authHeader } from "$lib/auth.svelte";

	interface SessionInfo {
		status: string;
		port: number | null;
		url: string | null;
		pid: number | null;
		proxy?: string | null;
	}

	interface Stream {
		ws: WebSocket;
		lastW: number;
		lastH: number;
		lastMove: number;
	}

	let sessions = $state<Record<string, SessionInfo>>({});
	let streamNote = $state<Record<string, string>>({});
	let name = $state("");
	let url = $state("https://muse.ai");
	let useProxy = $state(true);
	let busy = $state(false);
	let error = $state("");
	let notice = $state("");

	const streams = new Map<string, Stream>();

	function token(): string {
		const h = authHeader() as Record<string, string>;
		const a = h.Authorization ?? h.authorization ?? "";
		return a.replace(/^Bearer\s+/, "");
	}

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

	function canvasOf(sname: string): HTMLCanvasElement | null {
		return document.getElementById(`cv-${sname}`) as HTMLCanvasElement | null;
	}

	function drawFrame(sname: string, m: { data: string; width: number; height: number }): void {
		const st = streams.get(sname);
		const canvas = canvasOf(sname);
		if (!st || !canvas) return;
		st.lastW = m.width;
		st.lastH = m.height;
		const img = new Image();
		img.onload = () => {
			canvas.width = m.width;
			canvas.height = m.height;
			canvas.getContext("2d")?.drawImage(img, 0, 0);
		};
		img.src = `data:image/jpeg;base64,${m.data}`;
	}

	function sendInput(sname: string, obj: Record<string, unknown>): void {
		const st = streams.get(sname);
		if (st && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify(obj));
	}

	function coords(sname: string, ev: MouseEvent | WheelEvent): { x: number; y: number } {
		const canvas = canvasOf(sname);
		const st = streams.get(sname);
		if (!canvas) return { x: 0, y: 0 };
		const rect = canvas.getBoundingClientRect();
		const w = st?.lastW || canvas.width || rect.width;
		const h = st?.lastH || canvas.height || rect.height;
		return {
			x: (ev.clientX - rect.left) * (w / rect.width),
			y: (ev.clientY - rect.top) * (h / rect.height),
		};
	}

	function connect(sname: string): void {
		const old = streams.get(sname);
		if (old && old.ws.readyState <= 1) return;
		const tk = token();
		if (!tk) {
			error = "请先登录主站";
			return;
		}
		const proto = location.protocol === "https:" ? "wss:" : "ws:";
		const ws = new WebSocket(`${proto}//${location.host}/windows/stream`);
		streams.set(sname, { ws, lastW: 0, lastH: 0, lastMove: 0 });
		streamNote[sname] = "连接中…";
		ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: tk }));
		ws.onmessage = (ev) => {
			let m: { type: string; data?: string; width?: number; height?: number; message?: string };
			try {
				m = JSON.parse(ev.data as string);
			} catch {
				return;
			}
			if (m.type === "ready") ws.send(JSON.stringify({ type: "attach", session: sname }));
			else if (m.type === "attached") streamNote[sname] = "直播中";
			else if (m.type === "frame" && m.data && m.width && m.height)
				drawFrame(sname, { data: m.data, width: m.width, height: m.height });
			else if (m.type === "error") streamNote[sname] = m.message ?? "连接失败";
			else if (m.type === "detached") streamNote[sname] = "已断开";
		};
		ws.onclose = () => {
			if (streamNote[sname] === "直播中") streamNote[sname] = "已断开";
		};
	}

	function disconnect(sname: string): void {
		const st = streams.get(sname);
		if (st) {
			try {
				st.ws.close();
			} catch {
				/* ignore */
			}
			streams.delete(sname);
		}
		delete streamNote[sname];
	}

	async function refresh(): Promise<void> {
		busy = true;
		error = "";
		try {
			sessions = (await call("/muse-sessions")) as Record<string, SessionInfo>;
			for (const [sname, s] of Object.entries(sessions)) {
				if (s.status === "running") connect(sname);
				else disconnect(sname);
			}
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	async function act(action: string, target: string, extra: Record<string, unknown> = {}): Promise<void> {
		busy = true;
		error = "";
		notice = "";
		try {
			const r = (await call("/muse-sessions", {
				method: "POST",
				body: JSON.stringify({ action, name: target, ...extra }),
			})) as { ok?: boolean; error?: string };
			if (!r.ok) error = r.error ?? "操作失败";
			else {
				notice = `${action} ${target} 已执行`;
				if (action === "close" || action === "remove") disconnect(target);
			}
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
			error = "窗口名只允许字母、数字、_、- (1-32 位)";
			return;
		}
		const u = url.trim();
		if (!u.startsWith("https://")) {
			error = "网址必须以 https:// 开头";
			return;
		}
		await act("create", n, { url: u, ...(useProxy ? { proxy: "127.0.0.1:7890" } : {}) });
		if (!error) name = "";
	}

	function onCanvasMouse(sname: string, ev: MouseEvent, kind: string): void {
		const canvas = canvasOf(sname);
		if (!canvas) return;
		canvas.focus();
		const p = coords(sname, ev);
		const button = ev.button === 0 ? "left" : ev.button === 1 ? "middle" : "right";
		sendInput(sname, { type: "mouse", kind, x: p.x, y: p.y, button, clickCount: 1 });
	}

	function onCanvasMove(sname: string, ev: MouseEvent): void {
		const st = streams.get(sname);
		if (!st) return;
		if (Date.now() - st.lastMove < 30) return;
		st.lastMove = Date.now();
		const p = coords(sname, ev);
		sendInput(sname, {
			type: "mouse",
			kind: "mouseMoved",
			x: p.x,
			y: p.y,
			button: ev.buttons & 1 ? "left" : "none",
			clickCount: 0,
		});
	}

	function onKey(ev: KeyboardEvent, kind: "keyDown" | "keyUp"): void {
		const el = document.activeElement;
		if (!(el instanceof HTMLCanvasElement)) return;
		const sname = el.id.replace(/^cv-/, "");
		if (!streams.has(sname)) return;
		ev.preventDefault();
		const msg: Record<string, unknown> = { type: "key", kind, key: ev.key, code: ev.code };
		if (kind === "keyDown" && ev.key.length === 1) msg.text = ev.key;
		sendInput(sname, msg);
	}

	function onKeyDown(ev: KeyboardEvent): void {
		onKey(ev, "keyDown");
	}
	function onKeyUp(ev: KeyboardEvent): void {
		onKey(ev, "keyUp");
	}
	function onWindowMouseDown(ev: MouseEvent): void {
		if (!(ev.target instanceof HTMLCanvasElement)) {
			const el = document.activeElement;
			if (el instanceof HTMLCanvasElement) el.blur();
		}
	}

	onMount(() => {
		window.addEventListener("keydown", onKeyDown);
		window.addEventListener("keyup", onKeyUp);
		window.addEventListener("mousedown", onWindowMouseDown);
		void refresh();
	});

	onDestroy(() => {
		// 注意：Svelte 5 的 onDestroy 在服务端渲染结束时也会执行一次，
		// 而服务端没有 window，必须先判存在，否则整页 500。
		if (typeof window !== "undefined") {
			window.removeEventListener("keydown", onKeyDown);
			window.removeEventListener("keyup", onKeyUp);
			window.removeEventListener("mousedown", onWindowMouseDown);
		}
		for (const sname of [...streams.keys()]) disconnect(sname);
	});
</script>

<div class="mx-auto max-w-5xl px-4 py-6">
	<h1 class="text-xl font-semibold text-terminal-fg">窗口</h1>
	<p class="mt-2 text-sm leading-6 text-terminal-dim">
		一个页面，多个窗口：每扇窗口都是一台独立的浏览器，各登各的账号、互不干扰。
		Muse、Hark 这类网站都能登，窗口直接嵌在页面里，点画面就能用，不用再去别的桌面。
		新建窗口默认走本机代理（Muse 这类站需要）；如果某个站走代理打不开，建窗口时取消勾选再试。
	</p>

	<div class="mt-4 flex flex-wrap items-center gap-2">
		<input
			type="text"
			placeholder="窗口名，如 muse-a"
			autocomplete="off"
			bind:value={name}
			onkeydown={(e) => {
				if (e.key === "Enter") void create();
			}}
			class="w-44 rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-white/30 focus:border-sky-400/50"
		/>
		<input
			type="text"
			placeholder="网址，如 https://muse.ai"
			autocomplete="off"
			bind:value={url}
			onkeydown={(e) => {
				if (e.key === "Enter") void create();
			}}
			class="w-64 rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-terminal-fg outline-none placeholder:text-white/30 focus:border-sky-400/50"
		/>
		<label class="flex items-center gap-1.5 text-sm text-terminal-dim">
			<input type="checkbox" bind:checked={useProxy} class="accent-sky-400" />
			走本机代理
		</label>
		<button
			type="button"
			disabled={busy}
			onclick={() => void create()}
			class="rounded-lg bg-sky-500/20 px-4 py-2 text-sm text-sky-200 transition hover:bg-sky-500/30 disabled:opacity-50"
		>
			新建窗口
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

	<div class="mt-4 space-y-4">
		{#each Object.entries(sessions) as [sname, s] (sname)}
			<div class="rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<div class="text-sm">
						<p class="font-medium text-terminal-fg">
							{sname}
							<span class="ml-2 rounded bg-white/10 px-1.5 py-0.5 text-xs text-terminal-dim">{s.status}</span>
							{#if streamNote[sname]}
								<span class="ml-1 rounded bg-white/10 px-1.5 py-0.5 text-xs text-terminal-dim">{streamNote[sname]}</span>
							{/if}
						</p>
						<p class="mt-1 text-xs text-terminal-dim">
							{s.url ?? ""}{s.proxy ? ` · 走本机代理` : " · 直连"}
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
							删除窗口
						</button>
					</div>
				</div>
				{#if s.status === "running"}
					<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
					<canvas
						id={`cv-${sname}`}
						tabindex="0"
						class="mt-3 w-full cursor-pointer rounded-lg border border-white/10 bg-black focus:outline-2 focus:outline-sky-400"
						onmousemove={(e) => onCanvasMove(sname, e)}
						onmousedown={(e) => onCanvasMouse(sname, e, "mousePressed")}
						onmouseup={(e) => onCanvasMouse(sname, e, "mouseReleased")}
						onwheel={(e) => {
							e.preventDefault();
							const p = coords(sname, e);
							sendInput(sname, { type: "wheel", x: p.x, y: p.y, deltaX: e.deltaX, deltaY: e.deltaY });
						}}
						oncontextmenu={(e) => e.preventDefault()}
					></canvas>
					<p class="mt-1 text-xs text-terminal-dim">点画面进入窗口，键盘直接打字；点画面外退出。</p>
				{:else}
					<p class="mt-3 text-sm text-terminal-dim">窗口已关闭，点“打开窗口”恢复（登录态还在）。</p>
				{/if}
			</div>
		{:else}
			<p class="text-sm text-terminal-dim">还没有窗口，上面起一个。</p>
		{/each}
	</div>
</div>
