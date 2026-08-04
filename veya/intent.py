"""LLM-backed intent classifier.

Classifies a user request as ``simple`` (run directly) or ``complex`` (needs
research → plan → execute multi-agent decomposition).

Design principles:
- **Deterministic fast paths first**: length/keyword heuristics decide clear cases,
  avoiding needless LLM calls.
- **LLM arbitrates only the middle ground**: with an API key configured, the LLM
  judges ambiguous requests; without one, it falls back to heuristics entirely
  (offline/test behavior matches the legacy ``_is_simple``).
- **Result caching**: the same text triggers at most one LLM call (dict cache with
  functools.lru_cache semantics).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from veya import llm as _llm

__all__ = ["Intent", "IntentClassifier", "classify_intent"]

# 确定性复杂信号（与旧版 coordinator._is_simple 保持一致的阈值与关键词）
_COMPLEX_KEYWORDS = ("重构", "重構", "refactor", "リファクタ", "全体", "モジュール全")
_LONG_TEXT_THRESHOLD = 200
_SHORT_TEXT_THRESHOLD = 12
_CACHE_MAX = 256

_SYSTEM_PROMPT = (
    "你是一个任务复杂度分类器。判断用户请求是否需要多智能体协作分解"
    "（research→plan→execute 三段流水线）。"
    '仅输出 JSON：{"intent": "simple" 或 "complex", "reason": "一句话理由"}。'
    "simple = 单步即可完成（查信息、改一个文件、跑一条命令）；"
    "complex = 涉及多模块/多文件/系统性重构/需要调研与规划。"
)


class Intent(StrEnum):
    """Task complexity intent (SIMPLE vs COMPLEX)."""

    SIMPLE = "simple"
    COMPLEX = "complex"


class IntentClassifier:
    """Text → Intent classifier (LLM + heuristic two-layer)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        config: dict[str, Any] | None = None,
        cache_size: int = _CACHE_MAX,
    ) -> None:
        self.model = model
        self.config = config
        self._cache: dict[str, Intent] = {}
        self._cache_size = cache_size

    # ── 公开接口 ──────────────────────────────────────────────────────
    async def classify(self, text: str | None) -> Intent:
        """Classify: deterministic fast path → LLM arbitration → heuristic fallback."""
        text = (text or "").strip()
        if not text:
            return Intent.SIMPLE

        # 快速路径：明确信号直接裁决
        if len(text) >= _LONG_TEXT_THRESHOLD:
            return Intent.COMPLEX
        if any(k in text for k in _COMPLEX_KEYWORDS):
            return Intent.COMPLEX
        if len(text) <= _SHORT_TEXT_THRESHOLD:
            return Intent.SIMPLE

        # 缓存命中
        cached = self._cache.get(text)
        if cached is not None:
            return cached

        # LLM 裁决（仅当配置了 API key）
        result = await self._llm_classify(text)
        if result is not None:
            self._cache_put(text, result)
            return result

        # 回落：中间地带按旧行为视为简单（单 squad 并行执行）
        return Intent.SIMPLE

    # ── 内部实现 ──────────────────────────────────────────────────────
    async def _llm_classify(self, text: str) -> Intent | None:
        """Ask the LLM for the intent; returns None on missing key / parse failure (caller falls back)."""
        provider, _model = _llm.get_provider_config(self.config, model=self.model)
        if not _llm.get_api_key(provider, self.config):
            return None

        try:
            resp = await _llm.llm_call(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"用户请求：{text}"},
                ],
                config=self.config,
                model=self.model,
                max_tokens=128,
                timeout=30.0,
            )
        except Exception:
            return None

        content = _extract_content(resp)
        if not content:
            return None
        parsed = _parse_intent_json(content)
        if parsed is None:
            # 极简文本匹配兜底：明确说 complex/复杂 → complex
            lowered = content.lower()
            if any(w in lowered for w in ("complex", "复杂", "复杂任务")):
                return Intent.COMPLEX
            return None
        return parsed

    def _cache_put(self, text: str, intent: Intent) -> None:
        if len(self._cache) >= self._cache_size:
            # 简单逐出：清空重建（LRU 语义在本场景收益有限）
            self._cache.clear()
        self._cache[text] = intent

    # ── 兼容：旧版启发式（供外部调用 / 测试断言） ─────────────────────
    def is_simple_heuristic(self, text: str) -> bool:
        """Keyword/length heuristic mirroring the legacy ``_is_simple``."""
        text = text or ""
        if len(text) >= _LONG_TEXT_THRESHOLD:
            return False
        lowered = text.lower()
        return not any(k in text or k in lowered for k in _COMPLEX_KEYWORDS)


def _extract_content(resp: dict[str, Any]) -> str:
    """Extract assistant text from a normalized OpenAI response."""
    try:
        msg = resp["choices"][0]["message"]
        content = msg.get("content", "")
        return str(content).strip() if content else ""
    except (KeyError, IndexError, TypeError):
        return ""


def _parse_intent_json(content: str) -> Intent | None:
    """Parse ``{"intent": "simple"|"complex"}``, tolerating markdown fences and surrounding noise."""
    import json

    cleaned = content.strip()
    # 剥离 ```json ... ``` 围栏
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("intent")
    if isinstance(raw, str):
        try:
            return Intent(raw.lower())
        except ValueError:
            return None
    return None


# 模块级便捷入口（无状态用法）
async def classify_intent(
    text: str | None,
    *,
    model: str | None = None,
    config: dict[str, Any] | None = None,
) -> Intent:
    """Module-level convenience classifier (new instance per call; no shared cache)."""
    return await IntentClassifier(model=model, config=config).classify(text)
