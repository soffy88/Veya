<script lang="ts">
	/**
	 * Per-bot memory: what the bot remembers about you (W11 W-4).
	 *
	 * The listing endpoint returns file *names*; each body is its own read, which
	 * is what keeps a long memory cheap to open. Deleting a file is "遗忘" — the
	 * only verb the backend has for forgetting, so the button says it plainly.
	 */
	import { onMount } from "svelte";
	import { Plus, Save, Trash2 } from "lucide-svelte";
	import {
		deleteMemory,
		getMemory,
		listMemory,
		putMemory,
		type MemoryDoc,
	} from "$lib/team/api";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";

	let { botId }: { botId: string } = $props();

	let docs = $state<MemoryDoc[]>([]);
	let selected = $state("");
	let content = $state("");
	let supported = $state(true);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state("");
	let confirmForget = $state("");

	async function open(doc: MemoryDoc): Promise<void> {
		selected = doc.name;
		error = "";
		if (doc.content) {
			content = doc.content;
			return;
		}
		// A name-only listing still needs the body to be editable.
		try {
			content = (await getMemory(botId, doc.name)).content ?? "";
		} catch {
			content = "";
		}
	}

	async function load(): Promise<void> {
		loading = true;
		try {
			docs = await listMemory(botId);
			supported = true;
			error = "";
		} catch {
			supported = false;
		} finally {
			loading = false;
		}
	}

	function startNew(): void {
		selected = "";
		content = "";
		confirmForget = "";
	}

	async function save(): Promise<void> {
		const name = selected.trim();
		if (!name || busy) return;
		busy = true;
		error = "";
		try {
			await putMemory(botId, name, content);
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function forget(name: string): Promise<void> {
		busy = true;
		error = "";
		try {
			await deleteMemory(botId, name);
			if (selected === name) startNew();
			confirmForget = "";
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	onMount(load);
</script>

<div class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
	{#if loading}
		<p class="text-sm opacity-60">加载记忆…</p>
	{:else if !supported}
		<p class="{cardClass} text-sm opacity-70">
			这个部署还没有 memory 功能（端点未上线）。上线后这里就能编辑这个 bot 的长期记忆。
		</p>
	{:else}
		<div class="flex gap-2">
			<button class={actionClass} onclick={startNew}><Plus size={16} /> 新记忆</button>
			<button class={primaryClass} onclick={() => void save()} disabled={busy || !selected.trim()}>
				<Save size={16} /> 保存
			</button>
		</div>

		<input class={inputClass} placeholder="文件名（如 preferences.md）" bind:value={selected} autocomplete="off" />
		<textarea
			class="{inputClass} min-h-56 resize-y py-2 font-mono text-sm"
			placeholder="Markdown 内容"
			bind:value={content}
		></textarea>

		{#if error}<p class="text-xs text-red-300">{error}</p>{/if}

		{#if docs.length > 0}
			<h3 class="text-xs opacity-60">已记住 {docs.length} 条</h3>
			<ul class="space-y-2">
				{#each docs as doc (doc.name)}
					<li class="{cardClass} flex items-center gap-2 !p-3">
						<button class="min-w-0 flex-1 truncate text-left text-sm" onclick={() => void open(doc)}>
							{doc.name}
						</button>
						{#if confirmForget === doc.name}
							<button
								class="{actionClass} !border-red-500/40 !text-red-300"
								onclick={() => void forget(doc.name)}
							>
								确认遗忘
							</button>
						{:else}
							<button
								class={actionClass}
								onclick={() => (confirmForget = doc.name)}
								title="让 bot 忘掉这一条"
							>
								<Trash2 size={14} />
							</button>
						{/if}
					</li>
				{/each}
			</ul>
		{:else}
			<p class="{cardClass} text-sm opacity-60">还没有记忆。在聊天里说「记住…」，或在这里新建一条。</p>
		{/if}
	{/if}
</div>
