# RFC-06: ToolSpec/ToolResult v1 — 落地可行性评估

> 状态：§3 的最小步骤已落地（2026-08-24 第二轮）；见下方「6. 落地记录」
> 依据：docs/VEYA_10_OF_10_PLAN.md §7（Tool 系统做到 10/10）
> 范围：§1-5 是原始评估（不做实际迁移的判断），§6 是后续真正执行的记录。

## 1. 目的

计划文档 §7.1/§7.2 提出用统一 `ToolSpec`/`ToolResult` 取代 `server/tool_registry.py`
里的巨石注册字典 + 自由字符串结果，并把 `_PARALLEL_SAFE_TOOLS`/`_TOOL_GROUPS` 这两个
模块级名单迁成 spec 上的字段。这份 RFC 评估：在不破坏现有 76 处静态 `register()` 调用
（另有运行时 MCP 动态注册）的前提下，v1 该怎么切、第一步多大合适。

## 2. 现状调研（2026-08-24）

- `ToolRegistry.register()`（`tool_registry.py:389`）已经不是纯字典 API：签名里已有
  `max_result_chars`/`timeout_s`/`parallel_safe` 三个显式关键字参数——即计划里说的
  "并发安全靠名单"只对**没有显式传参**的工具成立，`parallel_safe` 缺省 `None` 时才
  回退到模块级 `_PARALLEL_SAFE_TOOLS` 白名单（`tool_registry.py:94`，21 个只读工具）。
  这比计划文档描述的"纯名单驱动"更接近 spec 化的中间态。
- `_TOOL_GROUPS`（`tool_registry.py:125`）是工具名 → 职能分组的字典（`code_exec`/
  其他若干组），用于 `_group_for` 之类的展示/统计场景，不参与执行时的权限或并发判断
  ——它是纯元数据，风险最低，是最适合先搬的一块。
- 工具返回值现状确实是自由字符串（`str`）或 `dict`，没有统一 `status`/`evidence`/
  `audit_id` 字段；这次顺带修 mypy 错误时能直接看到（例如 `tool_registry.py` 里
  大量 `return str(...)` 是运行时早已是字符串、只是类型层面没声明——数据形状本身
  没问题，缺的是显式契约，不是缺数据）。
- 静态 `master_tools.register(...)` 调用点 76 处；计划文档提到的 119 是含运行时
  `register_mcp_tools`/技能热加载等动态注册路径的总数，不是这次要碰的静态清单的
  真实规模。

## 3. 评估结论：v1 可以分层引入，不需要一次性迁移

`register()` 已经是关键字参数式 API，说明**加新的可选字段不会破坏任何一个现有的
76 处调用**——这是这次评估最重要的结论：ToolSpec v1 不需要"一口气改 76 个注册点"
这种大爆炸迁移，可以做成纯增量、逐个工具认领的模式，跟计划文档 §22 PR-05 的原则
（"只引入 metadata 与 contract，不改 tool behavior"）完全对得上。

推荐的最小可行第一步（未执行，留给下一轮做）：

1. 定义 `ToolSpec`（dataclass，只含 §7.1 列的 `risk`/`side_effect`/`idempotency`
   字段，不含 `input_schema`/`output_schema`——这两个已经由 `parameters` JSON Schema
   参数覆盖，重复定义没有收益）。
2. `register()` 新增可选关键字 `side_effect: SideEffect | None = None`，缺省时按
   现有的 `parallel_safe`/`_TOOL_GROUPS` 双路推断（保出老工具零改动、零回归）。
3. 只给 21 个已经在 `_PARALLEL_SAFE_TOOLS` 白名单里的只读工具显式传 `side_effect=
   PURE_READ`——这些工具的副作用分类已经是团队做过的判断（白名单本身就是这个判断
   的结果），标注它们是"确认过的事实搬家"，不是"新判断"，风险最低、收益立即可见
   （能验证 `side_effect == SAFE_PARALLEL` 等价于现在的 `_PARALLEL_SAFE_TOOLS`
   查表）。
