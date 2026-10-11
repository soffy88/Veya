<script lang="ts">
	/**
	 * Bot settings: create/edit this bot's identity, LLM binding, and model source.
	 *
	 * A PATCH with the same body the list endpoint already returned is how a bot
	 * gets renamed or re-pointed at another model; nothing here can start a turn.
	 */
	import { onMount } from "svelte";
	import { Trash2, Database, Key, Terminal } from "lucide-svelte";
	import { deleteBot, patchBot, type BotView } from "$lib/team/api";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";

	let { bot, onSaved }: { bot: BotView; onSaved: () => void } = $props();

	let name = $state("");
	let description = $state("");
	let instructions = $state("");
	let provider = $state("");
	let model = $state("");
	let modelSource = $state<"server" | "user_key" | "cli">("server");
	let modelSourceConfig = $state<Record<string, unknown>>({});
	/** The CLI extra-args box is one line of text; the API wants a list. */
	let cliArgsText = $state("");
	let busy = $state(false);
	let error = $state("");
	let confirmDelete = $state(false);

	const MODEL_SOURCE_OPTIONS = [
		{ value: "server", label: "服务器默认", icon: Database, desc: "使用服务器配置的默认模型" },
		{ value: "user_key", label: "个人 API Key", icon: Key, desc: "使用你存储的个人 API Key" },
		{ value: "cli", label: "CLI 代理", icon: Terminal, desc: "委托给已安装的 CLI 代理 (codex/claude/kimi/gemini)" },
	] as const;

	function cliArgsToList(text: string): string[] {
		return text.split(/\s+/).filter(part => part.length > 0);
	}

	function cliArgsToText(value: unknown): string {
		return Array.isArray(value) ? value.join(" ") : "";
	}

	$effect(() => {
		name = bot.name ?? "";
		description = bot.description ?? "";
		instructions = bot.instructions ?? "";
		provider = bot.provider ?? "";
		model = bot.model ?? "";
		modelSource = (bot.model_source as "server" | "user_key" | "cli") ?? "server";
		modelSourceConfig = (bot.model_source_config as Record<string, unknown>) ?? {};
		cliArgsText = cliArgsToText(modelSourceConfig.cli_args);
	});

	function getModelSourceOption(source: string) {
		return MODEL_SOURCE_OPTIONS.find(o => o.value === source) ?? MODEL_SOURCE_OPTIONS[0];
	}

	async function save(): Promise<void> {
		busy = true;
		error = "";
		try {
			const patch: Record<string, unknown> = {};
			if (name !== bot.name) patch.name = name;
			if (description !== bot.description) patch.description = description;
			if (instructions !== bot.instructions) patch.instructions = instructions;
			if (provider !== (bot.provider ?? "")) patch.provider = provider;
			if (model !== (bot.model ?? "")) patch.model = model;
			if (modelSource !== bot.model_source) patch.model_source = modelSource;
			// cli_args is a list on the wire; the text box is only its display form.
			const cliArgs = cliArgsToList(cliArgsText);
			if (JSON.stringify(cliArgs) !== JSON.stringify(bot.model_source_config?.cli_args ?? [])) {
				patch.model_source_config = { ...modelSourceConfig, cli_args: cliArgs };
			}
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

		<!-- Model Source Picker (NANO-W8) -->
		<fieldset class="border-t pt-3 space-y-3">
			<legend class="text-sm font-medium">模型来源</legend>
			<div class="grid grid-cols-3 gap-2">
				{#each MODEL_SOURCE_OPTIONS as option}
					<label class="relative flex flex-col items-center p-3 rounded-lg border transition
						{modelSource === option.value
							? 'border-sky-500/60 bg-sky-500/10'
							: 'border-white/10 bg-white/[0.03] hover:border-white/20'}"
					>
						<input
							type="radio"
							name="model-source"
							value={option.value}
							bind:group={modelSource}
							class="sr-only"
						/>
						<svelte:component this={option.icon} size={20} class="mb-1 opacity-70" />
						<span class="text-sm font-medium">{option.label}</span>
						<p class="text-xs opacity-50 text-center mt-1">{option.desc}</p>
					</label>
				{/each}
			</div>

			{#if modelSource === "user_key"}
				<div class="space-y-2 p-3 rounded bg-white/[0.03] border">
					<p class="text-xs opacity-60">选择已存储的 API Key 对应的 provider 和 model</p>
					<div class="grid grid-cols-2 gap-2">
						<div>
							<label class="block text-xs opacity-60" for="model-source-provider">Provider</label>
							<select id="model-source-provider" class={inputClass} bind:value={modelSourceConfig.provider}>
								<option value="">-- 选择 provider --</option>
								<option value="openai">OpenAI</option>
								<option value="anthropic">Anthropic</option>
								<option value="gemini">Google Gemini</option>
								<option value="deepseek">DeepSeek</option>
								<option value="openrouter">OpenRouter</option>
								<option value="bailian">DashScope/Bailian</option>
								<option value="kimi">Moonshot Kimi</option>
								<option value="zhipu">Zhipu AI</option>
							</select>
						</div>
						<div>
							<label class="block text-xs opacity-60" for="model-source-model">Model</label>
							<input id="model-source-model" class={inputClass} bind:value={modelSourceConfig.model} placeholder="模型名称" />
						</div>
					</div>
					<p class="text-xs opacity-50">API Key 需在设置页的「提供商配置」中预先存储</p>
				</div>
			{:else if modelSource === "cli"}
				<div class="space-y-2 p-3 rounded bg-white/[0.03] border">
					<p class="text-xs opacity-60">选择 CLI 代理 (需已安装并在 PATH 中)</p>
					<div class="grid grid-cols-2 gap-2">
						<div>
							<label class="block text-xs opacity-60" for="model-source-cli-agent">CLI 代理</label>
							<select id="model-source-cli-agent" class={inputClass} bind:value={modelSourceConfig.cli_agent}>
								<option value="">-- 选择 CLI --</option>
								<option value="codex">Codex</option>
								<option value="claude">Claude Code</option>
								<option value="gemini">Gemini CLI</option>
								<option value="kimi">Kimi CLI</option>
							</select>
						</div>
						<div>
							<label class="block text-xs opacity-60" for="model-source-cli-args">额外参数</label>
							<input id="model-source-cli-args" class={inputClass} bind:value={cliArgsText} placeholder="--flag value" />
						</div>
					</div>
				</div>
			{:else}
				<p class="text-xs opacity-50">使用服务器全局默认模型配置</p>
			{/if}
		</fieldset>

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