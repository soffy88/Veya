# GBP1 岗位技能真正生效报告

## 执行总结
- **任务目标**：使 Bot 配置中的 `available_skill_ids` 在 Bot 每次运行时通过现有 `skill_authority` 和渐进加载机制真正注入 Bot 提示/技能上下文（引入 feature flag `VEYA_ROLE_SKILLS_RUNTIME`，带总长上限、超限截断及日志）。
- **完成状态**：已完成（TDD RED -> GREEN，全部测试通过，全量回归零新增失败）。

## 改动文件与行号
1. **`server/feature_flags.py`**
   - 行 80-105：新增 `FeatureFlag("VEYA_ROLE_SKILLS_RUNTIME", False, "runtime", "2027-01-01")`。
2. **`server/bot_team/turns.py`**
   - 行 85-130 (`build_objective`)：加入 `VEYA_ROLE_SKILLS_RUNTIME` / 渐进加载逻辑。使用 `server.skill_authority.canonical_skill_id` 校验技能 ID，渲染渐进技能块，设置 3000 字符总长上限及超限截断与警告日志。
3. **`tests/test_gbp1_role_skills_runtime.py`**
   - 新增：4 个单元测试（默认关、开/关表现断言、无效技能 ID 过滤等）。

## 回归测试及结果
- **基线前测**（针对 `tests/test_w1*.py`, `tests/test_nano_*.py`, `tests/test_lorca_*.py`, `tests/test_orca_*.py`, `tests/test_p3a_bot_isolation.py`, `tests/test_product_shell.py`）：
  - 1053 passed, 6 skipped。
- **最终全量测试**（包含新增 `tests/test_gbp1_role_skills_runtime.py`）：
  - 1057 passed, 6 skipped，0 failed。
- **Pytest 核心输出摘录**：
  ```
  tests/test_gbp1_role_skills_runtime.py::test_role_skills_runtime_flag_default_off PASSED [ 25%]
  tests/test_gbp1_role_skills_runtime.py::test_build_objective_without_runtime_flag PASSED [ 50%]
  tests/test_gbp1_role_skills_runtime.py::test_build_objective_with_runtime_flag PASSED [ 75%]
  tests/test_gbp1_role_skills_runtime.py::test_build_objective_invalid_skill_id_filtered PASSED [100%]
  =========== 1057 passed, 6 skipped, 26 warnings in 161.97s ===========
  ```

GBP1 DONE
