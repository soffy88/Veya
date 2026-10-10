<script lang="ts">
	/**
	 * DiffAnnotations — wraps a diff view with ORCA-W3 annotation, AI gutter markers,
	 * and "Fix broken checks" button.
	 *
	 * Props:
	 *   goalId    — goal_run id for annotation storage
	 *   diffText  — the raw unified diff text to render
	 *   repoPath  — optional server-side repo path
	 *   branch    — optional branch name (for fix-checks)
	 *
	 * Keyboard shortcut: focus a line and press 'c' to open the annotation form.
	 */
	import { API_BASE } from "$lib/api";
	import { MessageSquarePlus, Bot, Bug, Loader2, Send, CheckCircle } from "lucide-svelte";

	type Props = {
		goalId: string;
		diffText: string;
		repoPath?: string;
		branch?: string;
	};

	let { goalId, diffText, repoPath = "", branch = "" }: Props = $props();

	// ── Parse diff into lines ────────────────────────────────────────────────

	type DiffLine = {
		raw: string;
		lineNo: number; // 0 if not applicable
		file: string;
		side: "left" | "right";
		isAi?: boolean;
	};

	type ParsedFile = {
		name: string;
		lines: DiffLine[];
	};

	let parsedFiles = $derived(() => {
		const files: ParsedFile[] = [];
		let current: ParsedFile | null = null;
		let rightLine = 0;
		for (const raw of diffText.split("\n")) {
			if (raw.startsWith("--- ") || raw.startsWith("+++ ")) {
				if (raw.startsWith("+++ ")) {
					const name = raw.replace("+++ b/", "").replace("+++ ", "");
					current = { name, lines: [] };
					files.push(current);
					rightLine = 0;
				}
				continue;
			}
			if (raw.startsWith("@@ ")) {
				// parse hunk header e.g. @@ -1,4 +3,7 @@
				const m = raw.match(/\+(\d+)/);
				rightLine = m ? parseInt(m[1]) - 1 : rightLine;
				current?.lines.push({ raw, lineNo: 0, file: current?.name ?? "", side: "right" });
				continue;
			}
			if (!current) continue;
			if (raw.startsWith("+")) {
				rightLine++;
				current.lines.push({ raw, lineNo: rightLine, file: current.name, side: "right" });
			} else if (raw.startsWith("-")) {
				current.lines.push({ raw, lineNo: 0, file: current.name, side: "left" });
			} else {
				rightLine++;
				current.lines.push({ raw, lineNo: rightLine, file: current.name, side: "right" });
			}
		}
		return files;
	});

	// ── AI attribution ────────────────────────────────────────────────────────

	// Map of "file:line" -> true for AI-attributed lines
	let aiLines = $state<Record<string, boolean>>({});

	async function loadAttribution(fileName: string) {
		if (!goalId) return;
		try {
			const q = new URLSearchParams({ path: fileName, repo_path: repoPath });
			const res = await fetch(`${API_BASE}/api/v1/review/attribution/file?${q}`);
			if (!res.ok) return;
			const data = (await res.json()) as { lines: { line: number; attribution: string }[] };
			for (const l of data.lines ?? []) {
				if (l.attribution === "ai") {
					aiLines[`${fileName}:${l.line}`] = true;
				}
			}
			aiLines = { ...aiLines };
		} catch {
			// ignore — attribution is best-effort
		}
	}

	$effect(() => {
		for (const f of parsedFiles()) {
			loadAttribution(f.name);
		}
	});

	// ── Annotation form ───────────────────────────────────────────────────────

	let annotationTarget = $state<{ file: string; line: number; side: "left" | "right" } | null>(
		null
	);
	let annotationBody = $state("");
	let annotationSaving = $state(false);
	let annotationError = $state("");
	let annotations = $state<{ id: string; file: string; line: number; body: string }[]>([]);

	async function openAnnotation(file: string, line: number, side: "left" | "right") {
		if (line <= 0) return;
		annotationTarget = { file, line, side };
		annotationBody = "";
		annotationError = "";
	}

	async function saveAnnotation() {
		if (!annotationTarget || !annotationBody.trim()) return;
		annotationSaving = true;
		annotationError = "";
		try {
			const res = await fetch(`${API_BASE}/api/v1/review/annotations/${encodeURIComponent(goalId)}`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					worktree: "",
					file: annotationTarget.file,
					line: annotationTarget.line,
					side: annotationTarget.side,
					body: annotationBody.trim(),
				}),
			});
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = await res.json();
			annotations = [...annotations, data.annotation];
			annotationTarget = null;
			annotationBody = "";
		} catch (e) {
			annotationError = e instanceof Error ? e.message : String(e);
		} finally {
			annotationSaving = false;
		}
	}

	function handleKeydown(e: KeyboardEvent, file: string, line: number) {
		if (e.key === "c" && !annotationTarget) {
			e.preventDefault();
			openAnnotation(file, line, "right");
		}
	}

	// ── Send to agent ─────────────────────────────────────────────────────────

	let recipient = $state("");
	let sending = $state(false);
	let sendResult = $state<{ sent: number; message_id: string | null } | null>(null);
	let sendError = $state("");

	async function sendAnnotations() {
		if (!recipient.trim() || !goalId) return;
		sending = true;
		sendError = "";
		sendResult = null;
		try {
			const res = await fetch(
				`${API_BASE}/api/v1/review/annotations/${encodeURIComponent(goalId)}/send`,
				{
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ recipient: recipient.trim(), repo_path: repoPath }),
				}
			);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			sendResult = await res.json();
		} catch (e) {
			sendError = e instanceof Error ? e.message : String(e);
		} finally {
			sending = false;
		}
	}

	// ── Fix broken checks ────────────────────────────────────────────────────

	let fixLoading = $state(false);
	let fixResult = $state<{ status: string; goal_id?: string; failing_checks?: string[] } | null>(
		null
	);
	let fixError = $state("");

	async function fixChecks() {
		fixLoading = true;
		fixError = "";
		fixResult = null;
		try {
			const res = await fetch(`${API_BASE}/api/v1/review/fix-checks`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ repo_path: repoPath || null, branch: branch || null }),
			});
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			fixResult = await res.json();
		} catch (e) {
			fixError = e instanceof Error ? e.message : String(e);
		} finally {
			fixLoading = false;
		}
	}
