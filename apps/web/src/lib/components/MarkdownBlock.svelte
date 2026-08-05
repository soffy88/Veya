<script lang="ts">
	/**
	 * MarkdownBlock — full Markdown rendering (Claude/Gemini-grade):
	 * headings / lists / tables / blockquote / inline code / fenced code
	 * with language label + copy button + highlight.js syntax highlighting.
	 */
	import { marked } from "marked";
	import hljs from "highlight.js";
	import "highlight.js/styles/github-dark.css";
	import { Check, Copy } from "lucide-svelte";

	interface Props {
		content: string;
	}

	let { content }: Props = $props();

	let copiedLang = $state<string | null>(null);

	const html = $derived(
		marked.parse(content ?? "", { async: false, breaks: true }) as string,
	);

	function renderCode(lang: string, code: string): string {
		const cls = hljs.getLanguage(lang) ? lang : "plaintext";
		let highlighted: string;
		try {
			highlighted = hljs.highlight(code, { language: cls, ignoreIllegals: true }).value;
		} catch {
			highlighted = hljs.highlightAuto(code).value;
		}
		return `<div class="codeblock" data-lang="${cls}">
			<div class="codeblock-head">
				<span class="codeblock-lang">${cls}</span>
				<button type="button" class="codeblock-copy" data-copy-lang="${cls}" title="复制代码">
					<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
					<span>copy</span>
				</button>
			</div>
			<pre><code class="hljs language-${cls}">${highlighted}</code></pre>
		</div>`;
	}

	const rendered = $derived.by(() => {
		const renderer = new marked.Renderer();
		renderer.code = (token) => {
			const lang = (token.lang ?? "").trim();
			return renderCode(lang, token.text);
		};
		return marked.parse(content ?? "", { renderer, breaks: true, async: false }) as string;
	});

	function onRenderedClick(e: MouseEvent) {
		const btn = (e.target as HTMLElement).closest("button[data-copy-lang]") as HTMLButtonElement | null;
		if (!btn) return;
		// find the sibling <pre> text via the block container
		const block = btn.closest(".codeblock");
		const pre = block?.querySelector("pre code");
		if (!pre) return;
		const code = pre.textContent ?? "";
		void navigator.clipboard?.writeText(code).then(() => {
			copiedLang = btn.dataset.copyLang ?? null;
			setTimeout(() => (copiedLang = null), 1600);
		});
	}
</script>

<div class="markdown-body" onclick={onRenderedClick}>
	{@html rendered}
</div>

{#if copiedLang}
	<div class="pointer-events-none fixed right-6 top-6 z-[100] flex items-center gap-1.5 rounded-lg border border-emerald-900/50 bg-terminal-panel px-3 py-1.5 font-mono text-xs text-emerald-300">
		<Check class="size-3.5" />
		copied
	</div>
{/if}

<style>
	.markdown-body :global(h1),
	.markdown-body :global(h2),
	.markdown-body :global(h3) {
		margin: 0.9em 0 0.4em;
		font-weight: 650;
		line-height: 1.3;
	}
	.markdown-body :global(h1) {
		font-size: 1.35em;
	}
	.markdown-body :global(h2) {
		font-size: 1.15em;
	}
	.markdown-body :global(h3) {
		font-size: 1.02em;
	}
	.markdown-body :global(p) {
		margin: 0.45em 0;
		line-height: 1.7;
	}
	.markdown-body :global(ul),
	.markdown-body :global(ol) {
		margin: 0.45em 0;
		padding-left: 1.4em;
		display: flex;
		flex-direction: column;
		gap: 0.2em;
	}
	.markdown-body :global(li) {
		line-height: 1.65;
	}
	.markdown-body :global(blockquote) {
		margin: 0.6em 0;
		padding: 0.3em 0.9em;
		border-left: 3px solid var(--terminal-edge, #2a2f3a);
		color: var(--terminal-dim, #8b93a7);
	}
	.markdown-body :global(table) {
		margin: 0.6em 0;
		border-collapse: collapse;
		font-size: 0.92em;
	}
	.markdown-body :global(th),
	.markdown-body :global(td) {
		border: 1px solid var(--terminal-edge, #2a2f3a);
		padding: 0.35em 0.7em;
	}
	.markdown-body :global(th) {
		background: var(--terminal-panel, #141822);
	}
	.markdown-body :global(code:not(.hljs)) {
		padding: 0.12em 0.4em;
		border-radius: 4px;
		background: var(--terminal-bg, #0b0e14);
		border: 1px solid var(--terminal-edge, #2a2f3a);
		font-size: 0.88em;
	}
	.markdown-body :global(.codeblock) {
		margin: 0.7em 0;
		border: 1px solid var(--terminal-edge, #2a2f3a);
		border-radius: 10px;
		overflow: hidden;
	}
	.markdown-body :global(.codeblock-head) {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.3em 0.8em;
		border-bottom: 1px solid var(--terminal-edge, #2a2f3a);
		background: var(--terminal-panel, #141822);
		font-family: ui-monospace, monospace;
		font-size: 0.72em;
	}
	.markdown-body :global(.codeblock-lang) {
		color: var(--terminal-dim, #8b93a7);
	}
	.markdown-body :global(.codeblock-copy) {
		display: inline-flex;
		align-items: center;
		gap: 0.35em;
		color: var(--terminal-dim, #8b93a7);
		background: transparent;
		border: none;
		cursor: pointer;
		font-size: 0.72em;
		padding: 0.15em 0.4em;
		border-radius: 4px;
	}
	.markdown-body :global(.codeblock-copy:hover) {
		color: var(--terminal-fg, #d5dbe6);
		background: var(--terminal-bg, #0b0e14);
	}
	.markdown-body :global(pre) {
		margin: 0;
		padding: 0.8em 1em;
		overflow-x: auto;
	}
	.markdown-body :global(pre code.hljs) {
		background: transparent;
		padding: 0;
		font-size: 0.86em;
		line-height: 1.6;
	}
	.markdown-body :global(hr) {
		border: none;
		border-top: 1px solid var(--terminal-edge, #2a2f3a);
		margin: 0.9em 0;
	}
	.markdown-body :global(a) {
		color: #38bdf8;
		text-decoration: none;
	}
	.markdown-body :global(a:hover) {
		text-decoration: underline;
	}
</style>
