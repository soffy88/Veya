# TDX 交付报告（R1 复核）

日期：2026-10-15  
工作区：`/data/soffy/projects/veya-tdx`  
分支：`feat/todos-borrow`  
基线：`c1364460`

clean-room concept borrowing from todos.dev; no code copied

## 1. 摘要

| 项目 | 实施状态 | 验收状态 |
|---|---|---|
| Needs You 待决策列 | 完成：读取真实 approval pending 项和 canonical blocked/needs-input/review 事件；审批继续使用现有 approval API | TDX 定向测试通过。 |
| 按工人的发布权限 | 完成：持久化 push/merge/tag/deploy 授权，接入真实工具执行 guard，并写 canonical 审计事件 | TDX 定向测试通过；集成测试确认被拒绝的 git push 回调不会执行。 |
| Run History | 完成：从现有事件库投影 task/round 数据，支持轮次对比，并通过可选 GSK evidence adapter 读取监督/验证证据回退路径 | TDX 定向测试通过；空数据不会合成样本。 |

三项功能各有默认关闭的 feature flag：`VEYA_TDX_NEEDS_YOU`、`VEYA_TDX_AGENT_PUBLISH_PERMS`、`VEYA_TDX_RUN_HISTORY`。没有新增 model-callable 工具。TDX 定向测试和前端门禁通过；规定回归中仅剩 `tests/test_canonical_proxies.py` 的 4 个失败，属于任务规格注明可接受的已知环境失败。由于当前环境没有 `gstack-evidence` 和 `gstack-review` 命令，证据台账门禁无法生成对应标签/评审结论，故不宣称全部验收门禁无条件通过。

## 2. 实施细节与变更证据

### 2.1 Owner-only 鉴权与 flags 默认关闭

- 路由：TDX 新增 API 均依赖 `require_owner`；该名称直接复用 `server.routes.tailnet.require_tailnet_owner`，不再使用硬编码用户 ID 或自定义角色回退。
- 覆盖：未登录返回 401、登录但非 owner 返回 403；owner 通过受保护的数据接口收到 200；flags 未启用时各路由返回 404。
- 文件：`server/routes/tdx.py`、`server/app.py`、`server/feature_flags.py`、`tests/test_tdx_flags_off.py`、`tests/test_tdx_needs_you.py`。
- Commit：`92e8935fe0afb3cc9da37a77e8efeefd50f158db` — `tdx: use canonical owner gate`。
- 测试：`test_all_tdx_flags_off`、`test_needs_you_requires_signed_in_owner`、`test_owner_gate_reuses_tailnet_owner_decision`、`test_needs_you_reads_actual_approval_and_blocked_events`。
- Evidence：对应 TDX 测试直接运行通过（详见第 4 节）。gstack 标签因执行环境缺少 CLI 未生成，不以 pytest 结果冒充 gstack 标签。

### 2.2 按工人的发布权限、持久化和审计

- API：`GET /api/v1/tdx/publish-perms`、`PUT /api/v1/tdx/publish-perms/{agent_id}`，均受共享 owner 依赖保护；写入时对 `push`、`merge`、`tag`、`deploy` 分别授权，默认拒绝。
- 接入点：策略作为 enforced policy 接入 `global_tool_guard`，因此检查发生在工具执行层而非仅 REST 路由层。集成测试通过 `MasterToolRegistry.execute("git_push", ...)` 调用真实执行路径，断言未授权动作在 callback 前被阻断且有拒绝审计事件；flag 关闭时沿用原行为。
- 持久化：使用 Veya 数据目录中的 JSON 配置与原子写入，允许 `VEYA_TDX_PUBLISH_PERMS_PATH` 覆盖路径；测试使用 `tmp_path`。决策/配置变更写入现有 canonical event API，不创建第二套事件库；审计失败会显式记录/阻止，不静默吞掉。
- 文件：`veya/tdx/publish_permissions.py`、`server/routes/tdx.py`、`tests/test_tdx_publish_perms.py`、`apps/web/src/routes/settings/agent-publish-perms/+page.svelte`、`apps/web/src/routes/settings/channels/+page.svelte`。
- Commits：
  - `9a1515579fbfccb96e4b99571362a34df600ece7` — `tdx: enforce persisted publish permissions`。
  - `ac4df7ca2d1dd4db025c54768d4424d49cb67b7d` — `tdx: include publish targets in audit events`。
  - `d5b8a75c636e96c3f971249065cfee0acb67ad9b` — `tdx: add flag-gated agent publish permissions UI`。
