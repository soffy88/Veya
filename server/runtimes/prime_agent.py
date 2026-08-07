"""server.runtimes.prime_agent — shim: 适配器实现来自主库 oservi.runtime_bridge。"""

from __future__ import annotations

from veya.platform import load as _load

_oservi = _load("oservi")
from oservi.runtime_bridge import (  # noqa: E402
    PrimeAgentRuntime,
    prime_agent_runtime,
)

__all__ = ["PrimeAgentRuntime", "prime_agent_runtime"]
