# Monty 集成评估：价值、接入点与风险

> **日期**: 2026-10-18
> **评估对象**: [pydantic/monty](https://github.com/pydantic/monty) — Rust 实现的 Python 3.14 安全子集沙箱
> **评估范围**: 工具调用 Code Mode、模型生成代码执行、harness2 candidate probe 隔离执行

---

## 1. 结论：值得做，但定位要精准

**值得引入 Monty，但它不是现有沙箱的替代品，而是一个新的轻量级执行层。**

Veya 现有沙箱架构（`ProcessSandbox` + `oprim.LocalSandbox` + `omodul.sandbox_session`）依赖 OS 级隔离（`unshare -Urn` 网络命名空间、`bwrap`、`ulimit`），能跑完整的 CPython + 第三方包。Monty 是语言级沙箱，隔离完全来自解释器本身——沙箱代码无法表达触碰宿主的操作，因为字节码 VM 里不存在这种操作。

**核心价值主张**：

| 维度 | Veya 现有沙箱 | Monty |
|------|-------------|-------|
| 启动延迟 | ~2ms（unshare）~1500ms（容器） | <1ms（池化） |
| Python 版本 | CPython 3.x（完整） | Python 3.14 子集 |
| 第三方包 | 支持（pip install） | 不支持 |
| 文件系统 | 有完整 FS（临时工作区） | 无默认 FS，需 mount |
| 网络隔离 | `unshare -Urn` / `bwrap` | 不存在（无网络能力） |
| 环境变量 | 白名单过滤 | 不存在（需 host 函数） |
| 隔离边界 | OS 级（命名空间） | 语言级（解释器） |
| 资源限制 | `ulimit`（子进程级） | 内置内存/时间/暂停限制 |
| 快照/恢复 | 不支持 | 支持 |
| 崩溃隔离 | 子进程天然隔离 | worker 子进程隔离 |

**判断**：Monty 适合「模型生成的、无需第三方包的、短生命周期的 Python 代码片段」执行场景。不适合替代现有沙箱跑 pytest / 构建 / 安装依赖等任务。

---

## 2. 三个目标场景分析

### 2.1 工具调用的 Code Mode

**现状**：`run_in_sandbox` 工具（`server/tool_registry.py:2429`）通过 `omodul.sandbox_scope("chat_verify")` 创建沙箱会话，执行 `python3 -c <code>` 或 shell 命令。模型用它验证代码片段行为。

**Monty 适用性**：**高**

- 模型生成的验证代码通常是短片段（<50 行），不需要第三方包
- Monty 的 host 函数机制可以替代现有沙箱的 `put_file`/`get_file` 文件交换
- 快照能力允许「执行 → 检查 → 回滚」的交互式验证循环
- <1ms 的池化启动比 `unshare -Urn` + 临时目录创建更快

**限制**：
- 现有 `run_in_sandbox` 支持 `bash -lc` 命令执行（如 `npm test`、`git diff`），Monty 不支持 shell
- 需要保留现有 `ProcessSandbox` 作为 shell 命令的后端

### 2.2 安全执行模型生成的代码

**现状**：模型通过 `run_in_sandbox` 执行 Python 代码，或通过 `coding_task_run` / `hicode_run` 在隔离工作区中执行更复杂的编码任务。

**Monty 适用性**：**中高**

- Monty 的语言级隔离比 OS 级隔离更难绕过（没有 `os.system`、`subprocess`、`ctypes` 等逃逸面）
- 资源限制（内存/时间/暂停次数）比 `ulimit` 更精细，且从解释器内部度量执行时间（wall-clock 在 host 函数调用期间暂停）
- 快照允许保存/恢复执行状态

**限制**：
- 模型生成的代码如果使用 `enum`、`contextlib`、`logging`、`uuid`、`inspect` 等缺失模块会失败
- 类继承、生成器、`async with`/`async for` 不可用
- 无法 `pip install` 任何东西

### 2.3 Harness2 集成里的 Candidate Probe

**现状**：代码库中没有找到 "harness2" 或 "candidate probe" 的直接引用。`veya_loop` 有 `InterventionCandidate`（干预候选）和 `run_probes` / `unittest_probe` 等探针机制，但这些都是决策层面的概念，不是「在隔离环境里跑一段探针代码」。

**如果「candidate probe」指的是**：在隔离环境中快速执行一段探针代码来验证某个假设（如「这个 import 能不能工作」「这段代码输出什么」），则 Monty **非常适用**：
- <1ms 启动适合高频探针
- 无状态、无副作用
- host 函数可以提供探针所需的外部数据

**如果「candidate probe」指的是**：跑完整的 pytest 套件或构建验证，则 Monty **不适用**（缺第三方包、缺 pytest）。

---

## 3. 接入点设计

### 3.1 架构分层

```
server/tool_registry.py
  └─ _tool_run_in_sandbox()
       ├─ 现有路径: omodul.sandbox_scope("chat_verify") → ProcessSandbox (保留)
       └─ 新路径:   MontySandboxAdapter (新增)
            ├─ 判定条件: code 参数存在 且 无 command 参数 且 标记 use_monty=True
            └─ 执行: Monty pool.checkout() → feed_run(code, inputs=..., external_lookup=...)
```

**原则**：不改变 `run_in_sandbox` 的工具签名和语义。Monty 作为可选后端，由调用方（模型）或配置决定走哪条路径。

### 3.2 适配器草案

```python
# veya/obase/monty_sandbox.py（新增文件）

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

@dataclass
class MontySandboxConfig:
    max_memory: int = 256 * 1024 *  # 256 MB
    max_feed_duration_secs: float = 30.0
    max_turn_duration_secs: float = 30.0
    max_suspensions: int = 100
    request_timeout: float = 60.0

class MontySandboxAdapter:
    """Monty 语言级沙箱适配器。
    
    与 ProcessSandbox 的关键区别:
    - 无 shell、无文件系统、无网络、无环境变量
    - 通过 host 函数和 mounts 提供外部访问
    - 支持快照和资源限制
    """
    
    def __init__(self, config: MontySandboxConfig | None = None):
        self.config = config or MontySandboxConfig()
        self._pool: Any = None  # pydantic_monty.Monty 实例
    
    def _get_pool(self):
        if self._pool is None:
            from pydantic_monty import Monty
            self._pool = Monty()
        return self._pool
    
    def execute_python(
        self,
        code: str,
        inputs: dict[str, Any] | None = None,
        external_lookup: dict[str, Callable] | None = None,
        snapshot: bool = False,
    ) -> dict[str, Any]:
        """在 Monty 沙箱中执行 Python 代码片段。
        
        Returns:
            dict with keys: ok, result, stdout, snapshot_id, error
        """
        try:
            pool = self._get_pool()
            with pool.checkout() as session:
                if snapshot:
                    snap = session.snapshot()
                rec = session.feed_run(
                    code,
                    inputs=inputs or {},
                    external_lookup=external_lookup or {},
                )
                return {
                    "ok": True,
                    "result": rec,
                    "stdout": str(rec),
                    "snapshot_id": snap.id if snapshot else None,
                    "error": None,
                }
        except Exception as e:
            logger.warning("monty execution failed: %s", e)
            return {
                "ok": False,
                "result": None,
                "stdout": "",
                "snapshot_id": None,
                "error": str(e),
            }
    
    def execute_with_files(
        self,
        code: str,
        files: dict[str, str],
        snapshot: bool = False,
    ) -> dict[str, Any]:
        """执行代码并暴露虚拟文件（通过 OSAccess）。
        
        files: {"virtual/path": "content", ...}
        """
        from pydantic_monty import MemoryFile, OSAccess, Monty
        
        mem_files = [MemoryFile(p, content=c) for p, c in files.items()]
        os_access = OSAccess(mem_files)
        
        pool = self._get_pool()
        with pool.checkout() as session:
            rec = session.feed_run(code, os=os_access)
            return {"ok": True, "result": rec, "stdout": str(rec), "error": None}
```

### 3.3 工具层集成

在 `_tool_run_in_sandbox` 中增加 Monty 路径选择：

```python
# server/tool_registry.py 修改（伪代码）

async def _tool_run_in_sandbox(
    code: str | None = None,
    command: str | None = None,
    host: str = "",
    approved: bool = False,
    approval_id: str = "",
    use_monty: bool = False,  # 新参数
) -> str:
    # ... 现有 host 远程路径不变 ...
    
    if use_monty and code and not command:
        # Monty 路径：纯 Python 代码片段
        from veya.obase.monty_sandbox import MontySandboxAdapter
        adapter = MontySandboxAdapter()
        result = adapter.execute_python(code)
        if result["ok"]:
            return f"monty_exit=0\n{result['stdout']}"
        raise ToolExecutionError(f"monty_error: {result['error']}")
    
    # 现有 ProcessSandbox 路径（不变）
    # ...
```

### 3.4 Host 函数桥接

Monty 的 host 函数是沙箱访问外部的唯一途径。Veya 可以暴露以下 host 函数：

| Host 函数 | 用途 | 安全边界 |
|-----------|------|---------|
| `veya_read_file(path)` | 读取工作区文件 | 限制在 workspace 根目录下 |
| `veya_write_file(path, content)` | 写入工作区文件 | 限制在 workspace 根目录下 |
| `veya_list_files(dir)` | 列出工作区文件 | 限制在 workspace 根目录下 |
| `veya_git_diff()` | 获取 git diff | 只读 |
| `veya_project_config(key)` | 读取项目配置 | 只读，白名单 key |

这些函数在宿主进程中运行，必须自行做路径校验和权限检查。

---

## 4. Python 子集限制带来的风险

### 4.1 高风险限制

| 限制 | 影响 | 缓解措施 |
|------|------|---------|
| **无第三方包** | 模型代码 `import numpy` / `import pydantic` 会失败 | 在工具描述中明确说明；对已有依赖的代码回退到 ProcessSandbox |
| **无类继承** | 模型无法定义 `class MyError(Exception)` | 提供 host 函数抛出自定义异常 |
| **无生成器** | `yield` 不可用；生成器表达式退化为 list | 文档说明；大多数验证代码不需要 |
| **无 `async with` / `async for`** | 异步上下文管理器不可用 | 用 `asyncio.run()` + 同步 host 函数替代 |
| **无 `enum` 模块** | 枚举类不可用 | 用 `ClassInstance` 包装宿主枚举 |
| **无 `logging` 模块** | 日志不可用 | 用 `print` + host 函数收集 |

### 4.2 中风险限制

| 限制 | 影响 | 缓解措施 |
|------|------|---------|
| `map`/`filter`/`zip` 是 eager | `map(f, count())` 会无限执行直到资源限制触发 | Monty 的资源限制会兜底，但可能浪费资源 |
| `re` 使用 `fancy-regex` 而非 CPython engine | 某些正则行为不同 | 避免复杂正则；用 `json` 替代 |
| 类 `__bool__` 和 `__getattr__` 被忽略 | 实例永远 truthy；动态属性不工作 | 文档说明 |
| 函数属性不可用 | `fn.__name__` 抛异常 | 用 `functools.partial` 替代装饰器模式 |

### 4.3 低风险限制

| 限制 | 影响 | 缓解措施 |
|------|------|---------|
| `assert` 有 pytest 风格消息 | 断言错误信息更详细 | 无需处理 |
| 只有 UTF-8/ASCII/UTF-16/UTF-32 编码 | `latin-1` 不可用 | 大多数场景不涉及 |
| 无 `del` 语句 | 无法删除变量 | 用 `None` 赋值替代 |
| 无 `super()` | 无法调用父类方法 | 子集不支持类继承，无影响 |

### 4.4 安全风险

| 风险 | 说明 | 缓解 |
|------|------|------|
| **Host 函数是安全边界** | Host 函数在宿主进程中以完整权限运行，如果 host 函数有漏洞，沙箱形同虚设 | 严格校验 host 函数输入；限制 host 函数能力范围 |
| **快照反序列化** | 如果快照来源不可信，恢复时可能触发问题 | 只使用自家 Monty 实例生成的快照 |
| **Worker 二进制** | Monty worker 从 PATH 解析，可能被替换 | 显式指定 worker 路径 |
| **内存限制是 allocator 级** | 进程 RSS 可能超过 max_memory（碎片、非 allocator 内存） | 设置 worker 级 backstop（如 cgroup 内存限制） |

---

## 5. 分阶段实施步骤

### Phase 1：验证（1-2 周）

**目标**：验证 Monty 在 Veya 环境中的可用性和性能。

1. 安装 `pydantic-monty`（`uv add pydantic-monty`）
2. 编写集成测试：
   - 基本 Python 代码执行（算术、字符串、列表推导）
   - host 函数调用
   - 快照/恢复
   - 资源限制（内存、时间）
3. 性能基准测试：
   - Monty 池化启动 vs `unshare -Urn` 启动
   - 典型代码片段执行延迟对比
4. 限制清单验证：
   - 模型实际生成的代码中，有多少能在 Monty 子集中运行？
   - 收集 Top 失败模式（`import` 缺失、类继承、生成器等）

**交付物**：`tests/runtime/test_monty_sandbox.py` + 性能基准报告

**Go/No-Go 判定**：如果 >70% 的模型验证代码能在 Monty 中运行，且 P95 延迟优于现有沙箱，则继续。

### Phase 2：适配器层（2-3 周）

**目标**：实现 `MontySandboxAdapter` 并集成到 `run_in_sandbox` 工具。

1. 实现 `veya/obase/monty_sandbox.py`（适配器 + 配置）
2. 实现 host 函数集：
   - `veya_read_file` / `veya_write_file` / `veya_list_files`
   - `veya_git_diff`
3. 在 `_tool_run_in_sandbox` 中增加 `use_monty` 分支
4. 更新工具描述，告知模型 Monty 的能力边界
5. 编写端到端测试：
   - 模型通过 `run_in_sandbox(use_monty=True)` 执行代码
   - host 函数正确桥接
   - 失败时优雅降级到 ProcessSandbox

**交付物**：可工作的 Monty 执行路径 + 测试覆盖

### Phase 3：快照与交互（1-2 周）

**目标**：利用 Monty 的快照能力实现交互式代码验证。

1. 实现快照保存/恢复 API
2. 在 `run_in_sandbox` 中支持 `snapshot` 参数
3. 测试「执行 → 检查快照 → 继续执行」的交互模式
4. 如果适用，集成到 `coding_task_run` 的验证循环中

**交付物**：快照功能 + 交互式验证演示

### Phase 4：Candidate Probe 集成（可选，1-2 周）

**目标**：如果 harness2 的 candidate probe 需要隔离执行，提供 Monty 后端。

1. 确认 candidate probe 的具体需求（代码形式、外部访问需求）
2. 实现 probe 专用的 Monty 配置（更短超时、更小内存、无快照）
3. 集成到 veya_loop 的 probe 执行路径
4. 性能对比：Monty probe vs 现有 probe 执行

**交付物**：probe 执行路径 + 性能对比报告

---

## 6. 总体建议

### 推荐策略：渐进引入，双后端并存

1. **不要替换现有沙箱**。`ProcessSandbox` 能跑完整 Python + 第三方包 + shell 命令，这是 Monty 无法替代的。
2. **Monty 作为可选后端**。在 `run_in_sandbox` 中增加 `use_monty` 参数，由模型自主决定是否使用。工具描述中明确说明 Monty 的能力和限制。
3. **优先在低频验证场景引入**。先用 Monty 处理「这个 import 能不能工作」「这段代码输出什么」这类简单验证，积累经验后再扩展到更复杂场景。
4. **host 函数是安全关键**。投入精力确保 host 函数的输入校验和权限边界，这是 Monty 沙箱的「信任锚」。

### 不推荐的做法

- ❌ 用 Monty 替代 `hicode_run` / `coding_task_run`（需要完整 Python 环境）
- ❌ 用 Monty 执行 pytest / npm test / build（需要第三方包和 shell）
- ❌ 在 host 函数中暴露过多能力（扩大攻击面）
- ❌ 忽略 Python 子集限制，假设所有 CPython 代码都能跑

---

## 附录：Monty 关键 API 速查

```python
from pydantic_monty import Monty, ClassInstance, MemoryFile, OSAccess, MountDir

# 池化执行
with Monty() as pool:
    with pool.checkout() as session:
        # 基本代码执行
        result = session.feed_run("1 + 2")
        
        # 带 host 函数
        result = session.feed_run(
            "get_price('A1') * 2",
            external_lookup={'get_price': lambda sku: 3.5},
        )
        
        # 带内存文件
        os_access = OSAccess([MemoryFile('/config.json', content='{"x": 1}')])
        result = session.feed_run("import json; json.load(open('/config.json'))", os=os_access)
        
        # 带快照
        snap = session.snapshot()
        # ... 后续恢复 ...
```

---

DONE