- 测试：`test_publish_perms_flag_off`、`test_publish_perms_are_persisted_and_audited`、`test_tool_guard_blocks_physical_git_push_and_writes_denial_event`、`test_tool_guard_flag_off_preserves_original_execution`。
- Evidence：上述 TDX 测试通过。gstack 标签未生成，原因见第 7 节。

### 2.3 Needs You 待决策视图

- API：`GET /api/v1/tdx/needs-you`，通过共享 owner 依赖授权。
- 权威来源：审批来自 `approval_mod.get_approval_service().store.list_approvals(..., status="pending")`；blocked/needs-input/review 类状态从 `server.events.event_store.read_all(...)` 投影。使用原始记录的 ID、时间、状态和来源；不创建伪造的 approval/task ID。前端对 approval 操作调用现有 approval API，不另建审批逻辑。
- 前端：`apps/web/src/routes/team/NeedsYouWidget.svelte` 以及 `apps/web/src/routes/team/+page.svelte`；入口由 feature flag/API 可用性控制。
- 文件：`server/routes/tdx.py`、`apps/web/src/routes/team/NeedsYouWidget.svelte`、`apps/web/src/routes/team/+page.svelte`、`tests/test_tdx_needs_you.py`。
- Commit：`e228eddbb834eb8fa927ad325ef208af6c222e1b` — `tdx: project needs-you and run history from canonical events`。
- 测试：`test_needs_you_flag_off`、`test_needs_you_requires_signed_in_owner`、`test_needs_you_reads_actual_approval_and_blocked_events`、`test_owner_gate_reuses_tailnet_owner_decision`。
- Evidence：测试通过；gstack 标签未生成，原因见第 7 节。

### 2.4 Run History / rounds 对比

- API：`GET /api/v1/tdx/runs?task_id=&limit=`、`GET /api/v1/tdx/runs/{task_id}/rounds`、`GET /api/v1/tdx/runs/{task_id}/compare?a=&b=`。路由 owner-only 且只读。
- 权威来源：`_run_events()` 读取既有 canonical event store；`_infer_round_groups()` 优先采用持久化 round 编号，在缺少编号时只依据重复 start 事件推断轮次；`_read_run_evidence()` 优先尝试可选的 `veya.gsk.evidence` 读取接口，不可用时回退到既有 `supervision.*` / `verification.*` 事件中的证据 payload。没有新增 evidence store。`_project_round()` 投影来源事件中实际存在的计划、diff、测试/证据、模型/token、耗时和状态字段；上游没有记录的字段不会伪造。
- 前端：`apps/web/src/routes/team/RunHistoryWidget.svelte` 和 `apps/web/src/routes/team/+page.svelte`；空存储显示空结果，不展示虚构 run。
- 文件：`server/routes/tdx.py`、`apps/web/src/routes/team/RunHistoryWidget.svelte`、`apps/web/src/routes/team/+page.svelte`、`tests/test_tdx_run_history.py`。
- Commit：`e228eddbb834eb8fa927ad325ef208af6c222e1b` — `tdx: project needs-you and run history from canonical events`。
- 测试：`test_run_history_flag_off`、`test_run_history_empty_store_returns_no_samples`、`test_run_history_and_round_comparison_come_from_canonical_events`、`test_run_history_infers_rounds_from_repeated_starts_when_no_round_metadata`。
- Evidence：测试通过；gstack 标签未生成，原因见第 7 节。

