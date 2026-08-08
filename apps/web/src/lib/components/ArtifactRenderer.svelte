<script lang="ts">
	/**
	 * ArtifactRenderer — sandboxed iframe executing an artifact's code. CDN-injected
	 * React 18 + Babel standalone (in-browser JSX compile) + Apache ECharts + Tailwind +
	 * Lucide inside an iframe with sandbox="allow-scripts allow-forms allow-popups"
	 * (deliberately NO allow-same-origin — that's what makes this an actual sandbox,
	 * isolated from the parent page's cookies/localStorage/DOM).
	 */
	import { Code, MonitorPlay, X } from "lucide-svelte";
	import { artifactStore } from "$lib/artifacts.svelte";

	let active = $derived(artifactStore.activeArtifact);
	let viewMode = $state<"preview" | "code">("preview");

	let sandboxHTML = $derived.by(() => {
		if (!active) return "";

		// html / threejs artifact: code 是完整自包含页面 (img2threejs 预览页含
		// three.js CDN + OrbitControls), 直接原样 srcdoc 渲染, 不套 babel 模板
		if (active.type === "html" || active.type === "threejs") {
			return active.code;
		}

		return `
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.tailwindcss.com"><\/script>
    <script crossorigin src="https://unpkg.com/react@18/umd/react.development.js"><\/script>
    <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"><\/script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"><\/script>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"><\/script>
    <script src="https://unpkg.com/lucide@latest"><\/script>
    <style>
        body { font-family: ui-sans-serif, system-ui, sans-serif; background: #ffffff; margin: 0; }
        #root { padding: 1rem; min-height: 100vh; }
    </style>
</head>
<body>
    <div id="root"></div>
    <script type="text/babel" data-type="module">
        ${active.code}
    <\/script>
    <script>
        setTimeout(() => { if (window.lucide) window.lucide.createIcons(); }, 500);
    <\/script>
</body>
</html>`;
	});
</script>

{#if active}
	<div class="flex h-full flex-col overflow-hidden border-l border-terminal-edge bg-terminal-panel shadow-2xl">
		<div class="flex h-14 shrink-0 items-center justify-between border-b border-terminal-edge bg-terminal-bg px-4">
			<div class="flex items-center gap-2 font-mono text-sm font-medium text-terminal-fg">
				<MonitorPlay class="size-5 text-status-success" />
				{active.title}
			</div>
			<div class="flex items-center gap-2">
				<div class="flex rounded-lg bg-terminal-panel p-1">
					<button
						type="button"
						onclick={() => (viewMode = "preview")}
						class="rounded-md px-3 py-1 font-mono text-xs transition {viewMode === 'preview' ? 'bg-terminal-bg font-bold text-status-success shadow' : 'text-terminal-dim'}"
					>
						Preview
					</button>
					<button
						type="button"
						onclick={() => (viewMode = "code")}
						class="rounded-md px-3 py-1 font-mono text-xs transition {viewMode === 'code' ? 'bg-terminal-bg font-bold text-status-success shadow' : 'text-terminal-dim'}"
					>
						Code
					</button>
				</div>
				<div class="mx-2 h-4 w-px bg-terminal-edge"></div>
				<button type="button" onclick={() => artifactStore.close()} class="rounded-md p-1.5 text-terminal-dim transition hover:bg-terminal-bg hover:text-terminal-fg">
					<X class="size-5" />
				</button>
			</div>
		</div>

		<div class="relative flex-1 overflow-hidden bg-white">
			{#if viewMode === "preview"}
				<iframe title="Veya Artifact Sandbox" srcdoc={sandboxHTML} sandbox="allow-scripts allow-forms allow-popups" class="h-full w-full border-none"></iframe>
			{:else}
				<div class="h-full w-full overflow-auto bg-terminal-bg p-4">
					<div class="mb-2 flex items-center gap-1.5 font-mono text-[10px] text-terminal-dim">
						<Code class="size-3" /> {active.title}
					</div>
					<pre class="font-mono text-xs text-status-success"><code>{active.code}</code></pre>
				</div>
			{/if}
		</div>
	</div>
{/if}
