<script lang="ts">
	/**
	 * The "新建 Bot" sheet (W11 W-1).
	 *
	 * A first-time user should be able to create a bot in under a minute from a
	 * phone: a name, one line about the job, a starting preset, and a model.
	 * The bot id is derived from the name here (snake_case, unique against the
	 * bots that already exist) because the id is a directory name on the server
	 * and nobody should have to learn that alphabet to start.
	 *
	 * The model list comes from the read-only catalog; when it cannot be read
	 * (logged out, or an old deployment) the picker degrades to a plain model
	 * field instead of blocking the create.
	 */
	import { onMount } from "svelte";
	import { X } from "lucide-svelte";
	import { createBot, listModels, type ModelOption } from "$lib/team/api";
	import { BOT_PRESETS, DEFAULT_PRESET_ID, presetById } from "$lib/team/presets";
	import { suggestBotId } from "$lib/team/bot_id";
	import { actionClass, inputClass, primaryClass } from "$lib/team/ui";

	let {
		open,
		takenIds = [],
		onclose,
		oncreated,
	}: {
		open: boolean;
		takenIds?: string[];
		onclose: () => void;
		oncreated: (botId: string) => void;
	} = $props();

	let name = $state("");
	let job = $state("");
	let instructions = $state("");
	let presetId = $state(DEFAULT_PRESET_ID);
	let models = $state<ModelOption[]>([]);
	let modelChoice = $state("");
	let defaultModel = $state("");
	let modelsLoaded = $state(false);
	let busy = $state(false);
	let error = $state("");

	const botId = $derived(suggestBotId(name, takenIds));
	const nameError = $derived(name.trim() ? "" : "给它起个名字");

	function applyPreset(id: string): void {
		presetId = id;
		const preset = presetById(id);
		// Only fill what the user has not personalised: overwriting a hand-written
		// instruction block on every chip tap is how a preset stops being a hint.
		if (!instructions.trim()) instructions = preset.instructions;
		if (!job.trim()) job = preset.description;
	}

	async function loadModels(): Promise<void> {
		try {
			const catalog = await listModels();
			models = catalog.models ?? [];
			defaultModel = catalog.default_model ?? "";
			modelChoice = defaultModel;
			modelsLoaded = true;
		} catch {
			models = [];
			modelsLoaded = false;
		}
	}

	async function submit(): Promise<void> {
		if (busy || nameError) return;
		busy = true;
		error = "";
		try {
			const body: Record<string, unknown> = {
				bot_id: botId,
				name: name.trim(),
				description: job.trim(),
				instructions: instructions.trim(),
			};
			// An untouched picker means "the server default", which is the very
			// model it was preselected to — so send nothing and let the server
			// resolve it, rather than pinning a provider the user never chose.
			if (modelChoice && modelChoice !== defaultModel) {
				const picked = models.find((option) => option.id === modelChoice);
				body.provider = picked?.provider ?? "";
				body.model = picked?.id ?? modelChoice;
			}
			const bot = await createBot(body);
			oncreated(bot.bot_id);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	function close(): void {
		if (busy) return;
		name = "";
		job = "";
		instructions = "";
		presetId = DEFAULT_PRESET_ID;
		modelChoice = defaultModel;
		error = "";
		onclose();
	}

	onMount(() => {
		void loadModels();
	});
</script>

{#if open}
	<div class="fixed inset-0 z-50 flex flex-col justify-end">
		<button
			class="absolute inset-0 bg-black/60"
			aria-label="关闭"
			onclick={close}
		></button>
		<div
			class="relative max-h-[92dvh] overflow-y-auto rounded-t-2xl border-t border-white/10 bg-terminal-panel pb-[max(1rem,env(safe-area-inset-bottom))]"
			role="dialog"
			aria-modal="true"
			aria-label="新建 Bot"
		>
			<div class="sticky top-0 flex items-center gap-2 border-b border-white/[0.07] bg-terminal-panel/95 px-4 py-3">
				<h2 class="flex-1 text-base font-semibold">新建 Bot</h2>
				<button class={actionClass} onclick={close} aria-label="取消"><X size={16} /></button>
			</div>

			<form
				class="space-y-4 p-4"
				onsubmit={(event) => {
					event.preventDefault();
					void submit();
				}}
			>
				<div>
					<label class="mb-1 block text-xs opacity-60" for="new-bot-name">名字</label>
					<input
						id="new-bot-name"
						class={inputClass}
						placeholder="例如：资料搜集员"
						bind:value={name}
						autocomplete="off"
					/>
					{#if nameError}
						<p class="mt-1 text-xs text-amber-300">{nameError}</p>
					{/if}
				</div>

				<div>
					<label class="mb-1 block text-xs opacity-60" for="new-bot-job">一句话职责</label>
					<input
						id="new-bot-job"
						class={inputClass}
						placeholder="它负责做什么？"
						bind:value={job}
						autocomplete="off"
					/>
				</div>

				<div>
					<p class="mb-1.5 text-xs opacity-60">从哪种起点开始（之后都能改）</p>
					<div class="grid grid-cols-2 gap-2">
						{#each BOT_PRESETS as preset (preset.id)}
							<button
								type="button"
								class="min-h-11 rounded-lg border px-3 py-2 text-left text-sm transition {presetId ===
								preset.id
									? 'border-sky-500/60 bg-sky-500/10 text-sky-200'
									: 'border-white/10 bg-white/[0.03]'}"
								onclick={() => applyPreset(preset.id)}
								aria-pressed={presetId === preset.id}
							>
								{preset.label}
								<span class="block text-[11px] opacity-50">{preset.hint}</span>
							</button>
						{/each}
					</div>
				</div>

				<div>
					<label class="mb-1 block text-xs opacity-60" for="new-bot-instructions">指令（它怎么工作）</label>
					<textarea
						id="new-bot-instructions"
						class="{inputClass} min-h-32 resize-y py-2"
						bind:value={instructions}
					></textarea>
				</div>

				<div>
					<label class="mb-1 block text-xs opacity-60" for="new-bot-model">模型</label>
					{#if models.length > 0}
						<select id="new-bot-model" class={inputClass} bind:value={modelChoice}>
							{#each models as option (option.provider + option.id)}
								<option value={option.id}>
									{option.provider_label} · {option.id}{option.default ? "（默认）" : ""}
								</option>
							{/each}
						</select>
					{:else}
						<input
							id="new-bot-model"
							class={inputClass}
							placeholder={modelsLoaded ? "模型名（留空用服务器默认）" : "加载模型列表…"}
							bind:value={modelChoice}
							autocomplete="off"
						/>
						{#if !modelsLoaded}
							<p class="mt-1 text-[11px] opacity-50">正在读取可用模型…</p>
						{/if}
					{/if}
				</div>

				<p class="text-[11px] opacity-50">
					bot_id：<span class="font-mono">{botId}</span>（按名字自动生成，不可改）
				</p>

				{#if error}
					<p class="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</p>
				{/if}

				<button class="{primaryClass} w-full" type="submit" disabled={busy || !!nameError}>
					{busy ? "创建中…" : "创建并开始聊天"}
				</button>
			</form>
		</div>
	</div>
{/if}
