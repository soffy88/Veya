<script lang="ts">
	/**
	 * ModelPicker — 聊天框右下角引擎/模型选择器 (cindy 风格)。
	 *
	 * 顶部: 执行引擎 (Master / Claude / Codex / Pi) — 选谁执行任务。
	 * 下方: 模型列表 (引擎 CLI 支持 -m 时透传; master 走主脑 provider/model)。
	 * 选择写入 apiKeyStore (localStorage 持久化), 请求经 engine/model 字段透传。
	 */
	import { ChevronDown, Cpu, KeyRound, Sparkles } from "lucide-svelte";
	import { onMount } from "svelte";
	import { API_BASE } from "$lib/api";
	import { ENGINES, MODEL_PRESETS, apiKeyStore } from "$lib/settings.svelte";

	let open = $state(false);
	let rootEl = $state<HTMLDivElement | null>(null);
	// null = 未加载/后端不可达 → 不禁用 (保守); Set = 后端可用引擎 id
	let available = $state<Set<string> | null>(null);

	// 点击外部关闭菜单 (体验: 不再只能点按钮收回)
	$effect(() => {
		if (!open) return;
		function onDown(e: PointerEvent) {
			if (rootEl && !rootEl.contains(e.target as Node)) open = false;
		}
		document.addEventListener("pointerdown", onDown, true);
		return () => document.removeEventListener("pointerdown", onDown, true);
	});

	// 引擎可用性: 后端 /api/v1/engines (非 master 需本机装 CLI)
	onMount(() => {
		void (async () => {
			try {
				const res = await fetch(`${API_BASE}/api/v1/engines`);
				if (res.ok) {
					const data = (await res.json()) as { engines?: Record<string, string> };
					available = new Set(Object.keys(data.engines ?? {}));
				}
			} catch {
				/* 后端不可达: 保持 null */
			}
		})();
	});

	function selectEngine(id: string) {
		apiKeyStore.engine = id;
	}

	function selectModel(m: string) {
		apiKeyStore.model = m;
		apiKeyStore.save();
	}

	const presets = $derived(MODEL_PRESETS[apiKeyStore.engine === "master" ? apiKeyStore.provider : apiKeyStore.engine] ?? []);
	const hasKey = $derived(apiKeyStore.api_key.trim().length > 0);
	const engineLabel = $derived(ENGINES.find((e) => e.id === apiKeyStore.engine)?.label ?? apiKeyStore.engine);
	const modelLabel = $derived(apiKeyStore.model.trim() || apiKeyStore.current.defaultModel || "默认");
</script>

<div class="relative" bind:this={rootEl}>
	<button
		type="button"
		onclick={() => (open = !open)}
		title="切换引擎/模型"
		class="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 font-mono text-[10px] text-white/60 transition hover:border-white/25 hover:text-white"
	>
		<Sparkles class="size-3 text-emerald-400" />
		<span class="max-w-32 truncate">{engineLabel} · {modelLabel}</span>
		<ChevronDown class="size-3 {open ? 'rotate-180' : ''} transition-transform" />
	</button>

	{#if open}
		<div class="absolute bottom-9 right-0 z-50 w-64 overflow-hidden rounded-xl border border-terminal-edge bg-terminal-panel shadow-2xl">
			<!-- 顶部: 执行引擎选择 -->
			<div class="flex gap-1 border-b border-terminal-edge p-2">
				{#each ENGINES as eng (eng.id)}
					{@const unavailable = eng.id !== "master" && available !== null && !available.has(eng.id)}
					<button
						type="button"
						onclick={() => selectEngine(eng.id)}
						disabled={unavailable}
						title={unavailable ? "后端未安装该引擎 CLI (仅 master 可用)" : ""}
						class="flex-1 rounded-lg px-1.5 py-1.5 font-mono text-[10px] font-medium transition
							{unavailable
								? 'cursor-not-allowed text-white/25'
								: apiKeyStore.engine === eng.id
									? 'bg-white text-black'
									: 'bg-white/5 text-white/60 hover:bg-white/15 hover:text-white'}"
					>
						{eng.label}
					</button>
				{/each}
			</div>

			<!-- 下方: 模型列表 -->
			<div class="max-h-56 overflow-y-auto p-2">
				<div class="mb-1 flex items-center gap-1 px-1 font-mono text-[9px] uppercase tracking-wider text-white/30">
					<Cpu class="size-2.5" /> {engineLabel} · 模型
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
						无预设模型 — 在下方输入任意模型名。
					</p>
				{/if}
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
				{hasKey ? 'API Key 已配置' : '未配置 Key — 引擎/主脑按各自凭据运行'}
			</div>
		</div>
	{/if}
</div>
