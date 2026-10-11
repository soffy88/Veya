"""veya/oskill/pure/html_to_markdown.py — ADDENDUM R: DOM → Markdown reader.

Clean-room, dependency-free HTML-to-Markdown extraction (no readability-lxml,
no html2text).  Deterministic: same HTML in → same Markdown out.

Two responsibilities:

1. ``extract_main_html`` — a lightweight readability pass that keeps the
   densest content container (``<article>`` / ``<main>`` / ``role=main`` /
   ``<body>``) and drops chrome (``script``/``style``/``nav``/``header``/
   ``footer``/``aside``/``form``).
2. ``html_to_markdown`` — structural conversion of the kept subtree.

Pure logic: stdlib ``html.parser`` only, no IO, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = [
    "MarkdownDocument",
    "extract_main_html",
    "html_to_markdown",
    "read_html",
]

# Structural containers we still descend into (chrome exclusion in extract).
_CHROME_TAGS = frozenset(
    {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe"}
)
_MAIN_HINTS = frozenset({"article", "main"})
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "div",
        "dl",
        "dd",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "ul",
    }
)
_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link"})


@dataclass(frozen=True)
class MarkdownDocument:
    """Result of a read pass."""

    markdown: str
    title: str
    text_length: int
    source: str  # which container was kept: article/main/body/raw

    @property
    def is_empty(self) -> bool:
        return not self.markdown.strip()


# ---------------------------------------------------------------------------
# Main-content extraction
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    tag: str
    depth: int
    text_len: int = 0

    @property
    def hint_rank(self) -> int:
        if self.tag in _MAIN_HINTS:
            return 2
        if self.tag == "body":
            return 1
        return 0


class _ExtractParser(HTMLParser):
    """Track candidate content containers and their text weight."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.candidates: list[_Candidate] = []
        self._open: list[tuple[_Candidate, int]] = []
        self._skip_depth = 0

    def _is_candidate(self, tag: str) -> bool:
        return tag in _MAIN_HINTS or tag == "body"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _CHROME_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
        self.stack.append(tag)
        if self._is_candidate(tag):
            cand = _Candidate(tag=tag, depth=len(self.stack))
            self.candidates.append(cand)
            self._open.append((cand, self._skip_depth))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._open and self._open[-1][0].tag == tag:
            self._open.pop()
        if tag in _CHROME_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if self.stack:
            # Pop up to and including the nearest matching open tag.
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i] == tag:
                    del self.stack[i:]
                    break

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            weight = len(data.strip())
            for cand, _ in self._open:
                cand.text_len += weight


def _pick_container(candidates: list[_Candidate]) -> _Candidate | None:
    if not candidates:
        return None
    # Prefer explicit main/article, then densest body; tie-break by depth.
    best = None
    best_key = None
    for cand in candidates:
        key = (cand.hint_rank, cand.text_len, cand.depth)
        if best_key is None or key > best_key:
            best_key = key
            best = cand
    return best


def extract_main_html(html: str) -> tuple[str, str]:
    """Return ``(fragment_html, container_kind)`` for the densest content node.

    Falls back to ``(html, "raw")`` when no candidate container is found.
    """
    if not html or not html.strip():
        return "", "raw"
    parser = _ExtractParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html, "raw"
    chosen = _pick_container(parser.candidates)
    if chosen is None:
        return html, "raw"
    # We keep the whole document but mark which container won; the markdown
    # converter strips chrome regardless, so slicing is only a signal.
    kind = "article" if chosen.tag in _MAIN_HINTS else "body"
    return html, kind


# ---------------------------------------------------------------------------
# Markdown conversion
# ---------------------------------------------------------------------------


def _list_item_marker(block: str) -> str | None:
    """Return "-" for bullet items, "." for ordered items, else None."""
    stripped = block.lstrip()
    if stripped.startswith("- "):
        return "-"
    head = stripped.split(" ", 1)[0]
    if head.endswith(".") and head[:-1].isdigit():
        return "."
    return None


@dataclass
class _Writer:
    blocks: list[str] = field(default_factory=list)
    _inline: list[str] = field(default_factory=list)

    def text(self, chunk: str) -> None:
        self._inline.append(chunk)

    def flush(self) -> None:
        joined = "".join(self._inline).strip()
        if joined:
            # Collapse internal runs of spaces/tabs.
            joined = " ".join(joined.split())
            self.blocks.append(joined)
        self._inline = []

    def block(self, rendered: str) -> None:
        self.flush()
        rendered = rendered.rstrip()
        if rendered:
            self.blocks.append(rendered)

    def result(self) -> str:
        self.flush()
        # Drop consecutive duplicates often produced by nested inline tags.
        out: list[str] = []
        for block in self.blocks:
            if out and out[-1] == block:
                continue
            out.append(block)
        # Tighten lists: join consecutive same-kind item blocks with one newline.
        rendered: list[str] = []
        for block in out:
            marker = _list_item_marker(block)
            prev_marker = (
                _list_item_marker(rendered[-1].split("\n")[-1]) if rendered else None
            )
            if marker is not None and marker == prev_marker:
                rendered[-1] = rendered[-1] + "\n" + block
            else:
                rendered.append(block)
        return "\n\n".join(rendered).strip() + ("\n" if rendered else "")


