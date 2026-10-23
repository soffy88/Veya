# Veya × harness2 集成方案

> 目标：把 [google-research/harness2](https://github.com/google-research/harness2)（递归 agent harnessing：任务级 Propose-Probe-Compose + 持久化 improvement harness）的方法论集成进 Veya，让 Veya 原生具备"为当前任务改进自身 harness、并把可迁移经验沉淀到持久库"的能力。
>
> 本文档只规划，不改 Veya 现有代码。文末 DONE 标记完成。

---

## 1. 两边是什么

### 1.1 harness2（被集成方）

harness2 是一个**即插即用的递归 harness 优化框架**（Apache-2.0，论文 arXiv:2610.14224）。核心思想：在模型权重固定的前提下，通过两层递归改进"模型周围的一切"（harness）：

- **任务级递归**：对当前任务执行 Propose-Probe-Compose——提出 harness 编辑、在**同一任务上**对照执行、从行为对比中组合出更优 harness。**全程无 ground truth**。
- **Harnessing 级递归**：把被采纳的 harness 和证据支持的教训写入持久的 *improvement harness*（按领域组织的经验库），指导后续任务的改进。

关键代码事实（已核对源码）：

| 机制 | 实现位置 | 要点 |
|---|---|---|
| PPC 循环编排 | `harness2/core/recursion.py` | `run_parallel`（宽：2k 候选 + k 组合）/ `run_sequential`（深：k 轮嵌套）；恒等式 R=4k rollouts、P=3k sessions |
| Editor / Composer / Reflector | **没有** `editor.py`/`composer.py`/`reflector.py` 文件——三者都是概念角色，由**同一个 `Proposer` LLM session** 通过不同 prompt job（`prompts.propose` / `aggregate` / `compose_round` / `experience`）实现 |
| 可编辑单元 | `core/tree.py` + `core/prompts.py::_SURFACE_OPENCODE/_CODEX` | 一棵 `Tree = dict[str, str]`（相对路径 → 文件内容）；proposer **直接在磁盘上编辑 `context/harness/`，"你的编辑就是提案"** |
| 8 组件 | `core/prompts.py` 的 surface 表 | 见 §2 映射表 |
| 执行器适配 | `substrate/{base,opencode,codex}.py` + `core/spec.py` 的 `BenchmarkAdapter` Protocol | 整个集成面就是实现一个 Adapter：`execute` / `trajectory` / `deliverable_text` / `base_harness` / `validate_tree` / `score` 等 |
| 证据装配 | `core/context.py` | proposer 不把证据塞进 prompt，而是写成**证据树文件**（task.md / rollouts/ / deliverables/ / harness/ / pool/ / library/），prompt 只是短指针 |
| 盲选 | `core/selection.py` | 候选匿名化（seeded shuffle → A..Z），selector 只看轨迹+交付物选一个；**方法内部刻意不看分数** |
| Reflector / 持久化 | `core/selection.py::reflect_adoption` + `core/library.py` | 采纳后跑 experience session 提炼 `cue/lesson/evidence`，写入 `<root>/<mode>/<domain>/INDEX.md + harnesses/<task>/ + EXPERIENCE.md`；按 domain 隔离，`retain()` 只保留同领域早期条目 |
| 预算账本 | `core/ledger.py` | 记录 USD 成本、缓存命中、重试次数；`assert_identity()` 校验 R/P 恒等式 |
| 安全边界 | `core/context.py` / `core/tree.py` | `assert_no_leak_paths`、`assert_no_grader_verdicts`、`validate_tree`、`autotrigger`——任何 rollout 前强制 |
| Judge | `core/judge.py` | **在方法之外**，只由 CLI 事后调用；judge 失败 = None，不是 0 分 |

Proposer 通道：`opencode`（bun 直调 opencode CLI）或 `claude_cli`，与 solver 通道（被改进的执行器）解耦。带内容寻址缓存（SHA-256 fingerprint）。

### 1.2 Veya（宿主）

- **主链路（冻结）**：单一入口 = 单一 LLM 的 ReAct 循环。`platform/3O/oservi/oservi/master_agent.py::MasterAgent.chat_stream` 每轮：语义模型调用（全量工具 schema）→ 解析 tool_calls → 执行（并行安全则 gather）→ 失败原文回喂反思。零程序路由、零工具面裁藏、零预抓。
- **工具系统**：`server/tool_registry.py::MasterToolRegistry`，`register(name, description, parameters, func, ...)`，~155+ 工具；MCP 网关可折叠为单工具。
- **子代理**：`agent_loop_run` 工具（`tool_registry.py` L3633）→ `DelegateRuntime` + `SpawnGuard`，子任务用 `tool_group` 裁剪工具面，是**工具**不是第二主链路（冻结架构 §5）。
- **技能**：`server/skill_hub.py::VeyaSkillHub`，`~/.veya/skills/<name>/manifest.json` + `run.py`；dispatcher 模式下折叠为 `list_skills` / `run_skill` 两个工具。
- **记忆**：`server/memory_bank.py`（全局偏好台账 `~/.veya/global_memory.json`，编译进 system prompt 的 subconscious 块）；`server/memory_controller.py`（PersonalRuntime 候选，**需人工审核**）；`server/decision_ledger.py`；`server/context_graph.py`。
- **持久执行**：`server/goal_run/`（G1 Plan → G2 Loop → G3 Finalize，事件源 + checkpoint）。
- **Hooks**：`hooks/` 七个生命周期点（pre_dispatch / pre_tool / post_tool / pre_result / post_result / squad_stop / session_start），决策优先级 halt > block > modify > pass。
- **LLM 层**：`veya/obase/llm.py` 多 provider 兜底链（opencode-go DeepSeek V4.1 Flash → 免费池 → 本地 gpt-5.6-luna）。

### 1.3 为什么值得集成

Veya 当前缺的正是 harness2 提供的：**没有任务级自我改进循环，没有跨任务可迁移经验库**。现有的 memory/distill 是"用户偏好/事实"的被动台账，且需人工审核；harness2 的 Library 是"组件级编辑 + 证据 + 教训"的主动经验库，按领域生长。两者互补而非替代。

---

## 2. 组件映射：harness2 的 8 组件 → Veya 对应物

| # | harness2 组件 | harness2 文件路径（OpenCode surface） | Veya 对应物 | 集成难度 |
|---|---|---|---|---|
| c1 | system prompt | `systemprompt.md` | `master_agent.py::MASTER_SYSTEM_PROMPT` + `_HOST_SOP_APPEND`（**编译时常量**）+ subconscious 注入 | ⚠️ 高：需先"文件化" |
| c2 | guardrails | `guardrails.md` | `hooks/`（pre_dispatch / permission / redact）+ `tool_guard.py` 策略链 | ⚠️ 中：语义等价但形态不同（代码 vs 文件） |
| c3 | memory | `LongTermMEMORY.md` | `memory_bank.py` 台账 + `history_store` 会话史 | 🟢 低：台账已经是数据 |
| c4 | tool guidance | `tool_descriptions/tool_guidance.md` | 工具 schema 的 `description` 字段 + `_tool_inventory()` 文本块 | 🟢 低：数据已在 |
| c5 | subagents | `sub_agents/<name>/AGENT.md` | `agent_loop_run` 的 `tool_group` + `SubagentOrchestratorEngine` | ⚠️ 中：Veya 用工具面裁剪替代 AGENT.md |
| c6 | skills | `skills/<name>/SKILL.md` | `~/.veya/skills/<name>/manifest.json` + `run.py` | 🟢 低：结构几乎一致 |
| c7 | scripts | `scripts/<name>.py` | `tools/` 确定性工具 + `code_exec` | 🟢 低 |
| c8 | plugins | `plugin/<name>.ts` | `hooks/` 生命周期钩子 + MCP 网关 | ⚠️ 中：Veya 无 tool-loop 内插件等价物 |

**关键差异（决定集成形态）**：harness2 的 8 组件全部是**磁盘上的文件树**，proposer 可整树编辑；Veya 的 c1/c2/c8 是**编译期 Python 代码**。这是集成的核心摩擦点，见 §3 方案与 §5.1 风险。

---

## 3. 集成方案对比与推荐

### 方案 A：Veya 作为 harness2 的 executor adapter（外部改进层）

```
harness2 核心（原样复用）
  ├─ Proposer（独立 channel：opencode/claude_cli 直调 harness2 自带 CLI）
  ├─ 证据树 / PPC 循环 / 盲选 / Library（harness2 原生）
  └─ VeyaBenchmarkAdapter（新写）
       execute()      → 启动一个隔离 Veya session 跑任务（CLI headless 或 MasterCoordinator.chat_stream）
       trajectory()   → 读 history_store 持久化日志，渲染 [ACTION]/[OBSERVATION] 语法
       deliverable_*()→ 读任务产物文件
       base_harness() → 导出 Veya 当前 c1–c8 快照到 harness2 surface 路径
```

- **优点**：零侵入冻结主链路；harness2 方法论立刻可验证；失败不影响 Veya 线上。
- **缺点**：改进产物（采纳的 harness）在 Veya 之外，需要额外同步机制（人工或 Phase 3 审核流）才能回流到 Veya。

### 方案 B：Propose-Probe-Compose 内建为 Veya 新工具（推荐的目标形态）

完全模仿 `agent_loop_run` 的注册模式：

```
MasterToolRegistry.register(
    name="recursive_harness_run",
    description="为当前任务改进 harness：提出编辑→对照执行→组合更优 harness（无 ground truth）",
    ...
)
```

模型自主判断何时调用（与 `agent_loop_run` 同级，不构成第二主链路，符合冻结架构 §5）。工具内部实现简化版 PPC；**持久化位置：`~/.veya/harness_library/{mode}/{domain}/`**（镜像 harness2 Library 布局）。

- **优点**：原生能力，模型可自主使用；持久化在 Veya 内；复用 harness2 core 模块（可 vendor 或 import）。
- **缺点**：需要新增 `server/harness/` 模块；proposer session 需要一个独立于主脑池的模型通道配置。

### 方案 C：harness2 整体替代 Veya 的 prompt/技能/工具装配层

与冻结架构正面冲突（单一大模型入口、零程序判断），**不推荐**。

### 推荐路径

**Phase 1 走方案 A 验证方法论在 Veya executor 上有效 → Phase 2 落地为方案 B 的工具 + Library → Phase 3 把 reflector 产出接入 PersonalRuntime 审核流回流 Veya**。Phase 1 的 adapter 代码在 Phase 2 直接复用为工具的执行后端。

---

## 4. 新增模块与接口设计

### 4.1 新增文件（Phase 2 目标形态）

```
server/harness/
├── __init__.py
├── adapter.py        # VeyaBenchmarkAdapter：实现 harness2 spec.BenchmarkAdapter Protocol
│                     #   execute() 用 agent_loop_run 后端（SpawnGuard 隔离）
├── channel.py        # 新增 "veya" proposer channel：包 veya/obase/llm.py 的 llm_call
├── library.py        # Library 持久化（移植 harness2 core/library.py，root 改 ~/.veya/harness_library）
├── selection.py      # 盲选 + reflect_adoption（移植 harness2 core/selection.py）
├── prompts.py        # Veya 版 BenchPrompts 模板（8 组件 surface 换成 Veya 文件树）
└── recursion.py      # 任务级 PPC（移植 harness2 core/recursion.py，调 Veya channel）

server/tool_registry.py          # 注册 recursive_harness_run 工具（唯一改动点）
veya/obase/llm.py                # 不改；channel.py 只调用其公开 llm_call
```

复用策略：harness2 的 `core/`（recursion / context / ledger / tree / prompts / spec / judge）与 `substrate/base.py` 是**纯 Python、零 Google 依赖**的模块，可直接 vendor 进 `server/harness/` 或作为 git submodule 引入；只需重写 `surface` 表（Veya 路径）和 `adapter.py`。

### 4.2 关键接口

```python
# server/harness/adapter.py
class VeyaBenchmarkAdapter:
    """把 Veya 包装成 harness2 的执行器。"""
    spec: BenchSpec            # Veya surface + prompts（c1–c8 的 Veya 路径表）
    substrate: SubstrateLike   # VeyaSubstrate（新写，surface 指向 Veya base harness 目录）

    def execute(self, task_id, harness_dir, run_id, *, timeout=None) -> Path:
        """用 agent_loop_run 后端在隔离 session 中执行任务。
        harness_dir 的 c1/c6 等文件被注入子 session 的 system prompt / skills。"""
        ...

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        """读 history_store 日志，渲染 [ACTION]/[OBSERVATION]/[THINKING]/[ERROR]。"""
        ...

    def deliverable_text(self, run_dir: Path) -> str: ...
    def base_harness(self) -> Path:
        """导出当前 Veya c1–c8 快照到 harness surface 目录（每次 Phase 1 启动时生成）。"""
        ...

# server/tool_registry.py 新增注册（示意，不实际改代码）
master_tools.register(
    name="recursive_harness_run",
    description="为当前任务改进 harness（Propose-Probe-Compose，无 ground truth）。"
                "适合长任务、重复域任务、或主链连续失败时。",
    parameters={...},
    func=_recursive_harness_run,
    parallel_safe=False,
    side_effect=True,
)
```

### 4.3 base harness 文件化的过渡设计（Phase 1 前置）

Veya 的 c1（system prompt）当前是 Python 常量。Phase 1 **不改主链路代码**，而是：

1. 写一个导出脚本 `scripts/export_base_harness.py`：把 `MASTER_SYSTEM_PROMPT` + `_HOST_SOP_APPEND` + 当前 hooks 策略摘要 + 工具 inventory + `~/.veya/skills/` 清单 + `~/.veya/global_memory.json` 快照，渲染成 harness2 OpenCode surface 路径的文件树，输出到 `~/.veya/harness/base/`。
2. Phase 2 若要让 proposer 真正编辑 c1 并**回流**，再把 `MasterAgent.get_system_prompt()` 改为"常量 → `~/.veya/harness/base/systemprompt.md` 存在则读文件"的兜底逻辑。**这是对冻结主链路的改动，必须先获用户批准**（见 §7 风险 R1）。

---

## 5. Improvement harness 的持久化位置

三层，各司其职（避免与现有 memory 机制重叠）：

| 层 | 位置 | 内容 | 生命周期 |
|---|---|---|---|
| 任务级证据 | `~/.veya/harness/runs/<run_id>/<task_id>/` | 证据树、候选 harness、rollout、selection.json（镜像 harness2 `runs/`） | 可归档/清理 |
| 持久经验库 | `~/.veya/harness_library/{mode}/{domain}/` | `INDEX.md` + `harnesses/<task_id>/harness/` + `EXPERIENCE.md`（cue/lesson/evidence + 采纳的完整 harness 树） | 长期生长；`retain()` 只保留同领域早期条目 |
| 候选回流 | `~/.veya/memory/`（PersonalRuntime 现有审核流） | reflector 产出的 experience 条目 → 人工审核 → 晋升进 base harness | 人工闸门 |

边界约定：**Library 存"组件级编辑 + 证据 + 可迁移教训"；PersonalRuntime 存"用户偏好与事实"**。前者是 harness 工程证据，后者是用户模型，互不覆盖。

---

## 6. 无 ground truth 时的评估信号

harness2 的 selection 是盲选（只看轨迹 + 交付物，不看分数）。Veya 场景多数任务没有 rubric，按优先级可用的信号：

1. **交付物存在性/完整性**（最强）：文件是否产出、是否通过编译/lint/测试（Veya 已有 ruff/mypy 传感器 + `test_gate` hook 可复用为信号源）。
2. **轨迹效率**（harness2 原生）：步数、工具调用数、连续错误重试次数、`MAX_NO_PROGRESS_ROUNDS` 触发次数、token 成本、wall time。
3. **重跑稳定性**（Veya 独有优势）：同一任务跑 k 次，成功率的方差。
4. **下游 LLM-as-judge**：selector session 看完整轨迹+交付物，无答案打分（harness2 的 `prompts.select` 模式）。
5. **用户显式反馈**：会话中纠正、重做请求、thumbs——作为长期信号回注 reflector。
6. **禁忌**：绝不用答案 key / grader verdict 进 proposer 上下文（harness2 的 `assert_no_grader_verdicts` 必须移植）。

---

## 7. 风险

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | **冻结架构冲突**：让 proposer 编辑 c1/c2/c8 需要把常量文件化，触及主链路 | 🔴 高 | Phase 1 只读快照（不改主链路）；回流改动单独向用户报批 |
| R2 | **成本失控**：R=4k rollouts + P=3k sessions，Veya 全是大模型调用 | 🔴 高 | Ledger 预算闸门 + `--dry-run` 预演 + k 从 1 起 + Proposer 内容寻址缓存 + 独立 proposer 模型通道（可用便宜模型） |
| R3 | **proposer 模型能力不足**：harness2 验证过 GLM-5/Gemini 3.5 Flash，Veya 主脑池是 DeepSeek V4.1 Flash | 🟡 中 | 先用 harness2 `DemoProposer` 无模型跑通流程（Phase 0），再小成本验证真实 proposer |
| R4 | **安全边界**：harness 作者本身是 agent，可能编辑出危险 harness | 🟡 中 | 移植 `validate_tree` / `assert_no_leak_paths`；Veya 侧 ToolGuard/governance 继续生效；候选 harness 只在隔离 session 执行 |
| R5 | **状态污染**：候选 harness 的 rollout 不能污染主 session 历史 | 🟡 中 | 全部走 `agent_loop_run` 的 SpawnGuard 隔离 session；Library 写入前先 `assert_tree_has_no_grader_verdicts` |
| R6 | **与现有 distill 机制重叠**：可能造出第二套记忆系统 | 🟢 低 | §5 三层边界约定；reflector 产出默认只进审核流，不自动激活 |
| R7 | **harness2 依赖钉死**：opencode 等依赖有 pinned commit | 🟢 低 | 只 vendor `core/` + `substrate/base.py`（纯 Python），不碰 benchmark/CLI |

---

## 8. 分阶段实施步骤与工作量

| 阶段 | 内容 | 工作量 |
|---|---|---|
| **Phase 0** 流程验证 | 写 `VeyaBenchmarkAdapter` + `export_base_harness.py`；用 harness2 `DemoProposer`（无模型调用）在 2 个 Veya 任务上跑通 k=1 全循环，产出 `~/.veya/harness/runs/` | 3–5 天 |
| **Phase 1** 真实 proposer | 接入 harness2 `Proposer`（独立模型通道，配 `HARNESS2_IMPROVER_MODEL` 风格环境变量）；2–3 个任务跑 k=1 parallel，验证盲选 + 轨迹信号可用 | 1–2 周 |
| **Phase 2** 内建工具 | `server/harness/` 模块落地；注册 `recursive_harness_run` 工具；Library 持久化到 `~/.veya/harness_library/`；预算闸门 | 2–3 周 |
| **Phase 3** reflector 回流 | `reflect_adoption` → experience → PersonalRuntime 审核流；人工批准后晋升进 base harness（需 R1 批准） | 1–2 周 |
| **Phase 4** 评估闭环 | judge 信号集成（test_gate 传感器 + 用户反馈 + LLM-as-judge）；报告工具 | 持续 |

最脆假设：**DeepSeek V4.1 Flash 能胜任 proposer 的编辑任务**；若 Phase 1 验证失败，proposer 通道换更强模型（harness2 的 `ChannelSpec` 本就支持 proposer ≠ solver），不阻塞主线。

---

DONE
