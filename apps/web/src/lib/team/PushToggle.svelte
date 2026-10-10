<script lang="ts">
	/**
	 * PushToggle — turn this device's Web Push on or off.
	 *
	 * Rendered on the bot / group pages, because that is where a user notices
	 * the point: "I want to know when a bot answers while the tab is closed."
	 * Every state is a sentence (unsupported browser, denied permission,
	 * backend without pywebpush) — a silently dead button is worse than an
	 * honest reason.
	 */
	import { onMount } from "svelte";
	import { Bell, BellRing, LoaderCircle } from "lucide-svelte";
	import { disablePush, enablePush, pushState, type PushState } from "$lib/team/push";
	import { actionClass } from "$lib/team/ui";

	let push = $state<PushState>({ permission: "", subscribed: false, serverEnabled: false, detail: "" });
	let busy = $state(false);
	let error = $state("");

	const label = $derived(push.subscribed ? "推送已开启" : "开启推送");
	const reason = $derived(error || push.detail);

	async function load(): Promise<void> {
		push = await pushState();
		error = "";
	}

	async function toggle(): Promise<void> {
		busy = true;
		error = "";
		try {
			push = push.subscribed ? await disablePush() : await enablePush();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
			await load();
		} finally {
			busy = false;
		}
	}

	onMount(() => {
		void load();
	});
</script>

{#if push.permission !== "unsupported"}
	<button
		class={actionClass}
		class:text-sky-300={push.subscribed}
		onclick={() => void toggle()}
		disabled={busy}
		title={label}
		aria-pressed={push.subscribed}
	>
		{#if busy}
			<LoaderCircle size={16} class="animate-spin" />
		{:else if push.subscribed}
			<BellRing size={16} />
		{:else}
			<Bell size={16} />
		{/if}
		<span class="hidden sm:inline">{label}</span>
	</button>
{/if}
{#if reason}
	<p class="text-xs text-amber-300/80" data-testid="push-hint">{reason}</p>
{/if}
