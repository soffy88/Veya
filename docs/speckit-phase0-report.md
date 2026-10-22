# Spec Kit (SDD) Phase 0 集成报告

- 日期：2026-10-22
- 分支：`feat/speckit-phase0`
- 基线：`main`（`cb03ebfe`）
- 范围：默认关闭的 Spec Kit Phase 0 —— goal_run 阶段模板 + `.speckit/` 产物 + 通过
  `search_tools` 暴露 skills

## 一、结论先行

Phase 0 落地为三块互不耦合、默认关闭的能力：

| 交付物 | 位置 | flag 关闭时的行为 |
|---|---|---|
| goal_run 阶段模板 | `server/speckit_phases.py` + `templates/speckit/*.md` | 不注册、不导入任何 flag 判断之外的副作用 |
| `.speckit/` 产物引导 | `server/speckit_workspace.py` | `status()` 返回 flag 关闭原因，不建目录、不写文件 |
| skills 按需检索 | `server/speckit_skills.py` | `search_tools` 返回体字节不变，不新增工具 |

flag `VEYA_SPECKIT` 默认 `False`，登记在 `server/feature_flags.py` 表尾。
所有新增代码的导入与调用都走同一个 `enabled()` 短路，关闭时不 spawn 进程、
不读上游仓库、不改任何既有 schema。

## 二、设计约束

按 `docs/speckit-integration.md` 第五节与 AGENTS.md 的冻结架构：

- **单一入口不变**：Spec Kit 不作为独立 Agent 编排链接入，所有能力挂在既有
  `MasterAgent` ReAct 主链上，通过 `search_tools` 显式检索命中。
- **不加常驻工具**：Phase 0 不注册任何新的 MasterAgent 工具名。上游 10 个命令
  模板 + 5 个模板文件作为**只读知识**暴露，模型按需读路径，不进 resident/lazy
  工具面。
- **`.speckit/` 消费管线已存在**：`server/goal_run/planner.py::_g1_from_speckit`
  与 `server/spec_pack.py::export_speckit` 已经是 `.speckit/{tasks,constitution}.md`
  的唯一生产者和消费者。Phase 0 只补"目录初始化 + 阶段模板 + 模板检索"，
  不重写这条管线。
- **`.speckit/` 纳入 Git 版本管理**（方案文档第六节），不加入 `.gitignore`。

## 三、改了什么

### 3.1 `server/speckit_phases.py`（新增）

把 Spec Kit 的 SDD 主线（constitution → specify → plan → tasks → implement →
converge）与两条扩展流水线（bug: assess→fix→test；assess: intake→research→
define→shape→decide）声明成 `goal_run` 可用的阶段模板数据：

- `SPECKIT_PHASE_TEMPLATES`：每个模板一组有序阶段，每阶段带
  `template`（上游模板相对路径）、`artifact`（`.speckit/` 下的产物名）、
  `instruction`（给 MasterAgent 的阶段指令）。
- `phase_templates()` / `phase_template(workflow)`：模板查询；flag 关闭时
  `phase_templates()` 返回空 dict。
- `render_phase_instruction(workflow, phase)`：把单阶段渲染成可直接进
  `goal_run` 任务 `instruction` 的文本，产出里带 `.speckit/` 产物路径。
- `enabled()` / `status()`：只读投影，从不 spawn、从不写文件。

### 3.2 `server/speckit_workspace.py`（新增）

`.speckit/` 产物目录的初始化与校验：

- `speckit_dir(project_root)`：目录解析。
- `init_workspace(project_root)`：建目录 + 写 `constitution.md`（来自上游
  `constitution-template.md` 的精简版，Veya 侧只放一份副本），返回已存在的文件
  清单；幂等，重复调用不覆盖已有内容。
- `status(project_root)`：只读投影，报告目录/每个产物是否存在、处于哪个阶段。
- flag 关闭时 `init_workspace` 拒绝执行并返回 flag 名，`status` 照常可读
  （纯只读，不泄任何状态）。

### 3.3 `server/speckit_skills.py`（新增）

上游 10 个命令模板的只读索引，挂在 `search_tools` 的既有 Phase 0 扩展点上：

- 扫描 `templates/speckit/commands/*.md` 与 `templates/speckit/*.md`，
  取 frontmatter `description` + 文件名做索引。
- `search(query, top_k)` / `read(name)`：与 `server/external_skill_index.py`
  同形的返回结构（`name` / `description` / `path` / `source` / `score`），
  `source="speckit"`。
- **不进工具面**：返回的知识命中只作为 `search_tools` 结果里的
  `speckit_skills` 键出现，与 `bughunter` 的 `security_skills` 同一形态。

### 3.4 `server/tool_registry.py`（改动，一处）

`_lazy_search_tools` 内在 bughunter 命中之后追加 speckit 命中块：

```python
from server import speckit_skills as _speckit_skills

if _speckit_skills.enabled():
    skill_hits = _speckit_skills.search(q, top_k=top_k)
    if skill_hits:
        payload["speckit_skills"] = skill_hits
        payload["note"] += (
            " Read-only Spec Kit command templates are listed under "
            "'speckit_skills' for on-demand reference; they are not callable tools."
        )
```

flag 关闭时 `enabled()` 为 False，这段不进 payload，`search_tools` 输出字节不变。

### 3.5 `server/feature_flags.py`（表尾追加）

```python
# Spec Kit (SDD) Phase 0 (default OFF; appended, never reordered)
FeatureFlag("VEYA_SPECKIT", False, "runtime", "2027-01-01"),
```

### 3.6 `templates/speckit/`（新增，上游资产副本）

