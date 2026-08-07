"""server.runtimes.pi_bridge — shim: 适配器实现来自主库 oservi.runtime_bridge。"""

from __future__ import annotations

from veya.platform import load as _load

_oservi = _load("oservi")
from oservi.runtime_bridge import (  # noqa: E402
    PiBridgeRuntime,
    pi_bridge,
)

__all__ = ["PiBridgeRuntime", "pi_bridge"]
