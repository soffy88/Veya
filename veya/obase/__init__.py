"""veya/obase — the 3O infrastructure layer (§3/§7, orthogonal to the business layers).

obase runs parallel to the three 3O layers (oprim/oskill/omodul): those layers own
how business computes/orchestrates/produces, while obase owns how we call external
LLMs / compute cost / rate-limit / fetch credentials / authorize / telemetry / sandbox.

Dependency direction (§7.4, MUST):
    ✅ omodul/oskill/oprim/service layers → obase
    ❌ obase → any 3O layer or project business layer (veya.tools, server, agents, config...)

This package may only import: stdlib, third-party libraries, ``veya.errors``,
``veya.compat`` and internal ``veya.obase`` modules (enforced by
``scripts/check_obase_no_reverse_dep.py``).

§2.5: the library exposes ``__manifest__`` (element list + signature + version) for
catalog queries and reuse decisions.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

# 元素清单：name -> {signature 摘要, 引入版本}
__manifest__: dict[str, dict[str, Any]] = {
    "telemetry.TraceContext": {
        "signature": "begin_trace(name, *, trace_id=None, meta=None)",
        "since": "0.1.0",
    },
    "telemetry.traced": {"signature": "@traced(name=None)", "since": "0.1.0"},
    "telemetry.emit": {"signature": "emit(event, *, force=False)", "since": "0.1.0"},
    "telemetry.jsonl_write": {"signature": "jsonl_write(trace, *, path)", "since": "0.1.0"},
    "telemetry.latest_trace": {"signature": "latest_trace(*, path)", "since": "0.1.0"},
    "authz.evaluate_permission": {
        "signature": "evaluate_permission(action, *, resource=None, persona='build', rules=None)",
        "since": "0.1.0",
    },
    "authz.InteractivePermissionGate": {
        "signature": "gate.evaluate(...) / request_approval(...) / approve(id) / deny(id)",
        "since": "0.1.0",
    },
}

__all__ = ["__manifest__", "__version__"]
