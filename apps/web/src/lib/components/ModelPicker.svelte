<script lang="ts">
	/**
	 * ModelPicker — 聊天框右下角模型快捷选择器 (cindy 风格)。
	 *
	 * 顶部三个 provider 按钮 (Claude / Codex / Pi) → 下方模型列表点选即用。
	 * 选择写入 apiKeyStore (per-provider, localStorage 持久化),
	 * ChatConsole 请求时经 provider/model 字段透传给后端主脑。
	 * Pi 为本地推理 provider: 若已注册 custom provider 'pi' 则直达, 否则提示配置。
	 */
	import { ChevronDown, Cpu, KeyRound, Sparkles } from "lucide-svelte";
	import { MODEL_PRESETS, PICKER_QUICK, apiKeyStore, type ProviderDef } from "$lib/settings.svelte";

	let open = $state(false);

	const quickIds = $derived(PICKER_QUICK.map((q) => q.provider));

	function quickProviderId(qp: (typeof PICKER_QUICK)[number]): string {
		if (qp.provider === "pi") {
			// pi = 本地推理: 优先用户注册的 custom provider, 否则无
			const pi = apiKeyStore.all.find((p) => p.id === "pi" || p.id.includes("pi"));
			return pi?.id ?? "pi";
		}
		return qp.provider;
	}

	function quickLabel(qp: (typeof PICKER_QUICK)[number]): string {
		if (qp.provider !== "pi") return qp.label;
		const pi = apiKeyStore.all.find((p) => p.id === "pi" || p.id.includes("pi"));
		return pi ? pi.label : "Pi · 未配置";
	}

	function selectQuick(qp: (typeof PICKER_QUICK)[number]) {
		const pid = quickProviderId(qp);
		const exists = apiKeyStore.all.some((p) => p.id === pid);
		if (qp.provider === "pi" && !exists) {
			// 未注册本地 provider → 提示去设置面板添加 OpenAI 兼容端点
			alert("未配置 Pi (本地推理) provider：请在设置中添加 OpenAI 兼容端点，provider id 含 'pi'。");
			return;
		}
		apiKeyStore.provider = pid;
		apiKeyStore.save();
	}

	function selectModel(m: string) {
		apiKeyStore.model = m;
		apiKeyStore.save();
	}

	const presets = $derived(MODEL_PRESETS[apiKeyStore.provider] ?? []);
	const hasKey = $derived(apiKeyStore.api_key.trim().length > 0);
</script>

<div class="relative">
	<button
		type="button"
		onclick={() => (open = !open)}
		title="切换模型"
		class="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 font-mono text-[10px] text-white/60 transition hover:border-white/25 hover:text-white"
	>
		<Sparkles class="size-3 text-emerald-400" />
		<span class="max-w-28 truncate">{apiKeyStore.current.label} · {apiKeyStore.model.trim() || apiKeyStore.current.defaultModel || "未设置"}</span>
		<ChevronDown class="size-3 {open ? 'rotate-180' : ''} transition-transform" />
	</button>

	{#if open}
		<div class="absolute bottom-9 right-0 z-50 w-64 overflow-hidden rounded-xl border border-terminal-edge bg-terminal-panel shadow-2xl">
			<!-- 顶部: 三 provider 快捷按钮 -->
			<div class="flex gap-1 border-b border-terminal-edge p-2">
				{#each PICKER_QUICK as qp (qp.provider)}
					<button
						type="button"
						onclick={() => selectQuick(qp)}
						class="flex-1 rounded-lg px-2 py-1.5 font-mono text-[11px] font-medium transition
							{apiKeyStore.provider === quickProviderId(qp)
								? 'bg-white text-black'
								: 'bg-white/5 text-white/60 hover:bg-white/15 hover:text-white'}"
					>
						{quickLabel(qp)}
					</button>
				{/each}
			</div>

			<!-- 下方: 模型列表 -->
			<div class="max-h-56 overflow-y-auto p-2">
				<div class="mb-1 flex items-center gap-1 px-1 font-mono text-[9px] uppercase tracking-wider text-white/30">
					<Cpu class="size-2.5" /> {apiKeyStore.current.label} · 模型
				</div>
				{#each presets as m (m)}
					<button
						type="button"
						onclick={() => selectModel(m)}
						class="w-full rounded-lg px-2 py-1.5 text-left font-mono text-[11px] transition
							{apiKeyStore.model === m
								? 'bg-emerald-500/15 text-emerald-300'
								: 'text-white/70 hover:bg-white/10 hover:text-white'}"
					>
						{m}
					</button>
				{/each}
				{#if presets.length === 0}
					<p class="px-1 py-2 font-mono text-[10px] text-white/35">
						该 provider 无预设模型 — 在下方输入任意模型名。
					</p>
				{/if}
				<!-- 自定义模型输入 -->
				<input
					type="text"
					placeholder="自定义模型名…"
					value={apiKeyStore.model}
					oninput={(e) => selectModel((e.currentTarget as HTMLInputElement).value)}
					class="mt-1 w-full rounded-lg border border-white/10 bg-white/5 px-2 py-1.5 font-mono text-[11px] text-white outline-none placeholder:text-white/25 focus:border-emerald-500/50"
				/>
			</div>

			<!-- 底部: key 状态 -->
			<div class="flex items-center gap-1.5 border-t border-terminal-edge px-3 py-2 font-mono text-[10px]
				{hasKey ? 'text-emerald-400/80' : 'text-amber-400/80'}">
				<KeyRound class="size-3" />
				{hasKey ? 'API Key 已配置' : '未配置 Key — 主脑将走离线 stub'}
			</div>
		</div>
	{/if}
</div>
