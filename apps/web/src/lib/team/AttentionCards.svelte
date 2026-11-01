<script lang="ts">
	/**
	 * Open attention questions for one bot or one room, rendered as cards
	 * inside the chat flow (OpenMausBot method, adapted): the owner answers
	 * without leaving the conversation, and the answer reaches the bot or
	 * the room on its next turn. Renders nothing when there is nothing open.
	 */
	import { onMount } from "svelte";
	import {
		answerAttention,
		dismissAttention,
		getInbox,
		type AttentionItem,
	} from "$lib/team/api";
	import { actionClass, inputClass, primaryClass } from "$lib/team/ui";

	let {
		botId = null,
		groupId = null,
	}: { botId?: string | null; groupId?: string | null } = $props();

	let items = $state<AttentionItem[]>([]);
	let drafts = $state<Record<string, string>>({});
	let busy = $state("");
	let error = $state("");

	async function load(): Promise<void> {
		try {
			const data = await getInbox();
			items = data.attention.filter(
				(item) =>
					item.status === "open" &&
					(groupId ? item.group_id === groupId : item.bot_id === botId),
			);
			error = "";
		} catch {
			error = "问题卡加载失败";
		}
	}

	onMount(load);

	async function answer(item: AttentionItem): Promise<void> {
		const text = (drafts[item.id] ?? "").trim();
		if (!text || busy) return;
		busy = item.id;
		try {
			await answerAttention(item.id, text);
			await load();
		} catch {
			error = "答复没发出去";
		} finally {
			busy = "";
		}
	}

	async function dismiss(item: AttentionItem): Promise<void> {
		if (busy) return;
		busy = item.id;
		try {
			await dismissAttention(item.id);
			await load();
		} catch {
			error = "忽略失败";
		} finally {
			busy = "";
		}
	}
</script>

{#if items.length > 0}
	<div class="space-y-2 px-4 pt-3">
		{#each items as item (item.id)}
			<div class="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
				<p class="mb-1 text-xs font-medium text-amber-800 dark:text-amber-200">等你回答</p>
				<p class="text-sm whitespace-pre-wrap">{item.question}</p>
				{#if item.context}
					<p class="mt-1 text-xs whitespace-pre-wrap opacity-60">{item.context}</p>
				{/if}
				<div class="mt-2">
					<textarea
						class={inputClass}
						rows="2"
						placeholder="在这里答复，对方下一轮会看到"
						bind:value={drafts[item.id]}
					></textarea>
				</div>
				<div class="mt-2 flex gap-2">
					<button
						class={primaryClass}
						disabled={busy === item.id || !(drafts[item.id] ?? "").trim()}
						onclick={() => void answer(item)}>答复</button
					>
					<button
						class={actionClass}
						disabled={busy === item.id}
						onclick={() => void dismiss(item)}>忽略</button
					>
				</div>
			</div>
		{/each}
	</div>
{/if}
{#if error}
	<p class="bg-red-500/10 px-4 py-2 text-xs text-red-300">{error}</p>
{/if}
