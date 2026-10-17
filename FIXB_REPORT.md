# FIXB 交付报告：证据三段报告 + 真技能配对评测闸门

**分支**: `hark/fixb`  
**基线**: `1074 passed, 6 skipped`  
**最终回归**: `1082 passed, 6 skipped` (0 failed, 0 regression)  
**环境约束**: CPU-only，不改生产库，DB 不移至 `/tmp`，无付费 API 依赖。

---

## 一、问题背景与根因分析

### 1. 证据三段报告未产出与默认 Bot 报错“这一步没有完成”
- **根因分析**：
  1. **计划审查闸门误判（完成判定缺陷）**：
     在 `server/goal_run/runner.py` 中，`_run_plan_review_gate` 使用如下逻辑判断是否跳过单任务审查：
     ```python
     bot_turn = str(getattr(state, "bot_id", "") or DEFAULT_BOT_ID) != DEFAULT_BOT_ID
     ```
     对于默认 Bot（`bot_id == "veya-default" == DEFAULT_BOT_ID`），`bot_turn` 恒为 `False`。
     这导致默认 Bot 在接收单任务对话时被送入了 `dual_axis_plan_review` 双轴审查。审查模型认为“任务图只有一个任务，复述了目标，缺少证据采集拆解步骤”，因而判定 `reject` 并将任务停在 `awaiting_user` 状态。`extract_reply` 将该阻塞原因转写为友好回复：“`这一步没有完成：缺少证据采集步骤…`”，任务实际从未执行。
  2. **Prompt 注入点缺失**：
     原 GBP4 中仅在 `_default_bot_spec`（未注册时合成）与 `render_role_block`（仅限角色预设渲染）补充过一句“说不清数字怎么来的就不写；猜测和跳过的单独列出”，未在单回合组装主入口 `server/bot_team/turns.py:build_objective` 中对默认 Bot 和角色 Bot 提供标准格式定义。
  3. **猜测提取方式粗糙**：
     `server/supervisor/reporting.py:_extract_guesses` 原先仅检查 `task.execute_result` 是否包含英文单词 `"assume"`，丢失了执行轨迹中的结构化断言和默认值记录。

### 2. GBP6 技能草稿评测闸门硬编码打分
- 原 `server/skill_eval_gate.py` 对候选技能根据描述非空直接赋值 `1.0`，无技能 baseline 直接赋值 `0.5`，未执行真实评测管线。

---

## 二、架构改造与核心实现

### 1. 证据三段报告稳定输出与计划闸门放行
- **单任务 Bot 回合放行** (`server/goal_run/runner.py`, `server/coordinator_master.py`)：
  - 放行判定升级：支持 `is_bot_turn` 标志、`assignee == "builtin"` 或 `bot_id != DEFAULT_BOT_ID` 均识别为 Bot 回合；单任务回合直接跳过双轴前置计划审查，标记 `plan_review = {"skipped": True, "reason": "single-task bot turn"}`。
  - `CanonicalExecutionRequest(source="PRODUCT")` 明确注入 `is_bot_turn=True`。
- **全局 Prompt 注入** (`server/bot_team/turns.py`)：
  - 在 `build_objective` 中，当 `VEYA_EVIDENCE_REPORT` 开启时，对所有 Bot（默认 Bot 及角色 Bot）统一注入三段格式要求：
    ```
    【证据报告要求】：
    每个论断必须标注来源（文件/URL/证据）；未经工具验证的断言或默认值单独归入【猜测】；未做或跳过的步骤单独归入【跳过】。
    回答中必须明确包含以下三段结构：
    【来源】
    - 列出引用的文件路径或 URL（若无则写“（无记录来源）”）
    【猜测】
    - 列出未经工具验证的断言、假设或默认值（若无则写“（无未证实猜测）”）
    【跳过】
    - 列出未做或跳过的步骤（若无则写“（无跳过项）”）
    ```
- **结构化猜测提取（轨迹断言与默认值）** (`server/supervisor/reporting.py`)：
  - 重写 `_extract_guesses(state)`：
    1. 结构化断言：扫描 `task.assertions`，抽取状态为 `weak`/`unverified` 或缺少 `evidence_ids` 支撑的断言。
    2. 结构化证据：扫描 `task.evidence`，识别 `kind` 为 `unverified_assertion`、`default_value` 或 `assumption` 的条目。
    3. 语义回落：扫描 `task.execute_result` 中带有“默认/假设/推测/未经验证/assume”的语句。
    4. 兜底：“`（无未证实猜测）`”。
- **稳定保底格式化** (`server/bot_team/turns.py:format_evidence_report`)：
  - Bot 回合完成时，若模型直接回复中缺少【来源】、【猜测】或【跳过】段落，从当前 GoalRun 状态轨迹提取并自动补齐对应段落，确保无论是默认 Bot 还是角色 Bot 均 100% 稳定输出三段。

### 2. 真实配对技能评测闸门 (`server/skill_eval_gate.py`)
- **评测数据集构建** (`build_evaluation_dataset`)：
  - 提取来源轨迹的核心任务（输入、预期、预算）。
  - 检索个人运行时的同类技能或补充生成领域相关的变体/边界用例，组成配对评测集（2-3 个任务）。
