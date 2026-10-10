<script lang="ts">
	import { Eye, MousePointer2, Send, X, ChevronDown, Code2, Image, FileText, LayoutGrid, Maximize2 } from "lucide-svelte";
	import { api, API_BASE } from "$lib/api";
	import { authHeader } from "$lib/auth.svelte";
	import { sessionStore } from "$lib/sessionStore.svelte";

	interface BrowserState {
		session_id?: string;
		current_url?: string;
		control_state?: "AGENT_CONTROL" | "HUMAN_CONTROL";
		snapshot?: Record<string, unknown>;
		version?: number;
	}

	let { browser, taskId, sessionId }: { browser: BrowserState; taskId: string; sessionId: string } = $props();

	let designMode = $state(false);
	let captureMode = $state(false);
	let capturedElement: DesignCapture | null = $state(null);
	let capturePreviewOpen = $state(false);
	let instruction = $state("");
	let sending = $state(false);
	let sendError = $state("");
	let iframeEl = $state<HTMLIFrameElement | null>(null);
	let iframeReady = $state(false);
	let useIframe = $state(false);
	let designScriptInjected = $state(false);

	// For server-side browser (3O)
	let browserUrl = $derived(browser.current_url || "about:blank");
	let controlState = $derived(browser.control_state || "AGENT_CONTROL");
	let hasBrowserSession = $derived(!!browser.session_id);

	// Design capture types (mirror server types)
	interface ComputedStyles {
		boxModel: Record<string, string>;
		display: string;
		flex: Record<string, string> | null;
		grid: Record<string, string> | null;
		font: Record<string, string>;
		color: Record<string, string>;
		position: Record<string, string>;
	}

	interface BoundingBox {
		x: number;
		y: number;
		width: number;
		height: number;
	}

	interface SourceLocation {
		type: "veya" | "react" | "generic" | "unknown";
		file: string;
		line: number;
		column?: number;
		rawAttribute?: string;
	}

	interface DesignCapture {
		outerHTML: string;
		selectorPath: string;
		computedStyles: ComputedStyles;
		boundingBox: BoundingBox;
		elementScreenshotBase64: string;
		pageUrl: string;
		pageTitle: string;
		sourceLocation?: SourceLocation;
	}

	interface BrowserActionResult {
		outerHTML: string;
		selectorPath: string;
		computedStyles: ComputedStyles;
		boundingBox: BoundingBox;
		elementScreenshotBase64: string;
		pageUrl: string;
		pageTitle: string;
		sourceLocation?: SourceLocation;
		error?: string;
	}

	// Initialize iframe for same-origin dev server
	function initIframe() {
		if (!iframeEl) return;
		const url = browserUrl || "about:blank";
		// Check if same-origin (we can only capture DOM on same-origin)
		try {
			const iframeOrigin = new URL(iframeEl.src).origin;
			const pageOrigin = new URL(url).origin;
			useIframe = iframeOrigin === pageOrigin;
		} catch {
			useIframe = false;
		}
		iframeEl.src = url;
	}

	function onIframeLoad() {
		iframeReady = true;
		if (designMode && useIframe && !designScriptInjected) {
			injectDesignScript();
		}
	}

	function injectDesignScript() {
		if (!iframeEl?.contentWindow || designScriptInjected) return;
		try {
			// Inject the designModeCapture script
			const script = iframeEl.contentWindow.document.createElement("script");
			script.src = `${import.meta.env.BASE_URL}designModeCapture.js`;
			iframeEl.contentWindow.document.head.appendChild(script);
			designScriptInjected = true;

			// Listen for capture responses
			window.addEventListener("message", handleIframeMessage);
		} catch (e) {
			console.warn("Failed to inject design script:", e);
			useIframe = false;
		}
	}

	function handleIframeMessage(event: MessageEvent) {
		if (event.source !== iframeEl?.contentWindow) return;
		const data = event.data;
		if (!data?.type) return;

		switch (data.type) {
			case "ready":
				// Script loaded, activate design mode in iframe
				if (captureMode) {
					event.source?.postMessage({ type: "activate" }, "*");
				}
				break;
			case "captureResponse":
				if (data.capture) {
					capturedElement = data.capture;
					capturePreviewOpen = true;
					captureMode = false;
					// Deactivate highlight in iframe
					event.source?.postMessage({ type: "deactivate" }, "*");
				} else if (data.error) {
					sendError = `Capture failed: ${data.error}`;
				}
				break;
		}
	}

	function toggleDesignMode() {
		designMode = !designMode;
		if (!designMode) {
			captureMode = false;
			capturePreviewOpen = false;
			capturedElement = null;
			instruction = "";
			// Deactivate in iframe
			if (iframeEl?.contentWindow) {
				iframeEl.contentWindow.postMessage({ type: "deactivate" }, "*");
			}
		}
	}

	function startCapture() {
		if (!designMode) return;
		captureMode = true;
		capturedElement = null;
		capturePreviewOpen = false;
		sendError = "";

		if (useIframe && iframeEl?.contentWindow) {
			iframeEl.contentWindow.postMessage({ type: "activate" }, "*");
		} else {
			// Server-side browser: we'll use the browser_design_capture action
			// The actual capture happens on click via the preview flow
			sendError = "Server-side browser: click an element in the browser view, then use 'Capture Element' below";
		}
	}

	async function captureElementServerSide(selector: string) {
		if (!browser.session_id) {
			sendError = "No active browser session";
			return;
		}
		sending = true;
		sendError = "";
		try {
			const result = await api("gateway", `api/v1/workbench/${encodeURIComponent(taskId)}/browser/action`, {
				method: "POST",
				body: {
					action: "browser_design_capture",
					browser_session_id: browser.session_id,
					selector,
				},
			});
			if (result.ok && result.data) {
				const data = result.data as BrowserActionResult;
				capturedElement = {
					outerHTML: data.outerHTML,
					selectorPath: data.selectorPath,
					computedStyles: data.computedStyles,
					boundingBox: data.boundingBox,
					elementScreenshotBase64: data.elementScreenshotBase64,
					pageUrl: data.pageUrl,
					pageTitle: data.pageTitle,
					sourceLocation: data.sourceLocation,
				};
				capturePreviewOpen = true;
				captureMode = false;
			} else {
				sendError = (result.data as BrowserActionResult | undefined)?.error || "Capture failed";
			}
		} catch (e) {
			sendError = e instanceof Error ? e.message : "Capture failed";
		} finally {
			sending = false;
		}
	}

	async function sendToAgent() {
		if (!capturedElement) return;
		sending = true;
		sendError = "";
		try {
			const res = await fetch(`${API_BASE}/api/v1/design/capture`, {
				method: "POST",
				headers: { "content-type": "application/json", ...authHeader() },
				body: JSON.stringify({
					capture: capturedElement,
					instruction,
					session_id: sessionId,
					task_id: taskId,
				}),
			});
			if (!res.ok) {
				const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
				throw new Error(typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail));
			}
			// Success - close preview, reset
			capturePreviewOpen = false;
			capturedElement = null;
			instruction = "";
			captureMode = false;
			// Notify parent (chat console) that a design capture was sent
			window.dispatchEvent(new CustomEvent("veya:design-capture-sent", { detail: { taskId } }));
		} catch (e) {
			sendError = e instanceof Error ? e.message : "Failed to send";
		} finally {
			sending = false;
		}
	}

	function closePreview() {
		capturePreviewOpen = false;
		capturedElement = null;
		instruction = "";
	}

	function formatSourceLocation(loc?: SourceLocation): string {
		if (!loc || loc.type === "unknown" || !loc.file) return "Unknown";
		return `${loc.file}:${loc.line}`;
	}

	function formatStyles(styles: ComputedStyles): string {
		const parts: string[] = [];
		if (styles.display) parts.push(`display: ${styles.display}`);
		if (styles.boxModel?.width) parts.push(`width: ${styles.boxModel.width}`);
		if (styles.boxModel?.height) parts.push(`height: ${styles.boxModel.height}`);
		if (styles.font?.family) parts.push(`font: ${styles.font.family}`);
		if (styles.color?.color) parts.push(`color: ${styles.color.color}`);
		return parts.join("; ") || "—";
	}
