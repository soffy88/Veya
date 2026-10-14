<script lang="ts">
	/**
	 * Per-bot memory: what the bot remembers about you (W11 W-4 + OpenMinis borrow).
	 *
	 * 1. User Global background & preferences (read-only to agents, user editable).
	 * 2. Visual memory entries list with per-item retract action (remove fact & scrub logs).
	 * 3. Raw Markdown file store management (MEMORY.md and topic files).
	 */
	import { onMount } from "svelte";
	import { Check, Plus, Save, ShieldCheck, Trash2 } from "lucide-svelte";
	import {
		deleteMemory,
		getMemory,
		getUserGlobalMemory,
		listMemory,
		listMemoryEntries,
		putMemory,
		putUserGlobalMemory,
		retractMemoryEntry,
		type MemoryDoc,
		type MemoryEntryItem,
	} from "$lib/team/api";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";

	let { botId }: { botId: string } = $props();

	// User Global state
	let userGlobal = $state("");
	let userGlobalSaving = $state(false);
	let userGlobalSaved = $state(false);
	let userGlobalError = $state("");

	// Memory entries state
	let entries = $state<MemoryEntryItem[]>([]);
	let entriesLoading = $state(true);
	let retractingId = $state<string | null>(null);
	let confirmRetractId = $state<string | null>(null);
	let retractError = $state("");

	// Raw files state
	let docs = $state<MemoryDoc[]>([]);
	let selected = $state("");
	let content = $state("");
	let supported = $state(true);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state("");
	let confirmForget = $state("");

	async function openDoc(doc: MemoryDoc): Promise<void> {
		selected = doc.name;
		error = "";
		if (doc.content) {
			content = doc.content;
			return;
		}
		try {
			content = (await getMemory(botId, doc.name)).content ?? "";
		} catch {
			content = "";
		}
	}

	async function loadAll(): Promise<void> {
		loading = true;
		entriesLoading = true;
		error = "";
		try {
			const [fileDocs, entryItems, ugData] = await Promise.all([
				listMemory(botId).catch(() => [] as MemoryDoc[]),
				listMemoryEntries(botId).catch(() => [] as MemoryEntryItem[]),
				getUserGlobalMemory(botId).catch(() => ({ bot_id: botId, content: "" })),
			]);
			docs = fileDocs;
			entries = entryItems;
			userGlobal = ugData.content || "";
			supported = true;
		} catch {
			supported = false;
		} finally {
			loading = false;
			entriesLoading = false;
		}
	}

	async function saveUserGlobal(): Promise<void> {
		userGlobalSaving = true;
		userGlobalError = "";
		userGlobalSaved = false;
		try {
			await putUserGlobalMemory(botId, userGlobal);
			userGlobalSaved = true;
			setTimeout(() => {
				userGlobalSaved = false;
			}, 3000);
		} catch (err) {
			userGlobalError = err instanceof Error ? err.message : String(err);
		} finally {
			userGlobalSaving = false;
		}
	}

	async function retract(entry: MemoryEntryItem): Promise<void> {
		retractingId = entry.id;
		retractError = "";
		try {
			const needle = entry.raw || entry.text;
			await retractMemoryEntry(botId, needle);
			confirmRetractId = null;
			entries = await listMemoryEntries(botId);
			docs = await listMemory(botId);
		} catch (err) {
			retractError = err instanceof Error ? err.message : String(err);
		} finally {
			retractingId = null;
		}
	}

	function startNewDoc(): void {
		selected = "";
		content = "";
		confirmForget = "";
	}

	async function saveDoc(): Promise<void> {
		const name = selected.trim();
		if (!name || busy) return;
		busy = true;
		error = "";
		try {
			await putMemory(botId, name, content);
			await loadAll();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function forgetDoc(name: string): Promise<void> {
		busy = true;
		error = "";
		try {
			await deleteMemory(botId, name);
			if (selected === name) startNewDoc();
			confirmForget = "";
			await loadAll();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	onMount(loadAll);
</script>

<div class="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
	{#if loading && !docs.length && !entries.length}
		<p class="text-sm opacity-60">加载记忆系统…</p>
	{:else if !supported}
		<p class="{cardClass} text-sm opacity-70">
			这个部署还没有 memory 功能（端点未上线）。上线后这里就能编辑这个 bot 的长期记忆。
		</p>
	{:else}
		<!-- 1. 全局背景与偏好 (User Global) -->
		<div class="{cardClass} space-y-2.5 border border-sky-500/20 bg-sky-950/15 p-4 shadow-sm">
			<div class="flex items-center justify-between">
				<div class="flex items-center gap-2">
					<ShieldCheck class="size-4 text-sky-400" />
					<h3 class="text-sm font-semibold text-terminal-fg">全局背景与偏好 (User Global)</h3>
				</div>
				<button
					class="{primaryClass} text-xs px-2.5 py-1 inline-flex items-center gap-1.5"
					onclick={() => void saveUserGlobal()}
					disabled={userGlobalSaving}
				>
					{#if userGlobalSaved}
						<Check class="size-3.5 text-emerald-400" /> 已保存
					{:else}
						<Save class="size-3.5" /> 保存全局背景
					{/if}
				</button>
			</div>
			<p class="text-xs text-terminal-dim leading-relaxed">
				只读注入给 Agent，Agent 无法自主修改。在此设定您的个人背景、全局偏好与行为要求。
			</p>
			<textarea
				class="{inputClass} min-h-24 resize-y py-2 font-mono text-xs"
				placeholder="例如：我主要使用 Python 和 TypeScript 进行开发；回答时尽量简明直接；不要包含多余寒暄。"
				bind:value={userGlobal}
			></textarea>
			{#if userGlobalError}<p class="text-xs text-red-400">{userGlobalError}</p>{/if}
		</div>

		<!-- 2. 逐条记忆列表 (Memory Entries) -->
		<div class="space-y-2">
			<div class="flex items-center justify-between">
				<h3 class="text-xs font-semibold uppercase tracking-wider text-terminal-dim">
					已记住的事实与偏好 ({entries.length})
				</h3>
				{#if retractError}<span class="text-xs text-red-400">{retractError}</span>{/if}
			</div>

			{#if entriesLoading}
				<p class="text-xs opacity-60">加载记忆条目…</p>
			{:else if entries.length === 0}
				<div class="{cardClass} text-xs text-terminal-dim !p-3">
					暂无已学习的记忆条目。在对话中向 bot 说「记住…」，或通过工具记录后将在此处逐条展现。
				</div>
			{:else}
				<ul class="space-y-1.5">
					{#each entries as entry (entry.id)}
						<li class="{cardClass} flex items-start justify-between gap-3 !p-3 hover:border-white/20 transition">
							<div class="min-w-0 flex-1 space-y-1">
								<div class="flex items-center gap-2 text-[10px]">
									{#if entry.scope === "core"}
										<span class="rounded bg-sky-500/20 px-1.5 py-0.5 font-medium text-sky-300">核心</span>
									{:else}
										<span class="rounded bg-amber-500/20 px-1.5 py-0.5 font-medium text-amber-300">临时</span>
									{/if}
									{#if entry.added}
										<span class="text-terminal-dim">{entry.added}</span>
									{/if}
									{#if entry.source}
										<span class="text-terminal-dim truncate max-w-[140px]">来源: {entry.source}</span>
									{/if}
								</div>
								<p class="text-xs text-terminal-fg font-sans leading-relaxed break-words">{entry.text}</p>
							</div>

							<div class="shrink-0 flex items-center pt-0.5">
								{#if confirmRetractId === entry.id}
									<button
										class="{actionClass} !border-red-500/40 !bg-red-950/40 !text-red-300 text-xs px-2 py-1"
										disabled={retractingId === entry.id}
										onclick={() => void retract(entry)}
									>
										{retractingId === entry.id ? "撤回中…" : "确认撤回"}
									</button>
								{:else}
									<button
										class="{actionClass} text-xs px-2 py-1 text-terminal-dim hover:text-red-300 hover:border-red-500/30"
										title="从记忆中撤回并遗忘这条事实"
										onclick={() => (confirmRetractId = entry.id)}
									>
										<Trash2 size={13} class="mr-1 inline" /> 撤回
									</button>
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</div>

		<!-- 3. 底层 Markdown 文件管理 (Raw Files) -->
		<details class="group mt-4 border-t border-white/10 pt-3">
			<summary class="cursor-pointer text-xs text-terminal-dim hover:text-terminal-fg font-medium select-none mb-3">
				▸ 底层 Markdown 存储管理 ({docs.length} 个文件)
			</summary>

			<div class="space-y-3">
				<div class="flex gap-2">
					<button class={actionClass} onclick={startNewDoc}><Plus size={16} /> 新记忆文件</button>
					<button class={primaryClass} onclick={() => void saveDoc()} disabled={busy || !selected.trim()}>
						<Save size={16} /> 保存文件
					</button>
				</div>

				<input class={inputClass} placeholder="文件名（如 preferences.md）" bind:value={selected} autocomplete="off" />
				<textarea
					class="{inputClass} min-h-48 resize-y py-2 font-mono text-sm"
					placeholder="Markdown 内容"
					bind:value={content}
				></textarea>

				{#if error}<p class="text-xs text-red-300">{error}</p>{/if}

				{#if docs.length > 0}
					<ul class="space-y-2">
						{#each docs as doc (doc.name)}
							<li class="{cardClass} flex items-center gap-2 !p-3">
								<button class="min-w-0 flex-1 truncate text-left text-sm" onclick={() => void openDoc(doc)}>
									{doc.name}
								</button>
								{#if confirmForget === doc.name}
									<button
										class="{actionClass} !border-red-500/40 !text-red-300"
										onclick={() => void forgetDoc(doc.name)}
									>
										确认遗忘
									</button>
								{:else}
									<button
										class={actionClass}
										onclick={() => (confirmForget = doc.name)}
										title="让 bot 忘掉这整个文件"
									>
										<Trash2 size={14} />
									</button>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</details>
	{/if}
</div>
