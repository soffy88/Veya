# Veya 10/10 工程化升级总方案

> 文件建议路径：`docs/VEYA_10_OF_10_PLAN.md`  
> 适用仓库：`soffy88/Veya`  
> 基线版本：Veya `0.6.0`，`main` 分支，评估快照截至 2026-08-24  
> 目标：把 Veya 从“能力快速扩张的 AI Coding Agent / Agent Runtime 原型”收敛为“架构一致、可验证、可演进、可公开维护的 Agent Runtime”。

---

> **2026-08-24 核实结论（执行侧追加，不改动上文原始方案）**：本文档 §3/§5/§21/§22/§28 里把
> `server/agent_loop_bridge.py` 列为待验证删除的 legacy 是误判——`docs/ARCHITECTURE_STABLE.md:89`
> 早就记录它是 `VEYA_AGENT_LOOP=strict` 的双轨切换桥，`server/coordinator_master.py`/
> `server/tool_registry.py`（`agent_loop_run` 工具）现在还在用，不是待清理代码，不需要
> PR-04。§3 里 `server/coordinator.py` 四个调用点的现状同样比这里描述的更细——已核实的
> 难度分级见 `docs/graveyard.md`（“意图分类 + DAG 分解”条目）和 `architecture/manifest.yaml`，
> 不要重复调研，直接读那两份。落地进度追踪见 `architecture/manifest.yaml` + 各 `docs/dev/rfc-*.md`。

---

## 0. 执行摘要

Veya 当前已经具备真正 Agent Runtime 的多数关键构件：单 Master LLM 主链、ReAct、工具注册表、Sandbox、权限门、Session、Memory、SSE、Hicode、Vision、MCP、长任务、Goal Kernel、Veya Loop、审计与部分闭环能力。

当前限制 Veya 继续提升的主要问题已经不是“缺功能”，而是：

1. **架构事实与代码事实存在漂移**；
2. **legacy 路径仍有残留，且部分 CI 仍在保护旧架构而不是新架构**；
3. **工具、状态、任务、记忆、闭环模块增长过快，核心边界变模糊**；
4. **默认依赖偏重，核心包与扩展能力尚未彻底分离**；
5. **总体测试覆盖率和关键 invariant 测试仍不足以支撑高自治执行**；
6. **产品定义仍偏“AI 编码 Agent”，没有把真正差异化的 Runtime / Loop / Safety 说清楚**；
7. **开源项目所需的版本契约、贡献治理、兼容策略、安全响应机制尚未完全成型**。

因此，本方案的原则不是继续堆新能力，而是进行一次 **Architecture Consolidation + Reliability Hardening**。

最终目标架构：

```text
                         Veya

┌──────────────────────────────────────────────────────────┐
│                       Interfaces                         │
│ CLI / Web / TUI / VSCode / API / IM                    │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│                     Agent Kernel                         │
│ Master ReAct Loop                                       │
│ Model Gateway │ Context │ Session │ Delegation          │
│ Tool Protocol │ Event Protocol │ State Contract         │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│                   Capability Plane                       │
│ Code / Hicode │ Browser │ MCP │ Vision │ Sandbox        │
│ Skills │ AgentLoop │ GoalRun │ Veya Loop adapters       │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│                  Runtime / Control Plane                  │
│ AuthZ │ Audit │ Telemetry │ Storage │ Quota │ Scheduler │
│ Recovery │ Replay │ Policy │ Secrets                     │
└──────────────────────────────────────────────────────────┘
```

一句话原则：

> **一个主脑、一个状态契约、一个工具协议、一个事件协议、一个事实源；其余全部是可插拔能力。**

---

# 1. “10 分”的定义

这里的 10/10 不是“没有任何 bug”，而是每个维度都有明确、自动化、可持续验证的工程门槛。

| 维度 | 当前估计 | 10/10 定义 |
|---|---:|---|
| Agent 架构理念 | 8.7 | 核心原则编码成 invariant，并由 CI 自动防回退 |
| 主链设计 | 9.0 | 唯一用户主链，无隐式第二主链、无程序语义路由 |
| Tool / Delegation | 8.5 | 标准 Tool ABI、能力发现、权限、隔离、幂等与审计统一 |
| Sandbox / Permission | 8.0 | 默认最小权限、能力令牌、资源限制、审计、逃逸回归测试 |
| Observability | 7.8 | 每轮端到端 trace，可关联 model/tool/state/cost/error，可重放 |
| 模块边界 | 6.5 | Kernel 与 capability 明确，禁止循环依赖，legacy 清零 |
| Dependency Hygiene | 6.2 | Core 极轻，重能力 extras 化，锁定供应链与 SBOM |
| Testing | 6.8 | 核心 invariant ≥95%，整库 ≥80%，真实 E2E / fault injection |
| 文档一致性 | 6.5 | 文档从架构事实自动校验，禁止“代码已变、文档没变” |
| 产品定位 | 6.0 | 清楚回答为什么不是 Claude Code / Codex / OpenCode 的复制品 |
| 开源成熟度 | 5.0 | SemVer、CHANGELOG、CONTRIBUTING、SECURITY、release provenance 完整 |
| 长期潜力 | 8.5 | Capability 可插拔，Kernel 稳定，Eval 数据持续驱动演进 |

