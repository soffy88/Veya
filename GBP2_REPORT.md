# GBP2 报告: 教一次存成技能 (Teach Once Save as Skill)

## 1. 任务概述
实现了 `VEYA_TEACH_SKILL` 功能特性：
- 将已完成任务或人工操作轨迹转化为具名的技能草稿（状态为 `draft`/`candidate`），并自动附加“需要你补的判断规则”段落。
- 技能草稿必须通过现有的 `ApprovalService` 批准后才能变更为 `active`（trusted）。
- 提供完整的 REST API (`/api/v1/skills/teach/draft`, `/api/v1/skills/teach/drafts`) 以及 Bot 工具入口 (`skill_teach`)。
- 当 `VEYA_TEACH_SKILL` 特性开关关闭时，相关接口返回 404 / 禁用。

## 2. 改动清单
- **server/feature_flags.py** (末尾): 注册 `VEYA_TEACH_SKILL` 特性开关（默认 `False`）。
- **server/routes/skill_teach.py** (新建): 实现技能草稿创建、判断规则拼接、审批关联与草稿列表查询的 REST 路由。
- **server/app.py** (挂载): 注册引入 `skill_teach_router`。
- **server/approvals.py** (审批联动): 当 `teach_skill_draft` 类型的审批被批准时，自动调用 `runtime.confirm_skill()` 激活技能版本。
- **server/tool_registry.py** (工具注册): 新增 `skill_teach` Bot 工具入口。
- **tests/test_gbp2_skill_teach.py** (新建): TDD 测试驱动，覆盖开关禁用、草稿生成、判断规则校验、未批准不可加载及批准后激活可运行全流程。

## 3. 测试与回归证据
- **新测试名**: `tests/test_gbp2_skill_teach.py::test_teach_skill_flag_off`, `tests/test_gbp2_skill_teach.py::test_teach_skill_workflow`
- **Pytest 回归输出摘录**:
  ```
  tests/test_gbp2_skill_teach.py ..                                        [100%]
  ============================== 2 passed in 0.36s ===============================
  ```
- **完整回归测试套件**:
  `./venv/bin/python -m pytest tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py tests/test_gbp2_skill_teach.py -q`
  **结果**: 1055 passed, 0 failed.

GBP2 DONE
