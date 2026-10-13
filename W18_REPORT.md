# W18 — DeepEval 接入 Veya 现有评测（REPORT）

分支 `feat/w18-deepeval`（基于 canonical main）。设计见 `W18_DEEPEVAL_SPEC.md`。

## 1. 许可证

DeepEval = **Apache-2.0**（`gh api repos/confident-ai/deepeval` → `spdx_id: Apache-2.0`）。
允许使用/修改/再分发。已按 **append-only** 约定追加到 `THIRD_PARTY_NOTICES.md`
（新增 `## DeepEval` 段；上游代码**未 vendored**，只作可选运行时依赖，故不复现其许可证全文）。

## 2. 用了哪些指标（§2 选型）

| 指标 | 维度 | 备注 |
|---|---|---|
| `AnswerRelevancyMetric` | 答案相关性 | 回答跑题是最常见退化 |
| `TaskCompletionMetric` | 任务完成度 | 与 W12 verifier-gated finish 同旨 |
| `ToolCorrectnessMetric` | 工具调用正确性 | Veya 是工具型 agent，选错工具是核心失败模式；需 `expected_tools` |
| `FaithfulnessMetric` | 幻觉 / 忠实度 | 需 `retrieval_context`，缺则该指标标 unknown（见 §6 缺口） |
| `GEval`（自定义 criteria） | 兜底判据 | criteria = 「actual_output 是否直接准确回应 input 诉求」 |

映射：`EvalCase.input` → input；`EvalCase.expected["tool"]` → expected_tools；
`chat_stream` 结果的 `reply` → actual_output，`tool_calls` → tools_called。
**未新增 `EvalCase` 字段**——能映射的映射，映射不了的留空。

## 3. 接入点（一个适配层 + 四处接线，均默认关）

新增 **`server/deepeval_adapter.py`**（唯一适配层，不建第二套框架）：

1. `server/agent_eval.py` — `run_agent_eval_suite(..., use_deepeval=False)`。
   开了就对同一份 agent 输出**再**跑 DeepEval，分数记 `run.details[case.id]["deepeval"]`。
   **规则分 `run.scores` 仍是权威**，DeepEval 分不改统计/基线。新增
   `deepeval_breakdown(run)` 按指标聚合（只统计 `score` 非 None 的用例）。
2. `scripts/run_agent_eval.py` — 新增 `--deepeval` 开关，打印 `deepeval_breakdown`。
3. `scripts/bot_landing_live.py` — 新增 `--deepeval`，S1–S12 每条场景附加打印一行
   `deepeval[...]`。**不改 PASS/FAIL 判定**，退出码只由场景断言决定。
4. `server/supervisor/reporting.py`（W12）— 报告新增 `deepeval` 字段，
   flag `VEYA_DEEPEVAL` 关时恒为 `None`（结构不变）；开时在 `is_done` 分支对
   「目标 vs 最新证据」跑一次 `task_completion`，**纯附加，不改 `is_done`**。

## 4. Flag

| flag | 默认 | 作用 |
|---|---|---|
| `VEYA_DEEPEVAL` | **关** | 仅 W12 supervisor 完成验证时的可选检查 |
| `VEYA_DEEPEVAL_TIMEOUT` | 30s | 单次裁判调用超时 |

`agent_eval`/两个 CLI 的 DeepEval 由显式参数（`use_deepeval` / `--deepeval`）驱动，默认关。

## 5. 裁判模型走现有 router（不花钱）

`VeyaJudgeModel`（`server/deepeval_adapter.py` 内动态构造的 `DeepEvalBaseLLM` 子类）
的 `generate`/`a_generate` 调 `veya.llm.llm_call(messages, provider="veya1.2")` ——
即现有 **免费池**。不加任何付费 key，不直连上游。结构化输出：把 schema 描述塞进
prompt 让模型吐 JSON。同步路径在新线程里跑，避免与调用方已在运行的 event loop 冲突
（DeepEval 的 `metric.measure()` 内部用 `run_until_complete`，在 async suite 里会炸；
适配层因此直接驱动 `a_measure`）。

## 6. 超时 / 失败降级

