<script lang="ts">
	import { tick } from "svelte";
	import { login, register, logout, auth, authHeader } from "$lib/auth.svelte";
	import { notifyStore } from "$lib/notifications.svelte";
	import { sessionStore } from "$lib/sessionStore.svelte";
	import { Bot, Loader2, LogOut, User } from "lucide-svelte";

	let mode = $state<"login" | "register">("login");
	let username = $state("");
	let password = $state("");
	let busy = $state(false);
	let error = $state("");
	let showAuth = $state(false); // 默认收起, 点「登录」展开
	let triggerEl = $state<HTMLButtonElement>();
	let dialogEl = $state<HTMLDivElement>();
	let usernameEl = $state<HTMLInputElement>();
	const isAuthed = $derived(auth.user !== null && auth.token !== "");

	async function openAuth(): Promise<void> {
		showAuth = true;
		error = "";
		await tick();
		usernameEl?.focus();
	}

	async function closeAuth(): Promise<void> {
		showAuth = false;
		error = "";
		await tick();
		triggerEl?.focus();
	}

	function handleDialogKeydown(event: KeyboardEvent): void {
		if (!showAuth) return;
		if (event.key === "Escape") {
			event.preventDefault();
			void closeAuth();
			return;
		}
		if (event.key !== "Tab" || !dialogEl) return;

		const focusable = Array.from(
			dialogEl.querySelectorAll<HTMLElement>(
				'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
			),
		).filter((element) => element.getClientRects().length > 0);
		if (focusable.length === 0) return;
		const active = document.activeElement as HTMLElement | null;
		const index = active ? focusable.indexOf(active) : -1;
		const nextIndex = event.shiftKey
			? index <= 0
				? focusable.length - 1
				: index - 1
			: index < 0 || index >= focusable.length - 1
				? 0
				: index + 1;
		event.preventDefault();
		focusable[nextIndex]?.focus();
	}

	async function submit() {
		if (!username.trim() || !password) {
			error = "请输入用户名和密码";
			return;
		}
		busy = true;
		error = "";
		try {
			if (mode === "register") await register(username.trim(), password);
			else await login(username.trim(), password);
			username = "";
			password = "";
			showAuth = false;
			// 换用户 → 通知流重连 + 拉取云端会话列表 (多端同步)
			notifyStore.disconnect();
			notifyStore.connect();
			void sessionStore.syncCloud();
		} catch (e) {
			error = e instanceof Error ? e.message : "操作失败";
		} finally {
			busy = false;
		}
	}

	async function signOut(): Promise<void> {
		await logout();
		// token 清除后再重连匿名通知流，避免旧身份短暂复用。
		notifyStore.disconnect();
		notifyStore.connect();
	}
</script>

{#if isAuthed}
	<div class="auth-badge">
		<User size={13} />
		<span class="uname">{auth.user?.username}</span>
		<button class="ghost" title="退出登录" aria-label="退出登录" onclick={() => void signOut()}><LogOut size={13} /></button>
	</div>
{:else}
	<button
		bind:this={triggerEl}
		class="ghost login-btn"
		aria-haspopup="dialog"
		aria-expanded={showAuth}
		onclick={() => (showAuth ? void closeAuth() : void openAuth())}
	>
		<User size={13} /> 登录
	</button>
	{#if showAuth}
		<button
			type="button"
			class="auth-backdrop"
			aria-label="关闭登录"
			onclick={() => void closeAuth()}
		></button>
		<div
			bind:this={dialogEl}
			class="auth-card"
			role="dialog"
			tabindex="-1"
			aria-modal="true"
			aria-label={mode === "login" ? "登录 Veya" : "注册 Veya"}
			onkeydown={handleDialogKeydown}
		>
			<div class="tabs">
				<button type="button" class:on={mode === "login"} aria-pressed={mode === "login"} onclick={() => (mode = "login")}>登录</button>
				<button type="button" class:on={mode === "register"} aria-pressed={mode === "register"} onclick={() => (mode = "register")}>注册</button>
			</div>
			<input
				bind:this={usernameEl}
				aria-label="用户名"
				autocomplete="username"
				placeholder="用户名 (3-32 位)"
				bind:value={username}
				onkeydown={(e) => e.key === "Enter" && submit()}
			/>
			<input
				aria-label="密码"
				autocomplete={mode === "register" ? "new-password" : "current-password"}
				placeholder="密码 (≥6 位)"
				type="password"
				bind:value={password}
				onkeydown={(e) => e.key === "Enter" && submit()}
			/>
			{#if error}<div class="err">{error}</div>{/if}
			<button class="submit" disabled={busy} onclick={submit}>
				{#if busy}<Loader2 size={13} class="animate-spin" />{/if}
				{mode === "login" ? "登录" : "注册并登录"}
			</button>
			<div class="hint">登录后：多端同步会话/计划，手机发命令电脑可确认执行。</div>
		</div>
	{/if}
{/if}

<style>
	.auth-badge {
		display: flex;
		align-items: center;
		gap: 6px;
		color: var(--text-dim, #8b93a7);
		font-size: 12px;
		padding: 2px 8px;
	}
	.uname {
		max-width: 120px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ghost {
		background: none;
		border: 1px solid var(--border, #2a2f3a);
		color: var(--text-dim, #8b93a7);
		border-radius: 8px;
		min-height: 44px;
		padding: 0 10px;
		font-size: 12px;
		cursor: pointer;
		display: inline-flex;
		align-items: center;
		gap: 5px;
	}
	.ghost:hover {
		border-color: var(--accent, #4f8cff);
		color: var(--text, #e8ecf4);
	}
	.auth-backdrop {
		position: fixed;
		inset: 0;
		z-index: 49;
		border: 0;
		background: rgb(0 0 0 / 0.18);
		cursor: default;
	}

	.auth-card {
		position: absolute;
		top: 44px;
		right: 12px;
		width: 260px;
		background: var(--bg-panel, #171a21);
		border: 1px solid var(--border, #2a2f3a);
		border-radius: 12px;
		padding: 14px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		z-index: 50;
		box-shadow: 0 8px 28px rgb(0 0 0 / 0.45);
	}
	.tabs {
		display: flex;
		gap: 6px;
	}
	.tabs button {
		flex: 1;
		background: none;
		border: 1px solid var(--border, #2a2f3a);
		color: var(--text-dim, #8b93a7);
		border-radius: 8px;
		min-height: 44px;
		padding: 0;
		cursor: pointer;
		font-size: 12px;
	}
	.tabs button.on {
		border-color: var(--accent, #4f8cff);
		color: var(--text, #e8ecf4);
	}
	input {
		background: var(--bg, #101319);
		border: 1px solid var(--border, #2a2f3a);
		color: var(--text, #e8ecf4);
		border-radius: 8px;
		min-height: 44px;
		padding: 0 10px;
		font-size: 13px;
		outline: none;
	}
	input:focus {
		border-color: var(--accent, #4f8cff);
	}
	.err {
		color: #f47067;
		font-size: 12px;
	}
	.submit {
		background: var(--accent, #4f8cff);
		color: #fff;
		border: none;
		border-radius: 8px;
		min-height: 44px;
		padding: 0;
		cursor: pointer;
		font-size: 13px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
	}
	.submit:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.hint {
		font-size: 11px;
		color: var(--text-dim, #8b93a7);
		line-height: 1.5;
	}
</style>
