# 严格 3O 迁移 — 阶段 0 基线（冻结与盘点）

> 状态: ✅ 阶段 0 完成（2026-08）· 阶段 1 起未动工
> 配套脚本: `scripts/check_no_reverse_dep.py` / `scripts/check_oskill_pure.py`
> 守护测试: `tests/guardians/test_3o_migration_guards.py`

## 0. 目标

把当前松散的「3O 命名 + 通用编排」改造成严格分层底座：

```
veya/obase   (rank 0) 句柄层   — telemetry / authz / vfs sandbox / kv / llm 通道 / daemon bus
veya/oprim   (rank 1) 原子层   — fs_read_write / shell_exec / db snapshot / emit_event / llm_call
veya/oskill  (rank 2) 纯算法层 — protocol_translate / compress / ast_parse / diff / parse_tool_call /
                                 validate_args / evaluate_stop / genetic_weight (强制纯函数)
veya/omodul  (rank 3) 流程层   — session_tree_mgr / tool_pipeline / agent_loop / evidence_refine
veya/oservi  (rank 4) 服务层   — daemon_engine / api_gateway (骨架, 业务靠注入)
```

依赖方向 **单向向下**：`obase ← oprim ← oskill ← omodul ← oservi`；任何 3O 层不得
import 业务根（`server/agents/cli/...`）。幻觉拦截发生在 oskill 的 parse/validate，
物理隔离发生在 obase_vfs → oprim 原子操作。

**治理红线（AGENTS.md 冻结架构）**：阶段 4–5 若触碰主链
（`server/coordinator_master.py` / LLM 层 / 前端交互 / 默认模型），必须先向用户
说明并获同意；阶段 0–1 只立边界与适配，不改变行为。

## 1. 冻结基线

- Tag: `pre-3O-strict` → 指向 `d80c61eb`（迁移前的提交基线）。
- 全量测试（941 collected，2026-08 跑）: **923 passed / 14 failed / 4 skipped**
  （约 9.6 分钟；`./venv/bin/python -m pytest -q`）。
- 说明: 打 tag 时工作区仍有用户未提交改动（`server/graft_*`、`unified_pipeline.py`
  等 WIP），tag 只含已提交基线；WIP 由用户自行决定是否并入。

### 1.1 存量测试失败清单（迁移前既有，非阶段 0 引入）

| 失败测试 | 类型 | 初步归因 |
|---|---|---|
| `guardians/test_single_source.py::test_known_symbols_are_covered` | 守护失效 | KNOWN_SYMBOLS 列出 7 个已不再冲突的符号（ExecResult/Message/SkillMeta/Symbol/ToolResult/git_add/git_commit），「obase 归位」重构后需修剪清单 |
| `test_stratum_memory.py` ×4 | MCP stdio 子进程握手失败 | `connector.ready==False` / `_client is None`，依赖外部 stratum MCP 服务 |
| `test_open_design.py` ×2 | 同上 | `obase.mcp_stdio` 子进程不可用 |
| `test_rag_vault.py` ×3 | HITL toast 等待超时 | 审批事件流/定时器环境相关 |
| `test_audit_emitter.py::test_audit_route_replay` | auth 401 vs 200 | 审计重放依赖鉴权状态 |
| `test_layer4_service.py::test_gateway_history_missing_returns_empty` | 断言 | 环境相关 |
| `test_officecli.py` ×2 | manifest/skill_hub 加载 | 模板/环境相关 |

以上 11 项已复跑确认稳定失败；3 项 rag_vault 为超时型。修复它们不属于阶段 0
范围（不改业务行为），列入后续阶段或用户决定。

## 2. 能力映射表（现有 → 严格层处置）

