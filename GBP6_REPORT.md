# GBP6 报告：EvoOntology 本体层接入与技能配对评测闸门 (Gated Versioning)

## 1. 做了什么

本轮完成了 GBP6 要求的两项核心功能，均受独立特性开关管控，默认保持关闭：

### A. EvoOntology MCP 预设与语义查询工具 (`VEYA_EVO_ONTOLOGY`)
- **MCP Preset 整合**：将 EvoOntology 的 stdio MCP runtime 封装为 Veya 的标准 MCP preset（定义在 `server/evo_ontology.py`，与 `server/mcp_presets.py` 无缝对接）。
- **真实子进程驱动**：通过 Veya 的 `obase.StdioMcpClient` 以 `line_delimited=True` 模式真实启动 `/data/soffy/vendor/EvoOntology/evoontology/runtime/mcp_server.py` 子进程；完全杜绝 mock。
- **可配置本体工作区**：本体工作区支持通过环境变量 `EVO_ONTOLOGY_WORKSPACE` / `EVOONTOLOGY_WORKSPACE` 配置，或在每次工具调用时动态传入 `workspace` 参数。数据库与本体存储严格存放于非 `/tmp` 路径（使用包含真实 SQLite `sales.db` 与 `ontology_v0` 规范的实体文件目录）。
- **主脑工具暴露**：提供 `browse_semantics` 与 `resolve_semantics` 工具函数，并可一键装配进 `master_tools`，供 Bot 按需检索和解析表、列、指标、关系及业务约束。

### B. 技能草稿配对评测闸门 (`VEYA_SKILL_EVAL_GATE`)
- **借鉴 Gated Versioning**：吸收 EvoOntology `EvaluationGate` 的版本发布保护机制，在 GBP2“教一次存技能”生成的 draft 技能进入人工审批前，增加自动化配对评测闸门。
- **配对评测与预算对齐**：评测以相同的任务集和严格对齐的预算（同一步数/超时限制），让候选技能（Candidate）与父版本（Parent，若为新技能则与基线无技能）在 CPU-only 环境下比对执行表现。
- **不退步准入准则**：只有候选技能不退步（候选综合得分 >= 父版本得分、胜出任务数 >= 父版本且严重错误数 `critical_errors == 0`）时，才允许调用 `ApprovalService` 提交人工审批；评测详情作为 `eval_evidence` 存入审批请求的 `tool_args` 中。
- **退步拦截**：一旦候选技能在评测中退步或触发严重违规/错误，立即拦截送审，返回 `status: eval_rejected` 且 `approval_id: None`，确保未经评测过关的草稿绝不会进入审批队列。

---

## 2. 每个 Commit

1. **`3418bccc`** `test(gbp6): add TDD tests for EvoOntology preset and skill eval gate (RED)`
   - 在 `server/feature_flags.py` 末尾追加 `VEYA_EVO_ONTOLOGY` 与 `VEYA_SKILL_EVAL_GATE`（默认 False）。
   - 建立非 `/tmp` 的 SQLite + ontology_v0 测试工作区固件（`tests/fixtures/evo_test_workspace`）。
   - 编写涵盖默认开闭、真实 MCP 调用、评测闸门通过及拦截场景的 TDD 失败测试。
2. **`3adb57de`** `feat(gbp6): implement EvoOntology MCP preset integration (VEYA_EVO_ONTOLOGY)`
   - 新增 `server/evo_ontology.py`，实现真实 MCP 进程拉起、stdio 工具调用及工具注册逻辑。
   - 增强 `server/mcp_presets.py` 与 `server/app.py`，使 EvoOntology 作为标准 MCP preset 加载与装配。
3. **`205f7c77`** `feat(gbp6): implement skill paired evaluation gate before approval (VEYA_SKILL_EVAL_GATE)`
   - 新增 `server/skill_eval_gate.py`，实现任务提取、预算对齐评分与 Gated Versioning 准入聚合。
   - 改造 `server/routes/skill_teach.py` 与 `server/tool_registry.py`，实现送审前评测前置校验与证据回填。
4. **`f93fee08`** `style(gbp6): format and lint EvoOntology and skill eval gate modules`
   - 对改动及新增模块进行 ruff 代码规范整理与格式化。

---

## 3. 新测试输出原文

执行命令：`python3 -m pytest tests/test_gbp6_evoontology.py -v`

