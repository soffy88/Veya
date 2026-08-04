#!/usr/bin/env python3
"""G16 — migrate remaining CJK docstrings (intent/telemetry/authz/utils)."""

from __future__ import annotations

import ast
import pathlib

EDITS: dict[str, list[tuple[str, str]]] = {}


def _add(f: str, old: str, new: str) -> None:
    EDITS.setdefault(f, []).append((old, new))


INTENT = "veya/intent.py"
_add(INTENT, '"""任务复杂度意图。"""', '"""Task complexity intent (SIMPLE vs COMPLEX)."""')
_add(
    INTENT,
    '"""文本 → Intent 分类器（LLM + 启发式双层）。"""',
    '"""Text → Intent classifier (LLM + heuristic two-layer)."""',
)
_add(
    INTENT,
    '"""从归一化 OpenAI 响应中提取 assistant 文本。"""',
    '"""Extract assistant text from a normalized OpenAI response."""',
)
_add(
    INTENT,
    '"""解析 ``{"intent": "simple"|"complex"}``；容忍 markdown 围栏与前后缀噪声。"""',
    '"""Parse ``{"intent": "simple"|"complex"}``, tolerating markdown fences and surrounding noise."""',
)
_add(
    INTENT,
    '"""模块级便捷分类（每次新建分类器，不共享缓存）。"""',
    '"""Module-level convenience classifier (new instance per call; no shared cache)."""',
)
_add(
    INTENT,
    '"""分类：确定性快速路径 → LLM 裁决 → 启发式回落。"""',
    '"""Classify: deterministic fast path → LLM arbitration → heuristic fallback."""',
)
_add(
    INTENT,
    '"""调用 LLM 返回意图；无 key / 解析失败返回 None（调用方回落）。"""',
    '"""Ask the LLM for the intent; returns None on missing key / parse failure (caller falls back)."""',
)
_add(
    INTENT,
    '"""简单逐出：清空重建（LRU 语义在本场景收益有限）"""',
    '"""Simple eviction: clear and rebuild (LRU semantics add little value here)."""',
)

TELEMETRY = "veya/obase/telemetry.py"
_add(
    TELEMETRY,
    '"""共享可变 trace 对象（ContextVar 持有引用；并发子 Task 累加同一对象）。"""',
    '"""Shared mutable trace object (ContextVar holds a reference; concurrent child tasks accumulate into the same object)."""',
)
_add(
    TELEMETRY,
    '"""当前 context 的 trace（无则 None；只 get 不 set）。"""',
    '"""The trace bound to the current context (None if absent; get-only, never set)."""',
)
_add(
    TELEMETRY,
    '"""顶层开启 trace（在 context 中 set 引用；结束后须 ``end_trace``/``close``）。"""',
    '"""Begin a top-level trace (binds the reference in the context; must ``end_trace``/``close`` when done)."""',
)
_add(
    TELEMETRY,
    '"""关闭 trace 并 emit 终结事件（须先 emit 再改 status —— StreamingManager 同款）。"""',
    '"""Close the trace and emit a terminal event (emit before mutating status — same as StreamingManager)."""',
)
_add(
    TELEMETRY,
    '"""写当前 trace 的 steps 并转发给注入的 emitter（不 raise，与 on_step 语义一致）。"""',
    '"""Write the current trace steps and forward them to the injected emitter (never raises, matching on_step semantics)."""',
)
_add(
    TELEMETRY,
    '"""服务层注入 on_step 回调（如 ``server.events.fire_step``）。返回 reset token。"""',
    '"""Service layer injects an on_step callback (e.g. ``server.events.fire_step``). Returns a reset token."""',
)
_add(
    TELEMETRY,
    '''"""Sync/async 通用 span 装饰器：自动记 enter/exit/error + duration。

    执行模型由本性决定（§0.2）：async def → await 包装；sync def → 同步包装。
    异常不吞（记录 status=failed 后重新 raise）；CancelledError 记 cancelled 后重抛。
    """''',
    '''"""Sync/async universal span decorator: records enter/exit/error + duration automatically.

    Execution model follows the callable kind (§0.2): async def → awaited wrapper;
    sync def → synchronous wrapper. Exceptions are never swallowed (status=failed is
    recorded, then re-raised); CancelledError is recorded as cancelled and re-raised.
    """''',
)
_add(
    TELEMETRY,
    '"""参数摘要（防 PII/大对象泄漏进 trace，§5.5.1 精神）。"""',
    '"""Argument summary (prevents PII/large objects from leaking into traces, §5.5.1 spirit)."""',
)
_add(
    TELEMETRY,
    '"""追加一行 JSON（事件顺序可复现）。单源：读取复用 compat.jsonl_latest。"""',
    '"""Append one JSON line (reproducible event order). Single source: reads reuse compat.jsonl_latest."""',
)
_add(
    TELEMETRY,
    '"""读取最新一条 trace（委托 compat.jsonl_latest —— §1.4 单源，不重复实现）。"""',
    '"""Read the latest trace (delegates to compat.jsonl_latest — §1.4 single source, no reimplementation)."""',
)
_add(
    TELEMETRY,
    '"""追加事件（list.append 而非 set —— C1 铁律的 decision_trail 同款）。"""',
    '"""Append an event (list.append, not set — same as the C1 iron-rule decision_trail)."""',
)

