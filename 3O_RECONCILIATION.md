# 3O_RECONCILIATION.md

> SPEC: Veya 3O 元素实施 SPEC v1.0 Final（3O Paradigm SPEC v3.0）
> 决策: 选项 A（冻结优先）—— `docs/ARCHITECTURE_STABLE.md` §2.2 禁令继续有效，
> ModelRouter 只做 availability / failover / usage，不做任务语义选型。
> 状态: Phase 1 盘点完成；Phase 2+ 未开始。
> 真值规则: 本文件所有结论按 SPEC §45 标注，未真实验证的一律记 `NOT_VERIFIED`。

- `CURRENT_SHA=7919504e08d1fbf306dde3d41d53ba5289e6ebed`
  （分支 `hotfix/v1.0.1-production-canonical-wiring`；盘点时 worktree 脏：
  `M scripts/veya_llm_gateway.py`）
- 盘点方式：只读 `rg` + 目录清单 + 已有门脚本复核，未批量新建元素（SPEC §43）。

## 1. 现状分类（SPEC §32）

### EXACT（不改）

| SPEC 元素 | 现状位置 | 证据 |
|---|---|---|
| ONE intelligence authority（MasterAgent 唯一决策中心） | `server/coordinator_master.py:1552` `chat_stream` | `docs/ARCHITECTURE_STABLE.md` §1/§2.5 |
| AgentLoop 已降格为工具（非第二主链） | `server/tool_registry.py`（`agent_loop_run`）、`veya/omodul/agent_loop.py` | `docs/ARCHITECTURE_STABLE.md` §2.5 |
| GoalRun 状态机 + durable 执行 | `runtime/execution/durable.py`、`runtime/execution/models.py`、`server/goal_run/models.py` | 容量约 4616 行（durable） |
| SideEffectLedger | `runtime/execution/side_effects.py:14` | `ActionGatewayAdapter` 注释引用 |
| ActionGateway 桥接 | `server/action_gateway_adapter.py:52` `ActionGatewayAdapter` | 桥 `oservi.ActionGatewayEngine` |
| Event/Outbox/Reconciler/Checkpoint/Worker | `runtime/execution/outbox.py`、`reconciler.py`、`checkpoint.py`、`worker.py`、`server/events.py` | 与 SPEC §1.4/§1.5/§5.7 语义对应 |

### EQUIVALENT（保持现有 tested API，不为 SPEC 名加 wrapper）

| SPEC 概念 | 现状等价物 | 说明 |
|---|---|---|
| DurableQueue/Lease | `runtime/execution/durable.py` + `worker.py` | 命名不同，行为等价 |
| EventStore | `server/events.py` + obase `loop_event_store` | 同上 |
| ArtifactStore | `runtime/execution/artifacts.py` + `server/goal_run/leaf.py` | 同上 |
| ProviderRegistry（统一 Protocol） | `platform/3O/obase` `provider_registry.py` + `server/providers.py` | Veya 侧未统一 Protocol，按 PARTIAL 跟踪 |
| ModelRouter | `server/model_router.py` + `veya/obase/llm.py` | 见 §2 CONFLICT-1（冻结约束下只做 availability/failover/usage） |
| Skill/Memory/Trust | `server/skill_hub.py`、`runtime/personal/runtime.py`、`server/goal_run/trust_plane.py` | 雏形，非 SPEC 契约 |
| 架构门 | `scripts/check_no_reverse_dep.py`、`check_oskill_pure.py`、`check_no_direct_io.py` + `tests/guardians/test_3o_migration_guards.py` | 本轮新增 `scripts/check_3o_gates.py` 只做聚合 + 缺口计数，不替代它们 |

### PARTIAL（补缺口，后续 Phase）

- `veya/obase/`（17 文件：`llm.py`、`sandbox.py`、`cache.py`、`telemetry.py` 等）：有通道/缓存/可观测雏形，无 `PgPool/UnitOfWork/DurableQueue/TransactionalOutbox/ArtifactStore/SecretStore` 的 Veya 原生统一实现。
- `veya/oprim/`：现有为媒体/FS 类原子（`fs/git/shell/browser/ast/snapshot/event/llm`），无 SPEC §2 命名原子（`goal_*/side_effect_*/approval_*` 等）。
- `veya/oskill/pure/` + `runtime/execution/no_progress.py`、`fanin.py`：有纯函数与进度语义，无 SPEC §3 命名函数。
- `veya/omodul/`（`execution.py`、`session_tree.py`、`tool_pipeline.py`）+ `server/tool_registry.py`：有执行面，无 SPEC §4 标准返回契约（`status/fingerprint/decision_trail/cost_usd/report_path`）。
- Bot 侧 `server/goal_run/`、`server/runtimes/`、`runtime/computer/`、`runtime/coding/`、`runtime/harness/`：有 Session/GoalRun/Workspace/Computer/Coding 雏形，无 SPEC §7–§10 命名契约。

