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
| 1 | obase 严格合同（5 Protocol）+ 适配器 + 回归 | ✅ 完成（2026-08，见 §7） |
| 2 | oskill 8 纯函数（parse/validate 优先） | ✅ 完成（2026-08，见 §8） |
| 3 | oprim 7 原子操作（禁止业务直接 I/O） | ✅ 完成（2026-08，见 §9） |
| 4 | omodul 重建（session_tree/tool_pipeline/agent_loop/evidence_refine）双轨 | ✅ 完成（2026-08，见 §10） |
| 5 | oservi daemon + api_gateway | ✅ 完成（2026-08，见 §11） |

## 6. 阶段 0 运行结果

- 冻结 tag: `pre-3O-strict`（d80c61eb）✅
- 全量测试: 941 collected → **923 passed / 14 failed / 4 skipped**（详见 §1.1）
- 反向依赖检查: 76 个 3O 层文件扫描 → 12 条存量违规已入基线（§4.1）✅
- oskill 纯净度检查: 232 条存量违规已入基线（§4.2）✅
- 守护测试: `tests/guardians/test_3o_migration_guards.py` 5/5 通过 ✅
- 核心路径（CLI/SSE/沙箱）: 迁移前主链路为 `platform/3O` 主库 MasterAgent +
  `server/coordinator_master.py` 装配，行为未被阶段 0 触碰；手工链路验证建议
  在阶段 1 适配器落地后一并回归。

## 7. 阶段 1 结果（严格句柄层）

**新增（全部在 `veya/obase/`，rank 0，不改业务行为）:**

| 文件 | 内容 |
|---|---|
| `interfaces.py` | 5 个 Protocol 合同: DaemonBus / VfsSandbox / EventBarrier / KvStore / LlmClient + Event / SandboxResult 类型 |
| `adapters.py` | 薄适配器: SandboxVfsAdapter（ProcessSandbox→VFS 文件面+执行面，越界拒绝）、TelemetryEventBarrier（telemetry.emit 桥接+扇出+屏障）、SqliteKvStore（stdlib SQLite 快照）、LlmClientAdapter（llm_call/stream）、InProcessDaemonBus（进程内 Pub/Sub+RPC，未来 gRPC 替换） |
| `container.py` | 全局单例句柄层: get_sandbox/get_bus/get_barrier/get_kv/get_llm + configure 注入 + reset/aclose_all |
| `__init__.py` | `__manifest__` 扩展 16 元素（共 23），顶层再导出句柄合同 |
| `tests/test_obase_strict_handles.py` | 15 项回归: 现有能力经适配器跑通（沙箱执行/危险拦截/VFS 越界、事件桥接、快照、LLM stub、总线 RPC） |

**配套清理:**
- 守护测试 `test_single_source.py`: 登记 `Event`（迁移期契约类型，主库退役后清除）；
  修剪 7 个已失效的 KNOWN_SYMBOLS（ExecResult/Message/SkillMeta/Symbol/ToolResult/git_add/git_commit）
  + 同步 `docs/dev/veya-3o-assembly.md`（守护从 9 项失败降到全绿）。

**验证:** 反依赖检查 PASS / manifest 23 元素 PASS / oskill 纯净 PASS /
守护测试 6/6 + 句柄层 15/15（RuntimeWarning 严格模式）。

**阶段 1 契约要点（后续阶段依赖）:**
- 业务代码禁止直接 import 旧实现，一律经 `veya.obase.container` 取句柄；
- `VfsSandbox` 文件面是 oprim_fs_* 的物理边界（越界抛 ValueError）；
- `SqliteKvStore.snapshot/restore` 是阶段 4 session_tree 时空回溯的恢复点；
- `LlmClient` 只发已打包数据（阶段 2 protocol_translate 产出标准消息）。

## 8. 阶段 2 结果（oskill 纯函数层）

**新增（全部在 `veya/oskill/pure/`，/pure/ 路径 = 强制纯净，基线不豁免）:**

