# 墓碑册（Graveyard）— 已被取代/退役的架构与组件

> 目的: 记录"曾经是什么、为什么不再是"，防止未来把踩过的坑当成新功能"优化回去"。
> 权威现状见 [`ARCHITECTURE_STABLE.md`](ARCHITECTURE_STABLE.md)。

## 🪦 意图分类 + DAG 分解（Coordinator → Squad → Engine）

- **代码**: `server/coordinator.py`（~99KB, `Coordinator`/`VeyaCoordinator`）、`veya/intent.py`（双层意图分类器）。
- **曾被视为核心成果**: `docs/BENCHMARK_AND_GAPS.md` 的 G2（LLM 意图分类）。
- **为何退役**: 冻结架构 §2.1 判定"程序前置路由/意图分类"是"不回复/乱调工具"的根因——
  截胡长文/URL、工具面裁藏把模型带偏。主链路改为单 LLM 零程序判断。
- **当前残留**: 仍被 CLI(`cli/main.py`)、IM(`veya/im/telegram.py`,`dingtalk.py`)、
  `server/automata.py`、`routes/flow.py` 引用（**双头脑**）。整改批次 C 负责统一到
  `coordinator_master` 后退役本组件。

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