### MISSING（新增，需按 §42 顺序）

- SPEC §2 全部命名 oprim（`goal_insert`、`side_effect_reserve`、`approval_create`、`memory_candidate_insert`、`skill_version_insert` 等：`rg` 零命中）。
- SPEC §3 全部命名 oskill（`evaluate_completion_condition`、`score_model_for_task`、`select_model_chain`、`evaluate_capability_permission`、`compare_skill_baseline`、`evaluate_contract_violation`、`evaluate_ratchet`、`evaluate_proven_red` 等：零命中）。
- SPEC §4 标准 omodul 返回契约；SPEC §5/§10 命名 oservi（`durable_run_engine`、`bot_goal_supervisor`、`bot_background_runner`、`bot_scheduler`、`spawn_delegate`、`create_workspace`、`create_computer_session`、`start_external_harness` 等：零命中或仅测试引用）。
- `benchmarks/veya_core_gold/`、`benchmarks/veya_bot_gold/`（`benchmarks/` 不存在；现有 `evals/` 只有 `p0/p1/p3/personal_agent/v1_0_1`，不得与其重复建 benchmark authority，按 SPEC §36 迁移统合）。
- Failure Matrix 真实注入（SPEC §38）与 Production Qualification 30min+ 长任务（SPEC §39）：`NOT_VERIFIED`。

### CONFLICT（已按选项 A 裁定）

1. **CONFLICT-1（已裁定：冻结优先）**：SPEC §3.2/§5.6/§18 `TaskProfile → ModelRouter → ordered chain + failover`
   vs `docs/ARCHITECTURE_STABLE.md` §2.2“禁止重新引入 oskill 复杂路由器”。
   裁定：冻结禁令继续有效；ModelRouter 仅限 profile resolution / availability / failover / usage-cost，
   不做任务规划替代，不改变 Goal 语义。SPEC 的选型语义记为 `OBSOLETE（在 Veya 主链内）`。
2. **CONFLICT-2（待重构，Phase 2）**：SPEC §0.2 `omodul → omodul 禁止裸调`
   vs 现状 `veya/omodul/agent_loop.py:33-34`、`veya/omodul/multimodal_agent.py:20-24`、
   `veya/oservi/daemon_engine.py:27-28` 存在 omodul→omodul 直接 import。
   方向：收敛为 oservi 编排或 DI，不新增第二主链。
3. **CONFLICT-3（待重归属，Phase 2）**：SPEC §3 oskill 纯内存无 IO
   vs `veya/oskill/im/feishu.py:18`、`slack.py:17`（httpx）、`vision_toolkit.py:89`（urllib）、
   `semantic_search.py:140`、`tools.py:621`（open）。
   方向：IO 部分重归属为 omodul/capability，oskill 只留纯算法。

### OBSOLETE（记录，不构建）

第二 MasterAgent / Memory Kernel / Skill Runtime / Capability Registry / Scheduler Runtime /
SideEffect system / Event Store / Approval system；LangGraph / Ruflo-swarm / DeerFlow-runtime /
Paseo-daemon / ACP authority；external harness authority；frontend authority（SPEC §31）。

## 2. 重复架构与旁路嫌疑（SPEC §43 后半）

- `DUPLICATE_ARCHITECTURES_FOUND=NOT_VERIFIED（嫌疑清单）`：
  `server/engine_runner.py`、`server/flow_engine.py`、`runtime/execution/runtime.py`、
  `server/goal_run/runner.py`、`veya/oservi/daemon_engine.py` 并存。
  是否构成第二 runtime authority 需逐条过门后才能定论；本轮不断言。
- `ACTION_GATEWAY_BYPASSES=NOT_VERIFIED（嫌疑）`：
  `server/goal_run/git_diff.py:11` `subprocess.run` 是否经 ActionGateway 待确认；
  `veya/omodul/execution.py:5` 自称“唯一执行入口”，与 ActionGateway 关系待裁定。
- `DIRECT_MEMORY_WRITES=嫌疑 2 处`：
  `runtime/personal/runtime.py:636,702` `INSERT INTO memory_candidates` 是否经 Memory Router 待确认。
- `OMODUL_DIRECT_CALLS=至少 3 组`：
  `veya/omodul/agent_loop.py:33-34`、`veya/omodul/multimodal_agent.py:20-24`、
  `veya/oservi/daemon_engine.py:27-28`（`rg` 实测命中）。
