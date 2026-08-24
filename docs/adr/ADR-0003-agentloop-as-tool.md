# ADR-0003: AgentLoop 降格为工具 (AgentLoop-as-Tool)

> 状态：accepted（既成事实，本 ADR 是补记，不改变现有行为）
> 依据：`docs/ARCHITECTURE_STABLE.md` §2.5、`architecture/manifest.yaml`
> `compat_facades` / `docs/VEYA_10_OF_10_PLAN.md` 文首 2026-08-24 核实结论

## 决策

`omodul.AgentLoop` 不是、也不应该是主链的替代品。它是一个普通工具——
`agent_loop_run`（见 `server/tool_registry.py`）——由 `MasterAgent` 在 ReAct
循环里像调用任何其他工具一样自主决定要不要委托一个隔离子任务给它。它不能
整体接管用户请求，不拥有自己的顶层入口。

`server/agent_loop_bridge.py` 是这个决策落地时期留下的双轨切换桥
（`run_strict_chat`），**不是待清理的 legacy 代码**——`docs/VEYA_10_OF_10_PLAN.md`
曾经把它列为"待验证删除"，这是误判：`server.coordinator_master` /
`server.tool_registry`（`agent_loop_run` 工具的实现）现在仍在用它，删除会破坏
`agent_loop_run` 工具本身。

## 背景

`docs/3O_STRICT_MIGRATION.md`（阶段 4-5）曾新增 `agent_loop_bridge.py` 作为
双轨切换桥：`VEYA_AGENT_LOOP=strict` 时整个 `coordinator_master.chat_stream()`
早退，改由 `AgentLoop` 接管用户请求（独立会话树 `session_tree.db`、独立工具面
裁剪、独立记忆蒸馏钩子）。`deploy/docker-compose.yml` 后来把这个 flag 默认值
改成了 `strict`，导致 production 长期实际跑的是这第二条主链，而不是文档描述的
`MasterAgent` ReAct，违反 ADR-0001（唯一主链）。

副作用记录在案（`docs/ARCHITECTURE_STABLE.md` §2.5）：
- 双轨各自维护一套"工具面裁剪"逻辑，其中一版直接复刻了 `_layer_tools` 反面
  教材（见 ADR-0002）——同一个坑踩了两次。
- 多端同步一度切换读 `session_tree.db`（当时生产实际写入的存储），
  `veya.history_store` 反而成了"从未被写入的旧路径专用存储"，本末倒置。

## 修正结果

- 唯一主链恢复为 `MasterCoordinator.chat_stream()` → `MasterAgent.chat_stream()`
  ReAct 循环，全量工具面（`get_all_tool_schemas()`），零程序裁剪。
- `VEYA_AGENT_LOOP` / `strict_loop_enabled()` 已整体删除，不再是可切换的主链
  入口——双轨切换本身已经不存在。
- `AgentLoop` 降格为 `agent_loop_run` 工具，模型自主决定要不要委托子任务，
  `agent_loop_bridge.py` 作为这个工具的实现依赖被保留、继续使用。

## 结论：`agent_loop_bridge.py` 不需要 PR-04

`docs/VEYA_10_OF_10_PLAN.md` §3/§5/§21/§22/§28 里把这个文件列为"待验证删除的
legacy"是误判——它是双轨切换桥的实现载体，双轨切换机制本身已废除，但文件仍是
`agent_loop_run` 工具的依赖，不是死代码，不需要 PR-04（"Remove Dead AgentLoop
Main-Path Artifacts"）里设想的删除动作。