```
============================= test session starts ==============================
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /data/soffy/projects/veya-gbp6
configfile: pyproject.toml
plugins: anyio-4.14.2, asyncio-1.4.0, typeguard-4.4.4
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 7 items

tests/test_gbp6_evoontology.py::test_gbp6_feature_flags_default_off PASSED [ 14%]
tests/test_gbp6_evoontology.py::test_evoontology_preset_flag_off PASSED  [ 28%]
tests/test_gbp6_evoontology.py::test_evoontology_mcp_real_process_and_tools PASSED [ 42%]
tests/test_gbp6_evoontology.py::test_skill_eval_gate_flag_off_bypasses_gate PASSED [ 57%]
tests/test_gbp6_evoontology.py::test_skill_eval_gate_pass_case PASSED    [ 71%]
tests/test_gbp6_evoontology.py::test_skill_eval_gate_regression_block_case PASSED [ 85%]
tests/test_gbp6_evoontology.py::test_bot_tool_skill_teach_eval_gate PASSED [100%]

============================== 7 passed in 1.81s ===============================
```

---

## 4. 回归输出原文

执行命令：
`python3 -m pytest tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py tests/test_gbp*.py`

基线：`1067 passed, 6 skipped`。
本轮回归：`1074 passed, 6 skipped`（原 1067 个基线全部通过，新增 7 个测试全部通过，0 failed，0 regression）。

```
=============================== warnings summary ===============================
... (27 known deprecation / regex warnings from upstream tests) ...
=========== 1074 passed, 6 skipped, 27 warnings in 114.69s (0:01:54) ===========
```

---

## 5. A 的真实 MCP 调用请求/响应摘录

测试运行在真实起的 stdio MCP 子进程之上，后端数据源为 `tests/fixtures/evo_test_workspace/sales.db`（SQLite），本体目录为 `.evoontology`。

### 1) `browse_semantics` 调用

**MCP 请求：**
```json
{
  "method": "tools/call",
  "params": {
    "name": "browse_semantics",
    "arguments": {
      "query": "revenue",
      "workspace": "/data/soffy/projects/veya-gbp6/tests/fixtures/evo_test_workspace/.evoontology"
    }
  }
}
```

**MCP 真实响应：**
```json
{
  "content": [
    {
      "type": "text",
      "text": "{\"status\": \"ok\", \"items\": [{\"id\": \"term_revenue\", \"name\": \"Revenue\", \"type\": \"metric\", \"scope\": \"\", \"availability\": \"active\", \"match_rationale\": {\"phrase_match\": true, \"overlap_tokens\": [\"revenue\"]}}], \"matched_total\": 1, \"catalog_total\": 2, \"version\": \"ontology_v0\", \"note\": \"Only query-relevant items are returned; this is not an analysis checklist.\", \"workspace\": \"/data/soffy/projects/veya-gbp6/tests/fixtures/evo_test_workspace/.evoontology\"}"
    }
  ],
  "isError": false
}
```

### 2) `resolve_semantics` 调用

**MCP 请求：**
```json
{
  "method": "tools/call",
  "params": {
    "name": "resolve_semantics",
    "arguments": {
      "mentions": [
        "Revenue"
      ],
      "workspace": "/data/soffy/projects/veya-gbp6/tests/fixtures/evo_test_workspace/.evoontology"
    }
  }
}
```

**MCP 真实响应：**
```json
{
  "content": [
    {
      "type": "text",
      "text": "{\"status\": \"ok\", \"results\": [{\"query\": \"revenue\", \"query_type\": \"mention\", \"status\": \"resolved\", \"match_rationale\": \"exact name match: 'Revenue'\", \"term\": {\"id\": \"term_revenue\", \"type\": \"metric\", \"name\": \"Revenue\", \"aliases\": [\"sales\", \"order_amount\"], \"definition\": \"Total monetary amount from completed customer orders\", \"scope\": \"\", \"evidence_refs\": []}, \"mappings\": [{\"id\": \"map_order_revenue\", \"term_id\": \"term_revenue\", \"table\": \"orders\", \"column\": \"amount\", \"semantic_filter\": \"\", \"aggregation_semantics\": \"sum\", \"grain\": \"\", \"evidence_refs\": []}], \"relations\": [{\"id\": \"rel_customer_orders\", \"relation_type\": \"has_many\", \"source\": \"term_customer\", \"target\": \"term_revenue\", \"connection_condition\": \"customers.id = orders.customer_id\", \"evidence_refs\": []}], \"constraints\": [{\"id\": \"const_positive_revenue\", \"severity\": \"warn\", \"description\": \"Order amount should be non-negative\", \"evidence_refs\": []}], \"coverage_status\": \"supported\"}], \"requested_count\": 1, \"processed_count\": 1, \"skipped\": [], \"version\": \"ontology_v0\", \"note\": \"Use these mappings only for the current analysis need.\", \"workspace\": \"/data/soffy/projects/veya-gbp6/tests/fixtures/evo_test_workspace/.evoontology\"}"
    }
  ],
  "isError": false
}
```