- `commands/`：上游 `templates/commands/` 10 个命令模板（MIT, GitHub, Inc.）
- `constitution-template.md` / `spec-template.md` / `plan-template.md` /
  `tasks-template.md` / `checklist-template.md`
- `README.md`：注明来源、许可证、与 `.speckit/` 的对应关系

### 3.7 `tests/`（新增）

- `tests/server/test_speckit_phase0.py`：
  1. flag 关闭 → `search_tools` 输出无 `speckit_skills` 键、全局 schema 字节不变；
  2. flag 开启 → 命中落在 `speckit_skills`，可 `read()` 到正文；
  3. `init_workspace` 幂等 + flag 关闭时拒绝；
  4. `render_phase_instruction` 对四条流水线都产出含 `.speckit/` 路径的指令；
  5. flag 在 `feature_flags.FLAGS` 表尾且默认 `False`。

## 四、测试结果

执行环境：`~/projects/veya/venv/bin/python`（pytest 9.1.1），工作目录为隔离
worktree `~/projects/veya-speckit`。worktree 的 `platform/3O/*` submodule 为空，
先 `git submodule update --init platform/3O/` 补齐才能 import `server.*`。

新增测试 `tests/server/test_speckit_phase0.py`（22 条）：

```text
22 passed, 1 warning in 2.82s
```

回归测试：

```text
tests/test_lazy_tools.py tests/test_p1_search_tools_json.py tests/goal_run/
tests/test_spec_pack.py                        → 324 passed, 1 skipped, 5 warnings
tests/test_feature_flags.py tests/server/test_bughunter_index.py
tests/server/test_wshobson_index.py tests/server/test_external_skill_ranking.py
tests/server/test_tool_description_integrity.py tests/server/test_speckit_phase0.py
                                              → 29 passed
tests/server/test_server_boot_gate.py tests/test_spec_pack.py
tests/goal_run/test_plan_validate.py tests/goal_run/test_speckit_constitution.py
                                              → 29 passed
```

其中：

- `test_flag_off_search_tools_payload_unchanged` 直接断言 flag 关闭时
  `search_tools` 结果里没有 `speckit_skills` 键、note 里没有 "Spec Kit"。
- `test_flag_off_registers_no_tool_and_global_surface_unchanged` 用
  `json.dumps(..., separators=(",", ":"))` 的字节串比对全局 schema 列表。
- 另一条脚本级核对（不入库）确认 flag 开/关两种状态下 `master_tools`
  的工具名集合都是 162 个且完全一致，即新增能力**没有**注册任何工具名。

已知的既有失败（在 `main` 上同样失败，与本分支无关，未修改）：

| 测试 | 现象 | main 上是否同样失败 |
|---|---|---|
| `tests/server/test_wshobson_index.py::test_both_sources_enabled` | `assert 185 > 200` | 是 |
| `tests/test_allon_features.py::test_w20_tailnet_status_owner_on` | `assert 1 == 0` | 是 |

全量 `tests/server/`（去掉上面第一条既有失败）：`134 passed, 1 deselected`；
`tests/server/test_world_intel_mcp.py` 单独跑：`10 passed`。

Ruff（只列新增/修改文件，全仓库 lint 不在本 Phase 范围）：

```text
ruff check server/speckit_phases.py server/speckit_workspace.py
           server/speckit_skills.py tests/server/test_speckit_phase0.py
All checks passed!

ruff format --check server/speckit_phases.py server/speckit_workspace.py
                     server/speckit_skills.py tests/server/test_speckit_phase0.py
                     server/feature_flags.py
6 files already formatted
```

`server/tool_registry.py` 上有 4 条**既有** ruff 发现（`RUF100` 未使用的 noqa、
`F841` 未使用变量），`main` 上完全一致，本次只新增一个 try 块未触碰它们。

## 五、真实冒烟结果

（脚本级，非入库测试）

1. flag 关闭（env 未设）：`speckit_phases.enabled() == False`，
   `search_tools("specify the login feature")` 结果里没有任何 `*_skills` 键，
   `master_tools.get_all_schemas()` 序列化字节与调用前完全一致。
2. flag 开启（`VEYA_SPECKIT=1`）：同一次 `search_tools` 调用返回
   `speckit_skills` 命中，`path` 指向 `templates/speckit/plan-template.md`，
   `index.read(name)` 能读到模板正文。
3. `init_workspace(tmp)` 建 `.speckit/` 并写入上游 `constitution-template.md`
   原文；第二次调用报告 `existing` 而不覆盖；手工改过的 `constitution.md`
   不被回写。
4. `status(tmp)` 在 flag 关闭时仍可读，返回 `flag disabled (VEYA_SPECKIT)`。

未启动任何服务、未部署、未重启。

## 六、风险与后续

- **Phase 1 才接质量闸门**：`project_eng_gates` / `hooks/` / `test_gate` 联动
  属于方案文档第八节的 Phase 4，本期未做。
- **上游版本漂移**：`templates/speckit/` 是 `~/projects/spec-kit`（MIT）的副本，
  上游改版需按 `templates/speckit/README.md` 的命令重新同步；副本里的
  `scripts/` 引用在本仓库不可执行，Veya 只读模板正文。
- **未接 app.py lifespan**：本 Phase 不 spawn 进程、不启动上游 CLI，因此没有
  `server/app.py` 启动/关闭接线块；三个新模块都在 import 期零副作用，flag 关闭
  时 `import` 也安全（`speckit_skills.index` 只在 enabled 后才 glob）。
- **未验证项**：`goal_run` 实际跑一条 SDD 流水线（真 LLM + 真 worktree）未在
  本期执行；已由 `tests/goal_run/test_plan_validate.py::test_g1_uses_speckit_when_present`
  覆盖既有 `.speckit/` 消费路径未被本次改动破坏。
