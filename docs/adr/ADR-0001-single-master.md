# ADR-0001: 唯一用户主链 (Single Master)

> 状态：accepted（既成事实，本 ADR 是补记，不改变现有行为）
> 依据：`docs/VEYA_10_OF_10_PLAN.md` §2 I-01、`docs/ARCHITECTURE_STABLE.md` §2.5、
> `architecture/manifest.yaml`

## 决策

所有用户交互（无论从 CLI / Web / TUI / VSCode / API 哪个 interface 进来）最终
必须收口到唯一一条主链：

```text
Interface → MasterCoordinator (server.coordinator_master)
          → MasterAgent / ReAct (3O oservi.MasterAgent)
          → Tool execution (server.tool_registry.MasterToolRegistry)
          → MasterAgent synthesis
          → Event stream
```

禁止出现第二条"能接管完整请求"的路径：不允许 keyword intent router、不允许主链
前置 URL 自动抓取、不允许程序按任务类型切模型、不允许程序按语义裁工具面。

## 背景

历史上出现过两次偏离：

1. **legacy `server.coordinator`**：早期主链实现，现被 `server.coordinator_master`
   取代，但尚未完全退役——`architecture/manifest.yaml::deprecated[0]` 记录了它
   仍被 `server.backends` / `server.routes.{session,flow,prompt}` 四处引用，
   四处难度不同（详见该文件注释），其中至少一处（`routes/session.py` 的
   `/resume`）卡在 checkpoint 数据格式不兼容，需要人工拍板才能迁移，不是代码
   能单方面解决的。
2. **`omodul.AgentLoop` 曾经短暂成为第二条主链**：`VEYA_AGENT_LOOP=strict` 时
   `MasterCoordinator.chat_stream()` 早退，改由 `AgentLoop` 接管请求，独立
   session tree、独立工具裁剪、独立记忆钩子——`deploy/docker-compose.yml`
   一度把这个 flag 默认值改成 `strict`，导致生产长期实际跑的是这第二条链，
   文档却从未同步，形成"文档与生产行为脱节"。详见
   `docs/ARCHITECTURE_STABLE.md` §2.5「主链路唯一性纠偏」。

## 现状

`VEYA_AGENT_LOOP` / `strict_loop_enabled()` 已整体删除，不再是可切换的主链
入口——第二次偏离已经修正。`omodul.AgentLoop` 降格为一个普通工具
（`agent_loop_run`，见 ADR-0003），由模型自主决定要不要委托一个隔离子任务给它，
不再能整体接管请求。

legacy `server.coordinator` 尚未完全退役（第一次偏离未完全修正），是当前
`architecture/manifest.yaml` 里唯一记录在案、仍在跟踪的 I-01 违规。

## 强制机制

`scripts/check_architecture_manifest.py` 已接入 CI（`ci.yml` 的
`architecture` job），但目前只做只读报告——`architecture/manifest.yaml` 的
`forbidden_imports` 留空，是有意为之：`server.coordinator` 的四个调用点没清空
之前直接 hard-fail 会拉红生产路径。等 `deprecated[0].known_importers` 清空后，
把 `server.coordinator` 挪进 `forbidden_imports`，检查才会对新增违规真正硬失败。

计划文档 §2 I-01 里设想的专用脚本 `scripts/check_single_master_path.py`
尚未建立，是本 ADR 记录时发现的差距，留给后续 PR。
