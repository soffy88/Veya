# veya 实现探查报告（code-review-graph，2026-08-11）

> 工具：`code-review-graph`（CRG CLI，持久增量知识图谱）
> 图谱：608 文件 / 6841 节点 / 52990 边 / 30 社区（commit `291afe99` 全量重建）

---

## 一、全局架构（30 社区）

| 社区 | 规模 | 凝聚力 | 说明 |
|---|---|---|---|
| `veya-agent` | 1416 | 0.26 | 核心库（veya/ + platform/3O） |
| `tests-fake` | 1222 | 0.07 | 测试套件 |
| `stage4-review-validate` | 1194 | 0.16 | 模板技能（img2threejs 等） |
| `server-request` | 1139 | 0.20 | HTTP 服务层（FastAPI 路由） |
| `veya-tui-print` | 321 | 0.32 | TUI/CLI 层 |
| `veya-loop-chain` | 295 | 0.09 | 自动化/循环链 |
| `components-load` | 160 | 0.13 | Svelte 前端组件 |
| 其余 23 个 | <60 每个 | — | 独立子模块（hooks/streaming/session…） |

**层级结构**：前端(Svelte) → `server-request`(FastAPI) → `veya-agent`(核心库) → `platform/3O`（oservi/oskill/oprim/omodul 多 3O 组件），tests 与 server 紧耦合。

## 二、耦合告警（architecture 10 条）

| 社区对 | 边数 | 观察 |
|---|---|---|
| server-request ↔ tests-fake | **919** | 测试直接 import 服务层（合理但重） |
| tests-fake ↔ veya-agent | 571 | 测试依赖核心库 |
| server-request ↔ veya-agent | 325 | 服务层调核心库（正常） |
| server-request ↔ tools-tool | 41 | 工具注册表接入 |
| session-load ↔ veya-agent | 34 | 会话层 |

无异常跨层耦合；919 边是测试规模大的结果，非坏味道。

## 三、关键调用流（flows 50 条）

| 流 | 节点 | depth | 文件 | criticality | 说明 |
|---|---|---|---|---|---|
| `legacy_agent_run` | 38 | 7 | 8 | 0.71 | **主脑 HTTP 入口**（/api/v1/agent/run） |
| `legacy_agent_stream` | 37 | 6 | 9 | 0.71 | **SSE 流式入口** |
| `master_chat` | 17 | 3 | 6 | 0.70 | 主脑聊天核心（MasterCoordinator.chat_stream） |
| `agent_graph_investigate` | 21 | 5 | 8 | 0.72 | 图谱探查子代理 |
| `browser_run` / `agent_verify` / `agent_vision` / `agent_long_horizon` | — | — | — | — | 工具/子代理流 |

**主脑链路**（用户消息 → 回答）静态可见链：`legacy_agent_run/stream → MasterCoordinator.chat_stream → oservi.MasterAgent.chat_stream → _bound_llm → veya.llm（veya1.1 别名 → opencode-go 直连）`。

## 四、死代码（540 符号，核心层过滤后）

**真死代码信号（旧架构遗留层）**：
- `commands/__init__.py`：`HookEventNames` / `HookDispatcher` / `WorkerAssembly`（CLI 钩子框架未用）
- `streaming/__init__.py`：`StreamRenderer` / `StdoutRenderer` / `DiffRenderer` / `TodoRenderer` / `ThinkingRenderer` / `OAuthFlow`（流式渲染器群，被新 SSE 取代）
- `server/coordinator.py::CognitivePhase`（旧协调器残留）
- `session/__init__.py::ModelSelector` / `ConfigLoader`

**误报类**（枚举/类型，非真死代码）：`SandboxStatus` / `ResourceType` / `ToolType` / `MobileAction` 等状态枚举。

**关键观察**：`server/coordinator.py`（**2374 行**，`Coordinator` 类 1376 行）与 `coordinator_master.py`（746 行）并存——旧版大协调器 `CognitivePhase` 无引用，疑似遗留；`commands/`/`streaming/` 的 CLI 层组件大量未接线。

## 五、大函数（>=50 行共 50 个）

| 文件/函数 | 行数 | 观察 |
|---|---|---|
| `templates/skills/img2threejs/forge/stage3_build/generate_threejs_factory.py` | 3216 | 技能包（独立领域，非核心） |
| `server/coordinator.py` | 2374 | **旧协调器整文件** |
| `veya/server/app.py::create_app` | 1397 | 单函数过大（应用装配） |
| `server/coordinator.py::Coordinator` | 1376 | 旧协调器类 |
| `veya/llm.py` | 1039 | LLM 调用层（含别名路由/兜底） |

核心服务层大函数集中在旧 `coordinator.py` 与 `veya/server/app.py::create_app`。

## 六、graph-engineer 模块影响面（CRG impact）

改 `server/graph_engineer.py` + `server/tool_registry.py` + `server/coordinator_master.py`：
- 81 节点直接变化，30 节点 2 跳内受影响，18 个附加文件受影响
- `graph_cycle` callers_of = **0**（静态不可见——由 LLM 动态调用，符合"零程序判断"设计；这是预期非缺陷）

## 七、结论与建议

1. **架构健康**：服务层/核心库/前端分层清晰，无异常耦合；graph-engineer 工具链接入干净（纯新增）。
2. **遗留清理候选**（非紧急）：`server/coordinator.py` 旧协调器（2374 行）、`commands/` 钩子框架、`streaming/` 渲染器群——被 coordinator_master + SSE 取代，可评估归档。
3. **大函数**：`veya/server/app.py::create_app`（1397 行）值得拆分；`veya/llm.py`（1039 行）可分层。
4. **测试耦合**：tests↔server 919 边是规模结果，若想降耦可引入接口契约测试隔离。

---
*生成：code-review-graph build + status/communities/architecture/flows/dead-code/large-functions/impact 全量探查*