</script>

<div class="flex flex-col gap-4">
	<!-- Toolbar -->
	<div class="flex items-center gap-3 flex-wrap">
		<span class="font-mono text-xs text-white/40">ORCA-W3 Review</span>
		<div class="flex-1"></div>

		<!-- Send to agent -->
		<div class="flex items-center gap-2">
			<input
				bind:value={recipient}
				placeholder="recipient (session/goal_id)"
				class="rounded-lg border border-white/10 bg-[#0d0d0d] px-2 py-1 text-xs text-terminal-fg outline-none placeholder:text-white/25 focus:border-emerald-500/40"
			/>
			<button
				type="button"
				onclick={sendAnnotations}
				disabled={sending || !recipient.trim()}
				class="flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 text-xs text-white/60 transition hover:border-emerald-500/40 hover:text-white disabled:opacity-40"
				title="Send all open annotations to agent"
			>
				{#if sending}<Loader2 class="size-3.5 animate-spin" />{:else}<Send class="size-3.5" />{/if}
				Send to agent
			</button>
		</div>

		<!-- Fix broken checks -->
		<button
			type="button"
			onclick={fixChecks}
			disabled={fixLoading}
			class="flex items-center gap-1.5 rounded-lg border border-orange-500/30 px-3 py-1.5 text-xs text-orange-400/80 transition hover:border-orange-400/60 hover:text-orange-300 disabled:opacity-40"
			title="Fetch failing CI checks and start a GoalRun to fix them"
		>
			{#if fixLoading}<Loader2 class="size-3.5 animate-spin" />{:else}<Bug class="size-3.5" />{/if}
			Fix broken checks
		</button>
	</div>

	<!-- Status bars -->
	{#if sendResult}
		<div class="rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-400">
			<CheckCircle class="inline size-3.5 mr-1" />
			Sent {sendResult.sent} annotation(s) — msg {sendResult.message_id?.slice(0, 8)}
		</div>
	{/if}
	{#if sendError}
		<div class="rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2 text-xs text-red-400">{sendError}</div>
	{/if}
	{#if fixResult}
		<div class="rounded-lg border border-orange-500/30 bg-orange-500/5 px-3 py-2 text-xs text-orange-300">
			{#if fixResult.status === "no_failing_checks"}
				✅ No failing checks on this branch.
			{:else if fixResult.status === "gh_unavailable"}
				⚠️ gh CLI not available.
			{:else if fixResult.status === "started"}
				🔧 GoalRun started: {fixResult.goal_id ?? "(enqueued)"}
				{#if fixResult.failing_checks?.length}
					— fixing: {fixResult.failing_checks.join(", ")}
				{/if}
			{:else}
				{fixResult.status}
			{/if}
		</div>
	{/if}
	{#if fixError}
		<div class="rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2 text-xs text-red-400">{fixError}</div>
	{/if}

	<!-- Diff with annotation + AI gutter -->
	{#each parsedFiles() as f (f.name)}
		<div class="rounded-lg border border-white/10 bg-black/30">
			<div class="border-b border-white/10 px-3 py-1.5 font-mono text-xs text-white/50">{f.name}</div>
			<div class="overflow-x-auto">
				{#each f.lines as l, i (i)}
					<!-- svelte-ignore a11y_no_static_element_interactions -->
					<div
						class="group flex items-start gap-1 font-mono text-[11px] leading-relaxed hover:bg-white/5
							{l.raw.startsWith('+') ? 'bg-emerald-500/5 text-emerald-300/80' : ''}
							{l.raw.startsWith('-') ? 'bg-red-500/5 text-red-300/80' : ''}
							{!l.raw.startsWith('+') && !l.raw.startsWith('-') ? 'text-white/50' : ''}"
						onkeydown={(e: KeyboardEvent) => handleKeydown(e, l.file, l.lineNo)}
						tabindex="0"
						role="row"
					>
						<!-- AI gutter marker -->
						<span class="w-5 shrink-0 text-center text-[9px]">
							{#if l.lineNo > 0 && aiLines[`${l.file}:${l.lineNo}`]}
								<span title="AI written" class="text-blue-400/80"><Bot class="inline size-3" /></span>
							{/if}
						</span>
						<!-- Line number -->
						<span class="w-8 shrink-0 text-right text-white/20">{l.lineNo > 0 ? l.lineNo : ""}</span>
						<!-- Annotation "+" button -->
						<button
							type="button"
							class="invisible shrink-0 rounded px-1 text-white/30 hover:text-emerald-400 group-hover:visible"
							title="Add annotation (or press c)"
							onclick={() => openAnnotation(l.file, l.lineNo, l.side)}
							disabled={l.lineNo <= 0}
						>
							<MessageSquarePlus class="size-3" />
						</button>
						<!-- Line content -->
						<span class="flex-1 whitespace-pre">{l.raw}</span>
						<!-- Existing annotations for this line -->
						{#if annotations.some((a) => a.file === l.file && a.line === l.lineNo)}
							<span class="ml-2 shrink-0 rounded bg-yellow-500/20 px-1 text-[9px] text-yellow-300/80" title={annotations.filter(a => a.file === l.file && a.line === l.lineNo).map(a => a.body).join("; ")}>
								💬
							</span>
						{/if}
					</div>
				{/each}
			</div>
		</div>
	{/each}

	<!-- Annotation form -->
	{#if annotationTarget}
		<div class="rounded-lg border border-yellow-500/30 bg-yellow-500/5 p-3 flex flex-col gap-2">
			<div class="text-xs text-yellow-300/70">
				Annotating <span class="font-mono">{annotationTarget.file}:{annotationTarget.line}</span>
			</div>
			<textarea
				bind:value={annotationBody}
				placeholder="Describe the issue: situation / what is wrong / expected"
				class="rounded-lg border border-white/10 bg-black/40 p-2 text-xs text-terminal-fg outline-none placeholder:text-white/25 focus:border-yellow-500/40 resize-none"
				rows="3"
			></textarea>
			{#if annotationError}
				<p class="text-xs text-red-400">{annotationError}</p>
			{/if}
			<div class="flex gap-2">
				<button
					type="button"
					onclick={saveAnnotation}
					disabled={annotationSaving || !annotationBody.trim()}
					class="flex items-center gap-1.5 rounded-lg bg-yellow-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-yellow-500 disabled:opacity-30"
				>
					{#if annotationSaving}<Loader2 class="size-3.5 animate-spin" />{/if}
					Save annotation
				</button>
				<button
					type="button"
					onclick={() => { annotationTarget = null; }}
					class="rounded-lg border border-white/10 px-3 py-1.5 text-xs text-white/50 hover:text-white"
				>
					Cancel
				</button>
			</div>
		</div>
	{/if}
</div>