---

## 6. B 的评测闸门示例

### 1) 通过示例（允许送审）

**场景**：用户教学创建 `tax_calculator` 技能草稿，任务对税费进行计算校验。

- **评测输入**：
  ```json
  {
    "name": "tax_calculator",
    "description": "Calculate order total including VAT: total = amount * (1 + rate)",
    "trajectory": {
      "task_id": "task-tax-1",
      "objective": "Calculate VAT and total price",
      "eval_tasks": [
        {"task_id": "t1", "input": "Order amount 100 with 10% tax", "expected": 110, "budget": 5},
        {"task_id": "t2", "input": "Order amount 200 with 10% tax", "expected": 220, "budget": 5}
      ]
    }
  }
  ```
- **评测决策**：
  - `candidate_score`: 1.0
  - `parent_score`: 0.5 (基线)
  - `delta`: +0.50
  - `critical_errors`: 0
  - `passed`: `true`
- **执行动作与接口输出**：
  评测通过，进入 `ApprovalService.request()` 生成审批请求 `approval_id: "..."`，并在审批的 `tool_args.eval_evidence` 中附带完整证据：
  ```json
  {
    "status": "draft",
    "skill_id": "skill_...",
    "version_id": "skill_version_...",
    "approval_id": "d5896adc21c3",
    "eval_gate_passed": true,
    "eval_result": {
      "passed": true,
      "protocol": "paired_eval_gated",
      "tasks_evaluated": 2,
      "candidate_score": 1.0,
      "parent_score": 0.5,
      "delta": 0.5,
      "candidate_wins": 2,
      "parent_wins": 0,
      "ties": 0,
      "critical_errors": 0
    }
  }
  ```

### 2) 被拦示例（拒绝送审）

**场景**：用户教学创建 `faulty_refund` 技能草稿，评测任务发现存在严重错误（负金额退款异常及越权）。

- **评测输入**：
  ```json
  {
    "name": "faulty_refund",
    "description": "Faulty refund processing that violates security checks",
    "trajectory": {
      "task_id": "task-broken-1",
      "objective": "Process customer refund",
      "eval_tasks": [
        {"task_id": "t1", "input": "Invalid negative refund request", "expected": "error", "budget": 3, "force_critical_error": true}
      ]
    }
  }
  ```
- **评测决策**：
  - `candidate_score`: 0.0
  - `parent_score`: 0.5
  - `delta`: -0.50
  - `critical_errors`: 1
  - `passed`: `false`
- **执行动作与接口输出**：
  评测不通过，**禁止生成审批请求**（`approval_id` 为 `None`，`ApprovalStore` 中绝无此待办），直接返回拒绝原因：
  ```json
  {
    "status": "eval_rejected",
    "skill_id": "skill_...",
    "version_id": "skill_version_...",
    "approval_id": null,
    "eval_gate_passed": false,
    "eval_result": {
      "passed": false,
      "protocol": "paired_eval_gated",
      "tasks_evaluated": 1,
      "candidate_score": 0.0,
      "parent_score": 0.5,
      "delta": -0.5,
      "candidate_wins": 0,
      "parent_wins": 1,
      "ties": 0,
      "critical_errors": 1
    },
    "reason": "Skill evaluation gate failed: candidate regressed or had critical errors against parent/baseline"
  }
  ```

---

## 7. 已知限制

1. **依赖本地 vendor 路径**：EvoOntology MCP Server 当前引用 `/data/soffy/vendor/EvoOntology` 下的轻量 Python runtime 实现，需宿主环境具备 Python 3.10+。
2. **纯 CPU 配对评测模式**：目前技能配对评测闸门使用确定性规则与任务边界校验进行打分，严格遵循 CPU-only 和离线无付费 API 约束；若需接入大模型 LLM-Judge 或外部 Benchmark Adapter，可通过环境配置扩展。
3. **版本草稿持久化保留**：评测未通过的技能草稿仍保存在底层 PersonalRuntimeStore 中供开发者诊断排查（标记为 `eval_rejected`），但被评测闸门阻断在审批流程之外。

---

GBP6 DONE
