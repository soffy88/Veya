# W18 — DeepEval 接入 Veya 现有评测（SPEC）

状态：设计定稿（2026-10-13）。分支 `feat/w18-deepeval`，基于 canonical main。

## 0. 目标与非目标

**目标**：把 [DeepEval](https://github.com/confident-ai/deepeval) 作为**可选打分后端**接进
Veya 已有的评测链路，让「答案相关性 / 任务完成度 / 工具调用正确性 / 忠实度」这些
LLM-judge 指标有现成的、社区维护的实现，而不在 Veya 里重写一套 judge。

**非目标（明确不做）**：
- **不建第二套评测框架**。Veya 已有 `oskill.eval_suite`（纯统计原语）+ `server/agent_eval.py`
  （真实 `chat_stream` → 打分 → 统计）+ `scripts/run_agent_eval.py`（CLI）。DeepEval 只是
  `_score_result` 旁边**多一个可插拔的 Scorer**，不是并行的一根管线。
- **不改 `oskill` 主库**（3O 只读 submodule）。
- **不引入付费 key**。裁判模型一律走 Veya 现有 `veya.llm.llm_call` 的免费池
  （`veya1.2` 主脑 / `veya-free` / `veya1.2-free` 免费轮询）。
- **不新增第二套存储 / 调度 / 事件**。分数就落在 `EvalRun.details` 里，跟现有字段同构。

## 1. 许可证

DeepEval 是 **Apache-2.0**（`gh api repos/confident-ai/deepeval` → `spdx_id: Apache-2.0`）。
允许使用、修改、再分发。已在 `THIRD_PARTY_NOTICES.md` 追加条目（append-only）。

## 2. 指标选型

| DeepEval 指标 | 对应 Veya 关心的维度 | 输入 | 为什么选 |
|---|---|---|---|
| `AnswerRelevancyMetric` | **答案相关性** | input / actual_output | 回答跑题是最常见的退化，现成实现 |
| `TaskCompletionMetric` | **任务完成度** | input / actual_output | 用户诉求「任务是否真做完」，与 W12 verifier-gated finish 同旨 |
| `ToolCorrectnessMetric` | **工具调用正确性** | tools_called / expected_tools | Veya 是工具型 agent，工具选错是核心失败模式 |
| `FaithfulnessMetric` | **幻觉 / 忠实度** | input / actual_output / retrieval_context | 有检索上下文的场景防编造 |
| `GEval`（自定义 criteria） | 兜底任意自定义判据 | input / actual_output / criteria | 上面四个覆盖不到的领域判据 |

**映射到现有 `EvalCase`**：`EvalCase.input` → input；`EvalCase.expected` 里的
`tool` 字段 → `expected_tools`；`chat_stream` 返回的 `tool_calls` → `tools_called`；
`reply` → `actual_output`。**不新增 `EvalCase` 字段**——能映射的就映射，映射不了的
（如 retrieval_context）留空，指标按 DeepEval 自己的语义处理缺失。

## 3. 接在哪（三个接入点，同一个适配层）

1. **`server/agent_eval.py`** —— 加 `deepeval_scorer(case, agent_result)`，与现有
   `_score_result` 并列。`run_agent_eval_suite(..., use_deepeval=False)` 默认关，开了就
   在规则分之外**再记一份 DeepEval 分**到 `run.details[case.id]["deepeval"]`，**不覆盖**
   规则分（规则分仍是 `run.scores` 的权威，保证现有统计/基线对比不变）。
2. **`scripts/bot_landing_live.py`** —— 加 `--deepeval` 开关。S1–S12 每条真实回合结束后，
   对 `reply` 跑一次指标，打印 `deepeval=S1 answer_relevancy=0.9 ...`。**默认关**，
   不影响现有 PASS/FAIL 退出码（DeepEval 分只是附加信息，不改变 pass/fail 判定）。
3. **W12 supervisor** —— 完成验证时**可选**用 DeepEval 加一道忠实度检查。flag
   `VEYA_DEEPEVAL`（默认关）。关时行为与今天完全一致；开时在
   `build_progress_report` 的 `is_done` 分支对「最新证据 vs 目标」跑一次
   `TaskCompletion`，把分数放进 report 的 `deepeval` 字段（纯附加，不改变 is_done）。

## 4. 裁判模型怎么走现有 router

DeepEval 要求一个 `DeepEvalBaseLLM` 子类。我们实现 `VeyaJudgeModel`，其
`generate()` / `a_generate()` 直接调 `veya.llm.llm_call(messages, provider=..., model=...)`，
provider 默认 `veya1.2`（主脑，免费）。**不加付费 key，不直连任何上游。**

- 同步 `generate()`：`llm_call` 是 async 的，用 `asyncio.run` 桥接（DeepEval 同步路径
  少用；主用 `a_generate`）。
- 结构化输出：DeepEval 部分指标会传 `schema=`。`VeyaJudgeModel` 把 schema 描述塞进
  prompt，让模型只吐 JSON，再 `json.loads` 回来。解析失败 → 抛异常，由指标的
  `error` 字段捕获（见 §5），**不 panic 整个 suite**。

## 5. 超时 / 失败降级（核心安全约束）

**裁判不可用绝不阻塞主流程。**

- `VeyaJudgeModel` 每次调用包一层 `asyncio.wait_for(timeout=VEYA_DEEPEVAL_TIMEOUT)`，
  默认 30s。
- 任何异常（超时 / 无 key / 解析失败 / 网络）→ 该指标的 `score = None`，在
  `run.details[case.id]["deepeval"][metric]` 记 `{"score": None, "error": "..."}`，
  **标记 `unknown`，不记 0 分**。0 分会污染均值，`None` 才是「不知道」的诚实表示
  （与现有 `unnecessary_tool_rate` 无数据返回 `None` 同一约定）。
- `deepeval` 包**未安装** → `server/deepeval_adapter.py` 的 `available()` 返回 False，
  所有 DeepEval 调用变成 no-op，返回 `{"enabled": False, "reason": "deepeval not installed"}`。
  **现有测试和主链零感知**（优雅降级）。
- suite 级别：单个用例的 DeepEval 失败只影响该用例的 deepeval 段，不影响规则分。

## 6. 可选依赖声明

`pyproject.toml` 加 extra：

```toml
[project.optional-dependencies]
deepeval = ["deepeval>=1.0"]
```

生产安装：`uv pip install 'veya[deepeval]'`（或 `pip install 'veya[deepeval]'`）。
**默认不装**，未装即优雅降级。

## 7. 测试策略

`tests/test_w18_deepeval.py`，**离线可跑**：
- 注入假裁判（`VeyaJudgeModel(llm_fn=fake)`），不碰网络、零成本。
- 覆盖：未安装时 `available()==False` 且 no-op；装了但裁判超时 → `score=None` + error；
  正常路径 → 分数进 `details`，`run.scores` 规则分**不变**；flag 关时 W12 行为不变。

## 8. 缺口（诚实记录）

- `retrieval_context` 目前 Veya 的 `EvalCase` 没有这个字段，`FaithfulnessMetric` 只在
  调用方显式提供上下文时才有意义——本 wave 只接线，不补检索链路。
- DeepEval 的 `TaskCompletionMetric` 会**自己**调工具/agent，这里只用它对**已有输出**
  打分（`actual_output` 传入），不让它再跑一遍 agent（那是第二套执行）。
- 离线测试不校验真实模型分数质量，只校验接线正确 + 降级正确。
