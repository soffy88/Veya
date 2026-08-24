# Veya 架构铁律 (Invariants)

> 来源：`docs/VEYA_10_OF_10_PLAN.md` §2「P0：立即冻结的五条架构铁律」。
> 本文件只是把那五条从方案文档里提出来单独存放，方便被代码/CI/PR review 引用；
> 规则本身不在这里重新定义，如有出入以 `VEYA_10_OF_10_PLAN.md` §2 为准。
> 「现状」段落是 2026-08-24 逐条核实过的真实落地情况，不是应然描述——
> 这是本文件相对原方案文档的唯一增量。

## I-01：唯一用户主链

```text
Interface
  → MasterCoordinator
  → MasterAgent / ReAct
  → Tool execution
  → MasterAgent synthesis
  → Event stream
```

禁止再次出现：keyword intent router、主链前置 URL 自动抓取、程序按任务类型切模型、
程序按语义裁工具、第二个"可接管整个请求"的 AgentLoop、legacy Coordinator 重新成为
聊天入口。

独立 HITL workflow（例如 Flow / Genesis）如果不属于聊天主链，须标记
`VEYA_EXECUTION_PLANE = "workflow"`。

**现状**：`kernel.master_entry` = `server.coordinator_master`，见
`architecture/manifest.yaml`。legacy `server.coordinator` 仍被
`server.backends` / `server.routes.{session,flow,prompt}` 四处引用——已逐个核实
迁移难度（`architecture/manifest.yaml::deprecated[0].note`），不是"忘了删"。
计划里设想的 `scripts/check_single_master_path.py`（专门做 CI 硬失败的静态检查）
尚未建立；现有的 `scripts/check_architecture_manifest.py` 只做只读报告
（`forbidden_imports` 留空，见该脚本头注释），还没有硬门禁。

## I-02：程序不替 LLM 做开放语义判断

程序可以判断：权限、schema、timeout、quota、resource、deterministic policy、
protocol、safety boundary。

程序不应判断："这是不是 research"、"用户是不是想编码"、"URL 是否应该抓"、
"该用哪个 agent"、"回答是否应该交给 Hicode"——这些应由模型在明确工具面上决策。

**例外**：模型主动调用 `tool_search` 进行工具发现是允许的（模型决定发现什么 ≠
程序猜模型需要什么）。

**现状**：这条是语义边界，没有可自动化的静态检查，目前靠 code review 把关；
`tool_search` 例外已经是既成事实（见 `server/tool_registry.py` 里的 skill_hub
元路由）。

## I-03：一个 Authority，多个 Projection

> 原始事实不可变，派生状态可以重建。

Veya 当前存在 history / session tree / compacted history / memory / goal event
store / trace / replay projection 多套存储，不应再出现"两份都看起来像权威源"的
状态。

**现状**：`docs/dev/rfc-11-state-authority-scoping.md` §1 核实过——这几套存储
现在是真的分散，不是名字不同但底层统一。第二轮已把 `history_store` 改成不可变
追加日志、`memory_store` 补了 provenance 字段（`architecture/manifest.yaml
kernel.canonical_history` = `veya.oservi.history_store`），但完整的 canonical
event model + 全部 projection 统一仍未做——rfc-11 §2 明确记录这是需要人拍板的
决策，不是本轮技术范围。

## I-04：Kernel 不感知业务 Capability

Kernel 只知道抽象协议：`Tool` / `SessionStore` / `EventSink` / `ModelProvider` /
`PermissionPolicy` / `Capability` / `Delegate`。

Kernel 不应直接知道：`wechat` / `quant` / `wayfinding` / `team` / `vision` /
`hicode` / `goal-run` 这些具体域名字，全部应由 capability registry 接入。

**现状**：**未满足，且是当前最明显的一处漂移**——`server/tool_registry.py`
（工具注册收口点）直接 `import server.wayfinding_tools` /
`server.wayfinding_github_tools` / `server.team_tools` /
`server.wechat_article_pipeline`，具体域名字硬编码在 kernel 层。这不是本次
核实新发现的问题，而是现状快照；改造成 capability registry 接入需要重构工具
注册路径，属于独立的一次架构改动，不在本次文档整理范围内。

## I-05：所有高影响行为都必须可解释、可审计、可恢复

任何外部副作用必须满足 `intent → authorization → execution → result → evidence
→ audit`；如果行为可逆，还需要 `rollback / compensation`。

**现状**：`server/audit.py` + `platform/3O/oprim/oprim/_audit_emit.py`
（`AuditEmitter`，单一写出口）覆盖了 intent/authorization/execution/audit 链路
的大部分；`docs/dev/rfc-03-veyaevent-envelope.md` 记录了这条链路目前跟另外两套
事件系统（DomainEvent / TraceContext）并存、未合流的现状。rollback /
compensation 覆盖面未逐项核实，不在本文件断言范围内。
