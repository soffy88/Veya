"""server.runtimes.base — shim: 协议与注册助手来自主库 oservi.runtime_bridge。

3O 单一来源 (§1.4): 机制在主库, 主仓只装配。
"""

from __future__ import annotations

from veya.platform import load as _load

_oservi = _load("oservi")
from oservi.runtime_bridge import (  # noqa: E402
    AgentRuntime,
    register_runtime,
    unavailable,
)

__all__ = ["AgentRuntime", "register_runtime", "unavailable"]