**最终 release gate：所有 12 项均达到定义，才标记 Veya 1.0。**

---

# 2. P0：立即冻结的五条架构铁律

这些规则应从文档升级为代码与 CI 约束。

## I-01：唯一用户主链

唯一用户交互主链：

```text
Interface
  → MasterCoordinator
  → MasterAgent / ReAct
  → Tool execution
  → MasterAgent synthesis
  → Event stream
```

禁止再次出现：

- keyword intent router；
- 主链前置 URL 自动抓取；
- 程序按任务类型切模型；
- 程序按语义裁工具；
- 第二个“可接管整个请求”的 AgentLoop；
- legacy Coordinator 重新成为聊天入口。

### CI gate

新增：

```text
scripts/check_single_master_path.py
```

静态检查所有用户入口只能导向 `coordinator_master` 允许的入口。

白名单明确列出独立 HITL workflow，例如 Flow / Genesis，如果它不属于聊天主链，则必须标记为：

```python
VEYA_EXECUTION_PLANE = "workflow"
```

否则 CI 失败。

---

## I-02：程序不替 LLM 做开放语义判断

程序可以判断：

- 权限；
- schema；
- timeout；
- quota；
- resource；
- deterministic policy；
- protocol；
- safety boundary。

程序不应判断：

- “这是不是 research”；
- “用户是不是想编码”；
- “URL 是否应该抓”；
- “该用哪个 agent”；
- “回答是否应该交给 Hicode”。

这些应由模型在明确工具面上决策。

### 例外

模型主动调用 `tool_search` 进行工具发现是允许的，因为：

```text
模型决定发现什么
≠
程序猜模型需要什么
```

但必须把这个区别写成正式 ADR，防止未来重新演化成 `_layer_tools`。

---

## I-03：一个 Authority，多个 Projection

Veya 当前存在：

- history；
- session tree；
- compacted history；
- memory；
- goal event store；
- trace；
- replay projection。

必须明确：

> 原始事实不可变，派生状态可以重建。

建议最终模型：

```text
Canonical Event / Conversation Log
          │
          ├── Active conversation projection
          ├── Session tree projection
          ├── Compaction projection
          ├── Long-term memory projection
          ├── Search index
          └── Replay / audit projection
```

不要再出现“两份都看起来像权威源”的状态。

---

## I-04：Kernel 不感知业务 Capability

Kernel 只知道抽象协议：

```python
Tool
SessionStore
EventSink
ModelProvider
PermissionPolicy
Capability
Delegate
```

Kernel 不应直接知道：

```text
wechat
quant
wayfinding
team
vision
hicode
goal-run
```

这些全部由 capability registry 接入。

---

## I-05：所有高影响行为都必须可解释、可审计、可恢复

任何外部副作用必须满足：

```text
intent
→ authorization
→ execution
→ result
→ evidence
→ audit
```

如果行为可逆，还需要：

```text
rollback / compensation
```

---

# 3. P0：先解决“架构事实漂移”

这是当前最高优先级。

当前仓库已经存在明显信号：

- `docs/ARCHITECTURE_STABLE.md` 是权威架构；
- `docs/graveyard.md` 记录 legacy；
- 但 legacy `coordinator.py` 仍有真实引用；
- `server/agent_loop_bridge.py` 仍存在；
- CI typecheck 仍重点检查 `server/coordinator.py` / `veya/intent.py`；
- smoke test 仍使用旧 `--agent plan` 语义；
- `veya/llm.py`、`veya/history_store.py` 仍通过 compatibility alias 指向 3O 实现。

## 目标

建立一个真正的机器可读架构事实源：

```text
architecture/manifest.yaml
```

示例：

```yaml
kernel:
  master_entry:
    - server.coordinator_master
  canonical_llm:
    - veya.obase.llm
  canonical_history:
    - veya.oservi.history_store

deprecated:
  - server.coordinator
  - veya.intent
  - server.agent_loop_bridge

planes:
  chat:
    authority: master
  workflow:
    allowed:
      - server.routes.flow

forbidden_imports:
  - from: server.coordinator_master
    to: server.coordinator
```

然后生成：

- 文档；
- architecture graph；
- CI checks；
- deprecated import report。

## 必做脚本

```text
scripts/check_architecture_manifest.py
scripts/check_no_deprecated_imports.py
scripts/check_single_master_path.py
scripts/check_docs_architecture_sync.py
```

## 10 分验收