| 现有模块 | 实际位置 | 处置（严格层） | 基线状态 |
|---|---|---|---|
| `veya/obase/`（telemetry + authz） | `veya/obase/{telemetry,authz,logging,errors,utils,cache,model_routing,llm}.py`（阶段 B 已归位，旧平铺名 `veya/llm.py` 等为 sys.modules 别名） | 部分 obase + 部分拆入 omodul | 已归位；缺严格 Protocol 合同（DaemonBus/VfsSandbox/EventBarrier/KvStore/LlmClient） |
| `veya/sandbox.py` | `veya/obase/sandbox.py`（危险命令检测 + 资源限制 + 审计 + 回滚） | 升级为 `obase_vfs_sandbox`（namespace/bubblewrap → Cloudflare Computer），执行原子下沉 `oprim_shell_exec` | 可用但非真隔离；`subprocess.run` 直接执行 |
| `tools/` + 工具调用逻辑 | `tools/` 是空壳；真实工具面在 `server/tool_registry.py`（1128 行 master_tools）+ `veya/oskill/tools.py` + `server/tool_guard.py` 闸门 | 拆纯函数 `oskill_parse_tool_call` / `oskill_validate_args`；注册面留 server 装配层 | 工具面在业务层，无 parse/validate 独立环节 |
| Coordinator / Squad / Engine | `server/coordinator.py` / `coordinator_master.py`（845 行薄适配）/ `engine_runner.py` / `chat_coordinator.py`；`agents/` 三件套为历史空壳；**真主链引擎 = `platform/3O/oservi/oservi/master_agent.py`**（经 `veya.platform` 加载） | 重写为 `omodul_agent_loop` + `omodul_tool_pipeline`（注入式），双轨运行 | 主链由旧主库驱动，veya 侧仅装配 |
| Session / 上下文管理 | `session/` 近空；真相源 = `server/state_kernel.py` + `plan_todo.py`（plan JSON，线性）；另有 `veya/oservi/context.py` + `history_store.py` | 重构为 `omodul_session_tree_mgr`（id+parentId+leaf，分支/时空回溯，KvStore 快照） | 线性 plan/todo，无树结构 |
| LLM 调用 | `veya/obase/llm.py`（门面，含 `_llm_config/_llm_protocol/_llm_transport`）；`server/opencode_client.py`（**冻结架构主链路 = opencode-go 直连**）+ `server/providers.py` | 下沉 `oprim_llm_call` → `obase_llm_client`（重试/流式/无 Prompt 逻辑） | 通道已具雏形；主链路模型路由冻结，改动需批准 |
| `platform/3O/*` 子库（helios-plat 参考） | `platform/3O/{obase,oprim,oskill,omodul,oservi}` + `veya_core/3O_lib` 符号链接 + `veya.platform` sys.path 装配器 | **全部逐步替换**为 `veya/*` 严格实现；oservi 的 `master_agent.py` 最后替换 | 旧实现参考；裸名 import（`import oskill`）= 违规 R3 |
| SSE 流 | `server/sse.py` + `veya/oservi/streaming.py`（已归位引擎） | `oservi_api_gateway` 统一入口 | 双实现并存 |
| 事件/遥测 | `veya/obase/telemetry.py` + `server/events.py`（fire_step 桥） | `obase_event_barrier`（跨线程 Pub/Sub + 屏障） | 有雏形，非屏障式 |
| 技能注入 | `server/skill_hub.py` + `templates/skills/` | 技能 → oskill 可加载纯描述，agent_loop 决定注入 | 现状为 server 装配 |
| 现有 `veya/oskill/*`（20 模块：stt/tts/vision/browser/memory/im…） | `veya/oskill/` | 阶段 2 拆纯函数（存量入基线逐步净化，目标零违规） | 232 条纯度违规入基线 |
| 现有 `veya/omodul/*`（15 模块 voice/vision/collab…） | `veya/omodul/` | 阶段 4 以注入式重建 | 现状为直接实现，无注入 |
| 现有 `veya/oprim/*`（audio/video/browser/ast/git…） | `veya/oprim/` | 阶段 3 原子化（fs/shell/db/event/llm/pause） | 已有部分原子，无统一合同 |

## 3. 阶段 0 强制检查（每阶段必跑）

### 3.1 反向依赖检查 `scripts/check_no_reverse_dep.py`

- R1 同层互引允许、高层引低层允许；**低层引高层 = 违规**。
- R2 3O 任何层禁止 import 业务根：server/agents/cli/commands/config/session/
  tools/registries/hooks/streaming/subagent/tui/apps/auth/permission/services/
  security/infra + `veya.tools`/`veya.im`/`veya.models`/`veya.server` 等。
