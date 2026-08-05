<script lang="ts">
	/**
	 * FlowConsole — Coordinator → approval → Genesis HITL flow (Claude-Code/OpenCode style).
	 *
	 * Phase 1: RequirementCoordinator researches the prompt and proposes a RequirementDoc
	 *          (POST /legacy/flow/phase1, blocks while streaming cognitive_round/requirement_doc
	 *          events over the same session's /legacy/stream/{sid}).
	 * Phase 2: approved doc → GenesisManifest (POST /legacy/flow/phase2, single call).
	 * Phase 3: approved manifest → GenesisAgent forges each element, then assembly
	 *          (POST /legacy/flow/phase3 kicks off a background task; progress streams
	 *          over the SAME /legacy/stream/{sid} connection as phase 1).
	 *
	 * One EventSource per flow run, carrying two different event shapes on the same
	 * session_id: coordinator events keyed by "type" (cognitive_round / requirement_doc / …,
	 * see server/coordinator.py fire_step) and flow_engine events keyed by "event"
	 * (manifest / genesis_element_start / genesis_element_done / assembly_done / flow_error,
	 * see server/sse.py emit()).
	 */
	import { Bot, Loader2, Search, Sparkles } from "lucide-svelte";
	import { api } from "$lib/api";
	import { apiKeyStore } from "$lib/settings.svelte";
	import RequirementCard from "./RequirementCard.svelte";
	import GenesisProgressList, { type ElementStatus } from "./GenesisProgressList.svelte";

	interface RequirementDoc {
		title: string;
		context_analysis: string;
		core_features: string[];
	}

	interface ManifestElement {
		layer: string;
		name: string;
		specs: string;
	}

	interface Manifest {
		mission_id: string;
		elements: ManifestElement[];
	}

	interface TrailEvent {
		type: string;
		[key: string]: unknown;
	}

	type Phase =
		| "idle"
		| "phase1_running"
		| "phase1_review"
		| "phase2_running"
		| "phase2_review"
		| "phase3_running"
		| "done"
		| "error";

	let input = $state("");
	let submittedPrompt = $state("");
	let sessionId = $state("");
	let phase = $state<Phase>("idle");
	let researchTrail = $state<TrailEvent[]>([]);
	let requirementDoc = $state<RequirementDoc | null>(null);
	let manifest = $state<Manifest | null>(null);
	let genesisElements = $state<(ManifestElement & { status: ElementStatus; error?: string; response?: string })[]>([]);
	let assemblyCode = $state("");
	let errorMessage = $state("");

	let es: EventSource | undefined;

	function ensureStream() {
		if (es) return;
		sessionId = crypto.randomUUID();
		es = new EventSource(`/legacy/stream/${sessionId}`);
		es.onmessage = (ev) => {
			if (ev.data === "[DONE]") return;
			let payload: Record<string, unknown>;
			try {
				payload = JSON.parse(ev.data);
			} catch {
				return;
			}
			const kind = (payload.type ?? payload.event) as string | undefined;
			if (!kind) return;

			if (kind === "manifest" || kind === "genesis_element_start" || kind === "genesis_element_done" || kind === "assembly_done" || kind === "flow_error") {
				handleFlowEvent(kind, payload);
			} else {
				researchTrail = [...researchTrail, { type: kind, ...payload }];
			}
		};
	}

	function handleFlowEvent(kind: string, payload: Record<string, unknown>) {
		if (kind === "genesis_element_start") {
			genesisElements = genesisElements.map((el) =>
				el.layer === payload.layer && el.name === payload.name ? { ...el, status: "running" } : el,
			);
		} else if (kind === "genesis_element_done") {
			const status = (payload.status as ElementStatus) ?? "failed";
			genesisElements = genesisElements.map((el) =>
				el.layer === payload.layer && el.name === payload.name
					? { ...el, status, error: payload.error as string | undefined, response: payload.response as string | undefined }
					: el,
			);
		} else if (kind === "assembly_done") {
			assemblyCode = (payload.code as string) ?? "";
			phase = "done";
		} else if (kind === "flow_error") {
			errorMessage = String(payload.error ?? "unknown error");
			phase = "error";
		}
	}

	export function newFlow() {
		es?.close();
		es = undefined;
		sessionId = "";
		phase = "idle";
		researchTrail = [];
		requirementDoc = null;
		manifest = null;
		genesisElements = [];
		assemblyCode = "";
		errorMessage = "";
		submittedPrompt = "";
	}

	async function submitPrompt() {
		const prompt = input.trim();
		if (!prompt || phase !== "idle") return;
		newFlow();
		ensureStream();
		input = "";
		submittedPrompt = prompt;
		phase = "phase1_running";

		const res = await api("legacy", "flow/phase1", {
			body: {
				prompt,
				session_id: sessionId,
				provider: apiKeyStore.provider,
				model: apiKeyStore.model.trim() || undefined,
				config: apiKeyStore.asConfig(),
			},
		});
		const data = res.data as Record<string, unknown>;
		if (!res.ok || data?.status !== "success") {
			errorMessage = String(data?.error ?? data?.final_answer ?? "phase1 failed");
			phase = "error";
			return;
		}
		try {
			requirementDoc = JSON.parse(String(data.final_answer)) as RequirementDoc;
			phase = "phase1_review";
		} catch {
			// no valid tool call came back (e.g. no API key configured) — surface raw text
			errorMessage = String(data.final_answer ?? "model did not return a structured requirement doc");
			phase = "error";
		}
	}

	async function approveRequirement() {
		if (!requirementDoc) return;
		phase = "phase2_running";
		const res = await api("legacy", "flow/phase2", {
			body: {
				doc: requirementDoc,
				session_id: sessionId,
				provider: apiKeyStore.provider,
				model: apiKeyStore.model.trim() || undefined,
				config: apiKeyStore.asConfig(),
			},
		});
		const data = res.data as Record<string, unknown>;
		if (!res.ok) {
			errorMessage = String((data as Record<string, unknown>)?.detail ?? "phase2 failed");
			phase = "error";
			return;
		}
		manifest = data.manifest as Manifest;
		genesisElements = manifest.elements.map((el) => ({ ...el, status: "pending" as ElementStatus }));
		phase = "phase2_review";
	}

	async function approveManifest() {
		if (!manifest) return;
		phase = "phase3_running";
		await api("legacy", "flow/phase3", {
			body: { manifest, session_id: sessionId, config: apiKeyStore.asConfig() },
		});
		// progress streams in over the open EventSource (genesis_element_start/done, assembly_done)
	}

	function rejectRequirement() {
		newFlow();
	}