- `ARCHITECTURE_STABLE.md` 不再靠人工同步；
- 所有主入口与 manifest 一致；
- graveyard 里的 retired 模块不允许被新代码 import；
- 任何架构漂移 PR 自动红灯。

---

# 4. 模块边界：从“很多目录”收敛成稳定四层

建议不要强制一次性物理搬目录，而是先建立**逻辑边界**，再逐步迁移。

## 目标逻辑结构

```text
veya/
  kernel/
    agent.py
    loop.py
    model.py
    context.py
    session.py
    events.py
    protocol.py

  runtime/
    authz.py
    audit.py
    telemetry.py
    sandbox.py
    quota.py
    storage.py
    scheduler.py

  capabilities/
    code/
    hicode/
    browser/
    vision/
    mcp/
    skills/
    delegation/
    goal/
    loop/

  interfaces/
    cli/
    api/
    web/
    tui/
    vscode/
```

3O 可以继续作为内部抽象体系，但必须满足：

> 用户不需要理解 3O 才能理解 Veya。

### 3O 的推荐定位

```text
3O = internal implementation architecture
Veya = public product/runtime architecture
```

不要让 README 的第一理解路径变成：

```text
用户 → obase → oprim → oskill → omodul → oservi
```

而是：

```text
用户 → Veya Runtime → capabilities
```

---

# 5. Legacy 清理计划

建立 `DEPRECATION_LEDGER.yaml`：

```yaml
server.coordinator:
  replacement: server.coordinator_master
  status: deprecated
  remove_by: 0.8.0

veya.intent:
  replacement: model-native tool decision
  status: deprecated
  remove_by: 0.8.0

server.agent_loop_bridge:
  replacement: tool:agent_loop_run
  status: verify-and-remove
  remove_by: 0.7.0
```

## 删除原则

不是看到 old 就删，而是：

1. 找所有 import；
2. 分类：chat / workflow / tests / compatibility；
3. 对每个调用点定义 replacement；
4. 添加 regression test；
5. 切换；
6. 删除；
7. CI 禁止回引。

## 目标

Veya 0.8 前：

- `server/coordinator.py` 不再作为运行依赖；
- `veya.intent` 退出生产路径；
- `agent_loop_bridge.py` 若确实无用则删除；
- alias shim 进入明确 deprecation 周期；
- `graveyard.md` 只记录历史，不再记录“仍在运行的墓碑”。

---

# 6. 主链做到 10/10

## 6.1 MasterCoordinator 变薄

当前 `coordinator_master.py` 已有“薄适配层”方向，但仍承担：

- session lock；
- steering；
- prompt patch；
- token budget；
- Hicode 适配；
- memory；
- history；
- context compaction；
- event bridge；
- long task。

继续拆成 protocol-oriented components：

```text
MasterCoordinator
 ├── SessionRuntime
 ├── ContextRuntime
 ├── ModelRuntime
 ├── ToolRuntime
 ├── EventRuntime
 └── RecoveryRuntime
```

Coordinator 只负责生命周期：

```python
turn = runtime.open_turn(...)
async for event in agent.run(turn):
    yield event
```

## 6.2 主链 invariant

必须测试：

- 每个用户请求最多一个 active master loop；
- 同 session 并发请求严格序列化；
- 不同 session 可并发；
- cancellation 不污染下轮；
- steering 只进入当前 in-flight turn；
- tool error 不吞掉最终回复；
- model empty response 必须产生显式 recovery event；
- SSE 断线不能破坏 canonical state；
- retry 不重复执行非幂等工具。

---

# 7. Tool 系统做到 10/10

`server/tool_registry.py` 已经是 Veya 的关键资产，但现在承担的概念太多。

## 7.1 定义稳定 Tool ABI

统一：

```python
class ToolSpec:
    name: str
    version: str
    description: str
    input_schema: dict
    output_schema: dict
    risk: RiskLevel
    side_effect: SideEffect
    idempotency: Idempotency
    concurrency: ConcurrencyPolicy
    timeout: float
    capability: str
```

工具执行返回：

```python
class ToolResult:
    status: Literal["ok", "error", "denied", "timeout", "cancelled"]
    data: Any
    evidence: list[Evidence]
    audit_id: str
    retryable: bool
```

不要让工具返回任意自由字符串作为唯一协议。

## 7.2 Side-effect 分类

```text
PURE_READ
LOCAL_WRITE
PROCESS_EXEC
NETWORK_WRITE
EXTERNAL_MUTATION
PRIVILEGED
```

权限、并发、重试全部从这个声明推导，而不是散落的名单。

当前 `_PARALLEL_SAFE_TOOLS` 应最终变为：

```python
spec.concurrency == SAFE_PARALLEL
```

当前 `_TOOL_GROUPS` 应最终变为 capability metadata，而不是巨大集中字典。

## 7.3 Tool discovery

保留模型主动发现模式，但协议化：