- **配对执行管线** (`evaluate_skill_candidate`, `default_eval_runner`)：
  - 在同一模型、同一预算（默认 5）下，候选技能（带候选指令/公式）与 Parent/无技能基线（不带或带父版本）各跑一遍。
  - 支持注入确定性测试运行器 `eval_runner`，亦支持真实模型本地调用。
- **可验证指标打分** (`compute_verifiable_metrics`)：
  1. `task_completion` (0.0 ~ 1.0)：任务是否无阻断/无崩溃完成。
  2. `tool_success` (0.0 ~ 1.0)：工具调用成功率（未调工具时与输出一致性对齐）。
  3. `output_consistency` (0.0 ~ 1.0)：输出内容与预期目标/关键字的匹配重合度。
  - 综合分：`0.4 * task_completion + 0.3 * tool_success + 0.3 * output_consistency`。
- **准入门禁与审批证据**：
  - 只有在候选技能无致命错误（`critical_errors == 0`）且总体不退步（`delta >= 0`, `cand_wins >= parent_wins`）时才允许送审。
  - 审批证据包含详细样本、各子项分数及 delta，写入 `eval_evidence`。

---

## 三、本地可用模型真实跑记录

在本地真实运行 `evaluate_skill_candidate`，对技能草稿 `tax_calculator` 进行来源任务与领域变体用例的配对评测：

```json
{
  "passed": true,
  "protocol": "paired_eval_gated",
  "tasks_evaluated": 2,
  "candidate_score": 1.0,
  "parent_score": 1.0,
  "delta": 0.0,
  "candidate_wins": 0,
  "parent_wins": 0,
  "ties": 2,
  "critical_errors": 0,
  "parent_version": null,
  "samples": [
    {
      "task_id": "eval-task-source",
      "input": "Calculate VAT and total price for 100 USD at 10% tax rate",
      "expected": "110",
      "candidate": {
        "score": 1.0,
        "output": "VAT: **10 USD**\nTotal price: **110 USD**",
        "metrics": {
          "task_completion": 1.0,
          "tool_success": 1.0,
          "output_consistency": 1.0,
          "score": 1.0
        }
      },
      "parent": {
        "score": 1.0,
        "output": "VAT = **10 USD**\nTotal price = **110 USD**",
        "metrics": {
          "task_completion": 1.0,
          "tool_success": 1.0,
          "output_consistency": 1.0,
          "score": 1.0
        }
      },
      "delta": 0.0,
      "winner": "tie"
    },
    {
      "task_id": "eval-task-variant",
      "input": "Variant verification: Calculate VAT and total price for 100 USD at 10% tax rate with edge case constraints",
      "expected": "110",
      "candidate": {
        "score": 1.0,
        "output": "VAT = 100 USD × 10% = **10 USD**\nTotal including VAT = 100 USD × (1 + 0.10) = **110 USD**\n\nEdge-case assumptions: rate is interpreted as 10% = 0.10, amount is non-negative, and no rounding is needed because the result is exact.",
        "metrics": {
          "task_completion": 1.0,
          "tool_success": 1.0,
          "output_consistency": 1.0,
          "score": 1.0
        }
      },
      "parent": {
        "score": 1.0,
        "output": "Assuming **100 USD is the net price before VAT** and the tax rate is **10%**:\n\n- **VAT** = 100 × 10% = **10.00 USD**\n- **Total price** = 100 + 10 = **110.00 USD**\n\n**Result:**\n- Net amount: **100.00 USD**\n- VAT: **10.00 USD**\n- Gross/total: **110.00 USD**",
        "metrics": {
          "task_completion": 1.0,
          "tool_success": 1.0,
          "output_consistency": 1.0,
          "score": 1.0
        }
      },
      "delta": 0.0,
      "winner": "tie"
    }
  ]
}
```

---

## 四、Git 提交历史（先 RED 后 GREEN，每步 commit）

1. `6cd6aaa8` `test(fixb): add RED tests for evidence report and default bot plan review gate`
2. `d2b8cc3b` `feat(fixb): fix default bot plan review gate and ensure stable 3-section evidence report`
3. `84b484b6` `test(fixb): add RED tests for real paired skill evaluation gate`
4. `e4bb9157` `feat(fixb): implement true paired skill evaluation gate with verifiable scoring metrics`
5. `3128bf34` `style(fixb): format and lint evidence report and skill eval gate modules`

---

## 五、回归测试与静态检查验证

### 1. 全量回归测试
```bash
python3 -m pytest -q tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py tests/test_gbp*.py tests/test_fixb_*.py
```
**结果**: `1082 passed, 6 skipped` (包含基线全部 1074 个用例及本轮新增的 8 个 TDD 测试全部通过)。

### 2. Ruff 格式与检查
```bash
ruff format --check server/bot_team/turns.py server/coordinator_master.py server/goal_run/models.py server/goal_run/runner.py server/supervisor/reporting.py server/skill_eval_gate.py tests/test_fixb_evidence_report.py tests/test_fixb_skill_eval_gate.py
ruff check server/bot_team/turns.py server/coordinator_master.py server/goal_run/models.py server/goal_run/runner.py server/supervisor/reporting.py server/skill_eval_gate.py tests/test_fixb_evidence_report.py tests/test_fixb_skill_eval_gate.py
```
**结果**: `8 files already formatted`, `All checks passed!`.

FIXB DONE