</script>

<div class="flex min-h-0 flex-1 flex-col overflow-hidden">
	<!-- ── chat flow ──────────────────────────────────────────────── -->
	<div class="flex-1 overflow-y-auto px-6 py-6">
		<div class="mx-auto flex max-w-3xl flex-col gap-6">
			{#if phase === "idle" && !submittedPrompt}
				<div class="flex flex-col items-center justify-center gap-2 py-24 text-terminal-dim">
					<Sparkles class="size-8 opacity-40" />
					<p class="text-sm">描述你想要的功能，Coordinator 会先调研并提出需求文档</p>
					<p class="text-xs opacity-70">phase1 → phase2 → phase3，Genesis 施工全程流式推送</p>
				</div>
			{/if}

			{#if submittedPrompt}
				<div class="flex justify-end">
					<div class="max-w-xl rounded-2xl rounded-tr-sm bg-terminal-panel px-4 py-2.5 text-sm text-terminal-fg">
						{submittedPrompt}
					</div>
				</div>
			{/if}

			{#if phase === "phase1_running"}
				<div class="flex items-center gap-2 text-sm text-terminal-dim">
					<Search class="size-4 animate-pulse" />
					主模型正在调研并生成需求文档…
				</div>
			{/if}

			{#if researchTrail.length > 0}
				<ol class="flex flex-col gap-1 border-b border-terminal-edge/60 pb-3">
					{#each researchTrail as ev, i (i)}
						<li class="flex items-start gap-2 font-mono text-xs text-terminal-dim">
							<Bot class="mt-0.5 size-3 shrink-0" />
							<span>{ev.type}{ev.phase ? ` · ${ev.phase}` : ""}</span>
						</li>
					{/each}
				</ol>
			{/if}

			{#if requirementDoc && (phase === "phase1_review" || phase === "phase2_running")}
				<RequirementCard doc={requirementDoc} approving={phase === "phase2_running"} onApprove={approveRequirement} onReject={rejectRequirement} />
			{/if}

			{#if manifest && (phase === "phase2_review" || phase === "phase3_running" || phase === "done")}
				<div class="flex w-full flex-col gap-2 rounded-xl border border-terminal-edge bg-terminal-panel p-4">
					<div class="text-sm font-semibold text-terminal-fg">3O 施工蓝图 · {manifest.mission_id}</div>
					<ul class="flex flex-col gap-1">
						{#each manifest.elements as el, i (i)}
							<li class="font-mono text-xs text-terminal-dim">{el.layer}/{el.name} — {el.specs}</li>
						{/each}
					</ul>
					{#if phase === "phase2_review"}
						<button
							type="button"
							onclick={approveManifest}
							class="mt-1 flex w-fit items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-semibold text-white transition hover:bg-emerald-500"
						>
							确认蓝图，开始施工
						</button>
					{/if}
				</div>
			{/if}

			{#if genesisElements.length > 0}
				<GenesisProgressList elements={genesisElements} />
			{/if}

			{#if phase === "done" && assemblyCode}
				<div class="flex w-full flex-col gap-2 rounded-xl border border-terminal-edge bg-terminal-panel p-4">
					<div class="text-sm font-semibold text-terminal-fg">最终组装代码</div>
					<pre class="overflow-x-auto whitespace-pre-wrap font-mono text-sm text-terminal-fg">{assemblyCode}</pre>
				</div>
			{/if}

			{#if phase === "error"}
				<div class="rounded-xl border border-rose-500/30 bg-rose-500/5 p-3 text-sm text-rose-300">
					{errorMessage}
				</div>
			{/if}
		</div>
	</div>

	<!-- ── input dock ─────────────────────────────────────────────── -->
	<div class="shrink-0 border-t border-terminal-edge bg-terminal-panel/60 px-6 py-4">
		<div class="mx-auto flex max-w-3xl flex-col gap-2">
			<div class="flex items-end gap-2 rounded-xl border border-terminal-edge bg-terminal-bg p-2 transition focus-within:border-sky-500/60">
				<textarea
					bind:value={input}
					rows="2"
					placeholder="描述你想要的功能，例如：写一个基于 MACD 的风险控制拦截器"
					disabled={phase !== "idle"}
					class="min-w-0 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 disabled:opacity-50"
				></textarea>
				<button
					type="button"
					onclick={submitPrompt}
					disabled={phase !== "idle" || !input.trim()}
					class="mb-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 text-white transition hover:brightness-110 disabled:opacity-40"
				>
					{#if phase === "phase1_running"}
						<Loader2 class="size-3.5 animate-spin" />
					{:else}
						<Sparkles class="size-3.5" />
					{/if}
				</button>
			</div>
			{#if sessionId}
				<span class="font-mono text-xs text-terminal-dim/70">session {sessionId.slice(0, 8)}</span>
			{/if}
		</div>
	</div>
</div>