| 元素 | 模块 | 逻辑来源 |
|---|---|---|
| protocol_translate | `pure/protocol_translate.py` | veya/obase/_llm_protocol（消息翻译/空 tool_calls 剥离/Anthropic 块） |
| context_compress | `pure/context_compress.py` | master_agent 滑窗（[sys]+tail 保首尾）+ 确定性 token 估算 + 预算裁剪 |
| ast_parse | `pure/ast_parse.py` | 新增（syntax_check/find_definitions/structure_summary/forbidden_imports） |
| diff_apply | `pure/diff_apply.py` | 新增（unified diff 生成/应用/统计，keepends 精确换行） |
| parse_tool_call | `pure/parse_tool_call.py` | master_agent tool_call 解析 + 文本内嵌 JSON；**解析失败显式 error 不再静默 {}** |
| validate_args | `pure/validate_args.py` | tools.py validate_parameters 升级为零依赖 JSON Schema 子集校验（type/required/enum/边界/pattern/items/anyOf/const）+ 旧格式桥 schema_of_legacy |
| evaluate_stop_condition | `pure/evaluate_stop_condition.py` | master_agent 停止分支（完成/最大轮次/致命错误/空回复疲劳） |
| genetic_weight_calc | `pure/genetic_weight_calc.py` | 预留占位（确定性移动平均，接口即未来形态） |

**接线（原位置改调 pure 层）:**
- `veya/oskill/tools.py::validate_parameters` → 委托 `validate_args + schema_of_legacy`，
  消息格式兼容旧版（Missing required parameter / Parameter X must be int…）；
  行为收紧 2 处（数字字符串 "42" 不再隐式强转、None 显式校验失败）——符合绝对校验目标。
- master_agent（旧主库）**不改**（冻结架构，阶段 4 双轨时经 tool_pipeline 接入）。

**检查器升级（check_oskill_pure.py）:**
- 模块级赋值改为「被变异才算全局状态」：`__manifest__`/`frozenset`/`re.compile` 等
  不可变常量不再误报；真实变异（subscript/attribute 赋值、mutator 调用、global）仍拦截。

**验证:** pure 层 9 文件 `--strict` 零违规；反依赖/基线 PASS；
`tests/test_oskill_pure_elements.py` 42/42（含幻觉拦截回归：坏 JSON → error 非空、
非对象 arguments 拒绝、diff 不匹配拒绝）。

**阶段 2 免疫点（幻觉拦截能力已就位）:**
- `parse_tool_call`：LLM 输出 → ToolCall（坏 JSON/非对象 → error，管道据此拒绝执行）；
- `validate_args`：参数绝对校验（类型/必填/枚举/边界/pattern）；
- `evaluate_stop_condition`：空回复/疲劳标记 → invalid_response（不再静默）；
- `ast_parse + diff_apply`：复杂代码生成先静态检查、diff 评审后应用（阶段 4 evidence_refine 注入点）。

## 9. 阶段 3 结果（oprim 物理触手原子层）

**新增（`veya/oprim/`，rank 1，经注入句柄、无业务逻辑）:**

| 元素 | 模块 | 经何句柄 |
|---|---|---|
| oprim_fs_read/write/exists/listdir/delete (+text 便捷) | `oprim/fs.py` | VfsSandbox（VFS 权限内，越界 ValueError） |
| oprim_shell_exec / exec_args / run_script | `oprim/shell.py` | VfsSandbox（沙箱执行 + 危险拦截 + 审计） |
| oprim_db_commit/fetch/list/delete_snapshot | `oprim/snapshot.py` | KvStore（键空间 `session_tree:` 统一管理） |
| oprim_emit_event | `oprim/event.py` | EventBarrier（标准 Event 载荷） |
| oprim_llm_call / llm_stream | `oprim/llm.py` | LlmClient（只发已打包数据，无 Prompt/路由逻辑） |
| oprim_pause/resume/status_daemon + daemon_bind | `oprim/daemon.py` | DaemonBus（RPC：暂停/恢复/状态查询） |

每个原子默认取 `veya.obase.container` 全局句柄，显式注入优先（测试/双轨）。
`veya/oprim/__init__.py` 导出 68 个符号（含新增 21 个原子）。

**接线（原位置改走原子操作）:**
- `veya/oskill/tools.py::TerminalTool._run_command`：裸 `create_subprocess_shell`
  → `oprim.shell_exec`（沙箱内执行、危险拦截双保险、cwd 语义改为沙箱根）。
- master_agent / execution.py（FastPathBackend 直跑 subprocess）**未改**——
  前者冻结架构、后者是阶段 4 tool_pipeline 重建对象。

**第三个强制检查 `scripts/check_no_direct_io.py`（阶段 3 新增）:**
- 业务层（server/veya.omodul/oskill/oservi/agents/cli/...）禁止直接
  subprocess/open/网络/写文件/读文件；检测类别 EXEC/NET/FILE_W/FILE_R；
