"""veya/obase/interfaces — 严格 3O 句柄层合同（阶段 1）。

5 个 Protocol + 共享类型。**只定义合同，不含实现**：
实现见 :mod:`veya.obase.adapters`（薄适配器），单例句柄见
:mod:`veya.obase.container`（全局注入点）。

合同目标（迁移计划阶段 1）：
- 业务逻辑禁止直接 import 旧实现，一律通过 container 注入的句柄交互；
- 每个句柄是一个可替换的物理边界：
    DaemonBus     — 长连接 IPC 通道（进程内实现先行，未来 gRPC 替换）
    VfsSandbox    — 持久化隔离沙盒代理（现适配 ProcessSandbox，未来 namespace）
    EventBarrier  — 跨任务 Pub/Sub + 同步屏障（现适配 telemetry.emit）
    KvStore       — Session Tree 快照存储（SQLite，阶段 4 启用）
    LlmClient     — 纯网络通道：重试/流式，无 Prompt 逻辑
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 共享类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """标准事件：所有关键节点经 EventBarrier 发出的统一载荷。"""

    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    trace_id: str = ""


@dataclass(frozen=True)
class SandboxResult:
    """沙盒执行结果（统一结构，屏蔽底层 dict 细节）。"""

    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    command: str = ""
    duration_ms: float = 0.0
    rejected: bool = False
    audit: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. DaemonBus — IPC/gRPC 长连接
# ---------------------------------------------------------------------------


@runtime_checkable
class DaemonBus(Protocol):
    """长连接消息总线：任务实例 ↔ daemon ↔ gateway 之间的 IPC 通道。

    - ``publish/subscribe``: 广播式 Pub/Sub；
    - ``request/register_handler``: 请求-响应 RPC（Human-in-the-loop 挂起/恢复、
      状态查询等点对点指令）；
    - 进程内实现见 :class:`veya.obase.adapters.InProcessDaemonBus`；
      未来 gRPC/WebSocket 实现只需满足本合同即可替换。
    """

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """向 topic 广播事件（fire-and-forget，无订阅者不报错）。"""

    def subscribe(self, topic: str) -> AsyncIterator[Event]:
        """订阅 topic，返回异步迭代器；迭代器关闭即取消订阅。"""

    async def request(
        self, topic: str, payload: dict[str, Any], *, timeout: float = 30.0
    ) -> dict[str, Any]:
        """同步式请求-响应：等待 register_handler 的处理结果，超时抛 TimeoutError。"""

    async def register_handler(
        self, topic: str, handler: Any
    ) -> None:
        """注册 topic 处理器（async (payload) -> dict）；重复注册覆盖。"""


# ---------------------------------------------------------------------------
# 2. VfsSandbox — 持久化隔离沙盒代理
# ---------------------------------------------------------------------------


@runtime_checkable
class VfsSandbox(Protocol):
    """隔离执行 + 受限文件系统视图。

    - 所有命令执行必须落在沙盒权限范围内（VFS 根 = 沙盒工作目录）；
    - 文件操作（read/write/exists/listdir/delete）只能触碰沙盒根之内，
      越界访问必须拒绝 —— 这是 oprim_fs_* / oprim_shell_exec 的物理边界；
    - 现适配 :class:`veya.obase.sandbox.ProcessSandbox`，后续升级为
      Linux namespace / bubblewrap / Cloudflare Computer。
    """

    async def execute(self, command: str, *, timeout: float | None = None) -> SandboxResult:
        """Shell 语义执行（仅限可信内部调用；不可信输入用 execute_args）。"""

    async def execute_args(
        self, argv: list[str], *, timeout: float | None = None
    ) -> SandboxResult:
        """argv 数组执行：无 shell 注入面，危险命令执行前拦截。"""

    async def run_script(self, script: str, *, timeout: float | None = None) -> SandboxResult:
        """运行脚本（沙盒内创建并执行）。"""

    async def read(self, path: str) -> bytes:
        """读取沙盒内文件（越界抛 ValueError）。"""

    async def write(self, path: str, data: bytes | str) -> None:
        """写入沙盒内文件（自动建父目录；越界抛 ValueError）。"""

    async def exists(self, path: str) -> bool: ...
    async def listdir(self, path: str) -> list[str]: ...
    async def delete(self, path: str) -> None: ...

    async def cancel(self) -> None: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# 3. EventBarrier — 跨任务 Pub/Sub + 同步屏障
# ---------------------------------------------------------------------------


@runtime_checkable
class EventBarrier(Protocol):
    """统一事件通道：所有关键节点经 ``emit`` 发出标准 Event。

    - ``stream``: 订阅（可多 topic），与现有 ``telemetry.emit`` 桥接；
    - ``barrier``: 名同步屏障 —— parties 个协程到达同一名字后同时放行，
      超时抛 asyncio.TimeoutError（用于多任务对齐/熔断协调）。
    """

    def emit(self, event: Event) -> None:
        """发出事件（同步、无阻塞；无订阅者静默丢弃）。"""

    def stream(self, *topics: str) -> AsyncIterator[Event]:
        """订阅一个或多个 topic，异步迭代事件；迭代器关闭即退订。"""

    async def barrier(self, name: str, parties: int, *, timeout: float | None = None) -> None:
        """同步屏障：等待 parties 个调用者到齐后同时放行。"""


# ---------------------------------------------------------------------------
# 4. KvStore — Session Tree 快照存储
# ---------------------------------------------------------------------------


@runtime_checkable
class KvStore(Protocol):
    """KV 快照存储（Session Tree 专用，SQLite 实现）。

    值必须是 JSON 可序列化对象；snapshot/restore 用于分支与时空回溯的
    整树检查点（阶段 4 ``omodul_session_tree_mgr`` 注入点）。
    """

    def put(self, key: str, value: Any) -> None: ...
    def get(self, key: str) -> Any: ...
    def delete(self, key: str) -> None: ...
    def keys(self, prefix: str = "") -> list[str]: ...
    def snapshot(self) -> dict[str, Any]:
        """全量快照 {key: value}（时空回溯恢复点）。"""

    def restore(self, data: dict[str, Any]) -> None:
        """原子恢复快照（先清空后写入，单事务）。"""

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# 5. LlmClient — 纯网络通道
# ---------------------------------------------------------------------------


@runtime_checkable
class LlmClient(Protocol):
    """LLM 网络通道：只发「已打包好的数据」，无 Prompt 逻辑。

    - ``complete``: 非流式补全，OpenAI 格式响应 dict；
    - ``stream``: 流式补全，逐条 delta 事件；
    - 重试/端点归一化/模型解析由实现负责（现适配 veya.obase.llm）。
    """

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        """messages = 标准 AgentMessage 列表（阶段 2 protocol_translate 产出）。"""

    def stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[dict]:
        """流式补全：逐条产出 delta dict。"""

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = [
    "DaemonBus",
    "Event",
    "EventBarrier",
    "KvStore",
    "LlmClient",
    "SandboxResult",
    "VfsSandbox",
]
