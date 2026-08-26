# Veya Product Runtime Specification
## P1–P3 可实施完整规格（修正版）

> 文件建议路径：`docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md`
> 适用仓库：`soffy88/Veya`
> 目标版本：`0.7.x → 0.8.x → 1.0.0`
> 规格状态：Implementation Ready
> 架构基线：严格服从 `docs/ARCHITECTURE_STABLE.md`
> 日期：2026-08-24

---

# 0. 产品目标

Veya 的最终目标不是“更多 Agent”，而是构建一个真正可以长期使用的 Personal Agent Runtime：

```text
能干活
  ↓
能养成
  ↓
能恢复
  ↓
能验证
  ↓
能长期用
```

统一产品闭环：

> **Ask → Act → Persist → Resume → Verify → Learn**

用户只需要说：

```text
“帮我做 X”
```

系统内部必须保持：

```text
User
 ↓
MasterAgent 单一 ReAct 主链
 ↓
模型自主调用 Tool / Hicode / AgentLoop / GoalRun / Veya Loop
 ↓
真实工具轨迹
 ↓
任务结果
 ↓
持久状态
 ↓
Memory / Skill / Trajectory / Eval
```

---

# 1. 不可违反的架构约束

## A-01 单一用户主链

唯一聊天主链：

```text
Interface
  → MasterCoordinator
  → MasterAgent
  → ReAct
  → Tool Calls
  → MasterAgent Final Synthesis
  → Event Stream
```

禁止：

- keyword intent router；
- 程序前置判断任务类型；
- 程序替模型决定是否抓 URL；
- 程序根据关键词裁工具；
- 程序按任务类型切 Agent；
- AgentLoop 接管整个用户请求；
- GoalRun 接管用户语义判断；
- legacy Coordinator 再次成为默认聊天入口。

## A-02 AgentLoop 只能是工具

合法：

```text
MasterAgent
  → agent_loop_run(...)
  → isolated subtask
  → structured result
  → MasterAgent
```

非法：

```text
User
  → AgentLoop
  → second main loop
```

## A-03 GoalRun 只能是 Durable Execution Capability

GoalRun 可以负责：

- DAG ready 判断；
- 显式 dependency；
- checkpoint；
- retry；
- quota；
- durable execution；
- acceptance execution；
- deterministic scheduling；
- explicit parallel marker。

GoalRun 不负责：

- 理解用户真实意图；
- 决定 persona；
- 决定调用哪个主 Agent；
- 重新解释自然语言任务；
- 替代 MasterAgent 做最终语义判断。

定义：

```text
MasterAgent = semantic authority
GoalRun     = durable execution authority
```

## A-04 Task 状态是 Projection，不是主链控制器

允许任务状态：

```text
pending
running
waiting_approval
completed
failed
cancelled
```

这些状态必须由事件投影得到。

禁止：

```python
if task.status == "running":
    choose_executor(...)
```

Task 状态只描述发生了什么，不决定 MasterAgent 应如何思考。

## A-05 前端只展示真实执行事实

允许：

- tool requested；
- tool started；
- tool output summary；
- approval required；
- task progress；
- cost；
- cancel；
- failure；
- completion。

禁止虚构：

- “正在深度思考”；
- “正在推理第 N 步”；
- 模型没有真实产生的内部思维；
- 虚构 Agent 工作状态。

---

# 2. 总体架构

```text
┌─────────────────────────────────────────────────────────┐
│                     EXPERIENCE                          │
│ Web / CLI / TUI / API                                  │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│                    MASTER AGENT                         │
│ Single LLM ReAct                                       │
│ Model-owned Tool Decisions                             │
└───────────────────────┬─────────────────────────────────┘
                        │
         ┌──────────────┼──────────────┐
         │              │              │
         ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ Capabilities │ │ Durable Work │ │ Learning     │
│ Tool / MCP   │ │ GoalRun      │ │ Memory       │
│ Vision       │ │ AgentLoop    │ │ Skills       │
│ Hicode       │ │ Team         │ │ Trajectory   │
└──────┬───────┘ └──────┬───────┘ └──────┬───────┘
       └────────────────┼──────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────┐
│                    AGENT RUNTIME                        │
│ Events / State / Session / AuthZ / Sandbox / Audit     │
│ Telemetry / Recovery / Eval / Storage                  │
└─────────────────────────────────────────────────────────┘
```