- 豁免：obase（句柄层）、oprim（原子层）、oskill/pure（已有纯净检查）、
  platform/3O（旧主库）、tests/scripts/docs；文件内 `# 3O-IO-ALLOW` 标记可显式豁免；
- 存量 310 条违规入 `scripts/baseline_direct_io.txt`（server 历史直接 I/O 规模），
  基线模式只拦新增 —— 存量在阶段 4+ 随 tool_pipeline 迁移逐步清零。

**验证:** 4 项门禁全 PASS（反依赖 / oskill 纯净 / 直接 I/O / manifest）；
`tests/test_oprim_atoms.py` 16/16（fs 越界、shell 危险拦截、快照 round-trip、
事件桥接、LLM stub、daemon RPC、注入优先）；守护测试 8/8（新增 3 项 direct_io）；
局部回归 100/100（含 TerminalTool 单源守卫 test_sandbox_g4）。

## 10. 阶段 4 结果（omodul 注入式流程控制核心）

**新增（`veya/omodul/`，rank 3，全部经注入句柄/纯函数，零直接 I/O）:**

| 元素 | 模块 | 注入 | 能力 |
|---|---|---|---|
| omodul_session_tree_mgr | `omodul/session_tree.py` | KvStore（经 oprim.snapshot）+ id_fn | id/parentId + leaf 指针；append/branch/fork（时空回溯）/path（根→叶消息链）/snapshot/restore |
| omodul_tool_pipeline（最重要） | `omodul/tool_pipeline.py` | barrier + permit 回调 | 五步管道 解析→校验→权限→执行→包装；全步骤 audit + 事件流；幻觉拦截（坏 JSON/参数不合格 → 拒绝且工具绝不执行） |
| omodul_agent_loop | `omodul/agent_loop.py` | llm + pipeline + tree + barrier + sleep_fn | 生成→调用→工具→更新树→停止判断；连续失败 ≥3 熔断 + 退避；LLM 异常=致命错误；快照随结果返回 |
| omodul_evidence_refine | `omodul/evidence_refine.py` | sandbox + barrier | AST 静态检查 → 沙箱执行验证 → 证据（stderr）→ build_fix_hint 修复提示（模型自我修复闭环） |

**双轨运行（零侵入）:**
- `server/agent_loop_bridge.py`（**新文件**，不改任何现有文件）：
  `VEYA_AGENT_LOOP=strict` → `run_strict()` 用新 AgentLoop 装配（llm 默认
  container.get_llm，tools 注入 ToolPipeline）；默认走旧主链 MasterAgent，
  行为零变化。阶段 5 gateway/daemon 直接调本桥完成切换。

**修复:** `oskill/pure/parse_tool_call` 支持两种 tool_calls 形态——OpenAI 线格式
（`function.name`）与 Agent 内部扁平格式（`name/arguments`，llm_message_to_agent
产出）——消除 loop 内部协议不一致（补 4 项测试）。

**验证:** `tests/test_omodul_core.py` 22/22（树分支/回溯/快照、管道五步审计+
幻觉拦截三拒绝路径、loop 端到端剧本/熔断退避/无效回复/轮次上限/LLM 故障、
evidence 语法+运行时证据、bridge flag 默认关 + 端到端）；门禁 4/4 PASS；
局部回归 110/110。

**阶段 4 后的完整底座（全部就位）:**
obase 句柄合同（阶段 1）→ oskill 纯算法 + 幻觉拦截（阶段 2）→ oprim
物理触手原子（阶段 3）→ omodul 注入式心脏 + 会话树 + 工具管道 + 代码证据
（阶段 4）。主链默认仍走旧路径（冻结架构），`VEYA_AGENT_LOOP=strict` 即可
切换新心脏——阶段 5 统一入口时收敛。

## 11. 阶段 5 结果（oservi 长时守护 + 统一网关）

**新增:**

| 元素 | 模块 | 能力 |
|---|---|---|
| oservi_daemon_engine | `veya/oservi/daemon_engine.py` | 常驻后台；每任务独立 AgentLoop（注入 gate 检查点）；状态机 PENDING→RUNNING⇄PAUSED→COMPLETED/FAILED；HITL 挂起/恢复 + 人类输入注入（写入会话树 user 节点）；任务事件流中继（agent_loop.* + task.end 按 task 分发，顺序由事件流保证）；DaemonBus 集成（start() 注册 daemon.pause/resume/status → 阶段 3 `oprim.daemon` 原子直达真实链路）；register_tool 共享工具表 |
| oservi_api_gateway | `veya/oservi/gateway.py` | 统一极简指令（FastAPI router，挂载 `/api/v1/3o/`）：POST tasks / GET tasks/{id} / pause / resume{input} / GET stream（SSE 事件流）；404/409/422 语义；engine 懒单例 + 测试可替换 |

