"""veya.model_routing — 模型路由增强 (freellmapi 机制内化)。

四项机制:
  1. **统一模型 + 组内 fallover** — 一个逻辑模型注册多个 provider 实现
     (register_route), 主 provider 失败 (429/5xx/超时/异常) 自动切组内兄弟,
     带尝试轨迹。
  2. **用量跟踪 + 限额学习** — 每 (provider, model) 的 RPM/RPD/TPM/TPD 计数,
     超限自动判定不可用; 从 provider 错误体/响应头学习限额 (learn_limit)。
  3. **粘性会话** — 会话内锁逻辑模型 (默认 30 分钟), 防多轮对话中途切模型
     引发的幻觉尖峰。
  4. **工具调用救援** — 模型输出纯文本 tool call (```json 块) 自动转
     结构化 OpenAI tool_calls。

零重复: provider 调用细节委托 veya.llm (llm_call/llm_stream); 本模块只管
路由/用量/粘性/救援四层。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

# ── 1. 统一模型 + 组内 fallover ──────────────────────────────────────

_ROUTES: dict[str, list[str]] = {}
"""logical_model → [provider, ...] (组内顺序, 优先在前)。"""


def register_route(logical_model: str, providers: list[str]) -> None:
    """注册一个逻辑模型的 provider 组 (幂等覆盖)。

    Args:
        logical_model: 逻辑模型名 (客户端只认这个)。
        providers: 组内 provider 顺序 (优先在前, 兄弟 fallover 顺序)。

    Example:
        >>> register_route("glm-4-flash", ["zhipu", "dashscope"])
    """
    _ROUTES[logical_model] = list(providers)


def get_route(logical_model: str) -> list[str]:
    """取逻辑模型的 provider 组; 未注册返回空列表。"""
    return list(_ROUTES.get(logical_model, []))


def list_routes() -> dict[str, list[str]]:
    """全部路由 (逻辑模型 → provider 组)。"""
    return {k: list(v) for k, v in _ROUTES.items()}


# ── 2. 用量跟踪 + 限额学习 ───────────────────────────────────────────


@dataclass
class Quota:
    """一个 (provider, model) 的限额 (缺省 None = 不限制)。

    Attributes:
        rpm: 每分钟请求数。
        rpd: 每天请求数。
        tpm: 每分钟 token 数。
        tpd: 每天 token 数。
    """

    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None


@dataclass
class UsageLedger:
    """(provider, model) 维度的用量计数器 + 限额学习。

    计数器滑动窗口 (RPM 用最近 60s, RPD 用最近 24h)。learn_limit 从
    provider 的错误体/响应头解析限额并收紧本地判断。
    """

    limits: dict[tuple[str, str], Quota] = field(default_factory=dict)
    _events: list[dict[str, Any]] = field(default_factory=list)

    def _prune(self, now: float) -> None:
        cutoff = now - 86400
        self._events = [e for e in self._events if e["ts"] > cutoff]

    def record(
        self,
        provider: str,
        model: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        ts: float | None = None,
    ) -> None:
        """记录一次成功调用的用量。"""
        now = ts or time.time()
        self._prune(now)
        self._events.append(
            {
                "provider": provider,
                "model": model,
                "ts": now,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

    def _counts(self, provider: str, model: str, now: float) -> dict[str, int]:
        window_rpm = now - 60
        window_rpd = now - 86400
        rpm = rpd = tpm = tpd = 0
        for e in self._events:
            if e["provider"] != provider or e["model"] != model:
                continue
            tokens = e["prompt_tokens"] + e["completion_tokens"]
            if e["ts"] >= window_rpm:
                rpm += 1
                tpm += tokens
            if e["ts"] >= window_rpd:
                rpd += 1
                tpd += tokens
        return {"rpm": rpm, "rpd": rpd, "tpm": tpm, "tpd": tpd}

    def check(
        self,
        provider: str,
        model: str,
        *,
        quota: Quota | None = None,
        ts: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """是否可用: 未超任何限额。

        Args:
            provider / model: 查询键。
            quota: 显式限额; None 用 learn 到的限额。
            ts: 时间基准。

        Returns:
            (ok, view) — view 含当前计数与命中限额。
        """
        now = ts or time.time()
        limits = quota or self.limits.get((provider, model), Quota())
        counts = self._counts(provider, model, now)
        over: dict[str, int] = {}
        for kind, limit in (
            ("rpm", limits.rpm),
            ("rpd", limits.rpd),
            ("tpm", limits.tpm),
            ("tpd", limits.tpd),
        ):
            if limit is not None and counts[kind] >= limit:
                over[kind] = limit
        return (not over, {"counts": counts, "over": over, "limits": limits})

    def learn_limit(
        self,
        provider: str,
        model: str,
        *,
        response_headers: dict[str, str] | None = None,
        error_body: str | None = None,
    ) -> Quota | None:
        """从响应头/错误体学习限额 (幂等收紧)。

        支持: x-ratelimit-{limit}-{rpm|rpd|tpm|tpd} 头; 错误体里的
        "RPM limit reached" / "TPM limit" 等描述。

        Args:
            provider / model: 限额键。
            response_headers: 响应头。
            error_body: 错误响应体 (JSON 或文本)。

        Returns:
            更新后的 Quota; 未学到返回 None。
        """
        key = (provider, model)
        quota = self.limits.get(key, Quota())
        changed = False
        if response_headers:
            for kind, attr in (("rpm", "rpm"), ("rpd", "rpd"), ("tpm", "tpm"), ("tpd", "tpd")):
                value = response_headers.get(f"x-ratelimit-limit-{attr}")
                if value and value.isdigit():
                    limit_value = int(value)
                    if getattr(quota, kind) is None or limit_value < getattr(quota, kind):
                        setattr(quota, kind, limit_value)
                        changed = True
        if error_body:
            lowered = error_body.lower()
            for kind, pattern in (
                ("rpm", r"rpm.{0,30}?limit\D*(\d+)"),
                ("rpd", r"rpd.{0,30}?limit\D*(\d+)"),
                ("tpm", r"tpm.{0,30}?limit\D*(\d+)"),
                ("tpd", r"tpd.{0,30}?limit\D*(\d+)"),
            ):
                match = re.search(pattern, lowered)
                if match:
                    limit_value = int(match.group(1))
                    if getattr(quota, kind) is None or limit_value < getattr(quota, kind):
                        setattr(quota, kind, limit_value)
                        changed = True
        if changed:
            self.limits[key] = quota
        return quota if changed else None


# ── 3. 粘性会话 ──────────────────────────────────────────────────────

STICKY_TTL_SECONDS = 1800  # 30 分钟


class StickySession:
    """会话内锁逻辑模型: 同一 session 在 TTL 内一直用同一模型。

    防多轮对话中途切模型 (不同模型对同一历史的理解/输出风格差异会引发
    幻觉尖峰)。锁过期或显式 clear 后可重新路由。
    """

    def __init__(self, ttl: float = STICKY_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._locks: dict[str, tuple[str, float]] = {}

    def lock(
        self,
        session_id: str,
        logical_model: str,
        *,
        ttl: float | None = None,
        ts: float | None = None,
    ) -> None:
        """锁定 session → 逻辑模型 (幂等覆盖)。"""
        now = ts or time.time()
        self._locks[session_id] = (logical_model, now + (ttl or self.ttl))

    def get(self, session_id: str, *, ts: float | None = None) -> str | None:
        """取 session 当前锁定的逻辑模型 (未锁/已过期返回 None)。"""
        now = ts or time.time()
        entry = self._locks.get(session_id)
        if entry is None:
            return None
        model, until = entry
        if now > until:
            self._locks.pop(session_id, None)
            return None
        return model

    def clear(self, session_id: str) -> None:
        """解除锁定 (会话结束/用户切换)。"""
        self._locks.pop(session_id, None)

    def snapshot(self) -> dict[str, tuple[str, float]]:
        """当前全部锁定 (调试/审计)。"""
        return dict(self._locks)


# ── 4. 工具调用救援 ──────────────────────────────────────────────────

_TOOL_CALL_RE = re.compile(
    r"```json\s*(\{.*?\})\s*```|"
    r"\{\s*\"(?:name|function)\"\s*:\s*\"[^\"]+\"\s*,.*?arguments.*?\}",
    re.DOTALL,
)


def rescue_tool_calls(content: str) -> list[dict[str, Any]] | None:
    """把纯文本 tool call (```json 块) 转结构化 OpenAI tool_calls。

    支持两种形态:
      * 单工具: {"name": "fn", "arguments": {...}} 或 {"function": {...}}
      * 多工具: {"tool_calls": [{"name": ..., "arguments": ...}, ...]}

    Args:
        content: 模型输出的文本。

    Returns:
        OpenAI 风格 tool_calls 列表; 无工具形态返回 None。

    Example:
        >>> rescue_tool_calls('```json\\n{"name": "f", "arguments": {"a": 1}}\\n```')
        [{'id': ..., 'type': 'function', 'function': {'name': 'f', 'arguments': '{"a": 1}'}}]
    """
    stripped = content.strip()
    if not stripped:
        return None

    # 形态 A: 包裹的 {"tool_calls": [...]}
    try:
        if stripped.startswith("{"):
            data = json.loads(stripped)
            if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
                return _normalize_tool_calls(data["tool_calls"])
    except (json.JSONDecodeError, ValueError):
        pass

    # 形态 B: 代码块内的单个工具
    match = re.search(r"```json\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                if isinstance(data.get("tool_calls"), list):
                    return _normalize_tool_calls(data["tool_calls"])
                single = data.get("function", data)  # {"name","arguments"} 或 {"function":{...}}
                if isinstance(single, dict) and single.get("name"):
                    return _normalize_tool_calls([single])
        except (json.JSONDecodeError, ValueError):
            pass

    # 形态 C: 顶层裸 {"name": ..., "arguments": ...}
    try:
        data = json.loads(stripped)
        if isinstance(data, dict) and data.get("name"):
            return _normalize_tool_calls([data])
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _normalize_tool_calls(raw: list[Any]) -> list[dict[str, Any]]:
    """规范化 raw 工具列表 → OpenAI tool_calls 结构。"""
    calls: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or (item.get("function") or {}).get("name") or ""
        args = item.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        elif not isinstance(args, str):
            args = json.dumps(item.get("arguments", {}), ensure_ascii=False)
        calls.append(
            {
                "id": item.get("id") or f"call_rescued_{i}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )
    return calls


# ── 默认装配 (常用免费/低价逻辑模型路由示例) ────────────────────────


def _ensure_default_routes() -> None:
    if "glm-4-flash" not in _ROUTES:
        register_route("glm-4-flash", ["zhipu", "dashscope"])
    if "deepseek-chat" not in _ROUTES:
        register_route("deepseek-chat", ["deepseek", "openrouter"])
    if "gpt-4o-mini" not in _ROUTES:
        register_route("gpt-4o-mini", ["openai", "openrouter"])


_ensure_default_routes()


__all__ = [
    "STICKY_TTL_SECONDS",
    "Quota",
    "StickySession",
    "UsageLedger",
    "get_route",
    "list_routes",
    "register_route",
    "rescue_tool_calls",
]
