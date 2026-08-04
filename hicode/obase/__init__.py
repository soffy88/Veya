"""hicode/obase — 3O 基础设施层落地包（§3/§7，与业务层平行）。

obase 与 3O 三层（oprim/oskill/omodul）平行：3O 三层关注"业务怎么算/编排/产出"，
obase 关注"怎么调外部 LLM/算成本/限流/取凭据/鉴权/遥测/沙箱"——职责正交。

依赖方向（§7.4，MUST）：
    ✅ omodul/oskill/oprim/服务层 → obase
    ❌ obase → 3O 任何层 / 项目业务层（hicode.tools、server、agents、config…）

本包仅允许 import：标准库、第三方库、``hicode.errors``、``hicode.compat``、
``hicode.obase`` 内部模块（由 ``scripts/check_obase_no_reverse_dep.py`` 强制）。

§2.5: 主库暴露 ``__manifest__``（元素清单 + 签名 + 版本），供 catalog 查询与复用决策。
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
