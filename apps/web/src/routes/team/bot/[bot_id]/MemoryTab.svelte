<script lang="ts">
	/**
	 * Per-bot memory editor.
	 *
	 * Memory is a sibling branch: this panel probes the endpoint once and, if the
	 * deployment does not have it, says so instead of rendering a dead form.
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

	async function open(doc: MemoryDoc): Promise<void> {
		selected = doc.name;
		content = doc.content ?? "";
		if (!content) {
			// A name-only listing still needs the body to be editable.
			try {
				content = (await getMemory(botId, doc.name)).content ?? "";
			} catch {
				/* keep whatever the listing gave us */
			}
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

	async function remove(name: string): Promise<void> {
		busy = true;
		error = "";
		try {
			await deleteMemory(botId, name);
			if (selected === name) startNew();
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
			<ul class="space-y-2">
				{#each docs as doc (doc.name)}
					<li class="{cardClass} flex items-center gap-2 !p-3">
						<button class="min-w-0 flex-1 truncate text-left text-sm" onclick={() => void open(doc)}>
							{doc.name}
						</button>
						<button class={actionClass} onclick={() => void remove(doc.name)} title="删除">
							<Trash2 size={14} />
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	{/if}
</div>