```text
tool_catalog()
tool_search(query)
tool_enable(names)
```

规则：

- 决策权在模型；
- 程序不根据 user text 做工具筛选；
- 每次启用写 trace；
- 工具 schema token 成本可测；
- full-tools 与 discovery 模式做固定 benchmark。

## 7.4 Tool Contract Tests

每个工具自动运行：

```text
schema valid
invalid args rejected
timeout works
permission respected
audit emitted
cancel works
result serializable
secret redaction
side-effect declaration present
```

新增：

```text
tests/contracts/tools/
```

目标：**100% 注册工具通过 contract test。**

---

# 8. Delegation / AgentLoop 做到 10/10

AgentLoop 的正确定位：

> MasterAgent 可主动调用的隔离子任务执行器，而不是第二主脑。

定义标准 Delegate ABI：

```python
delegate.run(
    objective,
    context_ref,
    capability_scope,
    budget,
    deadline
) -> DelegateResult
```

## 必须具备

- 独立 session namespace；
- 独立 budget；
- capability allowlist；
- 最大轮数；
- cancel；
- heartbeat；
- structured result；
- evidence；
- parent trace link；
- 不可直接覆盖 parent state；
- delegation depth 上限。

建议：

```text
max delegation depth = 2
```

避免递归 agent spawning。

## 验收

测试：

```text
master → delegate → tool → result → master
```

全链 trace 可关联，并能证明子任务无法获得未授权 capability。

---

# 9. State / Session / Memory 做到 10/10

这是 Veya 能否成为长期 Agent 的核心。

## 9.1 Canonical Conversation Event Log

建议把原始对话与 tool events 视为不可变事实：

```text
TurnStarted
UserMessageAdded
ModelMessageAdded
ToolRequested
PermissionGranted
ToolCompleted
TurnCompleted
TurnFailed
```

projection 才生成：

- active history；
- compacted context；
- session tree；
- memory candidate；
- replay。

## 9.2 Compaction

原则：

> 压缩只改变“给模型看的上下文”，绝不改变事实源。

需要自动测试：

- 压缩前原文可恢复；
- 压缩后 parent/branch 连贯；
- summarize failure 不破坏 session；
- compaction 可重复；
- replay 使用原始事实而不是摘要。

## 9.3 Memory

把 memory 分为：

```text
episodic
semantic
procedural
decision
user preference
```

写入 memory 必须带：

```text
source_event_ids
confidence
created_at
last_verified_at
scope
```

不能把模型总结直接视为事实。

## 9.4 Forget / correction

支持：

```text
memory invalidate
memory supersede
memory provenance
```

否则长期 Agent 会不断累积旧结论。

---

# 10. Veya Loop 做到 10/10

Veya Loop 是最可能形成 Veya 长期差异化的部分。

不要把它继续扩成“数学算法集合”，而要收敛成明确闭环协议：

```text
Observe
  ↓
Diagnose
  ↓
Hypothesis
  ↓
Plan
  ↓
Authorize
  ↓
Act
  ↓
Verify
  ↓
Attribute
  ↓
Learn
```

## 10.1 Loop Contract

每一次闭环必须产生：

```python
LoopRecord(
    observation,
    hypothesis,
    action,
    predicted_outcome,
    actual_outcome,
    evidence,
    attribution,
    policy_delta,
)
```

## 10.2 Learning gate

禁止：

```text
一次失败 → 自动大幅修改策略
```

要求：

- minimum evidence；
- confidence threshold；
- rollback；
- shadow evaluation；
- canary；
- human gate for high-impact policy changes。

## 10.3 与 MasterAgent 的边界

MasterAgent：

```text
决定是否调用 Loop capability
```

Veya Loop：

```text
在封闭、结构化、可审计控制问题内求解
```

Loop 不重新成为另一条通用聊天主链。

---

# 11. Sandbox / AuthZ / Security 做到 10/10

## 11.1 默认最小权限

工具默认：

```text
deny
```

能力显式声明：

```text
filesystem.read
filesystem.write
process.exec
network.read
network.write
secrets.use
```

## 11.2 Capability Token

每次敏感工具执行生成：

```text
capability nonce
scope
expiry
session
tool
resource
```

并进入 audit。

## 11.3 Sandbox profile

至少分：

```text
READ_ONLY
BUILD
TEST
NETWORKED
PRIVILEGED
```

默认 `BUILD` 不等于 unrestricted shell。

## 11.4 安全测试

新增 adversarial suite：

```text
path traversal
symlink escape
env secret leakage
shell injection
prompt injection through tool output
oversized output
fork bomb/resource exhaustion
network policy bypass
permission race
TOCTOU
cancel during write
```

必须在 CI nightly 运行。

## 11.5 Skill 安全

现有 AST + semantic scan 方向保留，但 semantic scan 只能作为 advisory signal。

真正 gate 仍应依赖：