- `OSKILL_IO_VIOLATIONS=多处（基线内跟踪）`：
  以 `scripts/check_oskill_pure.py` + `scripts/baseline_oskill.txt` 为准；
  `/pure/` 及 `3O-PURE` 标记文件强制纯净（见 `tests/guardians/test_3o_migration_guards.py`）。
- `OPRIM_PEER_CALLS=13（已验证，scripts/check_3o_gates.py 实测）`：
  非 types 互调 1 组（`veya/oprim/vad.py → veya.oprim.audio`，待裁定是否下沉共享逻辑），
  types 共享类型 2 组（例外候选），`__init__` 包重导出 10 组（info，不算违规）。
- 计数口径见 `scripts/check_3o_gates.py --help`（报告口径，非 CI 硬门）。
- 诚实记录：`check_no_direct_io.py` 基线模式当前 returncode=1（17 处新增 vs 基线，
  如 `server/tools/coding_task.py`、`tools/harness/control_harness.py` 等），
  系本轮之前已存在的基线漂移，与本轮两个新文件无关（FAIL 清单无本轮文件）。
  `check_no_reverse_dep.py` 与 `check_oskill_pure.py` 基线模式通过（本轮实测）。

## 3. IMPLEMENTATION_ORDER（SPEC §42，选项 A 约束版）

```text
Phase 1: 本文件 + scripts/check_3o_gates.py + 既有三门基线（reverse_dep/oskill_pure/direct_io）→ 全绿
Phase 2 Core: Durable primitives → ActionGateway 收敛（先裁定 bypass 嫌疑）→ Capability →
  ModelRouter（冻结约束：availability/failover/usage only）→ Approval → Checkpoint →
  Context → Memory → Skill Qual → Trust → Trace
Phase 3 Bot 基础: Session → GoalRun → Background → Workspace → Computer
Phase 4 Bot 高阶: Delegation → Takeover → Follow-up/Steering → Scheduling → Channels
Phase 5 Coding: Worktree → code intelligence → change set → ProvenRed → Ratchet → Contract
Phase 6: Core Gold（迁合 personal gold，不另建 authority）→ Bot Gold（≥300 case 按 capability invariant，
  不为数字复制）→ Failure Matrix（真实注入）→ Production Qualification（30min+ 真实长任务）
```

## 4. 本轮 Gate 状态（Phase 1 triage 后，全部实测）

```text
3O_RECONCILIATION=PASS（本文件 + 下方 §5 裁定表）
3O_STRICT_GATE=PASS（scripts/check_3o_gates.py --strict exit 0，实测）
既有三门基线模式=PASS（reverse_dep / oskill_pure / direct_io，guardians 10 passed）
DIRECT_IO_NEW_VIOLATIONS=0（17→0：D 修 detector 4，A 显式标记 8，B 基线 5，见 §5）
CORE_GOLD=NOT_VERIFIED
BOT_GOLD=NOT_VERIFIED
FAULT_MATRIX=NOT_VERIFIED
PRODUCTION_QUALIFICATION=NOT_VERIFIED
主链路行为变更=0（vad 内联为同值纯函数替换，定向测试锁定；其余为脚本/文档/标记/基线）
```

## 5. Phase 1 triage 裁定表

分类：A=LEGITIMATE（本性 IO，显式标记不进基线）/ B=GRANDFATHERED（仅 B 进基线，
逐项有 owner+remediation）/ C=REAL_3O_VIOLATION（本轮修正）/ D=FALSE_POSITIVE
（修 detector，不进基线）。Owner 均为当前 triage 结论的 remediation owner，
非代码归属变更。

