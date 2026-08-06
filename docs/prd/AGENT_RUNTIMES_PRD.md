# 三框架运行时集成 PRD — prime-agent (内核) / pi (工具链) / agentscope (平台)

> 版本: v1.0 · 状态: **PRD 待确认 · 账本已立档** (delegate_to_genesis, `server/operator_ledger.py` RUNTIME_LEDGER)
> 定位分层: prime-agent 补**内核** (单智能体运行时) / pi 补**工具链** (CLI/开发体验) /
> agentscope 补**平台** (多智能体/治理) —— 互不冲突, 覆盖 veya 运行时/工具/编排三个接缝。
> Genesis 账本核查: 无既有适配元素, 从零立项 (已登记, 状态 pending)。

---

## 1. 定位分层 (已确认)

| 维度 | prime-agent | pi | agentscope |
|---|---|---|---|
| 抽象层级 | 单智能体运行时 (个体) | 工具链/CLI 套件 (开发体验) | 多智能体服务平台 (组织级) |
| 核心哲学 | 代码即交互、自我改写 | 极简、可扩展、类型安全 | 生产级抽象、企业治理 |
| 语言/运行时 | Python (RLM) | TypeScript / Bun | Python |
| 隔离策略 | IPython 持久内核 (进程内) | 微VM / Docker / OpenShell | Docker / E2B / Daytona / OpenSandbox |
| 多智能体 | 子智能体直接发现+通信 | 无原生 (靠扩展) | 分布式多租户、多会话 |
| 独特卖点 | Continual Harness 自我优化 | 差分渲染 TUI + 统一多厂商 API | Event Bus + 中间件 + MCP/Skill Hub |

## 2. 接缝映射 (已核查: obase 接缝全部存在)

| Veya 接缝 | 位置 | 集成对象 |
|---|---|---|
| Agent/工具注册表 | `obase.agent_registry.py` | 三框架统一入口 (需扩展 `runtime` 类型) |
| 进程内沙箱池 | `obase.local_sandbox_pool.py` | prime-agent 持久内核隔离护栏 |
| 检查点/认知存储 | `obase.checkpoint_store.py` / `cognitive_store.py` | Continual Harness 自我优化状态 |
| 事件总线 | `obase.event_bus.py` | prime-agent 子智能体通信 + agentscope Event Bus 桥 |
| MCP 门面 | `obase/mcp_server/` | agentscope MCP/Skill Hub 同协议互通 |
| Hook 管线 | `hooks/builtin/` (permission/redact/test_gate) | agentscope 中间件 → veya 治理层 |
| Docker 沙箱 | `infra/code_sandbox/` | pi 微VM/Docker、agentscope 远程后端 |
| Provider 路由 | `obase.provider_registry.py` | pi 统一多厂商 API 并入 |
| 插件市场 | `obase.plugin_registry.py` | pi 工具包 + agentscope Skill Hub 双向同步 |
| 消息队列/编排 | `obase.mq.py` / `orchestrator.py` | agentscope 分布式多租户 |

## 3. 统一 AgentRuntime 协议 (L1 定义, 三适配器共用)

```python
class AgentRuntime(Protocol):
    """统一运行时协议 — 上层 (编排/CLI/MCP) 零感知差异。"""

    name: str                       # prime-agent | pi | agentscope
    kind: str = "runtime"           # agent_registry 类型 (待扩展)

    async def init(self, config: dict) -> dict: ...        # 启动/自检, 返回 {ok, version}
    async def dispatch(self, task: str, **kwargs) -> dict: # 派发任务, 返回 {ok, output, cost}
    async def invoke(self, prompt: str, **kwargs) -> dict: # 单轮交互 (对话式)
    async def lifecycle(self, action: str) -> dict: ...    # pause/resume/stop/health
    async def health(self) -> dict: ...                    # {ok, status, uptime}
```

注册: `agent_registry.register("runtime", name, adapter)` (REGISTRY_TYPES 增加 `"runtime"`, 3O 主库改动)。
治理: 权限/审计/脱敏由 veya hooks 统一包裹 (pre_dispatch/permission/redact), 适配器内部不重复实现。

## 4. 三适配器职责

### L1 — prime-agent: 内核运行时适配器 (进程内, 先交付)