- static policy；
- manifest；
- permission；
- sandbox。

LLM 不能成为唯一安全边界。

---

# 12. Observability 做到 10/10

统一 OpenTelemetry 风格的 trace 模型。

每个 turn：

```text
trace_id
 session_id
 turn_id
 model_span
 tool_span*
 delegate_span*
 state_span*
```

每个 span 至少记录：

```text
duration
status
token_in
token_out
estimated_cost
tool_name
retry_count
permission
error_class
```

敏感内容默认 redacted。

## 必备面板

1. request success rate；
2. empty response rate；
3. tool error rate；
4. p50/p95/p99 latency；
5. first-token latency；
6. tokens / turn；
7. tool schema token overhead；
8. context compaction rate；
9. delegated task success；
10. sandbox denial / timeout；
11. recovery rate；
12. model provider failure rate。

## SLO

建议 Veya 1.0：

```text
non-provider-induced turn success ≥ 99.5%
empty silent response = 0
audit coverage of side-effect tools = 100%
trace correlation completeness ≥ 99.9%
```

---

# 13. Dependency Hygiene 做到 10/10

当前默认依赖含数据与可视化重包。目标是核心安装极轻。

## 13.1 Core

```text
veya
```

只包含运行 MasterAgent 必须项。

## 13.2 Extras

```text
veya[web]
veya[tui]
veya[vision]
veya[data]
veya[sandbox]
veya[desktop]
veya[dev]
veya[all]
```

例如：

- pandas / numpy / pyarrow → `data`；
- matplotlib / plotly / networkx → 对应 visualization / analysis extra；
- Textual → `tui`。

## 13.3 目标指标

```text
core dependency count ≤ 12
clean pip install cold start < 2s（不含下载）
import veya p95 < 300ms
core wheel < 5MB（纯 Python 目标）
Docker runtime image 尽量 < 300MB
```

实际阈值用 benchmark 校准，但必须有 budget。

## 13.4 Supply chain

加入：

- lock / constraints；
- Dependabot；
- `pip-audit`；
- SBOM；
- provenance；
- signed release；
- pinned GitHub Actions commit SHA（1.0 前）。

---

# 14. Testing 做到 10/10

总体 coverage 不是唯一目标。

## 14.1 四层测试

### L1 Unit

纯函数、parser、policy、state projection。

### L2 Contract

Tool / Model / Store / Event / Sandbox ABI。

### L3 Integration

```text
Master → Tool
Master → Delegate
Master → Memory
Master → Goal
Master → Veya Loop
```

### L4 E2E

真实 CLI / HTTP / SSE：

```text
install
init
start
send task
tool
permission
completion
restart
resume
```

---

## 14.2 Coverage gate

建议：

```text
overall line coverage ≥ 80%
kernel ≥ 90%
security / authz ≥ 95%
tool protocol ≥ 95%
state / event store ≥ 95%
```

关键 invariant 用 mutation testing 验证，而不是只看 coverage。

## 14.3 Property-based tests

适用于：

- event replay；
- DAG；
- state transition；
- permission rules；
- tool schema；
- compaction；
- serialization。

## 14.4 Fault injection

模拟：

```text
LLM timeout
LLM empty
LLM malformed tool call
tool timeout
tool partial write
SSE disconnect
process killed
disk full
store corrupted
cancel
duplicate request
provider 429/5xx
```

验证系统是否能恢复。

---

# 15. CI/CD 做到 10/10

当前 CI 已有 Ruff / mypy / pytest / docs / smoke / build，但必须从“有 CI”升级到“保护当前架构”。

## 15.1 修正 typecheck 范围

当前重点仍包含 legacy 模块。

改为：

```text
veya/kernel/
veya/runtime/
server/coordinator_master.py
server/tool_registry.py
current session/state/event modules
```

legacy 只保留单独 migration job，不能代表主线类型质量。

## 15.2 Smoke 必须 hard fail

禁止：

```bash
... || true
```

以及：

```text
WARN but pass
```

核心 smoke 不满足预期必须失败。

## 15.3 新 jobs

```text
architecture
contracts
security
integration
e2e
package-minimal
dependency-audit
docs-consistency
benchmark-regression
```

## 15.4 Merge gate

main 分支保护至少要求：

```text
lint
typecheck
unit
contract
architecture
security-basic
e2e-smoke
docs
```

---

# 16. Eval 系统做到 10/10

Agent 不能只靠单元测试判断好坏。

建立 `evals/`：

```text
evals/
  coding/
  research/
  tool_selection/
  long_task/
  recovery/
  safety/
  context/
  delegation/
```

每个 case：

```yaml
prompt:
environment:
expected_capabilities:
forbidden_actions:
success_criteria:
max_cost:
max_turns:
```

## 核心指标

```text
Task Success Rate
Tool Precision
Tool Recall
Unnecessary Tool Rate
Recovery Rate
Cost / Successful Task
Latency / Successful Task
Human Intervention Rate
Regression Rate
```