---

# 3. 版本范围

## 0.7.x — P1：能干活

必须完成：

1. 统一主界面；
2. 跨入口统一会话；
3. Task Center；
4. 真实任务生命周期；
5. cancel / approval / progress / cost；
6. `veya doctor` 会话一致性检查；
7. 单主链 invariant 回归测试。

## 0.8.x — P2：能养成

必须完成：

1. 跨会话 Memory；
2. Memory provenance / confidence / correction；
3. 可教 Skill；
4. Skill provenance / version / usage evidence；
5. Resume 产品化；
6. 多 Agent 共享任务上下文；
7. Acceptance Contract；
8. Trajectory → Memory/Skill Candidate；
9. 长任务可恢复。

## 1.0.0 — P3：能长期用

必须完成：

1. 权限体验成熟；
2. Sandbox Profile；
3. 稳定性与可观测性；
4. 端到端恢复；
5. 多入口一致性；
6. release / migration / upgrade；
7. 文档与示例；
8. 关键 E2E 全绿；
9. Memory / Resume / Safety / Eval 达标。

桌面 App 不作为 1.0 硬门槛。

建议：

```text
1.0 = Runtime Stable
1.1 = Desktop Distribution
```

---

# 4. Canonical Event Model

P1 可继续兼容 `veya.history_store`，但所有新增功能必须 Event-first。

目标事件封装：

```python
class EventEnvelope:
    event_id: str
    trace_id: str
    session_id: str
    task_id: str | None
    turn_id: str | None
    topic: str
    ts: datetime
    actor: str
    payload: dict
    schema_version: int
```

最小事件：

```text
session.created
turn.started
message.user_added
message.assistant_added

tool.requested
tool.approval_required
tool.approved
tool.denied
tool.started
tool.completed
tool.failed
tool.cancelled

task.created
task.started
task.waiting_approval
task.completed
task.failed
task.cancelled

checkpoint.created
resume.started
resume.completed
resume.failed

memory.candidate_created
memory.committed
memory.corrected
memory.superseded
memory.forgotten

skill.candidate_created
skill.created
skill.updated
skill.executed
skill.failed

delegate.started
delegate.completed
delegate.failed

goal.started
goal.updated
goal.completed

trajectory.recorded
eval.recorded
```

1.0 最终目标：

```text
Immutable Event Store
  ├── History Projection
  ├── Session Projection
  ├── Task Projection
  ├── Compaction Projection
  ├── Memory Projection
  └── Replay/Audit Projection
```

---

# 5. Session 规格

Session ID：

```text
sess_<uuid7>
```

要求：

- CLI / Web / TUI 使用同一 session_id；
- Session 不绑定入口；
- Session 不绑定进程；
- 可跨重启；
- 可 attach；
- 可 resume。

API：

```http
GET  /api/v1/sessions
POST /api/v1/sessions
GET  /api/v1/sessions/{session_id}
GET  /api/v1/sessions/{session_id}/events
POST /api/v1/sessions/{session_id}/attach
POST /api/v1/sessions/{session_id}/resume
```

CLI：

```bash
veya sessions
veya attach <session_id>
veya resume
veya resume <session_id>
```

验收：

```text
CLI 开始
→ Web 打开同 session
→ 历史一致
→ Web 继续
→ TUI 查看
→ Task 状态一致
```

禁止入口私有 shadow history。

---

# 6. Task Center 规格

```python
class Task:
    id: str
    session_id: str
    workspace_id: str | None

    title: str
    objective: str

    status: Literal[
        "pending",
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "cancelled",
    ]

    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    current_step: str | None
    progress: float | None

    acceptance: list["AcceptanceCriterion"]
    latest_checkpoint_id: str | None

    cost_usd: float
    trace_id: str | None
```

状态投影：

```text
task.created            → pending
task.started            → running
tool.approval_required  → waiting_approval
tool.approved           → running
task.completed          → completed
task.failed             → failed
task.cancelled          → cancelled
```

