"""veya/oservi — Layer 4 Stateful Engine Skeletons (3O 服务装配层).

有状态引擎骨架: 调度循环 / 生命周期 / 心跳 / 重试 / IO 协调。
机制/业务分离: 业务靠注入 (ServiceManifest), 骨架代码零业务逻辑。

5 红线 (SPEC v7):
  1. ≥2 真实项目实证
  2. 机制/业务分离 (业务靠注入)
  3. 注入点类型契约 (kind + cardinality)
  4. 无状态骨架定义 (状态只在 Service 实例运行期)
  5. 不反向依赖 (3O 四包禁 import oservi)

Veya 项目服务层的有状态引擎归位于此:
  mcp_server  (MCP 服务引擎)
  streaming   (SSE 流式引擎)
  context     (会话上下文状态)
  history_store (历史存储)
"""

__version__ = "0.1.0"

# 装配通道: 机制/业务分离 — 引擎实现经 manifest 注入, 不硬编码具体元素
from veya.oservi.assembler import assemble, validate_manifest

# 阶段 5: 长时任务守护引擎 + 统一网关（极简指令入口）
from veya.oservi.daemon_engine import DaemonEngine, TaskState, TaskStatus
from veya.oservi.engines import (
    EngineSkeleton,
    Injection,
    get_skeleton,
    list_skeletons,
    register_skeleton,
)
from veya.oservi.gateway import gateway_engine
from veya.oservi.gateway import router as gateway_router
from veya.oservi.manifest import ManifestValidationError, ServiceManifest

__all__ = [
    "DaemonEngine",
    "EngineSkeleton",
    "Injection",
    "ManifestValidationError",
    "ServiceManifest",
    "TaskState",
    "TaskStatus",
    "assemble",
    "gateway_engine",
    "gateway_router",
    "get_skeleton",
    "list_skeletons",
    "register_skeleton",
    "validate_manifest",
]