最关键的是：

> 每次架构修改必须用固定 eval set 对比前后，而不是凭主观感觉。

建立：

```text
veya eval baseline
veya eval compare <commitA> <commitB>
```

---

# 17. Performance 做到 10/10

性能优化优先顺序：

1. schema token；
2. context；
3. model latency；
4. tool IO；
5. process startup。

不要先微优化 Python。

## 基准

建立：

```text
benchmarks/
```

至少测：

```text
cold import
coordinator construction
first-token latency
no-tool turn
1-tool turn
5-read-tools parallel turn
context 10k / 50k / 100k
tool discovery vs full schema
compaction
resume
```

所有性能改动必须保存 benchmark result。

---

# 18. 文档做到 10/10

文档分四类：

```text
docs/product/
docs/architecture/
docs/development/
docs/operations/
```

## 必备

```text
README.md
docs/architecture/overview.md
docs/architecture/invariants.md
docs/architecture/tool-protocol.md
docs/architecture/state-model.md
docs/architecture/event-model.md
docs/architecture/security-model.md
docs/development/testing.md
docs/development/adding-a-tool.md
docs/development/adding-a-capability.md
docs/operations/deployment.md
docs/operations/recovery.md
docs/operations/observability.md
docs/adr/
docs/graveyard.md
```

## ADR

每个大决策单独记录：

```text
ADR-0001-single-master.md
ADR-0002-no-program-semantic-router.md
ADR-0003-agentloop-as-tool.md
ADR-0004-canonical-event-log.md
ADR-0005-model-driven-tool-discovery.md
```

这样 `ARCHITECTURE_STABLE.md` 不再无限膨胀。

---

# 19. 产品定位做到 10/10

不要只说：

> AI Coding Agent。

推荐：

> **Veya — an open agent runtime for persistent, tool-using AI.**

中文：

> **Veya：面向长期任务、工具执行与闭环验证的开放 Agent Runtime。**

Coding 是第一应用面，而不是最终定义。

## 差异化三角

### 1. Persistent

不是一次性 chat，而是：

```text
session
memory
goal
resume
long task
```

### 2. Executable

不是只回答：

```text
tools
sandbox
permissions
delegation
```

### 3. Verifiable

不是执行完就算：

```text
evidence
audit
replay
Veya Loop
evaluation
```

真正需要传达的是：

```text
Think → Act → Verify → Learn
```

而不是“我支持几十个工具”。

---

# 20. 开源成熟度做到 10/10

Veya 1.0 前补齐：

```text
LICENSE
CONTRIBUTING.md
CODE_OF_CONDUCT.md
SECURITY.md
SUPPORT.md
CHANGELOG.md
ROADMAP.md
GOVERNANCE.md
```

## Release

采用 SemVer：

```text
0.7 architecture consolidation
0.8 protocol stabilization
0.9 public beta
1.0 stable runtime contracts
```

定义：

- Python API compatibility；
- Tool ABI compatibility；
- Event schema compatibility；
- config compatibility；
- database migration policy。

## Release checklist

每个 release：

```text
tests
eval
security scan
SBOM
signed artifact
migration notes
breaking changes
benchmark delta
```

---

# 21. 推荐的 Veya 版本路线图

## Veya 0.7 — Consolidation

目标：停止架构漂移。

必须完成：

- architecture manifest；
- 单 Master CI invariant；
- legacy import ledger；
- current-main typecheck；
- hard-fail smoke；
- ToolSpec v1；
- EventSpec v1；
- 状态权威源设计；
- 默认依赖第一次拆分。

**本阶段原则：不新增大型 feature。**

---

## Veya 0.8 — Runtime Hardening

目标：让高自治执行可相信。

完成：

- Tool contract tests；
- permission capability model；
- sandbox profiles；
- immutable event log；
- replay；
- memory provenance；
- fault injection；
- overall coverage ≥75%；
- kernel/security ≥90%；
- telemetry dashboard。

---

## Veya 0.9 — Public Beta

目标：陌生开发者能够安装、理解、扩展。

完成：

- clean-room install；
- Windows/macOS/Linux smoke；
- plugin/capability SDK；
- API docs；
- examples；
- public eval benchmark；
- CONTRIBUTING / SECURITY / CHANGELOG；
- migrations；
- release automation。

---

## Veya 1.0 — Stable Agent Runtime

发布条件：

```text
Single Master invariant: PASS
Tool ABI v1: frozen
Event ABI v1: frozen
State authority: single-source
Critical coverage ≥95%
Overall coverage ≥80%
Silent empty response = 0
Side-effect audit coverage = 100%
Full E2E on 3 OS = PASS
Security suite = PASS
Eval regression = PASS
Docs architecture sync = PASS
SemVer / migration contract = PASS
```

---