| FINDING | CLASSIFICATION | CURRENT_OWNER | CORRECT_3O_OWNER | ACTION | STATUS | EVIDENCE |
|---|---|---|---|---|---|---|
| server/action_gateway_adapter.py:87 Path.home | D | detector 误报 | n/a（纯 env 查找） | check_no_direct_io.py PATH_PURE_METHODS += home | FIXED | `venv/bin/ruff` clean；复测该项消失 |
| server/browser_computer_adapter.py:88 Path.home | D | 同上 | n/a | 同上 | FIXED | 同上 |
| server/goal_run/routine_registry.py:64 Path.home | D | 同上 | n/a | 同上 | FIXED | 同上 |
| server/tool_governance_adapter.py:68 Path.home | D | 同上 | n/a | 同上 | FIXED | 同上 |
| server/goal_run/routine_registry.py:88 open + :96 os.replace | B | server/goal_run（sync JSON registry 原子写） | oprim fs（async） | 进基线 2 行；remediation=Phase 4 调度持久化收敛（sync→async 重构风险， routine 调度路径） | BASELINED | baseline diff +5 中 2 行 |
| server/tools/coding_task.py:20 import subprocess + :56 subprocess.run | B | server/tools（worktree submodule 初始化，frozen MasterAgent 工具路径） | canonical side-effect path（Phase 5） | 进基线 2 行；remediation=Phase 5 Coding 经 ActionGateway/规范 provisioning；改动会碰 frozen 工具语义故暂缓 | BASELINED | baseline diff +5 中 2 行 |
| tools/harness/control_harness.py httpx×6 + socket×2 | A | tools/harness（harness 控制面传输 substrate） | 未来 HarnessProvider 实现 | `# 3O-IO-ALLOW` 显式标记 + 理由（不进基线） | ALLOWED | tools/harness/control_harness.py:15-19 |
| veya/oskill/browser.py:90 Path(__file__).resolve | B | veya/oskill（Playwright 资源定位，stat 触 FS，无测试覆盖） | canonical resource-location helper（待建） | 进基线 1 行；remediation=canonical 资源定位 helper 后迁移；改 resolve 语义不可验故暂缓 | BASELINED | baseline diff +5 中 1 行 |
| veya/oprim/vad.py:15 → veya.oprim.audio | C | veya/oprim/vad | 无（内联消除） | 内联 _bytes_to_int16/_compute_rms/_linear_to_db（同值纯函数），删 peer import；audio.py 保持 canonical owner | FIXED | tests/test_oprim_vad_dsp.py 3 passed（含与 canonical 逐值一致 + 无 peer import 断言） |
| veya/oprim types 引用 2 组 | ALLOW | — | veya/oprim/types（仅类型定义） | 门区分 INFO/TYPES/VIOLATIONS | RESOLVED | check_3o_gates.py OPRIM_PEER_CALLS_TYPES=2 |
| veya/oprim/__init__ 重导出 10 组 | INFO | — | n/a（包重导出） | 门单列 INFO，不算 peer call | RESOLVED | OPRIM_PEER_CALLS_INFO=10 |
| agent_loop→session_tree / →tool_pipeline | EXCEPTION（frozen） | veya/omodul/agent_loop（默认装配 + 全程调用） | 未来 bot supervisor（oservi） | 显式 exception manifest（到期 Phase 3）；装配点在 frozen agent_loop_run 工具路径（bridge:255,372 + daemon:152） | EXCEPTED | scripts/3o_exceptions.json[0,1] |
| multimodal_agent→vision_agent（构造 + analyze_single） | EXCEPTION（deferred） | veya/omodul/multimodal_agent | 未来 bot_workspace_supervisor（oservi） | 显式 exception（到期 Phase 4）；无编排意义的目标 oservi，薄 wrapper 被禁 | EXCEPTED | scripts/3o_exceptions.json[2] |
| multimodal_agent→voice_agent（仅 VoiceSessionConfig 类型耦合） | EXCEPTION（deferred） | 同上 | 待定（共享 agent-config 归属） | 显式 exception（到期 Phase 4）；无行为调用 | EXCEPTED | scripts/3o_exceptions.json[3] |
| runtime/personal/runtime.py INSERT INTO memory_candidates ×2 | FALSE_POSITIVE（authority 本体） | PersonalRuntimeStore（create_memory_candidate:595 + commit:1008，内含 validate/conflict/fingerprint/provenance 门 + 幂等事件） | 同文件（canonical write path；调用方 tool_registry:3614/runner:1257 均经 get_personal_runtime()） | 门 allowlist authority + 断言他处零直写（431 文件扫描） | RESOLVED | DIRECT_MEMORY_WRITE_VIOLATIONS=0 |
| git_diff.py:17 rev-parse + :34/:41/:57 diff ×3 | READONLY_INFO | server/goal_run（任务前后 diff 供 review，失败返空） | 未来只读观测 oprim（Phase 5） | 门区分 readonly/mutation；oprim/git.py 无只读原子故不硬改 review 路径 | RESOLVED | ACTION_GATEWAY_READONLY_INFO=4, BYPASS=0 |
| ModelRouter TaskProfile 选型语义 | CONFLICT_RESOLVED_FREEZE_WINS | server/model_router.py + veya/obase/llm.py | 冻结约束：availability/failover/usage only | 不实现 SPEC 选型链；本轮无改动 | RESOLVED | §1 CONFLICT-1 |

基线卫生：baseline diff 经逐行核对为精确 +5（2 routine + 2 coding + 1 browser），
regen 全量重写方案因会静默删除 ~96 行 stale 条目已被否决并回滚。
control_harness.py ruff 30 errors 为改前即有（HEAD 对照一致），本轮零新增。
预存 M scripts/veya_llm_gateway.py 全程未动。