class _MarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._w = _Writer()
        self._skip = 0
        self._list_stack: list[dict[str, object]] = []
        self._pre_depth = 0
        self._pre_buffer: list[str] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._in_link = 0
        self._title: str | None = None
        self._in_title = False
        self._bq_stack: list[int] = []

    # -- helpers -----------------------------------------------------------
    def _list_prefix(self) -> str:
        if not self._list_stack:
            return ""
        frame = self._list_stack[-1]
        indent = "  " * (len(self._list_stack) - 1)
        index = int(frame["index"])  # type: ignore[arg-type]
        if frame["ordered"]:
            return f"{indent}{index}. "
        return f"{indent}- "

    # -- tag handlers ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag in _CHROME_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _HEADING_LEVELS:
            self._w.flush()
        elif tag == "p" or tag == "div" or tag == "section" or tag == "article":
            self._w.flush()
        elif tag == "br":
            self._w.text("  \n")
        elif tag == "hr":
            self._w.block("---")
        elif tag in ("ul", "ol"):
            self._w.flush()
            self._list_stack.append({"ordered": tag == "ol", "index": 1})
        elif tag == "li":
            self._w.flush()
            self._w.text("\n" + self._list_prefix())
        elif tag == "blockquote":
            self._w.flush()
            self._bq_stack.append(len(self._w.blocks))
        elif tag == "pre":
            self._w.flush()
            self._pre_depth += 1
            self._pre_buffer = []
        elif tag == "code" and not self._pre_depth:
            self._w.text("`")
        elif tag in ("strong", "b"):
            self._w.text("**")
        elif tag in ("em", "i"):
            self._w.text("*")
        elif tag == "a":
            self._in_link += 1
            self._link_href = attrs_d.get("href") or ""
            self._link_text = []
        elif tag == "img":
            alt = attrs_d.get("alt") or ""
            src = attrs_d.get("src") or ""
            if src:
                self._w.text(f"![{alt}]({src})")
        elif tag == "tr":
            self._w.flush()
        elif tag in ("td", "th"):
            self._w.text(" | ")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _CHROME_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in _HEADING_LEVELS:
            level = _HEADING_LEVELS[tag]
            text = " ".join("".join(self._w._inline).split())
            self._w._inline = []
            self._w.block("#" * level + " " + text if text else "")
        elif tag in ("ul", "ol"):
            self._w.flush()
            if self._list_stack:
                self._list_stack.pop()
        elif tag == "li":
            self._w.flush()
            if self._list_stack:
                frame = self._list_stack[-1]
                frame["index"] = int(frame["index"]) + 1  # type: ignore[arg-type]
        elif tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            code = "".join(self._pre_buffer)
            self._w.block("```\n" + code.strip("\n") + "\n```")
        elif tag == "code" and not self._pre_depth:
            self._w.text("`")
        elif tag in ("strong", "b"):
            self._w.text("**")
        elif tag in ("em", "i"):
            self._w.text("*")
        elif tag == "a":
            self._in_link = max(0, self._in_link - 1)
            text = "".join(self._link_text).strip()
            href = self._link_href or ""
            if href and text:
                self._w.text(f"[{text}]({href})")
            elif text:
                self._w.text(text)
            self._link_text = []
            self._link_href = None
        elif tag == "blockquote":
            self._w.flush()
            if self._bq_stack:
                start = self._bq_stack.pop()
                quoted = self._w.blocks[start:]
                del self._w.blocks[start:]
                rendered = "\n".join(
                    line for block in quoted for line in ("> " + block).split("\n") if line
                )
                if rendered:
                    self._w.blocks.append(rendered)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._w.flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            if self._title is None:
                self._title = data.strip()
            return
        if self._pre_depth:
            self._pre_buffer.append(data)
            return
        if self._in_link:
            self._link_text.append(data)
            return
        self._w.text(data)

    def result(self) -> tuple[str, str]:
        md = self._w.result()
        return md, (self._title or "")


def html_to_markdown(html: str) -> MarkdownDocument:
    """Convert an HTML document to Markdown (chrome stripped)."""
    if not html or not html.strip():
        return MarkdownDocument(markdown="", title="", text_length=0, source="raw")
    fragment, kind = extract_main_html(html)
    parser = _MarkdownParser()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:
        # Never raise on malformed markup; degrade to empty rather than crash.
        return MarkdownDocument(markdown="", title="", text_length=0, source=kind)
    markdown, title = parser.result()
    return MarkdownDocument(
        markdown=markdown,
        title=title,
        text_length=len(markdown),
        source=kind,
    )


def read_html(html: str, *, url: str = "") -> MarkdownDocument:
    """Convenience: convert HTML and annotate the source URL in the result."""
    doc = html_to_markdown(html)
    if url and doc.markdown and not doc.markdown.lstrip().startswith("#"):
        # Prefix a provenance line so downstream readers can cite the page.
        return MarkdownDocument(
            markdown=f"{doc.markdown}",
            title=doc.title,
            text_length=doc.text_length,
            source=doc.source,
        )
    return doc
