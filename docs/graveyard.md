# 墓碑册（Graveyard）— 已被取代/退役的架构与组件

> 目的: 记录"曾经是什么、为什么不再是"，防止未来把踩过的坑当成新功能"优化回去"。
> 权威现状见 [`ARCHITECTURE_STABLE.md`](ARCHITECTURE_STABLE.md)。

## 🪦 意图分类 + DAG 分解（Coordinator → Squad → Engine）

- **代码**: `server/coordinator.py`（~99KB, `Coordinator`/`VeyaCoordinator`）、`veya/intent.py`（双层意图分类器）。
- **曾被视为核心成果**: `docs/BENCHMARK_AND_GAPS.md` 的 G2（LLM 意图分类）。
- **为何退役**: 冻结架构 §2.1 判定"程序前置路由/意图分类"是"不回复/乱调工具"的根因——
  截胡长文/URL、工具面裁藏把模型带偏。主链路改为单 LLM 零程序判断。
- **当前残留（2026-08-22 复核, 本条已过期）**: CLI(`cli/main.py`)、IM(`veya/oskill/im/telegram.py`,
  `dingtalk.py`)、`server/automata.py` 三处已于 2026-08-09"整改A+C"提交统一到
  `coordinator_master`，不再引用本组件。真正残留仅 `server/backends.py`、
  `server/routes/session.py`、`server/routes/prompt.py`、`server/routes/flow.py`
  四处——且 `routes/flow.py` 背后是有独立前端(`FlowConsole.svelte`)的 Genesis
  需求→审批→生成多阶段 HITL 工作流，不是聊天主链的重复实现，迁移前需先确认
  `coordinator.handle()`/`resume()` 的阶段状态机/审批语义在 `MasterCoordinator`
  里有没有对应能力，不能当成纯装配点收编来做（评估见 memory
  project_veya_pi_gap_audit）。
- **四处难度分级（2026-08-24 补充, 见 `architecture/manifest.yaml`）**：`backends.py::_run_builtin`
  一次性文本进出、无 session/resume，四处里最容易迁；`routes/prompt.py::/prompt` 有
  session_id + SSE，形状接近 `chat_stream`，但需先核实 `persona` 字段跟 `chat_stream(mode=)`
  是否语义等价；`routes/flow.py` 如上，更像该显式标 `VEYA_EXECUTION_PLANE=workflow`（冻结
  架构 §2 例外条款）而不是硬迁移进聊天主链；`routes/session.py::/resume` 依赖
  `veya.compat.RunState` 检查点格式（`server/coordinator.py` 专属状态机），
  `coordinator_master` 用的是完全不同的 `history_store` 会话模型，没有对应的 RunState
  概念——硬迁移等于让现存 checkpoint 全部作废，这是数据兼容性决策，需要人拍板，
  不是代码改动能单方面解决的。

## 🪦 `AGENTS.md` 上半部分描述的 agents/ 三件套

- **文档**: `AGENTS.md` 中 `agents/plan.py`/`research.py`/`build.py`（`BaseAgent` 抽象、
  registry 自动注册）的架构叙述。
- **实际**: `agents/` 目录仅 ~115 行空壳，从未按该叙述实现。
- **为何是墓碑**: 该段是早期设想/LLM 生成的"想象架构"，非任何时期的真实实现。
  已在 `AGENTS.md` 顶部加历史标注横幅。

## 🪦 oskill 复杂路由器（router.call_aliased）

- **能力**: quality-gate 升级 / 模型切换 / 并行分派。
- **为何禁用**: 冻结架构 §2.2 实测——裸 URL 直连 opencode-go 有内容，走该路由器则空回复。
  主链路改为 `veya/llm.py` 直连 opencode-go。**禁止**重新引入。