# 22. 建议的 PR 执行序列

为了防止再次“大改一片”，建议严格小 PR。

## PR-01 Architecture Truth

新增：

```text
architecture/manifest.yaml
scripts/check_architecture_manifest.py
```

只描述现状，不改运行行为。

## PR-02 CI Reality Fix

- mypy 切到 current core；
- smoke hard fail；
- 加 architecture job。

## PR-03 Legacy Inventory

生成完整 import graph，输出：

```text
docs/LEGACY_MIGRATION.md
```

## PR-04 Remove Dead AgentLoop Main-Path Artifacts

确认 `agent_loop_bridge.py` 无有效依赖后删除；若仍有依赖则先迁移。

## PR-05 ToolSpec v1

只引入 metadata 与 contract，不改 tool behavior。

## PR-06 EventSpec v1

统一 SSE / trace / audit / state correlation ID。

## PR-07 State Authority

落 canonical event model + projection，不一次性迁全部 memory。

## PR-08 Dependency Split

先移动明显重包到 extras。

## PR-09 Security Contracts

权限、sandbox、tool side-effect contract。

## PR-10 Eval Harness

固定 50–100 个高价值场景。

## PR-11 Observability

统一 trace schema + dashboard export。

## PR-12 Public Runtime Docs

重写 README / architecture / extension guide。

---

# 23. 每个维度从当前分数到 10 分的具体路径

## Agent 架构理念：8.7 → 10

缺口：

- 原则主要存在于文档；
- 新 feature 仍可能绕过原则。

完成：

- architecture manifest；
- invariants；
- ADR；
- CI enforcement。

---

## 主链设计：9.0 → 10

缺口：

- legacy 调用点；
- compatibility 复杂度；
- Coordinator 职责仍偏多。

完成：

- 清零聊天 legacy；
- coordinator lifecycle-only；
- cancellation/retry/session invariant 全测。

---

## Tool / Delegation：8.5 → 10

缺口：

- registry 中央字典持续膨胀；
- metadata 不统一；
- 并发安全靠名单；
- ToolResult 不够结构化。

完成：

- ToolSpec / ToolResult；
- capability metadata；
- contract tests；
- Delegate ABI。

---

## Sandbox / Permission：8.0 → 10

缺口：

- 安全能力存在，但需要统一能力模型；
- 需要系统性 adversarial regression。

完成：

- capability token；
- sandbox profiles；
- side-effect declaration；
- security nightly。

---

## Observability：7.8 → 10

缺口：

- 多个 trace / audit / event 概念可能分散。

完成：

- 一个 trace ID；
- 标准 span；
- 端到端关联；
- SLO。

---

## 模块边界：6.5 → 10

缺口：

- server / veya / 3O / loop / agents / services 能力交错；
- alias / legacy 增加理解成本。

完成：

- Kernel / Runtime / Capability / Interface 四层；
- forbidden dependency graph；
- legacy 清零。

---

## Dependency Hygiene：6.2 → 10

缺口：

- heavy packages 默认安装。

完成：

- extras；
- import/cold-start budget；
- SBOM；
- vulnerability gate。

---

## Testing：6.8 → 10

缺口：

- coverage 仍低；
- CI typecheck 有错位；
- smoke 有软通过。

完成：

- critical 95%；
- overall 80%；
- contracts；
- fault injection；
- multi-OS E2E。

---

## 文档一致性：6.5 → 10

缺口：

- stable / graveyard / current code 会漂移。

完成：

- manifest 生成；
- ADR；
- docs CI；
- deprecated API auto report。

---

## 产品定位：6.0 → 10

缺口：

- “AI 编码 Agent”过于同质化。

完成：

```text
Persistent + Executable + Verifiable Agent Runtime
```

把 Veya Loop 与安全执行作为核心差异化。

---

## 开源成熟度：5.0 → 10

缺口：

- 对外长期维护契约不足。

完成：

- governance；
- security process；
- SemVer；
- changelog；
- signed release；
- extension SDK；
- public benchmark。

---

# 24. 不应该做的事情

在 0.7 Consolidation 完成前，尽量不要：

1. 再加入新的大型 agent 模式；
2. 再增加一个 planner/router；
3. 再创建新的 session authority；
4. 再创建第二套 task state machine；
5. 再引入新的“大一统 orchestrator”；
6. 因 token 多就恢复关键词工具裁剪；
7. 继续把 capability 直接堆进 `tool_registry.py` 中央表；
8. 仅因为某开源项目有功能就直接内化；
9. 以提交数量作为进度；
10. 用 LLM judge 替代 deterministic correctness / security gate。

以后每个新能力必须先回答：

```text
这是 Kernel 能力还是 Capability？
是否已有相同状态机？
是否已有相同事件？
是否已有相同权限模型？
是否能作为插件实现？
删除它会不会影响 Kernel？
```

如果最后一个答案是“会”，就要非常谨慎。