4. `ToolResult` 暂不引入结构化返回类型——现有 76 个工具的返回值需要逐个读实现才能
   判断该分到哪个 `status`，这是需要单独一轮工作量的事，不该跟 ToolSpec 字段定义
   混在一次改动里。

## 4. 明确不做的部分（这次评估范围之外）

- 不把 `_TOOL_GROUPS` 全量迁成 spec 字段——先只加 `side_effect`，`_TOOL_GROUPS`
  的职能分组用途跟并发/权限判断无关，优先级更低。
- 不引入 `ToolResult` 结构化返回（见上，需要单独评估 76 个工具各自的真实返回契约）。
- 不做 contract test harness（`tests/contracts/tools/`，计划 §7.4）——这依赖
  `ToolResult` 先落地，顺序上排在后面。
- 不动任何一处现有 `register()` 调用——这次只评估，没有改代码。

## 5. 跟这轮已完成工作的关系

`server/coordinator_master.py`/`server/tool_registry.py` 这次刚做完 mypy 清零
（见 CI 新增的 `Run mypy (current chat kernel)` 步骤），后续给 `register()` 加
`side_effect` 参数、给 21 个只读工具补标注时，应该复用同一条 `--follow-imports=skip`
检查通道，不需要新开检查项。

## 6. 落地记录（2026-08-24 第二轮，§3 方案按原计划执行）

- `server/tool_registry.py` 新增 `SideEffect`(枚举, 只启用 `PURE_READ`, 其余五档占位)
  和 `ToolSpec`(冻结 dataclass, 目前只有 `name`/`side_effect` 两个字段)。
- `register()` 新增可选 `side_effect: SideEffect | None = None`；新增
  `spec_for(name) -> ToolSpec | None` 查询入口；`unregister()` 同步清理。
  纯附加——并发判断仍然只读 `_parallel_safe`，两者暂不合并。
- **实际标注了 21 个白名单只读工具里的 15 个**：`fetch_url`/`read_hashline`/
  `runtime_calls_query`/`ast_grep_search`/`read_file_ast`/`grep`/`list_files`/
  `search_genesis_ledger`/`get_market_data_schema`/`system_quota_should_run`/
  `system_gate_check`/`system_terminal_gate_check`/`system_boundary_scan`（都在
  `tool_registry.py` 内直接 `register()`）+ `decision_query`/`graph_query`（走
  `dict(...)` 批量注册循环，顺带给这条循环的 `mt.register(...)` 调用补了
  `side_effect=spec.get("side_effect")` 转发，否则字段会被静默吞掉）。
- **剩下 6 个没标注**：`assemble_code_context`（`server/graft_autocontext.py`）、
  `project_status`（`server/project_ask.py`）、`plan_status`
  （`server/plan_todo.py`）、`hicode_status`/`hicode_sessions`/`hicode_tasks`
  （`server/hicode_agent.py`）——注册点在别的文件, 这轮没跨文件展开, 不是漏了
  忘标, 是范围没扩大。这些文件要标注时直接照抄同样的 `side_effect=
  SideEffect.PURE_READ` 参数即可，不需要新设计。
- 验证：`tests/test_master_tools.py` 新增两条——`test_registry_tool_spec_side_effect`
  （独立 registry 单测：标注/未标注/未注册/unregister 后清理四种状态）、
  `test_side_effect_pure_read_matches_parallel_safe_whitelist`（对全局
  `master_tools` 做子集断言：所有标了 `PURE_READ` 的工具都在
  `_PARALLEL_SAFE_TOOLS` 里——断言子集不是相等，因为这轮只标了 15/21）。
  `tests/test_master_tools.py` 全量 30 项通过；`--follow-imports=skip` 模式下
  `coordinator_master.py`/`tool_registry.py` mypy 仍 0 错误。
