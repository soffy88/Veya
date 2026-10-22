<script lang="ts">
	/**
	 * FlowConsole — Coordinator → approval → Genesis HITL flow (Claude-Code/OpenCode style).
	 *
	 * Multi-turn: each submitted prompt becomes its own Turn with its own phase/session,
	 * appended to `turns` and rendered in order — like a normal chat log, not a single-shot
	 * form that locks up after one round trip. Only the most recent turn can be "in flight"
	 * (submitPrompt is a no-op while `busy`), so once it reaches done/error the input frees
	 * up again automatically for the next message. newFlow() (the sidebar's "新对话") is the
	 * only thing that clears history — a deliberate reset, not something the user hits by
	 * accident just to keep typing.
	 *
	 * Phase 1: RequirementCoordinator researches the prompt and proposes a RequirementDoc
	 *          (POST /api/v1/flow/phase1, blocks while streaming cognitive_round/requirement_doc
	 *          events over that turn's /api/v1/stream/{sid}).
	 * Phase 2: approved doc → GenesisManifest (POST /api/v1/flow/phase2, single call).
	 * Phase 3: approved manifest → GenesisAgent forges each element, then assembly
	 *          (POST /api/v1/flow/phase3 kicks off a background task; progress streams
	 *          over the SAME /api/v1/stream/{sid} connection as phase 1).
	 *
	 * One stream per turn, carrying two different event shapes on the same session_id:
	 * coordinator events keyed by "type" (cognitive_round / requirement_doc / …, see
	 * server/coordinator.py fire_step) and flow_engine events keyed by "event"
	 * (manifest / genesis_element_start / genesis_element_done / assembly_done /
	 * flow_error, see server/sse.py emit()).
	 *
	 * The stream is read with ``fetch`` rather than ``EventSource`` because
	 * /api/v1/stream/{sid} is an authenticated endpoint and EventSource cannot send
	 * an Authorization header — the same reason $lib/supervision/events.ts and
	 * $lib/notifications.svelte.ts use a reader. Frames are split on the SSE
	 * blank-line separator, and a "[DONE]" data line ends the connection.
	 */
	import { Bot, Code2, Loader2, Search, Sparkles } from "lucide-svelte";
	import { api, API_BASE } from "$lib/api";
	import { artifactStore } from "$lib/artifacts.svelte";
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

	type Phase = "phase1_running" | "phase1_review" | "phase2_running" | "phase2_review" | "phase3_running" | "done" | "error";

	interface Turn {
		id: string;
		sessionId: string;
		prompt: string;
		phase: Phase;
		researchTrail: TrailEvent[];
		requirementDoc: RequirementDoc | null;
		manifest: Manifest | null;
		genesisElements: (ManifestElement & { status: ElementStatus; error?: string; response?: string })[];
		assemblyCode: string;
		errorMessage: string;
	}

	let input = $state("");
	let turns = $state<Turn[]>([]);
	let activeTurnId = $state("");
	/** Abort handle for the turn's stream reader; one per open turn. */
	let streamAbort: AbortController | undefined;

	const activeTurn = $derived(turns.find((t) => t.id === activeTurnId));
	const busy = $derived(!!activeTurn && activeTurn.phase !== "done" && activeTurn.phase !== "error");

	function findTurn(id: string): Turn | undefined {
		return turns.find((t) => t.id === id);
	}

	/** Same credential resolution as $lib/api: localStorage first, then cookie. */
	function authToken(): string | null {
		if (typeof localStorage !== "undefined") {
			const saved = localStorage.getItem("veya.auth.token");
			if (saved) return saved;
		}
		if (typeof document !== "undefined") {
			for (const part of document.cookie.split(";")) {
				const [k, v] = part.trim().split("=");
				if ((k === "veya_token" || k === "veya.auth.token") && v) return decodeURIComponent(v);
			}
		}
		return null;
	}

	function ensureStream(sessionId: string, turnId: string) {
		// One reader per turn: closing the previous one first keeps a re-submitted
		// turn from leaking its connection.
		streamAbort?.abort();
		streamAbort = new AbortController();
		void readTurnStream(sessionId, turnId, streamAbort.signal);
	}

	async function readTurnStream(sessionId: string, turnId: string, signal: AbortSignal) {
		// Same credential resolution api() uses (localStorage, then the cookie),
		// because EventSource cannot send an Authorization header.
		const headers: Record<string, string> = { accept: "text/event-stream" };
		const token = authToken();
		if (token) headers.authorization = `Bearer ${token}`;
		try {
			const res = await fetch(`${API_BASE}/api/v1/stream/${sessionId}`, {
				headers,
				signal,
			});
			if (!res.ok || !res.body) {
				const turn = findTurn(turnId);
				if (turn) {
					turn.errorMessage = `stream ${res.status}`;
					turn.phase = "error";
				}
				return;
			}
			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			let buffer = "";
			for (;;) {
				const { done, value } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true });
				let sep = buffer.indexOf("\n\n");
				while (sep >= 0) {
					const frame = buffer.slice(0, sep);
					buffer = buffer.slice(sep + 2);
					handleStreamFrame(frame, turnId);
					sep = buffer.indexOf("\n\n");
				}
			}
		} catch (e) {
			// Aborting is a deliberate close (newFlow / a newer turn), not a failure.
			if (signal.aborted) return;
			console.error("[Veya] flow stream lost", e);
		}
	}

	function handleStreamFrame(frame: string, turnId: string) {
		const dataLines: string[] = [];
		for (const line of frame.split("\n")) {
			if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
		}
		if (!dataLines.length) return;
		const data = dataLines.join("\n");
		if (data === "[DONE]") return;
		let payload: Record<string, unknown>;
		try {
			payload = JSON.parse(data);
		} catch {
			return;
		}
		const kind = (payload.type ?? payload.event) as string | undefined;
		if (!kind) return;
		const turn = findTurn(turnId);
		if (!turn) return;

		if (kind === "manifest" || kind === "genesis_element_start" || kind === "genesis_element_done" || kind === "assembly_done" || kind === "flow_error") {
			handleFlowEvent(turn, kind, payload);
		} else {
			turn.researchTrail = [...turn.researchTrail, { type: kind, ...payload }];
		}
	}

	function handleFlowEvent(turn: Turn, kind: string, payload: Record<string, unknown>) {
		if (kind === "genesis_element_start") {
			turn.genesisElements = turn.genesisElements.map((el) =>
				el.layer === payload.layer && el.name === payload.name ? { ...el, status: "running" } : el,
			);
		} else if (kind === "genesis_element_done") {
			const status = (payload.status as ElementStatus) ?? "failed";
			turn.genesisElements = turn.genesisElements.map((el) =>
				el.layer === payload.layer && el.name === payload.name
					? { ...el, status, error: payload.error as string | undefined, response: payload.response as string | undefined }
					: el,
			);
		} else if (kind === "assembly_done") {
			turn.assemblyCode = (payload.code as string) ?? "";
			turn.phase = "done";
		} else if (kind === "flow_error") {
			turn.errorMessage = String(payload.error ?? "unknown error");
			turn.phase = "error";
		}
	}

	/** Sidebar "新对话" — clears the whole visible history. Not called between turns. */
	export function newFlow() {
		streamAbort?.abort();
		streamAbort = undefined;
		turns = [];
		activeTurnId = "";
		input = "";
	}

	async function submitPrompt() {
		const prompt = input.trim();
		if (!prompt || busy) return;
		input = "";

		const sessionId = crypto.randomUUID();
		const turn: Turn = {
			id: crypto.randomUUID(),
			sessionId,
			prompt,
			phase: "phase1_running",
			researchTrail: [],
			requirementDoc: null,
			manifest: null,
			genesisElements: [],
			assemblyCode: "",
			errorMessage: "",
		};
		turns = [...turns, turn];
		activeTurnId = turn.id;
		ensureStream(sessionId, turn.id);

		const res = await api("gateway", "api/v1/flow/phase1", {
			body: {
				prompt,
				session_id: sessionId,
				provider: apiKeyStore.provider,
				model: apiKeyStore.model.trim() || undefined,
				config: apiKeyStore.asConfig(),
			},
		});
		const data = res.data as Record<string, unknown>;
		const t = findTurn(turn.id);
		if (!t) return;
		if (!res.ok || data?.status !== "success") {
			t.errorMessage = String(data?.error ?? data?.final_answer ?? "phase1 failed");
			t.phase = "error";
			return;
		}
		try {
			t.requirementDoc = JSON.parse(String(data.final_answer)) as RequirementDoc;
			t.phase = "phase1_review";
		} catch {
			// no valid tool call came back (e.g. no API key configured) — surface raw text
			t.errorMessage = String(data.final_answer ?? "model did not return a structured requirement doc");
			t.phase = "error";
		}
	}

	async function approveRequirement(turn: Turn) {
		if (!turn.requirementDoc) return;
		turn.phase = "phase2_running";
		const res = await api("gateway", "api/v1/flow/phase2", {
			body: {
				doc: turn.requirementDoc,
				session_id: turn.sessionId,
				provider: apiKeyStore.provider,
				model: apiKeyStore.model.trim() || undefined,
				config: apiKeyStore.asConfig(),
			},
		});
		const data = res.data as Record<string, unknown>;
		const t = findTurn(turn.id);
		if (!t) return;
		if (!res.ok) {
			t.errorMessage = String((data as Record<string, unknown>)?.detail ?? "phase2 failed");
			t.phase = "error";
			return;
		}
		t.manifest = data.manifest as Manifest;
		t.genesisElements = t.manifest.elements.map((el) => ({ ...el, status: "pending" as ElementStatus }));
		t.phase = "phase2_review";
	}

	async function approveManifest(turn: Turn) {
		if (!turn.manifest) return;
		turn.phase = "phase3_running";
		await api("gateway", "api/v1/flow/phase3", {
			body: { manifest: turn.manifest, session_id: turn.sessionId, config: apiKeyStore.asConfig() },
		});
		// progress streams in over the turn's open reader (genesis_element_start/done, assembly_done)
	}

	function rejectRequirement(turn: Turn) {
		turn.phase = "error";
		turn.errorMessage = "已拒绝该需求";
	}
