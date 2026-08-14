# Loop Plane 微服务 SPEC（v1.0，落地记录）

> 状态: **Phase 0–3 已实施 + Phase 4 Sched 门面 + Phase 5 Skills stub（2026-08）**
> 目录: `services/loop-plane/`（可独立部署 :8787，或 `LOOP_PLANE_INPROCESS=true` 进程内）

## 0. 优化清单落地情况

| ID | 现状问题 | 落地动作 | 状态 |
|---|---|---|---|
| U1 | plan_todo 与 veya_loop GoalKernel 双源 | EventStore 单一真相源（events.jsonl append-only + 聚合索引 + 投影 project_goal_state）；旧 JSON 仅迁移脚本/兼容导出 | ✅ |
| U2 | 因果能力未接主工具面 | loop_plan_goal / loop_diagnose / loop_intervene 三个工具注册进 master_tools（只加工具，零路由） | ✅ |
| U3 | 目标规划入口弱 | /plan/goal（plan_for_goal → multi_step_plan 复用，execute=false 默认） | ✅ |
| U4 | 执行权限模型分散 | /exec/dispatch 服务端 mode 强制收缩（sandbox/shadow/live_canary 等级 ≤ adapter.needs）；编码仍 hicode | ✅ |
| U5 | 审计不完整 | AuditLog 五节点（diagnose/plan/decide/execute/learn）+ trace_id 关联 + 因果报告写 plan/diagnose 节点 | ✅ |
| U6 | 调度在 automata | /sched/jobs* 门面（注册/触发），内核委托点 set_backend 留给装配方；未复制调度内核 | ✅ 门面 |
| U7 | Skill 无实验闭环 | /skills/* 路由 + schema 就位，501 stub | ⬜ P2 |
| U8 | 多微服务重复 | 单部署单元内部分模块（api/domain/infra） | ✅ |
| U9 | 程序路由踩坑 | 零 Coordinator 意图分流；server 侧仅工具转发（feature flag） | ✅ |
| U10 | PermissionContract 与 mode 未统一 | mode_policy 强制收缩 + AdapterRegistry 白名单（未知 tool → permission_denied） | ✅ |

## 1. 架构落地

```
server master_tools ──(LOOP_PLANE_URL / INPROCESS)──▶ services/loop-plane (:8787)
                                                      ├─ api/   health|goals|plan|exec|sched|skills
                                                      ├─ domain/ state|exec|causal|sched
                                                      └─ infra/ event_store|audit_log
                                                            │
                                              veya_loop / 3O 主库（causal/multi_step_plan/hardened）
```

## 2. 领域行为要点

- **State**: 写路径只追加事件，读路径投影；claim fail-closed（未过期 409）；terminal 只返回「需审批」。
- **Causal**: plan_for_goal → `multi_step_plan(failure_log, store=CausalGraphStore, execute=False)`；
  diagnose → `causal_fault_diagnose`；API 层统一写审计（避免库层双写 audit_path）。
  **空因果图 → 保守提示**（无节点时 recommended_actions 为「扩大观测」类建议）——正确行为，图由二期 populate。
- **Exec**: AdapterRegistry 白名单；mode 等级 sandbox(0) < shadow(1) < live_canary(2)，服务端收缩；
  sandbox 写目录 `tmp/sandbox_{trace_id}`；禁 `python -m` 任意路径。
- **Sched**: 仅门面（job 注册/触发落 jobs.json；create_goal action 直落 GoalService）；内核委托现 automata。

## 3. 兼容映射（工具名兼容）

| 现工具名 | 新实现 | 备注 |
|---|---|---|
| create_plan / plan_status / update_todo | /goals* API | flag 开启时转发；返回文本视图 render_text 无感替换 |
| system_*（state_kernel） | /goals/{id}/quota/gates/terminal API | 语义对齐（claim 409/should_run/gate_check/terminal_check/spend） |
| **新增** loop_plan_goal | /plan/goal | 只规划不执行 |
| **新增** loop_diagnose | /plan/diagnose | root_causes + intervention |
| **新增** loop_intervene | /exec/dispatch | mode 收缩 + 白名单 |
| hicode_run | 不动 | 仍在 server |

## 4. 验收（SPEC §11 T1–T8）

| ID | 用例 | 状态 |
|---|---|---|
| T1 | goal+todos → 投影字段与进度 | ✅ test_goals_roundtrip |
| T2 | update done + evidence → 事件追加 | ✅ 同上（GoalCreated/TodoUpdated/EvidenceAppended/GoalCompleted 顺序断言） |
| T3 | claim 未过期再 claim → 409 | ✅ 同上 |
| T4 | plan/goal → ranked_actions + trace_id | ✅ test_plan_goal_mock（真实 multi_step_plan 调用） |
| T5 | plan/diagnose → root_causes 结构合法 | ✅ 同上 |
| T6 | dispatch sandbox + 未知 tool → failed/permission_denied | ✅ test_exec_sandbox_policy |
| T7 | 审计 plan/execute 行 + trace_id 关联 | ✅ 同上 |
| T8 | flag 切回本地 plan_todo 旧路径 | ✅ test_server_bridge |

## 5. 风险与禁令遵守

- 无 Coordinator 关键词路由 ✅（server 侧仅工具函数实现）
- 无 hicode 重复实现 ✅（exec 只做白名单适配器干预）
- 无 plan/exec/learn 多进程拆分 ✅
- 服务端 mode 收缩（客户端声明不放大）✅
- 无双写长期并行：flag 开启即单一事件源；旧 JSON 仅迁移脚本一次性读取 ✅

## 6. 后续增量（需批准）

- Phase 4 调度内核真实委托（automata 装配注入）
- Phase 5 Skills 实验闭环（SkillPackage + experiment/optimize）
- plan_for_goal 去字符串伪装（显式 target_node + UtilitySpec，下沉 omodul）
- 因果图 populate（从事件流重建 graphs/{graph_id}.json）
