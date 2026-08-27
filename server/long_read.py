"""long_read — 长文分块导航 (骨架 + 按需深入 + 可选局部语义摘要)。

给主脑"理解长文"的能力: 超长文档/网页不再整篇塞上下文, 而是:

1. 默认零 LLM: 分块 → 规则要点提取 (标题/代码符号/关键句) → 大纲 + chunk 索引;
2. 模型按需深入: 传 ``chunk_id`` 读指定块原文 (截断);
3. 可选语义摘要: ``summarize=true`` 只对指定块调 veya.llm 做局部摘要 (绝不全篇)。

支持本地文件与 http(s) URL。纯确定性骨架 + 局部语义增强。
"""

from __future__ import annotations

import re
from pathlib import Path

_HEADING_RE = re.compile(r"^(#{1,6}\s|==+\s*$|--+\s*$|^\d+[\.\)]\s)")
_SYMBOL_RE = re.compile(
    r"^\s*(class\s+\w+|def\s+\w+|async\s+def\s+\w+|fn\s+\w+|func\s+\w+|"
    r"public\s+(?:static\s+)?\w+\s+\w+|private\s+(?:static\s+)?\w+\s+\w+|"
    r"const\s+\w+\s*=|let\s+\w+\s*=|function\s+\w+|"
    r"interface\s+\w+|type\s+\w+\s*=|struct\s+\w+|enum\s+\w+)"
)
_LINE_BUDGET = 250  # 每块行数
_CHAR_BUDGET = 12_000  # 每块字符上限 (超长行截断)


# ── 读取 ──────────────────────────────────────────────────────────────


async def _read_text(path: str) -> tuple[str, str]:
    """返回 (文本, 来源描述)。支持本地文件 / http(s) URL。"""
    p = str(path).strip()
    if p.startswith(("http://", "https://")):
        import httpx

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(p)
            resp.raise_for_status()
            text = resp.text
        return text, p
    fp = Path(p).expanduser()
    if not fp.is_absolute():
        fp = Path.cwd() / fp
    if not fp.exists():
        raise ValueError(f"文件不存在: {p}")
    raw = fp.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc), str(fp)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), str(fp)


def _chunk_lines(lines: list[str], line_budget: int, char_budget: int) -> list[tuple[int, int]]:
    """按行 + 字符双重预算切块, 返回 [(start, end_exclusive)]。"""
    chunks: list[tuple[int, int]] = []
    start = 0
    cur_chars = 0
    for i, line in enumerate(lines):
        cur_chars += len(line) + 1
        if i - start + 1 >= line_budget or cur_chars >= char_budget:
            chunks.append((start, i + 1))
            start = i + 1
            cur_chars = 0
    if start < len(lines):
        chunks.append((start, len(lines)))
    return chunks


def _extract_block(lines: list[str], focus: str = "") -> tuple[list[str], list[str]]:
    """规则要点提取: 标题 + 符号 + 关键句 (focus 关键词过滤时只留命中句)。"""
    headings: list[str] = []
    symbols: list[str] = []
    key_sents: list[str] = []
    focus_kws = (
        [k for k in re.split(r"[\s,，;；、|]+", focus.strip()) if len(k) >= 2] if focus else []
    )
    for raw in lines:
        line = raw.rstrip()
        s = line.strip()
        if not s or len(s) > 400:
            continue
        if _HEADING_RE.search(s) and len(s) <= 120:
            headings.append(s[:120])
            continue
        m = _SYMBOL_RE.match(s)
        if m and len(s) <= 160:
            symbols.append(s[:160])
            continue
        if 24 <= len(s) <= 300 and (s.endswith(("。", ".", ":", "：", ")", "】")) or "→" in s):
            if focus_kws and not any(k in s for k in focus_kws):
                continue
            key_sents.append(s[:300])
    return headings[:8], symbols[:12], key_sents[:8]


def _render_chunk_index(lines: list[str], chunks: list[tuple[int, int]], focus: str) -> str:
    out: list[str] = []
    for ci, (s, e) in enumerate(chunks):
        block = lines[s:e]
        headings, symbols, sents = _extract_block(block, focus)
        head = f"chunk[{ci}] 行 {s + 1}-{e} ({sum(len(x) + 1 for x in block)}字符)"
        if headings:
            head += f" | 标题: {'; '.join(headings[:4])}"
        if symbols:
            head += f" | 符号: {'; '.join(symbols[:6])}"
        if sents:
            head += f"\n    要点: {' / '.join(sents[:3])}"
        out.append(head)
    return "\n".join(out)


def _render_outline(lines: list[str]) -> str:
    """全局大纲: 所有标题 + 顶层符号 (跨块去重)。"""
    headings: list[str] = []
    symbols: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s or len(s) > 120:
            continue
        if _HEADING_RE.search(s):
            if s not in headings:
                headings.append(s)
        elif _SYMBOL_RE.match(s) and len(s) <= 160 and s not in symbols:
            symbols.append(s)
    out: list[str] = []
    if headings:
        out.append("大纲(标题):\n  " + "\n  ".join(headings[:40]))
    if symbols:
        out.append("顶层符号:\n  " + "\n  ".join(symbols[:60]))
    return "\n".join(out)


