# RFC-05: MasterAgent Cognitive Policy — 减少无谓工具调用

> 状态：implemented（2026-08-23，用户明确要求"先按你建议执行"）
> 依据：用户提供的《Veya MasterAgent Cognitive Policy Spec v1.0》文档
> 范围：`ARCHITECTURE_STABLE.md` §4 定义的"改 LLM 层"红线——本次改动已获得用户
> 明确同意才动手，不是纯文档/测试类改动。

## 1. 目的

用户诉求原话："目的是为了更智能回复更准确，不要动不动就调用工具。"

对照用户提供的 spec 文档（三层 Constitution/SOP/Runtime Context 架构、
"direct-answer-first"策略、7 种"架构退化"清单、Trust Plane 语义框定）与代码
现实调研（见下）后，判断**不做完整三层重构**，只做三处最小、最直接命中诉求的
改动。

## 2. 代码现实调研结论（2026-08-23）

- MasterAgent 系统提示词是一整块字符串常量 `MASTER_SYSTEM_PROMPT`
  （`platform/3O/oservi/oservi/master_agent.py:110-218`），**在 3O 子库里**，
  没有 Constitution/SOP/Runtime Context 命名分层。真正拼装发生在
  `master_agent.py::get_system_prompt()` 和 veya 宿主层
  `coordinator_master.py:499`（`_slim_master_prompt(MASTER_SYSTEM_PROMPT) +
  _HOST_SOP_APPEND`）。
- `tool_search` 只在 `VEYA_MASTER_LITE_TOOLS=1`（默认关闭，本部署 `.env`/
  `deploy/.env` 未设置）时才进入模型视野，本部署当前不受影响。
- `run_in_sandbox` 的既有描述（`tool_registry.py:1500-1519`）只限定了用途范围
  （"仅用于执行/验证代码片段"），没有"证据/验证事实"这层显式语义标注。
- `docs/ARCHITECTURE_STABLE.md` §2.1 记录的"零程序判断"原则是**开发者文档**，
  从未被拼进 system prompt——模型看不到这条原则，现状是"代码里没有路由分支"，
  不是"模型知道不该这样做"。
- 生产环境没有统计"简单问题触发几次工具调用"这类行为遥测；唯一沾边的是
  `server/agent_eval.py` 的离线手动 eval（`AGENT_EVAL_CASES`），当时只有 3 条
  用例（direct_answer/tool_success/tool_failure_recovery）。

## 3. 决策：为什么不做完整三层重构

1. **收益跟诉求不匹配**：重新组织文字的物理位置（分层/改名）不会让模型更克制
   地调用工具，把正确的指导写得更清楚、更靠前才会。三层架构主要解决的是长期
   可维护性，不是"减少无谓工具调用"这个具体问题的直接杠杆。
2. **真正的 prompt 常量在 3O 子库**：按这个仓库"3O 单一来源"的纪律
   （见 `docs/dev/veya-3o-assembly.md` §1.4），veya 层不该直接改子库文件；
   要做真正的三层重构需要先改 `platform/3O/oservi` 本体，那是另一件更大、
   风险不同的事，不该在这次诉求里顺带做掉。
3. **veya 宿主层已经有对应的追加机制**（`_slim_master_prompt`/
   `_HOST_SOP_APPEND`），足以承载这次改动，不需要新基础设施。

## 4. 实际改动

### 4.1 `_HOST_SOP_APPEND` 新增 "# ANSWER FIRST" 段（`coordinator_master.py:207`）

```
# ANSWER FIRST
Before reaching for any tool, ask: can I answer this directly from what I already know?
Complexity or depth is NOT a reason to use tools — reason it through natively. Only use
tools when you need to: change something real (files/code/state), verify a fact you're not
certain of, or fetch information you don't have (current events, this repo's actual state,
runtime behavior). When unsure, default to answering directly first.
```

放在 `_HOST_SOP_APPEND` 的最前面（在 `# HANDS` 之前），呼应 spec 里"先判断能不
能直答，复杂不是调工具的理由"这条核心诉求。

### 4.2 `run_in_sandbox` 描述补充语义框定（`server/tool_registry.py:1500-1519`）

在原有用途限定之后插入一句：

> "This tool exists to produce evidence for a fact you're not certain of (does
> this import work, does this snippet behave as expected) — it is not a default
> step, and not needed just because a question involves code or is hard to
> reason about."

呼应 spec §38.4（Runtime Feasibility）"这个工具是用来产生证据的，不是默认动作"
这层语义，明确排除"问题涉及代码/问题很难 = 该用 sandbox"这个误判。

### 4.3 `agent_eval.py::AGENT_EVAL_CASES` 新增 5 条用例（取自 spec §38.1-38.5 原始例句）

| id | 取自 | 断言 |
|---|---|---|
| `stable_knowledge_no_tools` | §38.1 "什么是闭包？" | `max_tool_calls: 0` |
| `complex_reasoning_no_tools` | §38.2 "从认识论...分析 self-talk" | `max_tool_calls: 0` |
| `repo_state_needs_inspection` | §38.3 "Veya 当前 Python 版本是多少？" | `min_tool_calls: 1`（新增字段，见下） |
| `runtime_feasibility_needs_probe` | §38.4 "pydantic 能 import 吗？" | `tool: run_in_sandbox` |
| `code_modification_needs_delegation` | §38.5 "修复 auth.py 并跑测试。" | `tool: hicode_run` |

`_score_result` 新增 `min_tool_calls` 断言支持（`agent_eval.py`，与既有
`max_tool_calls` 对称）——没有这个就无法诚实地断言"这类问题应该至少查一次"，
用固定工具名断言会太脆（模型完全可能合理地选 `grep` 或 `read_file_ast` 或
`mcp_codebase_*` 中任意一个）。

§38.6（Worker Claim 语义）**没有加对应用例**——那是"worker 返回结果该被当作
Claim 还是直接当 verified completion"这类语义处理问题，不是"调不调工具/调几次"
的次数问题，现有 `max_tool_calls`/`min_tool_calls`/`tool` 断言机制回答不了，
勉强凑一个用例只会是形式主义。

## 5. 明确不做的部分

- 完整 Constitution/SOP/Runtime Context 三层重构（见 §3）。
- 生产实时行为遥测——离线 `agent_eval.py` 用例足够验证这次改动有没有效果，
  真需要持续监控再评估要不要建。
- Trust Plane 语义框定应用到主对话链的 worker 委托（`hicode_run`/`dsh` 等）——
  `server/goal_run/trust_plane.py`（P1）已经把这套语义用在 goal_run 任务粒度，
  但主对话链的临时委托是完全不同的调用形状，套用需要单独设计，不在这次范围。

## 6. 验证

- `tests/test_coordinator_cognitive.py`/`tests/test_master_tools.py`/
  `tests/test_hosted_sandbox.py`/`tests/test_sandbox_cmd_normalize.py`：改动后
  全部照旧通过（prompt 文本追加不改变既有工具调用/收尾逻辑，验证零回归）。
- `tests/test_agent_eval_suite.py`：更新 `_good_llm` fixture 覆盖新增 5 个场景，
  新增 2 个 `_score_result` 直接单测（`min_tool_calls`/`max_tool_calls`），
  修正原来写死的 `summary["n"] == 3` 为 `== len(AGENT_EVAL_CASES)`（避免下次
  加用例又要改一遍这行）。