| 项 | 设计 |
|---|---|
| 依赖 | `pip install prime-agent` (Python RLM) |
| 隔离 | IPython 持久内核不裸奔 → `local_sandbox_pool` 包一层进程内护栏 |
| 自我优化 | Harness 优化轨迹落 `checkpoint_store` / `cognitive_store` (断点续训式自我改写) |
| 子智能体通信 | `event_bus` 桥接 (发现/消息) |
| 交付物 | `server/runtimes/prime_agent.py` adapter + runtime 类型注册 + 测试门 |

### L2 — pi: 工具链/CLI 桥 (子进程适配器)

| 项 | 设计 |
|---|---|
| 依赖 | pi CLI (本机 0.83.0 已装 ✓, TS/Bun 无法进 Python 进程 → subprocess 桥) |
| 工具注册 | `register_plugin_tool` 包装 spawn 调用; 能力声明走 `plugin_registry.install(capabilities=...)` |
| Provider | pi 统一多厂商 API 注册进 `provider_registry`, 与 obase provider 平级路由 |
| 隔离 | 默认 `infra/code_sandbox` (Docker); 差分渲染 TUI 封装进 cli/ 独立入口 |
| 交付物 | `server/runtimes/pi_bridge.py` + 测试门 (mock spawn) |

### L3 — agentscope: 平台编排桥 (双向翻译)

| 项 | 设计 |
|---|---|
| 依赖 | `pip install agentscope` |
| Event Bus | agentscope Event Bus ↔ `obase.event_bus` 翻译桥 (事件映射表见 §5) |
| 中间件 | agentscope 中间件 ↔ veya hooks 一一对应 (permission/redact/test_gate) |
| MCP | 双方都是 MCP Server → 同协议互注册 (obase/mcp_server ↔ agentscope MCP) |
| Skill Hub | agentscope Skill Hub ↔ `plugin_registry` marketplace 双向同步 |
| 远程后端 | E2B / Daytona / OpenSandbox 注册进 obase 沙箱抽象 |
| 交付物 | `server/runtimes/agentscope_bridge.py` + 事件映射表测试 |

## 5. 事件映射表 (L3)

| agentscope 事件 | veya 事件 | 方向 |
|---|---|---|
| `EventType.START` | `event_bus.publish("agent.start")` | agentscope → veya |
| `EventType.MSG` | `event_bus.publish("agent.message")` | 双向 |
| `EventType.END` | `event_bus.publish("agent.end")` | agentscope → veya |
| `EventType.ERROR` | `event_bus.publish("agent.error")` | agentscope → veya |
| veya `permission.request` | agentscope 中间件审批钩子 | veya → agentscope |
| veya `redact.sensitive` | agentscope 脱敏中间件 | veya → agentscope |

## 6. 测试门 (每层独立可交付)

- [ ] L1: prime-agent adapter init/dispatch/invoke/lifecycle/health 全协议方法测试 (mock 内核)
- [ ] L1: 持久内核在 sandbox_pool 护栏内运行 (隔离回归)
- [ ] L1: checkpoint/cognitive 写入-恢复闭环
- [ ] L2: pi_bridge spawn 包装 (mock) + plugin_registry 能力声明 + provider 平级路由
- [ ] L3: 事件映射表双向翻译测试 (mock agentscope)
- [ ] L3: 中间件↔hooks 对应关系测试
- [ ] 三适配器统一: 经 AgentRuntime 协议调用的端到端冒烟 (上层零感知)

## 7. 落地路径 (3O 流程)

```
PRD (本文档) → 确认 → delegate_to_genesis 立档 (已登记 pending)
→ L1 prime-agent (进程内, 代价最低) → L2 pi bridge → L3 agentscope bridge
每层独立可交付; 主库改动仅 REGISTRY_TYPES 增加 "runtime" (单点, 向后兼容)。
```

## 8. 账本条目 (已立档)

| 立项 | 层 | 状态 | 注册类型 |
|---|---|---|---|
| `prime_agent_runtime` | L1 内核 | pending | runtime (待扩展) |
| `pi_bridge` | L2 工具链 | pending | runtime + plugin_tool |
| `agentscope_bridge` | L3 平台 | pending | runtime + event_bus |

查询: `GET /api/v1/operators` (扩展后含 runtimes) / `ledger_summary()`。