</script>

{#if hasBrowserSession}
<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
	<div class="mb-3 flex items-center gap-2">
		<Eye class="size-4 text-cyan-400" />
		<h2 class="text-sm font-semibold">浏览器</h2>
		{#if designMode}
			<span class="ml-auto inline-flex items-center gap-1 rounded-md border border-cyan-500/40 bg-cyan-500/10 px-2 py-0.5 text-[10px] font-medium text-cyan-300">
				<MousePointer2 class="size-2.5" /> Design Mode
			</span>
		{/if}
	</div>

	<div class="space-y-2">
		<div class="rounded-lg border border-white/5 bg-black/20 overflow-hidden">
			<div class="px-2 py-1.5 text-xs text-terminal-dim border-b border-white/5 flex items-center gap-2">
				<span class="truncate flex-1">{browserUrl}</span>
				<span class="px-1.5 py-0.5 rounded text-[10px] {controlState === 'HUMAN_CONTROL' ? 'bg-amber-500/20 text-amber-300' : 'bg-sky-500/20 text-sky-300'}">
					{controlState === 'HUMAN_CONTROL' ? '你' : 'Veya'}
				</span>
			</div>
			{#if useIframe && iframeReady}
				<iframe
					bind:this={iframeEl}
					onload={onIframeLoad}
					class="w-full h-96 border-0 bg-white"
					sandbox="allow-scripts allow-same-origin allow-forms allow-pointer-lock"
				></iframe>
			{:else if browserUrl && browserUrl !== "about:blank"}
				<div class="h-96 flex items-center justify-center bg-black/30">
					<div class="text-center text-terminal-dim p-4">
						<p class="mb-2">Server-side browser (3O)</p>
						<p class="text-xs">URL: {browserUrl}</p>
						<p class="text-xs mt-2">Design Mode uses server-side element capture</p>
					</div>
				</div>
			{:else}
				<div class="h-96 flex items-center justify-center bg-black/30 text-terminal-dim">
					No active page. Start a browser session to enable Design Mode.
				</div>
			{/if}
		</div>

		<div class="flex flex-wrap gap-2">
			<button
				type="button"
				class="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-2 text-xs text-terminal-dim hover:bg-white/5 hover:text-terminal-fg"
				onclick={toggleDesignMode}
			>
				<MousePointer2 class="size-3.5" />
				{designMode ? "退出 Design Mode" : "进入 Design Mode"}
			</button>

			{#if designMode && !captureMode}
				<button
					type="button"
					class="inline-flex items-center gap-1.5 rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-300 hover:bg-cyan-500/20"
					onclick={startCapture}
				>
					<MousePointer2 class="size-3.5" />
					开始捕获元素
				</button>
			{/if}

			{#if designMode && captureMode && !useIframe}
				<div class="flex items-center gap-2 text-xs text-terminal-dim">
					<kbd class="px-1.5 py-0.5 rounded bg-white/5 border border-white/10">点击浏览器视图中的元素</kbd>
					<span>然后在下方输入选择器</span>
				</div>
				<div class="flex gap-2">
					<input
						type="text"
						placeholder="CSS 选择器 (如: button#submit)"
						class="flex-1 min-h-10 rounded-lg border border-white/10 bg-black/30 px-2 text-xs text-terminal-fg outline-none focus:border-cyan-500/50"
						onkeydown={(e) => { if (e.key === "Enter") captureElementServerSide((e.currentTarget as HTMLInputElement).value); }}
					/>
					<button
						type="button"
						class="inline-flex items-center gap-1 rounded-lg border border-cyan-500/30 px-3 py-2 text-xs text-cyan-300 hover:bg-cyan-500/20 disabled:opacity-40"
						onclick={() => captureElementServerSide((document.querySelector('input[placeholder="CSS 选择器"]') as HTMLInputElement)?.value || "")}
						disabled={sending}
					>
						<Image class="size-3.5" /> 捕获
					</button>
				</div>
			{/if}
		</div>
	</div>

	{#if capturePreviewOpen && capturedElement}
	<div class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur" onclick={closePreview}>
		<div class="w-full max-w-3xl max-h-[85vh] overflow-hidden rounded-xl border border-terminal-edge bg-terminal-panel flex flex-col" onclick={(e) => e.stopPropagation()}>
			<div class="flex items-center justify-between border-b border-white/10 px-4 py-3">
				<h3 class="text-sm font-semibold">元素捕获预览</h3>
				<button type="button" class="rounded-lg p-1 text-terminal-dim hover:text-terminal-fg hover:bg-white/5" onclick={closePreview}><X class="size-4" /></button>
			</div>

			<div class="flex-1 overflow-auto p-4 space-y-4">
				<div class="grid gap-2 sm:grid-cols-2">
					<div class="rounded-lg border border-white/5 bg-black/20 p-3">
						<div class="text-[10px] uppercase tracking-wide text-terminal-dim mb-1">页面</div>
						<div class="text-xs truncate">{capturedElement.pageTitle}</div>
						<div class="text-[10px] text-terminal-dim truncate mt-1">{capturedElement.pageUrl}</div>
					</div>
					<div class="rounded-lg border border-white/5 bg-black/20 p-3">
						<div class="text-[10px] uppercase tracking-wide text-terminal-dim mb-1">选择器</div>
						<div class="text-xs font-mono truncate">{capturedElement.selectorPath}</div>
					</div>
					<div class="rounded-lg border border-white/5 bg-black/20 p-3">
						<div class="text-[10px] uppercase tracking-wide text-terminal-dim mb-1">源文件</div>
						<div class="text-xs font-mono text-sky-300">{formatSourceLocation(capturedElement.sourceLocation)}</div>
					</div>
					<div class="rounded-lg border border-white/5 bg-black/20 p-3">
						<div class="text-[10px] uppercase tracking-wide text-terminal-dim mb-1">尺寸</div>
						<div class="text-xs">
							{capturedElement.boundingBox.width.toFixed(0)} × {capturedElement.boundingBox.height.toFixed(0)}px
							@ ({capturedElement.boundingBox.x.toFixed(0)}, {capturedElement.boundingBox.y.toFixed(0)})
						</div>
					</div>
				</div>

				{#if capturedElement.elementScreenshotBase64}
				<div class="rounded-lg border border-white/5 bg-black/20 p-3">
					<div class="text-[10px] uppercase tracking-wide text-terminal-dim mb-2">截图</div>
					<img src="data:image/jpeg;base64,{capturedElement.elementScreenshotBase64}" alt="Element screenshot" class="max-w-full h-auto rounded border border-white/10" />
				</div>
				{/if}

				<div class="rounded-lg border border-white/5 bg-black/20 p-3">
					<div class="text-[10px] uppercase tracking-wide text-terminal-dim mb-2">计算样式</div>
					<pre class="font-mono text-[10px] text-terminal-dim overflow-auto max-h-40">{formatStyles(capturedElement.computedStyles)}</pre>
				</div>

				<div class="rounded-lg border border-white/5 bg-black/20 p-3">
					<div class="text-[10px] uppercase tracking-wide text-terminal-dim mb-2">OuterHTML (trimmed)</div>
					<pre class="font-mono text-[10px] text-terminal-dim overflow-auto max-h-60">{capturedElement.outerHTML}</pre>
				</div>
			</div>

			<div class="border-t border-white/10 p-4 space-y-3">
				<div>
					<label class="block text-xs text-terminal-dim mb-1">给 Agent 的指令 (可选)</label>
					<textarea
						bind:value={instruction}
						rows="3"
						placeholder="例如: 这个按钮的点击事件没有触发，请帮我调试"
						class="w-full min-h-[80px] rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-terminal-fg outline-none focus:border-cyan-500/50 resize-none"
					></textarea>
				</div>

				{#if sendError}
				<div class="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{sendError}</div>
				{/if}

				<div class="flex justify-end gap-2">
					<button type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-2 text-xs text-terminal-dim hover:bg-white/5 hover:text-terminal-fg" onclick={closePreview}>取消</button>
					<button
						type="button"
						class="inline-flex items-center gap-1.5 rounded-lg bg-sky-500/90 px-3 py-2 text-xs text-black hover:bg-sky-400 disabled:opacity-40"
						onclick={sendToAgent}
						disabled={sending}
					>
						<Send class="size-3.5" />
						{sending ? "发送中…" : "发送给 Agent"}
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}
</section>
{/if}