API：

```http
GET  /api/v1/tasks
GET  /api/v1/tasks/{task_id}
POST /api/v1/tasks/{task_id}/cancel
POST /api/v1/tasks/{task_id}/resume
GET  /api/v1/tasks/{task_id}/events
```

过滤：

```text
workspace
status
date
session
```

---

# 7. Web 主界面

```text
┌──────────────┬─────────────────────────────┬──────────────┐
│ Sessions     │ Conversation / Task         │ Context      │
│ Tasks        │                             │ Tools        │
│              │ Real execution timeline     │ Files        │
│              │                             │ Cost         │
└──────────────┴─────────────────────────────┴──────────────┘
```

左栏：

- Recent Tasks；
- Sessions；
- Workspace filter；
- running；
- waiting approval；
- failed；
- continue previous task。

中栏：

- 用户消息；
- 最终模型回复；
- 真实工具轨迹；
- approval；
- progress；
- cancellation；
- checkpoint；
- resumed 标识。

右栏：

- workspace；
- files changed；
- tool calls；
- active capabilities；
- acceptance；
- cost；
- trace_id；
- latest checkpoint。

输入区默认只有：

```text
“帮我做 X”
```

不暴露 Agent Selector。

允许：

```text
Agent Mode
Plan Mode
```

Plan Mode：

- read-only；
- 禁止写文件；
- 禁止 side-effect tool；
- 允许探索与 create_plan；
- 高影响工具拒绝。

---

# 8. Progress / Cost / Cancel

Progress 只能来源于真实可计算事件：

- GoalRun completed nodes；
- tool events；
- explicit progress；
- acceptance completion。

无法计算时：

```text
progress = null
```

UI 显示“执行中”，不能伪造百分比。

Cost：

```python
class CostRecord:
    model: str
    prompt_tokens: int
    completion_tokens: int
    tool_cost: float
    total_usd: float
```

Cancel 必须传播：

```text
UI
→ Task Runtime
→ Master Turn
→ Active Tool
→ Delegate
→ GoalRun
```

取消后：

- 不得静默 completed；
- 尽力取消工具；
- 必须生成 `task.cancelled`；
- 创建安全 checkpoint。

---

# 9. Permission / Approval

Risk Level：

```text
R0 read-only
R1 local write
R2 process execution
R3 network write
R4 destructive / privileged
```

权限决策：

```python
class PermissionDecision:
    action: Literal["allow", "deny", "ask"]
    reason: str
    scope: str
```

继承：

```text
user
 ↓
workspace
 ↓
session override
```

默认冲突：

```text
deny > ask > allow
```

Profile：

### READ_ONLY

```text
read = allow
write = deny
exec = deny
network_write = deny
```

### DEVELOPMENT

```text
read = allow
workspace_write = allow
test_exec = allow
external_write = ask
destructive = ask
```

### PRODUCTION

```text
read = allow
write = ask
exec = ask
external_write = ask
destructive = deny
```

---

# 10. Memory 规格

Memory 必须采用 Scope × Type 二维模型。

Scope：

```text
user
workspace
session
```

Type：

```text
episodic
semantic
procedural
preference
decision
```

```python
class MemoryRecord:
    id: str

    scope_type: Literal["user", "workspace", "session"]
    scope_id: str

    memory_type: Literal[
        "episodic",
        "semantic",
        "procedural",
        "preference",
        "decision",
    ]

    content: str

    source_event_ids: list[str]
    confidence: float

    created_at: datetime
    last_verified_at: datetime | None

    supersedes: list[str]
    status: Literal[
        "active",
        "superseded",
        "invalidated",
        "forgotten",
    ]

    tags: list[str]
```

## Memory Candidate

禁止关键事件直接写最终 Memory。

流程：

```text
Event
 ↓
Memory Candidate
 ↓
Dedup
 ↓
Conflict Detection
 ↓
Confidence
 ↓
Provenance
 ↓
Commit
```

```python
class MemoryCandidate:
    id: str
    proposed_content: str
    proposed_scope: str
    proposed_type: str
    source_event_ids: list[str]
    confidence: float
    reason: str
```

