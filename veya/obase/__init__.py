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
    # --- 阶段 1: 严格句柄层合同 (interfaces/adapters/container) ---
    "interfaces.DaemonBus": {
        "signature": "connect/close/publish/subscribe/request/register_handler",
        "since": "0.2.0",
    },
    "interfaces.VfsSandbox": {
        "signature": "execute/execute_args/run_script/read/write/exists/listdir/delete/cancel/close",
        "since": "0.2.0",
    },
    "interfaces.EventBarrier": {
        "signature": "emit(event)/stream(*topics)/barrier(name, parties, timeout)",
        "since": "0.2.0",
    },
    "interfaces.KvStore": {
        "signature": "put/get/delete/keys(prefix)/snapshot/restore/close",
        "since": "0.2.0",
    },
    "interfaces.LlmClient": {
        "signature": "complete(messages, **kw)/stream(messages, **kw)/close",
        "since": "0.2.0",
    },
    "adapters.SandboxVfsAdapter": {
        "signature": "ProcessSandbox → VfsSandbox (VFS 权限内文件面 + 执行面)",
        "since": "0.2.0",
    },
    "adapters.TelemetryEventBarrier": {
        "signature": "telemetry.emit 桥接 + 订阅扇出 + 名同步屏障",
        "since": "0.2.0",
    },
    "adapters.SqliteKvStore": {
        "signature": "KV 快照 (SQLite, JSON 值, snapshot/restore 原子)",
        "since": "0.2.0",
    },
    "adapters.LlmClientAdapter": {
        "signature": "llm_call/llm_stream → LlmClient",
        "since": "0.2.0",
    },
    "adapters.InProcessDaemonBus": {
        "signature": "asyncio 进程内 Pub/Sub + 请求-响应 (未来 gRPC 替换)",
        "since": "0.2.0",
    },
    "container.get_sandbox": {"signature": "VfsSandbox 单例句柄", "since": "0.2.0"},
    "container.get_bus": {"signature": "DaemonBus 单例句柄", "since": "0.2.0"},
    "container.get_barrier": {"signature": "EventBarrier 单例句柄", "since": "0.2.0"},
    "container.get_kv": {"signature": "KvStore 单例句柄", "since": "0.2.0"},
    "container.get_llm": {"signature": "LlmClient 单例句柄", "since": "0.2.0"},
    "container.configure": {"signature": "句柄注入 (sandbox/bus/barrier/kv/llm)", "since": "0.2.0"},
}

__all__ = ["__manifest__", "__version__"]

# 阶段 1: 句柄合同与全局单例句柄层
from veya.obase.container import (  # noqa: E402
    close_all,
    configure,
    get_barrier,
    get_bus,
    get_kv,
    get_llm,
    get_sandbox,
    reset,
)
from veya.obase.interfaces import (  # noqa: E402
    DaemonBus,
    Event,
    EventBarrier,
    KvStore,
    LlmClient,
    SandboxResult,
    VfsSandbox,
)