AUTHZ = "veya/obase/authz.py"
_add(
    AUTHZ,
    '"""一次待确认/已决权限请求。"""',
    '"""A single permission request (pending or decided)."""',
)
_add(
    AUTHZ,
    '''"""按顺序匹配 ``allow:``/``deny:``/``ask:`` 规则；返回命中规则的 verb 或 None。

    通配 ``allow:*`` 匹配任意 action。规则顺序优先（先匹配先生效）。
    """''',
    '''"""Match ``allow:``/``deny:``/``ask:`` rules in order; return the matched verb or None.

    A wildcard ``allow:*`` matches any action. Rule order wins (first match takes effect).
    """''',
)
_add(
    AUTHZ,
    '''"""评估权限 → 决策 dict（ALLOw/DENY/PENDING 三态）。

    返回结构（omodul 风格 status/error 字段，§5.3）：
        {"decision": "allow|deny|pending", "action": ..., "resource": ...,
         "persona": ..., "matched_rule": ...|None, "status": "decided"|"pending",
         "error": None}
    """''',
    '''"""Evaluate a permission → decision dict (ALLOW/DENY/PENDING three states).

    Return shape (omodul-style status/error fields, §5.3):
        {"decision": "allow|deny|pending", "action": ..., "resource": ...,
         "persona": ..., "matched_rule": ...|None, "status": "decided"|"pending",
         "error": None}
    """''',
)
_add(
    AUTHZ,
    '"""persona 默认规则（与 config/permissions.py 的 _RULES_BY_PERSONA 对齐）。"""',
    '"""Per-persona default rules (aligned with config/permissions.py _RULES_BY_PERSONA)."""',
)
_add(
    AUTHZ,
    '''"""把 PENDING 挂起为可 approve/deny 的请求，支持同步/异步等待。

    - 规则决定 ALLOW/DENY → 直接返回，不打扰用户。
    - 规则 PENDING（ask: 或无匹配）→ 生成 ``PermissionRequest`` 挂起，
      经 ``on_pending`` 回调通知（CLI → input()；HTTP → SSE/轮询），
      调用方 ``await_decision`` 阻塞到 approve/deny 或超时。
    """''',
    '''"""Suspend PENDING requests into approve/deny-able requests with sync/async waiting.

    - Rules yielding ALLOW/DENY return immediately without disturbing the user.
    - Rules yielding PENDING (ask: or no match) create a suspended ``PermissionRequest``,
      notified via the ``on_pending`` callback (CLI → input(); HTTP → SSE/polling);
      the caller blocks on ``await_decision`` until approve/deny or timeout.
    """''',
)
_add(
    AUTHZ,
    '"""评估并在必要时交互确认。``wait=False`` 时 PENDING 直接返回（挂起）。"""',
    '"""Evaluate and interactively confirm when needed. With ``wait=False`` a PENDING decision returns directly (suspended)."""',
)
_add(
    AUTHZ,
    '"""人工批准（同步，供 CLI/HTTP 回调）。"""',
    '"""Human approval (synchronous; for CLI/HTTP callbacks)."""',
)
_add(
    AUTHZ,
    '"""超时未决请求自动 DENY（安全默认）。返回处理数。"""',
    '"""Auto-DENY stale pending requests (safe default). Returns the number processed."""',
)
_add(
    AUTHZ,
    '"""阻塞到 approve/deny 或超时（超时 → DENY，安全默认）。"""',
    '"""Block until approve/deny or timeout (timeout → DENY, the safe default)."""',
)

UTILS = "veya/utils.py"
_add(
    UTILS,
    '"""compat 兼容方法:按 token/成本记录(§1.4 单源——compat.CostTracker 别名本类)。"""',
    '"""compat-compatible method: record token/cost (§1.4 single source — compat.CostTracker aliases this class)."""',
)
_add(
    UTILS,
    '"""compat 兼容方法:序列化视图。"""',
    '"""compat-compatible method: serializable view."""',
)

# veya/obase/__init__.py module docstring if CJK
_OBS = "veya/obase/__init__.py"


def main() -> int:
    total = 0
    for path_str, pairs in EDITS.items():
        p = pathlib.Path(path_str)
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        raw_docstrings = []
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
            ):
                seg = ast.get_source_segment(src, node.body[0])
                if seg:
                    raw_docstrings.append(seg)
        for old, new in pairs:
            if old not in raw_docstrings:
                print(f"[WARN] not found in {path_str}: {old[:50]!r}")
                continue
            src = src.replace(old, new)
            total += 1
        p.write_text(src, encoding="utf-8")
    print(f"translated {total} docstrings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