- R3 裸名导入旧主库（`import oskill` → platform/3O）= 违规，必须 `veya.oskill`。
- R4 `veya.errors` / `veya.compat` / `veya.platform` 为跨层桥，任何层可用。

```
python scripts/check_no_reverse_dep.py . --write-baseline scripts/baseline_reverse_dep.txt
python scripts/check_no_reverse_dep.py . --baseline scripts/baseline_reverse_dep.txt   # CI 用
```

### 3.2 oskill 纯净度检查 `scripts/check_oskill_pure.py`

检测三类违规：`IO`（os/pathlib/subprocess/socket/httpx/open/print…）、
`GLOBAL`（模块级可变状态 / global 语句）、`NONDET`（random/time.now/uuid/hash…）。
路径含 `/pure/` 或 docstring 含 `3O-PURE` 的文件 **强制纯净，基线不豁免**——
阶段 2 新增纯函数元素放 `veya/oskill/pure/` 即自动被强制。

```
python scripts/check_oskill_pure.py . --write-baseline scripts/baseline_oskill.txt
python scripts/check_oskill_pure.py . --baseline scripts/baseline_oskill.txt            # CI 用
python scripts/check_oskill_pure.py . --strict                                          # 阶段 2 收尾
```

## 4. 存量违规清单（迁移 TODO）

### 4.1 反向依赖（12 条，已入 `scripts/baseline_reverse_dep.txt`）

| 文件 | 违规 | 处置方向 |
|---|---|---|
| `veya/oskill/im/{dingtalk,discord,feishu,slack,telegram,wechat}.py` | R2 `from server.coordinator_master import master_coordinator`（各 1 条） | 阶段 1：runner 依赖反转注入（DaemonBus/回调），或整体移出 oskill |
| `veya/oskill/memory_hub.py` | R3 裸名 `import oskill` ×3 | 改写为 `veya.oskill.*` |
| `veya/oservi/history_store.py` | R2 `from server.auth import current_user` | 阶段 1：user 上下文经注入（contextvar 由外部设置） |
| `veya/oservi/mcp_server.py` | R2 `from veya.server.manifests import ELEMENT_ALIASES` ×2 | 阶段 1：element 别名表经 manifest 注入 |

### 4.2 oskill 纯度违规（232 条，已入 `scripts/baseline_oskill.txt`）

集中在存量复合管线（audio_io/stt/tts/browser/code_review/im/*/memory_*）——
按设计它们是「复合管线」而非纯函数，阶段 2 拆纯函数后逐项从基线删除，
目标 `--strict` 零违规。

## 5. 阶段追踪

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 冻结 + 盘点 + 双检查脚本 | ✅ 完成 |
| 1 | obase 严格合同（5 Protocol）+ 适配器 + 回归 | ⬜ 未动工 |
| 2 | oskill 8 纯函数（parse/validate 优先） | ⬜ 未动工 |
| 3 | oprim 7 原子操作（禁止业务直接 I/O） | ⬜ 未动工 |
| 4 | omodul 重建（session_tree/tool_pipeline/agent_loop/evidence_refine）双轨 | ⬜ 未动工（需批准） |
| 5 | oservi daemon + api_gateway | ⬜ 未动工（需批准） |

## 6. 阶段 0 运行结果

- 冻结 tag: `pre-3O-strict`（d80c61eb）✅
- 全量测试: 941 collected → **923 passed / 14 failed / 4 skipped**（详见 §1.1）
- 反向依赖检查: 76 个 3O 层文件扫描 → 12 条存量违规已入基线（§4.1）✅
- oskill 纯净度检查: 232 条存量违规已入基线（§4.2）✅
- 守护测试: `tests/guardians/test_3o_migration_guards.py` 5/5 通过 ✅
- 核心路径（CLI/SSE/沙箱）: 迁移前主链路为 `platform/3O` 主库 MasterAgent +
  `server/coordinator_master.py` 装配，行为未被阶段 0 触碰；手工链路验证建议
  在阶段 1 适配器落地后一并回归。