Tool：

```text
memory_search
memory_write
memory_correct
memory_supersede
memory_forget
```

禁止程序按关键词自动检索 Memory 注入主链。

允许 deterministic candidate extraction，但低置信候选不能直接作为事实注入。

Memory Correction：

```text
old memory → superseded
new memory → active
```

禁止简单 overwrite。

---

# 11. Skill 规格

Skill 表示：

> How Veya should act.

Memory 表示：

> What Veya knows.

```python
class SkillSpec:
    id: str
    name: str
    version: int

    description: str
    trigger_examples: list[str]

    parameters_schema: dict

    execution_type: Literal[
        "prompt",
        "tool_chain",
        "python",
        "external",
    ]

    execution_ref: str

    created_by: str
    source_event_ids: list[str]

    created_at: datetime
    updated_at: datetime

    trust_status: Literal[
        "trusted",
        "review_required",
        "blocked",
    ]

    success_count: int
    failure_count: int
```

教学流程：

```text
User: “以后代码审查按这个 checklist”
 ↓
MasterAgent 提议 Skill
 ↓
skill.candidate_created
 ↓
UI 展示 Draft
 ↓
用户确认
 ↓
Static Scan
 ↓
Semantic Advisory Scan
 ↓
Permission Manifest
 ↓
Registry
```

Tool：

```text
skill_search
skill_run
skill_create
skill_update
skill_delete
skill_show
```

CLI：

```bash
veya skill list
veya skill show <name>
veya skill edit <name>
veya skill delete <name>
```

LLM semantic scan 只能作为 advisory detector，不能成为唯一安全边界。

---

# 12. Resume / Checkpoint

必须创建 checkpoint：

- 高影响工具前；
- 高影响工具后；
- approval 前；
- approval 后；
- GoalRun node 完成后；
- Delegate 完成后；
- context compaction 前；
- task failure；
- graceful cancellation。

```python
class Checkpoint:
    id: str
    task_id: str
    session_id: str

    event_cursor: str

    active_tool_calls: list[str]
    completed_steps: list[str]
    pending_steps: list[str]

    acceptance_state: dict

    created_at: datetime
```

恢复流程：

```text
Load Canonical Events
 ↓
Rebuild Projections
 ↓
Find Latest Safe Checkpoint
 ↓
Inspect Dangling Tool Calls
 ↓
Never Blindly Replay Non-idempotent Tool
 ↓
Reconstruct Context Projection
 ↓
MasterAgent Resumes ReAct
```

Dangling Tool：

```text
tool.started
但无 tool.completed
```

恢复规则：

1. PURE_READ：允许安全重试；
2. side-effect：
   - 检查 evidence；
   - 检查外部状态；
   - 必要时用户确认；
3. 禁止自动重放危险操作。

---

# 13. Acceptance Contract

```python
class AcceptanceCriterion:
    id: str
    description: str

    type: Literal[
        "command",
        "test",
        "file_exists",
        "schema",
        "manual",
        "llm_review",
    ]

    required: bool
    evidence: list[str]

    status: Literal[
        "pending",
        "passed",
        "failed",
        "blocked",
    ]
```

可靠性优先级：

```text
deterministic evidence
  >
runtime evidence
  >
test
  >
static check
  >
LLM review
```

LLM Review 不允许替代 deterministic correctness。

---

# 14. AgentLoop Delegation

```python
class DelegateRequest:
    objective: str
    context_ref: str
    capability_scope: list[str]
    acceptance: list[AcceptanceCriterion]
    budget_usd: float | None
    max_rounds: int
    deadline: datetime | None
```

```python
class DelegateResult:
    status: str
    summary: str
    evidence: list[str]
    acceptance_results: list[dict]
    cost_usd: float
    child_trace_id: str
```

限制：

```text
max delegation depth = 2
```

子 Agent：

- 不得修改 parent canonical history；
- 不得扩大 capability scope；
- 不得接管用户 session；
- 结果必须回到 MasterAgent。

---

# 15. GoalRun

GoalRun 定位：

```text
Durable Task Executor
```

输入：

