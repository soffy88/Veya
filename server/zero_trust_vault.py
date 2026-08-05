"""Veya Zero-Trust Vault: 零信任密钥金库(薄适配层)。

3O 单一来源 (§1.4): 引擎本体已固化为主库 oskill.zero_trust_vault.ZeroTrustVault
(基于 oprim._fernet_vault 加密原语 + obase.secrets_store 资源)。
本层职责:
1. 把主库 EventBus 的 vault_hitl 事件桥接到 Veya 的 fire_step(SSE 管道);
2. 保留既有 API(VeyaVault / execute_secure_tool / resolve_approval ...)。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from server.events import fire_step
from veya.platform import obase as _load_obase
from veya.platform import oskill as _load_oskill

_obase = _load_obase()
_oskill = _load_oskill()

_DEFAULT_VAULT_DIR = str(Path.home() / ".veya" / "vault")
_DEFAULT_APPROVAL_TIMEOUT = 300.0


def _bridge_vault_hitl(event: Any) -> None:
    """主库事件总线 → Veya SSE 管道(fire_step)。"""
    p = event.payload
    fire_step(
        {
            "type": "vault_hitl",
            "level": p.get("level", "HITL_REQUIRED"),
            "title": p.get("title", "⚠️ 请求动用生产密钥"),
            "content": p.get("content", ""),
            "payload": {
                "task_id": p.get("task_id"),
                "action": p.get("action"),
                "vault_id": p.get("vault_id"),
            },
        }
    )


_bridge_registered = False


def _ensure_event_bridge() -> None:
    """把 vault_hitl 桥接订阅注册到主库默认事件总线(幂等)。"""
    global _bridge_registered
    if _bridge_registered:
        return
    _obase.event_bus.default_event_bus.subscribe("vault_hitl", _bridge_vault_hitl)
    _bridge_registered = True


class VeyaVault:
    """零信任密钥金库: 加密存储 + HITL 审批 + 密钥隐式注入(委托主库技能)。"""

    def __init__(
        self,
        vault_dir: str | Path | None = None,
        approval_timeout: float = _DEFAULT_APPROVAL_TIMEOUT,
    ):
        vault_dir = Path(vault_dir or os.environ.get("VEYA_VAULT_DIR", _DEFAULT_VAULT_DIR)).expanduser()
        _ensure_event_bridge()
        store = _obase.secrets_store.SecretsStore(vault_dir=vault_dir)
        self._skill = _oskill.zero_trust_vault.ZeroTrustVault(
            store=store, approval_timeout=approval_timeout
        )

    # ── 密钥管理(仅后端/运维调用, 绝不暴露给大模型) ──────────────────
    def set_secret(self, vault_id: str, secret: str) -> str:
        return self._skill.set_secret(vault_id, secret)

    def has_secret(self, vault_id: str) -> bool:
        return self._skill.has_secret(vault_id)

    def list_secret_ids(self) -> list[str]:
        return self._skill.list_secret_ids()

    def delete_secret(self, vault_id: str) -> str:
        return self._skill.delete_secret(vault_id)

    # ── HITL 审批执行 (核心) ─────────────────────────────────────────
    async def execute_secure_tool(
        self,
        tool_name: str,
        intent_args: dict,
        required_vault_id: str,
        physical_tool_callback: Callable[..., Awaitable[str]],
        *,
        timeout: float | None = None,
    ) -> str:
        return await self._skill.execute_secure_tool(
            tool_name=tool_name,
            intent_args=intent_args,
            required_vault_id=required_vault_id,
            physical_tool_callback=physical_tool_callback,
            timeout=timeout,
        )

    def resolve_approval(self, task_id: str, approved: bool) -> bool:
        return self._skill.resolve_approval(task_id, approved)

    def get_pending(self) -> list[dict]:
        return self._skill.get_pending()

    @property
    def pending_approvals(self) -> dict[str, asyncio.Event]:
        return self._skill.pending_approvals

    @property
    def approval_results(self) -> dict[str, bool]:
        return self._skill.approval_results


# 模块级单例(server 复用; 测试注入独立实例)
global_vault = VeyaVault()
