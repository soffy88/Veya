<script lang="ts">
	/**
	 * /inbox — one mobile place for everything waiting on the owner.
	 * Questions (attention) with inline answers, approvals (Needs You),
	 * per-bot status and rooms. Answers are delivered as user messages,
	 * so the bot sees them on its next turn.
	 */
	import { onMount } from "svelte";
	import { ArrowLeft, Inbox } from "lucide-svelte";
	import {
		answerAttention,
		dismissAttention,
		getInbox,
		type AttentionItem,
		type InboxData,
	} from "$lib/team/api";
	import NeedsYouWidget from "../team/NeedsYouWidget.svelte";

	let inbox = $state<InboxData | null>(null);
	let loading = $state(true);
	let error = $state("");
	let answers = $state<Record<string, string>>({});
	let busyId = $state("");

	const statusLabel: Record<string, string> = {
		waiting: "等你",
		working: "干活中",
		idle: "空闲",
	};

	function statusDot(status: string): string {
		if (status === "waiting") return "bg-amber-500";
		if (status === "working") return "bg-emerald-500";
		return "bg-zinc-400";
	}

	function timeAgo(ts: number | null): string {
		if (!ts) return "";
		const diff = Math.max(0, Date.now() / 1000 - ts);
		if (diff < 60) return "刚刚";
		if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
		if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
		return `${Math.floor(diff / 86400)} 天前`;
	}

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			inbox = await getInbox();
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
			inbox = null;
		} finally {
			loading = false;
		}
	}

	async function answer(item: AttentionItem): Promise<void> {
		const text = (answers[item.id] ?? "").trim();
		if (!text || busyId) return;
		busyId = item.id;
		error = "";
		try {
			await answerAttention(item.id, text);
			answers[item.id] = "";
			await load();
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			busyId = "";
		}
	}

	async function dismiss(item: AttentionItem): Promise<void> {
		if (busyId) return;
		busyId = item.id;
		error = "";
		try {
			await dismissAttention(item.id);
			await load();
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			busyId = "";
		}
	}

	onMount(() => {
		void load();
	});
</script>

<div class="mx-auto max-w-2xl px-4 py-4">
	<div class="mb-4 flex items-center gap-2">
		<a href="/" class="flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-800">
			<ArrowLeft class="size-4" /> 返回主页
		</a>
		<h1 class="flex items-center gap-2 text-lg font-semibold">
			<Inbox class="size-5" /> 收件箱
		</h1>
	</div>

	{#if loading}
		<p class="text-sm text-zinc-500">加载中…</p>
	{:else if error}
		<p class="text-sm text-red-600">{error}</p>
		<p class="mt-2 text-sm text-zinc-500">请先登录后再打开收件箱。</p>
	{:else if inbox}
		<section class="mb-6">
			<h2 class="mb-2 font-semibold">等你回答（{inbox.attention.length}）</h2>
			{#if inbox.attention.length === 0}
				<p class="text-sm text-zinc-500">没有等你回答的问题。</p>
			{:else}
				{#each inbox.attention as item (item.id)}
					<div class="mb-3 rounded-xl border p-3">
						<p class="text-sm font-medium">{item.question}</p>
						<p class="mt-1 text-xs text-zinc-500">
							来自 {item.bot_id ?? `房间 ${item.group_id ?? ""}`}
							{#if item.context} · {item.context}{/if}
						</p>
						<textarea
							class="mt-2 w-full rounded-lg border px-2 py-1.5 text-sm"
							rows="2"
							placeholder="写你的回答…"
							bind:value={answers[item.id]}
						></textarea>
						<div class="mt-2 flex gap-2">
							<button
								class="rounded-lg bg-zinc-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
								disabled={busyId === item.id || !(answers[item.id] ?? "").trim()}
								onclick={() => void answer(item)}
							>
								回答
							</button>
							<button
								class="rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
								disabled={busyId === item.id}
								onclick={() => void dismiss(item)}
							>
								忽略
							</button>
						</div>
					</div>
				{/each}
			{/if}
		</section>

		<NeedsYouWidget />

		<section class="mb-6">
			<h2 class="mb-2 font-semibold">Bot 状态</h2>
			{#if inbox.board.length === 0}
				<p class="text-sm text-zinc-500">还没有 bot。</p>
			{:else}
				{#each inbox.board as row (row.bot_id)}
					<div class="flex items-center gap-2 border-b py-2 text-sm">
						<span class={`size-2.5 shrink-0 rounded-full ${statusDot(row.status)}`}></span>
						<span class="flex-1 truncate">{row.name}</span>
						<span class="text-zinc-500">{statusLabel[row.status] ?? row.status}</span>
						{#if row.open_attention > 0}
							<span class="text-amber-600">{row.open_attention} 个问题</span>
						{/if}
						<span class="text-xs text-zinc-400">{timeAgo(row.last_active_ts)}</span>
					</div>
				{/each}
			{/if}
		</section>

		<section>
			<h2 class="mb-2 font-semibold">房间</h2>
			{#if inbox.groups.length === 0}
				<p class="text-sm text-zinc-500">还没有房间。</p>
			{:else}
				{#each inbox.groups as group (group.group_id)}
					<a
						href={`/team/group/${encodeURIComponent(group.group_id)}`}
						class="flex items-center gap-2 border-b py-2 text-sm hover:underline"
					>
						<span class={`size-2.5 shrink-0 rounded-full ${statusDot(group.status)}`}></span>
						<span class="flex-1 truncate">{group.name}</span>
						<span class="text-zinc-500">{group.member_count} 人 · {group.reply_mode}</span>
						<span class="text-xs text-zinc-400">{timeAgo(group.last_active_ts)}</span>
					</a>
				{/each}
			{/if}
		</section>
	{/if}
</div>