```python
class GoalRunSpec:
    goal: str
    tasks: list
    dependencies: list
    parallel_markers: list
    acceptance: list
    budget: dict
    workspace: str
```

允许：

- ready node；
- blocked dependency；
- `[P]` parallel marker；
- quota；
- retry；
- checkpoint；
- acceptance；
- task lifecycle。

禁止：

- 原始 prompt persona 分类；
- 通用 semantic router；
- 第二聊天入口；
- 跳过 MasterAgent 最终汇总。

---

# 16. Trajectory

每个完成或失败 Task 生成：

```python
class Trajectory:
    task_id: str
    objective: str

    steps: list[dict]
    tool_calls: list[dict]

    outcome: str
    acceptance_results: list[dict]

    failures: list[dict]
    recovery_actions: list[dict]

    cost_usd: float
    duration_ms: int

    trace_id: str
```

Trajectory 是 Eval / Learning 的事实输入。

---

# 17. Eval

Eval Case：

```yaml
id: coding.fix_bug.001
prompt: "修复..."
environment: ...
expected_capabilities:
  - code
forbidden_actions:
  - external_network_write
success_criteria:
  - pytest_pass
max_cost_usd: 1.0
max_rounds: 20
```

指标：

```text
Task Success Rate
Acceptance Pass Rate
Tool Precision
Unnecessary Tool Rate
Recovery Rate
Resume Success Rate
Memory Precision
Skill Reuse Success
Cost / Successful Task
Latency / Successful Task
Human Intervention Rate
```

关键 benchmark regression：

```text
> 3% absolute drop
```

默认阻止 release。

---

# 18. Learning Gate

禁止：

```text
single failure
→ auto modify skill
```

允许：

```text
Trajectory
 ↓
Eval
 ↓
Pattern
 ↓
Memory/Skill Candidate
 ↓
Confidence
 ↓
Human/Policy Gate
 ↓
Commit
```

---

# 19. Telemetry

统一关联：

```text
trace_id
session_id
task_id
turn_id
```

必须贯穿：

```text
model call
tool call
approval
delegate
GoalRun node
checkpoint
resume
memory write
skill run
eval
```

指标：

```text
turn_success_rate
task_success_rate
empty_response_rate
tool_failure_rate
resume_success_rate
approval_wait_time
first_token_latency
task_latency
tokens_per_task
cost_per_task
memory_search_hit_rate
memory_correction_rate
skill_success_rate
```

---

# 20. SSE

统一：

```json
{
  "event_id": "...",
  "trace_id": "...",
  "session_id": "...",
  "task_id": "...",
  "topic": "tool.started",
  "ts": "...",
  "payload": {}
}
```

要求：

- heartbeat；
- reconnect；
- last-event-id；
- duplicate-safe；
- large output truncation；
- backpressure。

---

# 21. Error UX

所有错误必须告诉用户：

```text
发生了什么
当前任务是否安全
是否可以继续
推荐下一步
可复制命令
```

示例：

```text
工具执行超时，任务未标记为完成。
你可以继续当前任务，Veya 会从最近 checkpoint 恢复。

veya resume task_xxx
```

---

# 22. Doctor

`veya doctor --json` 至少检查：

```text
model provider
workspace
history store
event store
session consistency
checkpoint consistency
memory index
skill registry
sandbox
permission config
tool registry
GoalRun
SSE endpoint
version/migration
dangling_tool_calls
projection_drift
```

---

# 23. Dependency / Packaging

建议：

```text
veya
veya[web]
veya[tui]
veya[vision]
veya[data]
veya[sandbox]
veya[desktop]
veya[dev]
veya[all]
```

至少重新评估并逐步拆出：

```text
pandas
numpy
pyarrow
matplotlib
plotly
networkx
```

---

# 24. Feature Flags

```text
VEYA_TASK_CENTER_V1
VEYA_SESSION_UNIFIED_V1
VEYA_MEMORY_V2
VEYA_SKILL_TEACH_V1
VEYA_RESUME_V2
VEYA_TOOL_CONTRACT_V1
VEYA_EVENT_STORE_V1
VEYA_PERMISSION_PROFILES_V1
```