### 2.5 回归修正：Bot objective 的最终文本

- 在跨模块回归中，三个 Lorca 用例发现 `build_objective()` 把中文格式指令放在用户文本之后，导致以 objective 最后一行生成响应的测试替身回显格式指令。将格式指令放到用户文本之前，让实际任务文本保留为最后一行。
- 文件：`server/bot_team/turns.py`。
- Commit：`49cd11ba832f6a33c8259fec6c273e42a72c2c2f` — `tdx: keep the bot objective text as its final line`。
- 测试：`test_bot_turn_carries_its_bot_into_the_canonical_request`、`test_local_turn_is_unchanged_by_lorca`、`test_runner_turn_endpoint_executes_the_hubs_bot_locally`。修正后单独复测 **3 passed, 5 warnings in 3.20s**，且完整 Lorca/Orca 分组 **258 passed, 7 warnings**。
- Evidence：由直接 pytest 输出支持；gstack 标签未生成，原因见第 7 节。

## 3. 新增 model-callable 工具

**无。** 未增加模型可调用工具、独立调度器、审批权威或第二套事件库。

## 4. 测试和回归结果

### TDX 定向测试

最终执行命令：

```bash
.venv/bin/python -m pytest -q --basetemp=.tdx-tmp/pt tests/test_tdx_*.py
```

结果原文摘要：`13 passed, 5 warnings in 5.35s`，exit 0。覆盖 `tests/test_tdx_flags_off.py`、`tests/test_tdx_needs_you.py`、`tests/test_tdx_publish_perms.py`、`tests/test_tdx_run_history.py`。

### 规定的跨模块回归

由于本环境的命令执行时限，将任务规格中的同一批文件按 glob 分三批运行；三份日志均保存于 worktree 的忽略目录 `.tdx-tmp/`：

| 批次 | 测试文件范围 | 摘要 |
|---|---|---|
| A | `tests/test_w1*.py`、`tests/test_nano_*.py` | `757 passed, 6 skipped, 24 warnings in 106.22s` |
| B | `tests/test_lorca_*.py`、`tests/test_orca_*.py` | `258 passed, 7 warnings in 18.39s` |
| C | `tests/test_p3a_bot_isolation.py`、`tests/test_product_shell.py`、`tests/test_llm_router.py`、`tests/test_canonical_proxies.py`、`tests/remote/test_policy_resolver.py`、`tests/remote/test_effect_registry_authority.py` | `4 failed, 125 passed, 5 warnings in 158.04s` |

按三批汇总（不是一次 pytest 进程的摘要）：**1140 passed, 4 failed, 6 skipped, 36 warnings**。失败名单仅为任务规格注明的 `tests/test_canonical_proxies.py`：

- `test_free_pool_eligibility`
- `test_no_raw_model_business_dependency`
- `test_dsh_route_isolation`
- `test_no_silent_model_substitution`

四项失败报告同一断言：上游 model 为空时，reason 为 `master brain delegation: unknown`，不含期望的 `frontier` 字样（测试位置 `tests/test_canonical_proxies.py:483`）。按 `TDX_BRIEF.md`，这些属于允许保留的已知环境失败；本轮未修改其相关文件。此前回归中失败的三个 Lorca 用例已由第 2.5 节的修正解决，完整 Lorca/Orca 分组已通过。

### 前端门禁

- `cd apps/web && pnpm check`：exit 0；结果为 `svelte-check found 0 errors and 32 warnings in 3 files`。
- `cd apps/web && pnpm build`：exit 0；生产构建成功，日志包含 `✓ built in 7.39s`。
- `apps/web/node_modules` 可供现有依赖使用；没有执行网络安装。日志位于 `.tdx-tmp/pnpm-check.log` 和 `.tdx-tmp/pnpm-build.log`。

## 5. 已知限制 / 未完成

