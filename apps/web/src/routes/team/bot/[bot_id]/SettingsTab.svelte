<script lang="ts">
	/**
	 * Bot settings: create/edit this bot's identity and LLM binding.
	 *
	 * A PATCH with the same body the list endpoint already returned is how a bot
	 * gets renamed or re-pointed at another model; nothing here can start a turn.
	 */
	import { onMount } from "svelte";
	import { Trash2 } from "lucide-svelte";
	import { deleteBot, patchBot, type BotView } from "$lib/team/api";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";

	let { bot, onSaved }: { bot: BotView; onSaved: () => void } = $props();

	let name = $state("");
	let description = $state("");
	let instructions = $state("");
	let provider = $state("");
	let model = $state("");
	let busy = $state(false);
	let error = $state("");
	let confirmDelete = $state(false);

	$effect(() => {
		name = bot.name ?? "";
		description = bot.description ?? "";
		instructions = bot.instructions ?? "";
		provider = bot.provider ?? "";
		model = bot.model ?? "";
	});

	async function save(): Promise<void> {
		busy = true;
		error = "";
		try {
			// Only the fields that actually changed: the server rejects an empty
			// PATCH, and an unchanged prompt is not worth a write.
			const patch: Record<string, string> = {};
			if (name !== bot.name) patch.name = name;
			if (description !== bot.description) patch.description = description;
			if (instructions !== bot.instructions) patch.instructions = instructions;
			if (provider !== (bot.provider ?? "")) patch.provider = provider;
			if (model !== (bot.model ?? "")) patch.model = model;
			if (Object.keys(patch).length === 0) {
				error = "没有改动";
			} else {
				await patchBot(bot.bot_id, patch);
				onSaved();
			}
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function remove(): Promise<void> {
		busy = true;
		error = "";
		try {
			await deleteBot(bot.bot_id);
			location.assign("/team");
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
			busy = false;
		}
	}
</script>

<div class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
	<form
		class="{cardClass} space-y-2"
		onsubmit={(event) => {
			event.preventDefault();
			void save();
		}}
	>
		<label class="block text-xs opacity-60" for="bot-name">名字</label>
		<input id="bot-name" class={inputClass} bind:value={name} autocomplete="off" />
		<label class="block text-xs opacity-60" for="bot-description">简介</label>
		<input id="bot-description" class={inputClass} bind:value={description} autocomplete="off" />
		<label class="block text-xs opacity-60" for="bot-instructions">指令（system prompt）</label>
		<textarea id="bot-instructions" class="{inputClass} min-h-40 resize-y py-2" bind:value={instructions}></textarea>
		<div class="grid grid-cols-2 gap-2">
			<div>
				<label class="block text-xs opacity-60" for="bot-provider">provider</label>
				<input id="bot-provider" class={inputClass} bind:value={provider} placeholder="默认" autocomplete="off" />
			</div>
			<div>
				<label class="block text-xs opacity-60" for="bot-model">model</label>
				<input id="bot-model" class={inputClass} bind:value={model} placeholder="默认" autocomplete="off" />
			</div>
		</div>
		{#if error}<p class="text-xs text-red-300">{error}</p>{/if}
		<button class={primaryClass} type="submit" disabled={busy}>保存</button>
	</form>

	<div class="{cardClass} !p-3">
		<p class="mb-2 text-xs opacity-60">删除这个机器人（同时清空它的对话）</p>
		{#if confirmDelete}
			<button class="{actionClass} !border-red-500/40 !text-red-300" onclick={() => void remove()} disabled={busy}>
				确认删除
			</button>
		{:else}
			<button class={actionClass} onclick={() => (confirmDelete = true)}>
				<Trash2 size={14} /> 删除
			</button>
		{/if}
	</div>
</div>