规则：

- 有 owner；
- 有删除日期；
- stable 后删除旧实现；
- 不允许永久双轨。

---

# 25. Migration Strategy

## M0 Observe

记录 current history / session / memory / task / event，不改行为。

## M1 Dual Write

```text
current write
+
event write
```

read 仍旧。

## M2 Shadow Projection

从 Event Store 重建 history/task/session，与旧结果 diff。

## M3 Read Switch

小流量切换。

## M4 Authority Switch

Event Store 成 canonical source。

## M5 Remove Legacy

删除旧写路径。

---

# 26. CI Gate

必须增加：

```text
architecture
single-master
tool-contract
event-schema
state-replay
resume
memory
skill
security
e2e
```

Coverage：

```text
overall >= 80%
kernel >= 90%
authz/security >= 95%
event/state >= 95%
resume >= 95%
tool protocol >= 95%
```

---

# 27. E2E 测试矩阵

## E2E-01 First Task

```text
install
→ init
→ start
→ task
→ tool
→ result
```

## E2E-02 Cross Entry

```text
CLI start
→ Web continue
→ TUI inspect
```

## E2E-03 Approval

```text
high-risk tool
→ waiting_approval
→ approve
→ continue
```

## E2E-04 Cancel

```text
long tool
→ cancel
→ safe task state
```

## E2E-05 Crash Resume

```text
running
→ kill process
→ restart
→ resume
→ no duplicated side effect
```

## E2E-06 Memory

```text
user correction
→ memory candidate
→ commit
→ new session
→ memory_search
→ correct use
```

## E2E-07 Skill

```text
teach skill
→ confirm
→ new session
→ skill search
→ skill run
→ success
```

## E2E-08 Delegation

```text
Master
→ AgentLoop
→ child tool
→ result
→ final acceptance
```

## E2E-09 GoalRun

```text
multi-task goal
→ parallel safe nodes
→ failure
→ retry branch
→ acceptance
```

---

# 28. Security Test Matrix

```text
path traversal
symlink escape
shell injection
prompt injection from tool output
secret leakage
network policy bypass
permission race
resume duplicate side-effect
untrusted skill subprocess
oversized tool output
cancel-during-write
TOCTOU
```

---

# 29. P1 PR 顺序

## P1-01 Task Event Projection

交付：

```text
task events
TaskProjection
tests
```

## P1-02 Unified Session API

交付：

```text
global session IDs
session list
attach
resume endpoint
```

## P1-03 Web Task Center

交付：

```text
left task/session panel
task status
workspace filter
```

## P1-04 Real Tool Timeline

交付：

```text
tool events
progress
cost
cancel
```

## P1-05 Approval UI

交付：

```text
plan mode
approval modal
permission profile selector
```

## P1-06 CLI/TUI Session Attach

交付：

```text
veya sessions
veya attach
veya resume
```

## P1-07 Doctor Consistency

交付：

```text
session consistency
dangling calls
projection check
```

---

# 30. P2 PR 顺序

## P2-01 MemoryRecord v2

引入：

```text
scope
type
provenance
confidence
status
```

## P2-02 Memory Candidate

禁止关键事件直接写最终 Memory。

## P2-03 Memory Toolset

```text
search
write
correct
supersede
forget
```

## P2-04 SkillSpec v1

统一 metadata / provenance / version。

## P2-05 Skill Teaching UX

```text
candidate
confirm
scan
registry
```

## P2-06 Resume v2

基于 Event Cursor + Safe Checkpoint。

## P2-07 Acceptance Contract

统一 normal task / GoalRun / AgentLoop acceptance。

## P2-08 Delegation Context Package

共享：

```text
objective
constraints
acceptance
completed evidence
```

## P2-09 Trajectory

Task 完成后产出结构化 trajectory。

## P2-10 Learning Candidate Pipeline

Trajectory → Memory/Skill Candidate。

---

# 31. P3 PR 顺序

## P3-01 Permission Profiles

READ_ONLY / DEVELOPMENT / PRODUCTION。

## P3-02 Sandbox Profiles

统一资源与 capability policy。

