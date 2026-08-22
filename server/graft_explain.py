"""server.graft_explain — Graft 讲解层 (nanonets/graft "资深工程师讲解" 的内化)。

`graft_context.py` 只给结构(定义位置/调用方/被调方), 明确"零 LLM"。nanonets/graft
的差异化卖点不是"能查结构"(oskill.parse_code 已经能做), 是把每个模块渲染成
"这部分是干什么的、怎么跟其他部分关联" 的大白话讲解——agent 直接读, 不用自己
拼线索。本模块补这一层, 刻意跟 graft_context.py 分开(那个模块的"零 LLM"是
既有设计承诺, 不能悄悄破坏): 给定模块源码 + 符号列表, LLM 生成一段讲解;
按内容哈希缓存(落盘 ~/.veya/graft_explain_cache.json, 惯例同 graft_context.py),
"builds that understanding once"——没变的模块不重新讲解, 不重复付费。

失败降级: LLM 不可用/超时/解析失败 → 返回空串。调用方据此跳过这部分, 绝不
拖垮既有的结构化上下文(那部分零 LLM、100% 可靠, 这层只是锦上添花)。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from veya.llm import llm_call

_DEFAULT_CACHE_PATH = Path.home() / ".veya" / "graft_explain_cache.json"
_STUB_MARKER = "LLM provider not configured"

_SYSTEM_PROMPT = """You are a senior engineer explaining a piece of a codebase to a \
teammate who has never seen it. Given a module's source code and the symbols \
defined in it, write a short, plain-English explanation of what this part of \
the system does and how it fits into the rest — not a restatement of the \
code, not a list of function names. Two to four sentences. If the module's \
purpose is unclear from the source alone, say what you can tell and flag the \
gap rather than guessing.

Respond with ONLY the explanation text — no preamble, no markdown headers."""


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class GraftExplainCache:
    """模块级讲解缓存(内容哈希 → 讲解文本), 落盘惯例同 GraftContext。"""

    def __init__(self, cache_path: str | Path | None = None) -> None:
        self._cache: dict[str, tuple[str, str]] = {}  # module → (hash, explanation)
        self._cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for module, entry in raw.items():
            try:
                self._cache[module] = (entry["hash"], entry["explanation"])
            except (KeyError, TypeError):
                continue

    def _save(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {m: {"hash": h, "explanation": e} for m, (h, e) in self._cache.items()}
            self._cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def get(self, module: str, content_hash: str) -> str | None:
        cached = self._cache.get(module)
        if cached and cached[0] == content_hash:
            return cached[1]
        return None

    def put(self, module: str, content_hash: str, explanation: str) -> None:
        self._cache[module] = (content_hash, explanation)
        self._save()


async def explain_module(
    *,
    module: str,
    source: str,
    symbol_names: list[str],
    cache: GraftExplainCache,
    llm_call_fn: Callable[..., Awaitable[dict[str, Any]]] = llm_call,
    max_source_chars: int = 12_000,
) -> str:
    """给一个模块生成/复用讲解。缓存命中直接返回; LLM 不可用/失败返回空串, 不抛异常。"""
    content_hash = _content_hash(source)
    cached = cache.get(module, content_hash)
    if cached is not None:
        return cached

    body = source[:max_source_chars]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"模块: {module}\n定义的符号: {', '.join(symbol_names) or '(无)'}\n\n源码:\n{body}"
            ),
        },
    ]
    try:
        resp = await llm_call_fn(messages)
    except Exception:
        return ""

    content = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
    if not content or _STUB_MARKER in content:
        return ""

    explanation = content.strip()
    cache.put(module, content_hash, explanation)
    return explanation
