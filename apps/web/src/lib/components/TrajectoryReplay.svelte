<script lang="ts">
	/**
	 * TrajectoryReplay — NANO-W9 addendum E "replay" view.
	 *
	 * Walks a run's recorded trajectory (command → artifact states) so an
	 * operator can see, step by step, which command produced which artifact.
	 * The trajectory itself is recorded on the existing event log
	 * (`trajectory.recorded` topic); this component only reads it back through
	 * `GET /api/v1/nano-w9/trajectory/{goalId}`.
	 *
	 * Props:
	 *   goalId  — the goal_run whose trajectory to replay
	 *   auto    — load on mount (default true)
	 */
	import { API_BASE } from "$lib/api";
	import { History, Loader2, PlayCircle, RotateCcw, FileText, ChevronRight } from "lucide-svelte";

	type ReplayState = {
		index: number;
		step: number;
		command: string;
		artifact_ref: string;
		preview_ref: string;
		preview: string;
		ts: number;
		status: string;
		artifacts_so_far: string[];
	};

	type ReplayPayload = {
		status: string;
		goal_id: string;
		step_count: number;
		artifacts: string[];
		states: ReplayState[];
	};

	type Props = {
		goalId: string;
		auto?: boolean;
	};

	let { goalId, auto = true }: Props = $props();

	// Read-model state is kept flat (rather than one nullable payload object) so
	// the markup can narrow on plain arrays without a `never` type.
	let states = $state<ReplayState[]>([]);
	let artifacts = $state<string[]>([]);
	let goalRef = $state("");
	let error = $state("");
	let loading = $state(false);
	let cursor = $state(0);

	async function load(): Promise<void> {
		if (!goalId) return;
		loading = true;
		error = "";
		try {
			const response = await fetch(`${API_BASE}/api/v1/nano-w9/trajectory/${goalId}`);
			if (!response.ok) throw new Error(`${response.status}`);
			const body = (await response.json()) as ReplayPayload;
			states = Array.isArray(body?.states) ? body.states : [];
			artifacts = Array.isArray(body?.artifacts) ? body.artifacts : [];
			goalRef = String(body?.goal_id ?? goalId);
			cursor = 0;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			states = [];
			artifacts = [];
		} finally {
			loading = false;
		}
	}

	function step(delta: number): void {
		if (states.length === 0) return;
		cursor = Math.max(0, Math.min(states.length - 1, cursor + delta));
	}

	function reset(): void {
		cursor = 0;
	}

	$effect(() => {
		if (auto && goalId) void load();
	});

	const visible: ReplayState | null = $derived(
		states.length > 0 ? states[Math.min(cursor, states.length - 1)] : null,
	);
</script>

<section class="trajectory-replay" aria-label="Execution trajectory replay">
	<header>
		<h3>
			<History size={16} /> Trajectory
			{#if states.length > 0}
				<span class="count">{states.length} steps</span>
			{/if}
		</h3>
		<div class="controls">
			<button type="button" onclick={reset} disabled={states.length === 0}>
				<RotateCcw size={14} /> Reset
			</button>
			<button type="button" onclick={load} disabled={loading}>
				{#if loading}<span class="spin"><Loader2 size={14} /></span>{:else}<PlayCircle size={14} />{/if}
				Reload{goalRef ? ` (${goalRef})` : ""}
			</button>
		</div>
	</header>

	{#if error}
		<p class="empty error">Could not load trajectory: {error}</p>
	{:else if states.length === 0}
		<p class="empty">No trajectory recorded for this run yet.</p>
	{:else if visible}
		<nav class="steps" aria-label="Trajectory steps">
			{#each states as state, index (index)}
				<button
					type="button"
					class="step"
					class:active={index === cursor}
					onclick={() => (cursor = index)}
					title={state.command}
				>
					{state.step}
				</button>
			{/each}
		</nav>

		<div class="player">
			<button type="button" onclick={() => step(-1)} disabled={cursor === 0} aria-label="Previous">
				<ChevronRight size={14} style="transform: rotate(180deg)" />
			</button>
			<div class="now">
				<code>{visible.command}</code>
				<span class="badge">{visible.status}</span>
			</div>
			<button
				type="button"
				onclick={() => step(1)}
				disabled={cursor >= states.length - 1}
				aria-label="Next"
			>
				<ChevronRight size={14} />
			</button>
		</div>

		<dl class="detail">
			<dt>Artifacts so far ({visible.artifacts_so_far.length})</dt>
			<dd>
				{#each visible.artifacts_so_far as artifact (artifact)}
					<span class="artifact"><FileText size={12} /> {artifact}</span>
				{/each}
			</dd>
			{#if visible.preview}
				<dt>Preview</dt>
				<dd><code>{visible.preview}</code></dd>
			{/if}
		</dl>
	{/if}
</section>

<style>
	.trajectory-replay {
		border: 1px solid var(--border, #333);
		border-radius: 8px;
		padding: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	h3 {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		margin: 0;
		font-size: 0.95rem;
	}
	.count {
		font-weight: 400;
		font-size: 0.8rem;
		opacity: 0.7;
	}
	.controls {
		display: flex;
		gap: 0.35rem;
	}
	button {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.2rem 0.5rem;
		font-size: 0.8rem;
		border-radius: 4px;
		border: 1px solid var(--border, #333);
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
	.steps {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
	}
	.step {
		min-width: 1.8rem;
		justify-content: center;
	}
	.step.active {
		background: var(--accent, #4f8cff);
		border-color: var(--accent, #4f8cff);
		color: #fff;
	}
	.player {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.now {
		flex: 1;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		min-width: 0;
	}
	.now code {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.badge {
		font-size: 0.72rem;
		padding: 0.05rem 0.35rem;
		border-radius: 999px;
		border: 1px solid var(--border, #333);
		white-space: nowrap;
	}
	.detail {
		margin: 0;
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 0.6rem;
		font-size: 0.82rem;
	}
	dt {
		opacity: 0.7;
	}
	dd {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.artifact {
		display: inline-flex;
		align-items: center;
		gap: 0.2rem;
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		background: rgba(127, 127, 127, 0.12);
	}
	.empty {
		margin: 0;
		font-size: 0.85rem;
		opacity: 0.7;
	}
	.empty.error {
		opacity: 1;
		color: #e5534b;
	}
	.spin {
		animation: trajectory-spin 1s linear infinite;
	}
	@keyframes trajectory-spin {
		to {
			transform: rotate(360deg);
		}
	}
</style>