- 每次裁判调用包 `asyncio.wait_for(timeout)`；超时/无 key/解析失败/网络错 → 该指标
  `{"score": None, "error": "..."}` 标 **unknown**，**不记 0 分**（0 会污染均值），
  **绝不阻塞主流程**。
- `deepeval` 未安装 → `available()` 为 False，所有调用 no-op（`{"enabled": False,
  "reason": "deepeval not installed"}`）。主链与整套测试零感知。
- `run_agent_eval_suite` 里单条用例的 DeepEval 失败只影响该用例的 `deepeval` 段，
  不影响规则分。

## 7. 生产安装步骤

`deepeval` 是**可选依赖**，默认不装（未装即优雅降级）。

```bash
# 生产/CI 需要 DeepEval 指标时：
uv pip install 'veya[deepeval]'      # 或: pip install 'veya[deepeval]'
# 或直接: pip install 'deepeval==4.2.8'
```

`pyproject.toml` 已加 `deepeval = ["deepeval==4.2.8"]`（pin 到本树验证过的版本）。
**不要**往共享 `.venv`（软链到 `../veya/.venv`）安装——本 wave 用 worktree 内独立
`.w18venv` 验证。

## 8. 测试

`tests/test_w18_deepeval.py`（离线可跑，假裁判模型，零网络零成本）：

- **Part A**（共享 `.venv`，deepeval 未装）：优雅降级 + `agent_eval` 集成 +
  W12 报告附加字段 + 端到端 `use_deepeval` 接线（规则分不变）。
- **Part B**（隔离 `.w18venv`，deepeval 4.2.8 已装）：假裁判走通指标、坏裁判/超时
  降级 unknown、`tool_correctness` 缺 `expected_tools` 降级。

| 运行环境 | 结果 |
|---|---|
| 共享 `.venv`（deepeval 未装） | **7 passed, 6 skipped** |
| 隔离 `.w18venv`（deepeval 4.2.8） | **6 passed**（Part B） |

回归：见 §9。

## 9. 回归 + gates

- 回归（`test_w1*` / `test_nano_*` / `test_lorca_*` / `test_orca_*` /
  `test_p3a_bot_isolation` / `test_product_shell` / `test_llm_router` /
  `remote/test_policy_resolver`）：**973 passed, 6 skipped, 0 failed**（168.9s）。
- `~/lorca-jobs/gates.sh`：worktree 改动前后**完全一致**（`check_3o_gates` 53、
  `check_no_direct_io` 267、`check_no_reverse_dep` 4、`check_oskill_pure` 0 行），
  即**零新增**。（相对 `/data/soffy/projects/veya` base 的差异全部是 base 侧
  `server/goal_run/execution_*.py`、`server/bot_runtime/product.py` 的**预存**违规，
  非本 wave 引入。）
- Ruff：改动文件**零新增 lint**（`bot_landing_live.py` 仅剩 2 条预存错误）。

### 环境说明（重要）

本 worktree 的 `platform/3O/*` 子模块 gitlink **未 checkout（空目录）**，
`veya.platform` 装配会报 `oskill is not mounted`。按本地既有约定（见
`veya-lorca` worktree），把 5 个 3O 子模块目录**软链到主仓库已 checkout 的
submodule** 后测试才能跑；这是**本地环境步骤，不是仓库改动**（`git status
platform/3O` 干净）。

## 10. 新增模型可调用工具

**无。** 本 wave 只加评测适配，不注册任何新工具。

## 11. 缺口（诚实记录）

- `FaithfulnessMetric` 需要 `retrieval_context`，而 `EvalCase` 无此字段——只在调用方
  显式提供上下文时才有分，否则标 unknown。本 wave 只接线，不补检索链路。
- `TaskCompletionMetric` 用其对**已有输出**打分（`actual_output` 传入），不让它再跑
  一遍 agent（那是第二套执行）。
- 离线测试只校验**接线正确 + 降级正确**，不校验真实模型分数质量。
- `tool_correctness` 在无 `expected_tools` 的用例上恒为 unknown——属预期（指标缺参），
  不是 bug。
