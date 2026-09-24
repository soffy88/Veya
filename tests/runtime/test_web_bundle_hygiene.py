from __future__ import annotations

from pathlib import Path


MARKDOWN = Path("apps/web/src/lib/components/MarkdownBlock.svelte").read_text(encoding="utf-8")


def test_markdown_highlighter_does_not_bundle_full_highlightjs() -> None:
    assert 'import hljs from "highlight.js/lib/core"' in MARKDOWN
    assert 'import hljs from "highlight.js";' not in MARKDOWN
    for language in (
        "bash",
        "cpp",
        "css",
        "go",
        "java",
        "javascript",
        "json",
        "markdown",
        "plaintext",
        "python",
        "rust",
        "sql",
        "typescript",
        "xml",
        "yaml",
    ):
        assert f'hljs.registerLanguage("{language}"' in MARKDOWN