async def _llm_summarize(chunk_text: str, source: str, focus: str) -> str:
    """对指定块做一次语义摘要 (veya.llm 默认模型; 失败降级为规则要点)。"""
    try:
        from veya.llm import get_provider_config, llm_call

        cfg = get_provider_config()
        prompt = (
            "以下是一段文本(来自: {src})。请用中文提炼要点, 300 字以内, "
            "按「结论/关键信息/待办或疑问」三段。" + (f" 重点关注: {focus}。" if focus else "")
        ).format(src=source[:120])
        resp = await llm_call(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": chunk_text[:8000]},
            ],
            **cfg,
        )
        content = resp.get("content") or ""
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        content = str(content).strip()
        if content and content.lower() not in ("none", "null"):
            return f"[语义摘要]\n{content[:3000]}"
    except Exception:
        pass
    return "[语义摘要不可用, 已退回规则要点]"


# ── 工具实现 ──────────────────────────────────────────────────────────


async def long_read(
    path: str,
    focus: str = "",
    summarize: bool = False,
    chunk_id: int = -1,
    max_chunks: int = 120,
) -> str:
    """分块导航式读长文。

    - 默认: 返回文件信息 + 全局大纲 + chunk 索引 (每块标题/符号/要点);
    - chunk_id >= 0: 返回该块原文 (截断);
    - summarize=True: 对指定块做语义摘要 (chunk_id 缺省 = 第一块);
    - focus: 关键句提取时只保留命中关注词的句子。
    """
    text, source = await _read_text(path)
    lines = text.splitlines()
    if not lines:
        return f"文件为空: {source}"
    chunks = _chunk_lines(lines, _LINE_BUDGET, _CHAR_BUDGET)[:max_chunks]
    if not chunks:
        chunks = [(0, len(lines))]

    if chunk_id >= 0:
        if chunk_id >= len(chunks):
            raise ValueError(f"chunk_id 越界: {chunk_id} (共 {len(chunks)} 块)")
        s, e = chunks[chunk_id]
        block = lines[s:e]
        if summarize:
            summary = await _llm_summarize("\n".join(block), source, focus)
            return (
                f"{source} chunk[{chunk_id}] 行 {s + 1}-{e}\n{summary}\n"
                + "[原文前 4000 字符]\n"
                + "\n".join(block)[:4000]
            )
        return (
            f"{source} chunk[{chunk_id}] 行 {s + 1}-{e} ({len(block)}行)\n"
            + "\n".join(block)[:9000]
        )

    if summarize:
        s, e = chunks[0]
        summary = await _llm_summarize("\n".join(lines[s:e]), source, focus)
        return f"{source} 共 {len(chunks)} 块; 首块语义摘要:\n{summary}\n\n" + _render_chunk_index(
            lines, chunks, focus
        )

    info = (
        f"📄 {source}\n"
        f"总行数 {len(lines)}, 分 {len(chunks)} 块 (每块 ≤{_LINE_BUDGET}行 / ≤{_CHAR_BUDGET}字符)\n"
        f"用法: 深入某块 long_read(path, chunk_id=<n>); 语义摘要加 summarize=true;"
        f' 关注点过滤加 focus="关键词"。\n'
    )
    outline = _render_outline(lines)
    index = _render_chunk_index(lines, chunks, focus)
    return "\n".join(x for x in (info, outline, "chunk 索引:", index) if x)[:18000]


# ── 注册 ──────────────────────────────────────────────────────────────


def wire_master_tools() -> int:
    """把 long_read 注册进 master_tools (幂等)。返回新注册数量。"""
    from server.tool_registry import master_tools

    if master_tools.has("long_read"):
        return 0
    master_tools.register(
        "long_read",
        "分块导航式读取超长文档/网页/代码文件: 先返回全局大纲 + 每块标题/符号/要点索引,"
        "再按 chunk_id 深入原文, 可选 summarize 做局部语义摘要 (focus 过滤关注点)。"
        "适用: 文档/论文/长代码/网页超长放不进上下文时, 先拿骨架再按需深入。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "本地文件路径或 http(s) URL。"},
                "focus": {
                    "type": "string",
                    "description": "可选。关注点关键词, 关键句提取只保留命中句。",
                },
                "summarize": {
                    "type": "boolean",
                    "description": "可选。对目标块做语义摘要 (默认 false 零 LLM)。",
                },
                "chunk_id": {
                    "type": "integer",
                    "description": "可选。>=0 时返回该块原文 (缺省 -1 = 只给骨架+索引)。",
                },
                "max_chunks": {"type": "integer", "description": "可选。最大分块数, 默认 120。"},
            },
            "required": ["path"],
        },
        long_read,
        max_result_chars=18000,
    )
    return 1
