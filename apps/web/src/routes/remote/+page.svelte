<script lang="ts">
	/**
	 * /remote — cross-device agent control (W13). Mobile-first.
	 *
	 * Hosts, running remote agents, a start-task form (text + browser
	 * speech-to-text, progressive enhancement), approve/deny cards fed by the
	 * existing approvals store, and the done notifications that arrive through
	 * the app-wide notification stream (+layout.svelte → NotificationCenter).
	 */
	import { onMount } from "svelte";
	import { Mic, Play, Plus, RefreshCw, Trash2, X } from "lucide-svelte";
	import {
		RemoteDisabledError,
		applySession,
		cancelSession,
		createHost,
		deleteHost,
		listHosts,
		listPendingCards,
		listSessions,
		probeHost,
		startSession,
		type RemoteCardView,
		type RemoteHostView,
		type RemoteProbeView,
		type RemoteSessionView,
	} from "$lib/remote/api";
	import { resolveApproval } from "$lib/approvals/api";
	import { actionClass, cardClass, timeLabel } from "$lib/team/ui";
	import XrtExternalRuntimes from "$lib/components/XrtExternalRuntimes.svelte";

	let hosts = $state<RemoteHostView[]>([]);
	let sessions = $state<RemoteSessionView[]>([]);
	let cards = $state<RemoteCardView[]>([]);
	let loading = $state(true);
	let disabled = $state(false);
	let error = $state("");
	let busy = $state("");
	let probes = $state<Record<string, RemoteProbeView | { reachable: boolean; error?: string }>>({});
	let probingHostId = $state<string>("");

	async function probe(targetHostId: string): Promise<void> {
		probingHostId = targetHostId;
		try {
			const res = await probeHost(targetHostId);
			probes[targetHostId] = res;
		} catch (err) {
			probes[targetHostId] = {
				reachable: false,
				error: err instanceof Error ? err.message : String(err),
			};
		} finally {
			probingHostId = "";
		}
	}

	// start-task form
	let task = $state("");
	let hostId = $state("");
	let wake = $state("now");
	let listening = $state(false);
	let speechSupported = $state(false);
	let recognition: { start(): void; stop(): void } | null = null;

	// add-host form
	let showAddHost = $state(false);
	let newHostId = $state("");
	let newHostIp = $state("");
	let newHostLabel = $state("pi");

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			[hosts, sessions, cards] = await Promise.all([
				listHosts(),
				listSessions(false),
				listPendingCards(),
			]);
			disabled = false;
			if (!hostId && hosts.length) hostId = hosts[0].host_id;
		} catch (err) {
			disabled = err instanceof RemoteDisabledError;
			error = disabled ? "" : err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	async function guard(label: string, fn: () => Promise<void>): Promise<void> {
		busy = label;
		error = "";
		try {
			await fn();
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = "";
		}
	}

	async function submitTask(): Promise<void> {
		if (!task.trim() || !hostId) return;
		await guard("start", async () => {
			await startSession({ host_id: hostId, task: task.trim(), requested_wake: wake });
			task = "";
		});
	}

	function approve(card: RemoteCardView): void {
		void guard(card.approval_id, async () => {
			await resolveApproval(card.approval_id, "once");
			// A remote start only runs after its approval lands: apply it now.
			if (card.tool === "remote_agent_start" && card.session_id) {
				await applySession(card.session_id, card.approval_id);
			}
		});
	}

	function deny(card: RemoteCardView): void {
		void guard(card.approval_id, () => resolveApproval(card.approval_id, "deny"));
	}

	function toggleSpeech(): void {
		if (!recognition) return;
		if (listening) {
			recognition.stop();
			listening = false;
			return;
		}
		recognition.start();
		listening = true;
	}

	onMount(() => {
		void load();
		// Progressive enhancement: voice input only where the browser offers it.
		const Ctor =
			(window as unknown as { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown })
				.SpeechRecognition ??
			(window as unknown as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition;
		if (typeof Ctor === "function") {
			speechSupported = true;
			const rec = new (Ctor as new () => {
				lang: string;
				interimResults: boolean;
				onresult: (e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void;
				onend: () => void;
				start(): void;
				stop(): void;
			})();
			rec.lang = "zh-CN";
			rec.interimResults = false;
			rec.onresult = (event) => {
				let text = "";
				for (let i = 0; i < event.results.length; i += 1) {
					text += event.results[i][0].transcript;
				}
				task = `${task}${task ? " " : ""}${text}`.trim();
			};
			rec.onend = () => {
				listening = false;
			};
			recognition = rec;
		}
	});

	const activeSessions = $derived(sessions.filter((s) => !["done", "failed", "cancelled", "handed_off"].includes(s.state)));
</script>

<div class="mx-auto flex max-w-2xl flex-col gap-4 p-4">
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-3">
			<a href="/" class="text-xs text-terminal-dim hover:text-terminal-fg">← 返回控制台</a>
			<span class="opacity-20 text-xs">/</span>
			<h1 class="text-lg font-semibold text-terminal-fg">远程控制</h1>
		</div>
		<button type="button" class={actionClass} onclick={() => void load()} aria-label="刷新"><RefreshCw class="size-4" /></button>
	</div>

	{#if loading}
		<div class="text-sm text-white/50">加载中…</div>
	{:else if disabled}
		<div class={cardClass}><span class="text-sm text-white/70">远程控制未开启。设置 VEYA_REMOTE_CONTROL=1 后，这里可以驱动 tailnet 上的 agent。</span></div>
	{:else}
		{#if error}<div class="text-sm text-rose-300">{error}</div>{/if}

		<!-- start a task -->
		<section class={cardClass}>
			<h2 class="mb-2 text-sm font-medium text-white/70">启动任务</h2>
			{#if hosts.length === 0}
				<div class="text-sm text-white/50">先添加一台主机。</div>
			{:else}
				<div class="flex flex-col gap-2">
					<div class="flex items-center gap-2">
						<span class="rounded bg-sky-500/20 px-2 py-1 text-xs font-mono text-sky-300">
							@{hostId || "未指定"}
						</span>
						<select bind:value={hostId} class="min-h-11 flex-1 rounded-lg bg-white/[0.06] px-2 text-sm text-terminal-fg" aria-label="目标主机">
							{#each hosts as host (host.host_id)}
								<option value={host.host_id}>@{host.host_id} · {host.display_name} ({host.agent_label})</option>
							{/each}
						</select>
					</div>
					<div class="flex items-end gap-2">
						<textarea
							bind:value={task}
							rows="3"
							placeholder="要 agent 做什么？"
							class="min-h-11 flex-1 resize-y rounded-lg bg-white/[0.06] px-2 py-2 text-sm text-terminal-fg placeholder:text-white/30"
						></textarea>
						{#if speechSupported}
							<button
								type="button"
								onclick={toggleSpeech}
								class="flex min-h-11 min-w-11 items-center justify-center rounded-lg {listening ? 'bg-rose-500/80 text-white' : 'bg-white/[0.06] text-terminal-dim'}"
								aria-label={listening ? "停止语音输入" : "语音输入"}
							>
								<Mic class="size-4" />
							</button>
						{/if}
					</div>
					<div class="flex items-center gap-2">
						<select bind:value={wake} class="min-h-9 rounded-lg bg-white/[0.06] px-2 text-xs text-terminal-fg" aria-label="唤醒策略">
							<option value="now">立即</option>
							<option value="batch">批量</option>
							<option value="none">不唤醒</option>
						</select>
						<button type="button" class={actionClass} disabled={busy === "start" || !task.trim()} onclick={() => void submitTask()}>
							<Play class="size-4" />启动
						</button>
					</div>
				</div>
			{/if}
		</section>

		<!-- approve / deny -->
		{#if cards.length > 0}
			<section class="flex flex-col gap-2">
				<h2 class="text-sm font-medium text-white/70">待批准（{cards.length}）</h2>
				{#each cards as card (card.approval_id)}
					<div class="{cardClass} flex flex-col gap-2">
						<div class="min-w-0">
							<div class="text-sm text-terminal-fg">{card.tool}</div>
							<div class="truncate text-[11px] text-white/40">{card.host_id} · {card.reason}</div>
						</div>
						<div class="flex gap-2">
							<button type="button" class={actionClass} disabled={busy === card.approval_id} onclick={() => approve(card)}>批准</button>
							<button type="button" class={actionClass} disabled={busy === card.approval_id} onclick={() => deny(card)}>拒绝</button>
						</div>
					</div>
				{/each}
			</section>
		{/if}

		<!-- running agents -->
		<section class="flex flex-col gap-2">
			<h2 class="text-sm font-medium text-white/70">远程 Agent（{activeSessions.length}）</h2>
			{#each activeSessions as session (session.session_id)}
				<div class="{cardClass} flex items-center gap-2">
					<div class="min-w-0 flex-1">
						<div class="truncate text-sm text-terminal-fg">{session.task}</div>
						<div class="text-[11px] text-white/40">
							{session.host_id} · {session.agent_label} · {session.state}
							{#if session.wake_outcome} · {session.wake_outcome}{/if}
						</div>
					</div>
					<button type="button" class={actionClass} onclick={() => void guard(session.session_id, () => cancelSession(session.session_id))} aria-label="取消">
						<X class="size-4" />
					</button>
				</div>
			{:else}
				<div class="text-sm text-white/50">没有正在运行的远程 agent。</div>
			{/each}
		</section>

		<!-- hosts -->
		<section class="flex flex-col gap-2">
			<div class="flex items-center justify-between">
				<h2 class="text-sm font-medium text-white/70">主机（{hosts.length}）</h2>
				<button type="button" class={actionClass} onclick={() => (showAddHost = !showAddHost)} aria-label="添加主机"><Plus class="size-4" /></button>
			</div>
			{#if showAddHost}
				<div class="{cardClass} flex flex-col gap-2">
					<input bind:value={newHostId} placeholder="host id（小写，如 ubuntu26）" class="min-h-9 rounded-lg bg-white/[0.06] px-2 text-sm text-terminal-fg placeholder:text-white/30" />
					<input bind:value={newHostIp} placeholder="tailnet IP（100.x.y.z）" class="min-h-9 rounded-lg bg-white/[0.06] px-2 text-sm text-terminal-fg placeholder:text-white/30" />
					<select bind:value={newHostLabel} class="min-h-9 rounded-lg bg-white/[0.06] px-2 text-sm text-terminal-fg" aria-label="执行器">
						<option value="pi">pi</option>
						<option value="claude-wb">claude-wb</option>
						<option value="codex">codex</option>
					</select>
					<button
						type="button"
						class={actionClass}
						disabled={!newHostId || !newHostIp}
						onclick={() =>
							void guard("add-host", async () => {
								await createHost({ host_id: newHostId, tailnet_ip: newHostIp, agent_label: newHostLabel });
								newHostId = "";
								newHostIp = "";
								showAddHost = false;
							})}
					>
						保存
					</button>
				</div>
			{/if}
			{#each hosts as host (host.host_id)}
				<div class="{cardClass} flex flex-col gap-2">
					<div class="flex items-center justify-between">
						<div class="flex items-center gap-2 min-w-0">
							<span class="font-mono text-xs text-sky-300">@{host.host_id}</span>
							<span class="text-sm font-medium text-terminal-fg truncate">{host.display_name}</span>
							{#if probes[host.host_id]}
								{#if probes[host.host_id].reachable}
									<span class="rounded-full bg-emerald-500/20 px-2 py-0.5 text-[10px] text-emerald-300">在线</span>
								{:else}
									<span class="rounded-full bg-rose-500/20 px-2 py-0.5 text-[10px] text-rose-300" title={probes[host.host_id].error}>离线</span>
								{/if}
							{:else}
								<span class="rounded-full bg-white/10 px-2 py-0.5 text-[10px] text-white/50">未探测</span>
							{/if}
						</div>
						<div class="flex items-center gap-1">
							<button
								type="button"
								class="{actionClass} text-xs px-2 py-1"
								disabled={probingHostId === host.host_id}
								onclick={() => void probe(host.host_id)}
								aria-label="探测连通性"
							>
								{probingHostId === host.host_id ? "探测中…" : "探测"}
							</button>
							<button type="button" class={actionClass} onclick={() => void guard(host.host_id, () => deleteHost(host.host_id))} aria-label="删除"><Trash2 class="size-4" /></button>
						</div>
					</div>
					<div class="grid grid-cols-2 gap-x-2 gap-y-1 text-[11px] text-white/40">
						<div>IP: {host.tailnet_ip}</div>
						<div>OS: {host.os_family || "linux"}</div>
						<div>SSH: {host.ssh_user ? `${host.ssh_user}@` : ""}{host.tailnet_ip}:{host.ssh_port || 22}</div>
						<div>Write Root: {host.write_root || "~/.veya/work"}</div>
					</div>
					<div class="text-[11px] text-white/30">
						{host.last_seen_at ? `seen ${timeLabel(host.last_seen_at)}` : "never seen"}
						{#if !host.enabled} · 已停用{/if}
					</div>
				</div>
			{:else}
				<div class="text-sm text-white/50">还没有主机。</div>
			{/each}
		</section>
	{/if}
	<XrtExternalRuntimes />
</div>
