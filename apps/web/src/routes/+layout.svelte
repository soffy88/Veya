<script lang="ts">
	import { onMount } from "svelte";
	import favicon from "$lib/assets/favicon.svg";
	import ArtifactRenderer from "$lib/components/ArtifactRenderer.svelte";
	import NotificationCenter from "$lib/components/NotificationCenter.svelte";
	import { artifactStore } from "$lib/artifacts.svelte";
	import { notifyStore } from "$lib/notifications.svelte";
	import { sessionStore } from "$lib/sessionStore.svelte";
	import "../app.css";

	let { children } = $props();

	onMount(() => {
		notifyStore.connect();
		// 已登录状态刷新页面 → 自动拉取云端会话列表 (多端同步)
		if (typeof localStorage !== "undefined" && localStorage.getItem("veya.auth.token")) {
			void sessionStore.syncCloud();
		}
	});
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<NotificationCenter />
{#if artifactStore.activeArtifact}
	<!-- global overlay so a notification toast's "查看图表" works from any section -->
	<div class="fixed inset-y-0 right-0 z-40 w-full max-w-2xl shadow-2xl">
		<ArtifactRenderer />
	</div>
{/if}
{@render children()}