1. 回归仍有 4 个 `test_canonical_proxies.py` 断言失败，内容如第 4 节所列；按任务规格属于可接受的已知环境失败，但它们本身没有在本轮被修复。
2. 执行环境中 `command -v gstack-evidence` 与 `command -v gstack-review` 均 exit 1，工具不可用。所有测试因此直接运行，没有生成 gstack evidence label，也无法提供 `/gstack-review` 的正式结论。第 7 节明确记录该门禁缺失，未伪造标签。
3. Run History 仅展示 canonical event / supervision evidence 中已被记录的字段；不补造上游缺失的数据。

## 6. 工作区与提交

- 分支：`feat/todos-borrow`。
- 本轮没有 push、没有重启服务、没有安装 `tds`、没有访问/下载 todos.dev 资源，也没有修改 worktree 外文件。
- 代码改动均已提交。最终 `git status --short` 仅显示本交付报告未跟踪：`?? TDX_REPORT.md`；保留该文件为交付物，没有把报告混入功能代码提交。

## 7. Evidence

| 修复项 | Commit SHA | 文件 | 测试名 | gstack evidence |
|---|---|---|---|---|
| Canonical owner gate / flags OFF | `92e8935fe0afb3cc9da37a77e8efeefd50f158db` | `server/routes/tdx.py`、`tests/test_tdx_flags_off.py`、`tests/test_tdx_needs_you.py` | `test_all_tdx_flags_off`、`test_needs_you_requires_signed_in_owner`、`test_owner_gate_reuses_tailnet_owner_decision` | 未生成：`gstack-evidence` 命令不可用；直接测试通过。 |
| 发布权限强制、持久化和审计 | `9a1515579fbfccb96e4b99571362a34df600ece7` | `veya/tdx/publish_permissions.py`、`server/routes/tdx.py`、`tests/test_tdx_publish_perms.py` | `test_publish_perms_are_persisted_and_audited`、`test_tool_guard_blocks_physical_git_push_and_writes_denial_event` | 未生成：`gstack-evidence` 命令不可用；直接测试通过。 |
| 审计包含发布目标 | `ac4df7ca2d1dd4db025c54768d4424d49cb67b7d` | `veya/tdx/publish_permissions.py`、`tests/test_tdx_publish_perms.py` | `test_tool_guard_blocks_physical_git_push_and_writes_denial_event` | 未生成：`gstack-evidence` 命令不可用；直接测试通过。 |
| Needs You / Run History 投影与组件 | `e228eddbb834eb8fa927ad325ef208af6c222e1b` | `server/routes/tdx.py`、`apps/web/src/routes/team/NeedsYouWidget.svelte`、`apps/web/src/routes/team/RunHistoryWidget.svelte`、对应 `tests/test_tdx_*.py` | `test_needs_you_reads_actual_approval_and_blocked_events`、`test_run_history_and_round_comparison_come_from_canonical_events`、`test_run_history_infers_rounds_from_repeated_starts_when_no_round_metadata` | 未生成：`gstack-evidence` 命令不可用；直接测试通过。 |
| 发布权限设置页 | `d5b8a75c636e96c3f971249065cfee0acb67ad9b` | `apps/web/src/routes/settings/agent-publish-perms/+page.svelte`、`apps/web/src/routes/settings/channels/+page.svelte` | `pnpm check`、`pnpm build` | 未生成：`gstack-evidence` 命令不可用；前端两项命令通过。 |
| 修复 objective 末行导致的三项回归 | `49cd11ba832f6a33c8259fec6c273e42a72c2c2f` | `server/bot_team/turns.py` | `test_bot_turn_carries_its_bot_into_the_canonical_request`、`test_local_turn_is_unchanged_by_lorca`、`test_runner_turn_endpoint_executes_the_hubs_bot_locally` | 未生成：`gstack-evidence` 命令不可用；3 项定向复测通过。 |

`gstack-evidence check --all`：未能运行；执行环境的 `command -v gstack-evidence` 返回 exit 1。无正式 gstack 证据清单输出。  
`/gstack-review`：未能运行；执行环境的 `command -v gstack-review` 返回 exit 1。无正式 review 结论。
