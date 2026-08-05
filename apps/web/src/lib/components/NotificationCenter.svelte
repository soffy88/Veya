<script lang="ts">
	/**
	 * NotificationCenter — global toast stack (top-right, fixed), fed by notifyStore's
	 * SSE connection to /legacy/notifications/stream. HITL_REQUIRED toasts don't
	 * auto-dismiss and carry approve/reject buttons that POST /legacy/notifications/{id}/approve.
	 */
	import { Bell, CheckCircle2, LineChart, ShieldAlert, X, XCircle } from "lucide-svelte";
	import { slide } from "svelte/transition";
	import { api } from "$lib/api";
	import { artifactStore } from "$lib/artifacts.svelte";
	import { notifyStore, type VeyaNotification } from "$lib/notifications.svelte";

	async function handleHITL(notif: VeyaNotification, approved: boolean) {
		await api("legacy", `notifications/${notif.id}/approve`, { body: { approved } });
		notifyStore.remove(notif.id);
	}

	// e.g. a grid-search completion toast carries the full synthesis text
	// (including a <veya-artifact> block) in payload.content — see
	// server/chat_coordinator.py's _run_grid_search_background.
	function artifactsIn(notif: VeyaNotification) {
		const content = notif.payload?.content;
		if (typeof content !== "string" || !content.includes("<veya-artifact")) return [];
		return artifactStore.parseArtifactsFromText(content).artifacts;
	}
</script>

<div class="pointer-events-none fixed top-5 right-5 z-50 flex w-96 flex-col gap-3">
	{#each notifyStore.list as notif (notif.id)}
		{@const artifacts = artifactsIn(notif)}
		<div
			transition:slide={{ duration: 300 }}
			class="pointer-events-auto overflow-hidden rounded-xl border bg-terminal-panel shadow-2xl {notif.type === 'HITL_REQUIRED'
				? 'border-status-running/50'
				: 'border-terminal-edge'}"
		>
			<div class="flex gap-3 p-4">
				<div class="mt-0.5 shrink-0">
					{#if notif.type === "HITL_REQUIRED"}
						<ShieldAlert class="size-5 animate-pulse text-status-running" />
					{:else if notif.type === "SUCCESS"}
						<CheckCircle2 class="size-5 text-status-success" />
					{:else if notif.type === "ERROR"}
						<XCircle class="size-5 text-status-error" />
					{:else}
						<Bell class="size-5 text-sky-400" />
					{/if}
				</div>

				<div class="min-w-0 flex-1">
					<div class="flex items-start justify-between gap-2">
						<h4 class="font-mono text-sm font-semibold text-terminal-fg">{notif.title}</h4>
						{#if notif.type !== "HITL_REQUIRED"}
							<button type="button" onclick={() => notifyStore.remove(notif.id)} class="shrink-0 text-terminal-dim transition hover:text-terminal-fg">
								<X class="size-4" />
							</button>
						{/if}
					</div>
					<p class="mt-1 font-mono text-xs leading-relaxed text-terminal-dim">{notif.content}</p>

					{#if artifacts.length > 0}
						<button
							type="button"
							onclick={() => artifactStore.setActive(artifacts[0])}
							class="mt-2 flex items-center gap-1.5 rounded-lg bg-status-success/10 px-2.5 py-1 font-mono text-xs font-medium text-status-success transition hover:bg-status-success/20"
						>
							<LineChart class="size-3.5" /> 查看图表
						</button>
					{/if}

					{#if notif.type === "HITL_REQUIRED"}
						<div class="mt-3 flex gap-2">
							<button
								type="button"
								onclick={() => handleHITL(notif, false)}
								class="flex-1 rounded-lg bg-terminal-bg py-1.5 font-mono text-xs font-medium text-terminal-dim transition hover:bg-status-error/10 hover:text-status-error"
							>
								拦截 (Reject)
							</button>
							<button
								type="button"
								onclick={() => handleHITL(notif, true)}
								class="flex-1 rounded-lg bg-status-running/10 py-1.5 font-mono text-xs font-medium text-status-running transition hover:bg-status-running hover:text-white"
							>
								授权 (Approve)
							</button>
						</div>
					{/if}
				</div>
			</div>

			{#if notif.type === "HITL_REQUIRED"}
				<div class="bg-status-running/10 px-4 py-1.5 text-center font-mono text-[10px] text-status-running/80">
					Agent paused. Awaiting manual override.
				</div>
			{/if}
		</div>
	{/each}
</div>
