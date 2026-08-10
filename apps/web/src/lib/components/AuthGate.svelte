<script lang="ts">
	import { login, register, auth, authHeader } from "$lib/auth.svelte";
	import { notifyStore } from "$lib/notifications.svelte";
	import { Bot, Loader2, LogOut, User } from "lucide-svelte";

	let mode = $state<"login" | "register">("login");
	let username = $state("");
	let password = $state("");
	let busy = $state(false);
	let error = $state("");
	let showAuth = $state(false); // 默认收起, 点「登录」展开
	const isAuthed = $derived(auth.user !== null && auth.token !== "");

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
			// 换用户 → 通知流重连 (按新 token 的用户隔离)
			notifyStore.disconnect();
			notifyStore.connect();
		} catch (e) {
			error = e instanceof Error ? e.message : "操作失败";
		} finally {
			busy = false;
		}
	}

	function signOut() {
		logout();
		// 登出 → 回落匿名通知流
		notifyStore.disconnect();
		notifyStore.connect();
	}
</script>

{#if isAuthed}
	<div class="auth-badge">
		<User size={13} />
		<span class="uname">{auth.user?.username}</span>
		<button class="ghost" title="退出登录" onclick={signOut}><LogOut size={13} /></button>
	</div>
{:else}
	<button class="ghost login-btn" onclick={() => (showAuth = !showAuth)}>
		<User size={13} /> 登录
	</button>
	{#if showAuth}
		<div class="auth-card">
			<div class="tabs">
				<button class:on={mode === "login"} onclick={() => (mode = "login")}>登录</button>
				<button class:on={mode === "register"} onclick={() => (mode = "register")}>注册</button>
			</div>
			<input
				placeholder="用户名 (3-32 位)"
				bind:value={username}
				onkeydown={(e) => e.key === "Enter" && submit()}
			/>
			<input
				placeholder="密码 (≥6 位)"
				type="password"
				bind:value={password}
				onkeydown={(e) => e.key === "Enter" && submit()}
			/>
			{#if error}<div class="err">{error}</div>{/if}
			<button class="submit" disabled={busy} onclick={submit}>
				{#if busy}<Loader2 size={13} class="spin" />{/if}
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
		padding: 3px 10px;
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
		padding: 5px 0;
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
		padding: 7px 10px;
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
		padding: 7px 0;
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
	.spin {
		animation: spin 1s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
</style>