**AgentLoop 增量（阶段 4 兼容扩展）:** `gate` 注入检查点——每轮开始前
await；daemon 注入后 paused 阻塞等待 resume，默认 None 行为不变。

**修复的时序 bug:** 任务终止信号原由 driver 直接 put 队列（先于 relay 处理
agent_loop.done）→ 改为 `task.end` 事件经事件流发出（relay 保证顺序），
stream 消费者按 task.end 收尾。

**挂载（零替换）:** `server/app.py` include_router(gateway) —— 新增前缀，
现有 veya start / CLI 路由全部保留。计划中的「现有入口改为只调用 gateway」
留作配置动作（`VEYA_AGENT_LOOP=strict` + 前端指向新端点），线上切换需
另行批准（冻结架构：主链/前端改动审批规则）。

**验证:** `tests/test_oservi_daemon_gateway.py` 10/10（后台任务完成、HITL
pause/resume、人类输入注入入树、DaemonBus 集成（oprim.daemon 原子直连）、
事件流中继、AgentLoop gate 检查点、gateway 创建/查询/pause/resume/404/422）；
门禁 4/4；局部回归 139/139。

## 12. 迁移完成状态（6–8 周计划的压缩落地）

**五层底座全部就位并持续被门禁保护:**

```
oservi   daemon_engine + api_gateway            (阶段 5)  ── 统一极简指令入口
omodul   session_tree/tool_pipeline/agent_loop/evidence_refine (阶段 4) ── 注入式心脏
oprim    fs/shell/snapshot/event/llm/daemon 原子 (阶段 3)  ── 物理触手, 业务禁止直接 I/O
oskill   pure/ 8 纯函数 (阶段 2)                 ── 算法大脑, 幻觉拦截 (parse/validate)
obase    interfaces/adapters/container (阶段 1)   ── 句柄合同, 全局注入点
```

**强制检查（每阶段 + CI）:** check_no_reverse_dep（单向依赖）/
check_oskill_pure（纯函数）/ check_no_direct_io（业务禁直接 I/O）/
check_manifest（元素清单）/ 守护测试（guardians）。

**存量违规（迁移 TODO，逐步清零）:** 反向依赖 12 条基线、oskill 纯度 232 条
基线、业务直接 I/O 310 条基线。

**双轨与切换:** 主链默认旧路径（主库 MasterAgent，冻结架构不动）；新心脏
随时可切（`VEYA_AGENT_LOOP=strict` → agent_loop_bridge.run_strict；gateway
已挂载 `/api/v1/3o/`）。线上切换、现有入口改造、Cloudflare Computer 深度
对接、遗传权重自适应为后续增量（需批准后实施）。

## 13. 阶段 6 — 双轨收敛：AgentLoop 降格为工具（2026-08-17）

`docker-compose.yml` 部署配置一度把 `VEYA_AGENT_LOOP` 默认值改成了
`strict`，使 §12 描述的"主链默认旧路径"在生产环境不再成立——production
实际长期跑的是 `agent_loop_bridge.run_strict_chat`，`docs/ARCHITECTURE_STABLE.md`
（用户确认的冻结架构）未同步更新，两份文档与实际部署三者脱节（详见
`docs/ARCHITECTURE_STABLE.md` §2.5）。

**收敛结果**（用户明确要求 + 确认）：双轨切换桥的角色终结。
`server/coordinator_master.py` 的用户请求入口只保留 MasterAgent ReAct 一条
路径；`server/agent_loop_bridge.py` 的 `run_strict_chat`/`run_strict` 不再是
可选的"另一条主链"，改为 `server/tool_registry.py` 新增的 `agent_loop_run`
工具的实现层——MasterAgent 在全量工具面里自主决定要不要把一个隔离子任务
委托给它，跑在临时会话（不写入 `session_tree.db` 之外的任何长期存储）里，
完成后把结果文本带回主链。`VEYA_AGENT_LOOP` / `strict_loop_enabled()` 已
整体删除。

阶段 0–5 建的五层底座（obase/oskill/oprim/omodul/oservi）不受影响，继续
存在并被 `agent_loop_run` 复用；变化的只是"谁能触发 AgentLoop 执行"——从
"环境变量决定的第二条主链"收敛为"MasterAgent 按需调用的一个工具"。
