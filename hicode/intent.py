"""LLM 意图分类器。

将用户请求分类为 ``simple``（直接执行）或 ``complex``（需要
research → plan → execute 多智能体分解）。

设计原则：
- **确定性快速路径优先**：长度 / 关键词启发式先裁决明确案例，避免无谓的 LLM 调用。
- **LLM 只裁决中间地带**：配置了 API key 时，用 LLM 判断模糊请求；未配置时
  完全回落启发式（离线 / 测试环境行为与旧版 ``_is_simple`` 一致）。
- **结果缓存**：相同文本只调用一次 LLM（``functools.lru_cache`` 语义的字典缓存）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from hicode import llm as _llm

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
    """任务复杂度意图。"""

    SIMPLE = "simple"
    COMPLEX = "complex"


class IntentClassifier:
    """文本 → Intent 分类器（LLM + 启发式双层）。"""

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
        """分类：确定性快速路径 → LLM 裁决 → 启发式回落。"""
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
        """调用 LLM 返回意图；无 key / 解析失败返回 None（调用方回落）。"""
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
    """从归一化 OpenAI 响应中提取 assistant 文本。"""
    try:
        msg = resp["choices"][0]["message"]
        content = msg.get("content", "")
        return str(content).strip() if content else ""
    except (KeyError, IndexError, TypeError):
        return ""


def _parse_intent_json(content: str) -> Intent | None:
    """解析 ``{"intent": "simple"|"complex"}``；容忍 markdown 围栏与前后缀噪声。"""
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
    """模块级便捷分类（每次新建分类器，不共享缓存）。"""
    return await IntentClassifier(model=model, config=config).classify(text)