## P3-03 SSE Reliability

heartbeat / reconnect / last-event-id / backpressure。

## P3-04 Telemetry v1

trace correlation + export。

## P3-05 Full E2E Suite

install → task → resume → memory → skill。

## P3-06 Migration / Upgrade

```text
veya upgrade --check
veya migrate
```

## P3-07 Docs

Quickstart、Examples、Troubleshooting。

## P3-08 Release Automation

Release Notes / Tag / Artifact / optional PyPI。

---

# 32. Desktop App（1.1）

优先 Tauri。

职责：

```text
package Web UI
manage local Veya service
system tray
file drag/drop
startup
service detection
```

桌面 App 不拥有自己的 session / task / memory authority。

所有数据仍来自 Runtime API。

---

# 33. 1.0 Release Gate

以下全部满足：

```text
Single Master invariant              PASS
AgentLoop-as-tool                    PASS
GoalRun non-semantic authority       PASS
Task projection                      PASS
Cross-entry session consistency      PASS
Crash resume                         PASS
No duplicate side-effects on resume  PASS
Memory provenance                    PASS
Memory correction                    PASS
Skill teach/reuse                    PASS
Skill security gate                  PASS
Tool permission audit                100%
Critical security coverage           >=95%
State/Event coverage                 >=95%
Kernel coverage                      >=90%
Overall coverage                     >=80%
Silent empty response                0
Critical Eval regression             0
E2E Linux                            PASS
E2E macOS                            PASS
E2E Windows                          PASS
Docs build                           PASS
Migration test                       PASS
```

---

# 34. 产品成功指标

Activation：

```text
veya start
→ first real task <= 30 sec
```

Reliability：

```text
task runtime non-provider success >= 99.5%
resume success >= 99%
silent empty response = 0
```

Learning：

```text
memory precision >= 95%
memory correction success >= 99%
skill reuse success >= 90%
```

Usability：

```text
Agent selector exposure = 0
cross-entry manual sync = 0
```

---

# 35. 不做事项

P1–P3 期间禁止：

- 新增第二主链；
- 新增固定 persona pipeline；
- 新增独立 session authority；
- 新增另一套 task engine；
- keyword routing；
- 程序语义裁工具；
- Memory 无 provenance 写入；
- Skill 无 trust gate 加载；
- LLM judge 代替 deterministic test；
- Desktop 提前拖慢 1.0 Runtime。

---

# 36. 最终产品定义

> **Veya — A persistent, executable, recoverable, verifiable and learnable Personal Agent Runtime.**

中文：

> **Veya：一个可持续记忆、可安全执行、可恢复、可验证、可长期协作的个人 Agent Runtime。**

真正差异化：

```text
Persistent
+
Executable
+
Recoverable
+
Verifiable
+
Learnable
```

而不是：

```text
更多工具
更多 Agent
更多 Router
```

---

# 37. 最终验收场景

第一次安装：

```text
veya start
```

用户：

```text
“帮我修这个项目的登录 bug”
```

Veya 必须：

1. 自动理解任务；
2. 自主查看代码；
3. 必要时调用 Hicode；
4. 必要时调用 AgentLoop；
5. 展示真实工具轨迹；
6. 写文件前按权限策略确认；
7. 实际运行测试；
8. 对照 acceptance 验收；
9. 保存 Task / Session / Checkpoint；
10. 任务完成。

第二天：

```text
“继续昨天那个”
```

Veya：

1. 找到 Task；
2. 恢复事件事实；
3. 找到安全 Checkpoint；
4. 不重复副作用；
5. 继续 ReAct。

一周后：

```text
“以后这个项目代码审查都按昨天那个标准”
```

Veya：

1. 创建 Skill Candidate；
2. 用户确认；
3. 安全扫描；
4. 注册 Skill。

一个月后：

```text
“帮我审这个 PR”
```

Veya：

1. 找到项目 Memory；
2. 找到审查 Skill；
3. 执行；
4. 验收；
5. 记录 Trajectory；
6. 形成可验证的改进证据。

当这一整条链可靠运行时，Veya 才真正完成：

> **下载就能干活 → 能养成 → 能长期用。**