</script>

<div class="flex min-h-0 flex-1 flex-col overflow-hidden">
	<!-- ── chat flow ──────────────────────────────────────────────── -->
	<div class="flex-1 overflow-y-auto px-6 py-6">
		<div class="mx-auto flex max-w-3xl flex-col gap-6">
			{#if turns.length === 0}
				<div class="flex flex-col items-center justify-center gap-2 py-24 text-terminal-dim">
					<Sparkles class="size-8 opacity-40" />
					<p class="text-sm">描述你想要的功能，Coordinator 会先调研并提出需求文档</p>
					<p class="text-xs opacity-70">phase1 → phase2 → phase3，Genesis 施工全程流式推送</p>
				</div>
			{/if}

			{#each turns as turn (turn.id)}
				<div class="flex flex-col gap-4">
					<div class="flex justify-end">
						<div class="max-w-xl rounded-2xl rounded-tr-sm bg-terminal-panel px-4 py-2.5 text-sm text-terminal-fg">
							{turn.prompt}
						</div>
					</div>

					{#if turn.phase === "phase1_running"}
						<div class="flex items-center gap-2 text-sm text-terminal-dim">
							<Search class="size-4 animate-pulse" />
							主模型正在调研并生成需求文档…
						</div>
					{/if}

					{#if turn.researchTrail.length > 0}
						<ol class="flex flex-col gap-1 border-b border-terminal-edge/60 pb-3">
							{#each turn.researchTrail as ev, i (i)}
								<li class="flex items-start gap-2 font-mono text-xs text-terminal-dim">
									<Bot class="mt-0.5 size-3 shrink-0" />
									<span>{ev.type}{ev.phase ? ` · ${ev.phase}` : ""}</span>
								</li>
							{/each}
						</ol>
					{/if}

					{#if turn.requirementDoc && (turn.phase === "phase1_review" || turn.phase === "phase2_running")}
						<RequirementCard
							doc={turn.requirementDoc}
							approving={turn.phase === "phase2_running"}
							onApprove={() => approveRequirement(turn)}
							onReject={() => rejectRequirement(turn)}
						/>
					{/if}

					{#if turn.manifest && (turn.phase === "phase2_review" || turn.phase === "phase3_running" || turn.phase === "done")}
						<div class="flex w-full flex-col gap-2 rounded-xl border border-terminal-edge bg-terminal-panel p-4">
							<div class="text-sm font-semibold text-terminal-fg">3O 施工蓝图 · {turn.manifest.mission_id}</div>
							<ul class="flex flex-col gap-1">
								{#each turn.manifest.elements as el, i (i)}
									<li class="font-mono text-xs text-terminal-dim">{el.layer}/{el.name} — {el.specs}</li>
								{/each}
							</ul>
							{#if turn.phase === "phase2_review"}
								<button
									type="button"
									onclick={() => approveManifest(turn)}
									class="mt-1 flex w-fit items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-semibold text-white transition hover:bg-emerald-500"
								>
									确认蓝图，开始施工
								</button>
							{/if}
						</div>
					{/if}

					{#if turn.genesisElements.length > 0}
						<GenesisProgressList elements={turn.genesisElements} />
					{/if}

					{#if turn.phase === "done" && turn.assemblyCode}
						{@const parsed = artifactStore.parseArtifactsFromText(turn.assemblyCode)}
						<div class="flex w-full flex-col gap-2 rounded-xl border border-terminal-edge bg-terminal-panel p-4">
							<div class="text-sm font-semibold text-terminal-fg">最终组装代码</div>
							{#if parsed.artifacts.length > 0}
								<div class="flex flex-col gap-2">
									{#each parsed.artifacts as art (art.id)}
										<button
											type="button"
											onclick={() => artifactStore.setActive(art)}
											class="group flex items-center gap-3 rounded-lg border border-emerald-900/50 bg-emerald-950/30 p-2.5 text-left transition hover:bg-emerald-900/40"
										>
											<span class="flex size-8 shrink-0 items-center justify-center rounded-md bg-emerald-900/50 group-hover:bg-emerald-800/80">
												<Code2 class="size-4 text-emerald-400" />
											</span>
											<span class="min-w-0">
												<span class="block text-sm font-bold text-emerald-100">{art.title || "React Artifact"}</span>
												<span class="block text-xs text-emerald-500/70">Click to run in Sandbox</span>
											</span>
										</button>
									{/each}
								</div>
							{/if}
							<pre class="overflow-x-auto whitespace-pre-wrap font-mono text-sm text-terminal-fg">{parsed.pureText}</pre>
						</div>
					{/if}

					{#if turn.phase === "error"}
						<div class="rounded-xl border border-rose-500/30 bg-rose-500/5 p-3 text-sm text-rose-300">
							{turn.errorMessage}
						</div>
					{/if}
				</div>
			{/each}
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
					disabled={busy}
					onkeydown={(e) => {
						if (e.key === "Enter" && !e.shiftKey) {
							e.preventDefault();
							submitPrompt();
						}
					}}
					class="min-w-0 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-terminal-fg outline-none placeholder:text-terminal-dim/60 disabled:opacity-50"
				></textarea>
				<button
					type="button"
					onclick={submitPrompt}
					disabled={busy || !input.trim()}
					class="mb-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 text-white transition hover:brightness-110 disabled:opacity-40"
				>
					{#if busy}
						<Loader2 class="size-3.5 animate-spin" />
					{:else}
						<Sparkles class="size-3.5" />
					{/if}
				</button>
			</div>
			<div class="flex items-center gap-2 font-mono text-xs text-terminal-dim/70">
				<span class="size-1.5 rounded-full {apiKeyStore.api_key ? 'bg-emerald-500' : 'bg-terminal-dim'}"></span>
				{apiKeyStore.current.label} · {apiKeyStore.model.trim() || apiKeyStore.current.defaultModel || "未设置模型"}
			</div>
		</div>
	</div>
</div>