---

# 25. 10/10 Scorecard

建议加入：

```text
docs/SCORECARD.md
```

每个 release 更新一次。

| Gate | Target |
|---|---|
| 单一聊天主链 | 100% |
| Deprecated 主链 imports | 0 |
| Tool contract pass | 100% |
| Side-effect tool audit | 100% |
| Critical security coverage | ≥95% |
| Kernel coverage | ≥90% |
| Overall coverage | ≥80% |
| Static type checked current core | 100% |
| Silent empty responses | 0 |
| E2E OS coverage | Linux/macOS/Windows |
| Architecture docs drift | 0 |
| Critical eval regression | 0 |
| Supply-chain high severity unresolved | 0 |
| State replay consistency | 100% |
| Capability dependency violation | 0 |

只有全部满足，才是真正意义上的“10 分”。

---

# 26. 最终目标

Veya 不应该最终变成：

```text
一个拥有 200 个工具、30 个 agent、10 个 router 的超级项目
```

而应该变成：

```text
一个很小、很稳定的 Agent Kernel
+
一套严格的 Runtime / Safety / State Contract
+
一组可以快速增加或删除的 Capability
+
一个能够验证执行结果并持续学习的 Veya Loop
```

最终判断一个能力是否设计正确，可以使用一个非常简单的问题：

> **删除这个 capability 后，Veya Kernel 是否仍然完整工作？**

如果答案是“是”，边界大概率正确。

如果答案是“否”，就要检查是否把业务能力重新塞进了 Kernel。

---

# 27. 推荐的最终仓库心智模型

```text
Veya
│
├── Kernel
│   ├── Master ReAct
│   ├── Model Protocol
│   ├── Tool Protocol
│   ├── Event Protocol
│   └── Session Protocol
│
├── Runtime
│   ├── AuthZ
│   ├── Sandbox
│   ├── Audit
│   ├── Telemetry
│   ├── Storage
│   └── Recovery
│
├── Capabilities
│   ├── Code / Hicode
│   ├── Web
│   ├── Vision
│   ├── MCP
│   ├── Skills
│   ├── Delegation
│   ├── Goal
│   └── Veya Loop
│
└── Interfaces
    ├── CLI
    ├── Web
    ├── API
    ├── TUI
    └── VSCode
```

**核心越小越好，能力越丰富越好。两者不能混为一件事。**

---

# 28. 第一阶段实际执行清单

建议现在就从以下顺序开始，不再先加 feature：

- [ ] 新建 `architecture/manifest.yaml`
- [ ] 新建 `docs/architecture/invariants.md`
- [ ] 新建 `docs/adr/ADR-0001-single-master.md`
- [ ] 新建 `docs/adr/ADR-0002-model-owned-tool-decision.md`
- [ ] 新建 `docs/adr/ADR-0003-agentloop-as-tool.md`
- [ ] 修正 CI mypy 检查对象
- [ ] 删除 smoke 的 `|| true`
- [ ] 建立 legacy import scanner
- [ ] 确认并处理 `server/agent_loop_bridge.py`
- [ ] 对 `server/coordinator.py` 四个剩余调用点逐个分类迁移
- [ ] 定义 `ToolSpec v1`
- [ ] 把 `_PARALLEL_SAFE_TOOLS` 迁移为 ToolSpec metadata
- [ ] 把 `_TOOL_GROUPS` 迁移为 capability metadata
- [ ] 定义 `ToolResult v1`
- [ ] 定义 `EventSpec v1`
- [ ] 定义 canonical state authority
- [ ] 建立 tool contract test harness
- [ ] 把 coverage gate 从 50 分阶段提升至 60 → 70 → 80
- [ ] 把 pandas/numpy/pyarrow/plotting 类重依赖迁入 extras
- [ ] 建立最小安装 smoke
- [ ] 建立固定 Agent Eval benchmark
- [ ] 增加 fault-injection suite
- [ ] 完成 OpenTelemetry 风格 trace correlation
- [ ] 重写 README 定位为 Agent Runtime
- [ ] 0.7 期间冻结大型 feature 合入

---

## 结论

Veya 距离“10 分”最主要的距离，不是模型不够强，也不是工具不够多。

真正的差距是：

```text
能力已经很多
↓
需要把能力收敛到稳定协议
↓
把协议变成自动化 invariant
↓
让 state / tool / event / safety 都有唯一事实源
↓
用测试、eval、trace 和 CI 证明它不会退化
```

当这一层完成以后，Veya 才会从“高速成长的 Agent 项目”升级为：

> **可以长期承载 Agent 能力演进的 Runtime。**

而 Veya 最值得长期形成壁垒的方向，不是再造更多 router 或 agent persona，而是：

> **Persistent execution + governed tools + verifiable closed loop。**

这三者组合，才是 Veya 与普通 coding agent 真正拉开差距的位置。
