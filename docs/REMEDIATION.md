# Veya 整改计划（审计沉淀）

> 状态: 进行中 · 起始 2026-08-09 · 权威架构以 [`ARCHITECTURE_STABLE.md`](ARCHITECTURE_STABLE.md) 为准。
> 本文是整改的单一追踪源。触及冻结主链路的批次（B/C/D）**必须先获用户同意再实施**。

## 目标架构（用户确认）

主 LLM = 唯一入口编排器：接收问题/任务 → 分析分解 → 自解 **或** 分派给工具/子 LLM。
编程任务 = 专用 Agent（Reasonix，独立进程 + 独立 workspace/checkpoint；**沿用主 LLM 同一 key**，
其"独立"= 进程/工作区隔离 + 接收主 LLM 下发的结构化编码指令，非 key 隔离）。

## 审计发现 → 整改批次

| 批次 | 内容 | 触及冻结主链路 | 状态 |
|---|---|---|---|
| **A** | 认知债清零（纯文档，不改行为） | 否 | ✅ 完成 (AGENTS.md/architecture.md/BENCHMARK 加历史横幅 + graveyard.md + 本文) |
| **B** | ~~编程 Agent key 独立化~~ → **沿用主 LLM 同一 key**（用户澄清 2026-08-09: 设计如此）| — | ✅ 定论 · 本轮误引入的独立 key 机制已撤除, deploy 回原状; reasonix 与主 LLM 共用 `OPENCODE_API_KEY`, 独立性体现在进程隔离 + 接收主 LLM 的结构化指令 |
| **C** | 统一会话入口到 `coordinator_master`（消灭会话双头脑）| 是（路由架构）| ✅ **核心达成** · 会话链路(Web/IM/automata/**CLI 本次**)全在主脑 · 专用/次级路径(prompt/vscode/backends/headless/flow/resume)按决策记录冻结保留, coordinator.py 整体退役降级为未来独立事项 |
| **D** | 补齐子 LLM(swarm) 分派为一等工具 or 明确内部触发；端点定义单源化（去双写）| 是（工具面/端点）| ⏸ 可选 · 建议后续 |
| **E** | 环境抽象：硬编码 host/port 收进单一配置层 | 部分 | ⏸ 可选 · 建议后续 |

## 决策记录（用户授权 · 按四原则「长期主义 / 质量为王 / 功能之上 / 体验最优」自定 2026-08-09）

> 结论: **会话脑已统一 → 停止对剩余专用/次级路径的强行迁移**, 锁定已验证成果。

1. **`prompt`/`vscode`/`backends` 端点 → 不强行迁移(保留旧脑), 标注 legacy。**
   - 理由(功能之上/体验最优): `vscode.py` 与扩展之间是 **squad_start→squad_done 的 SSE 契约**,
     master 只发 tool_call 事件 → 强切会**当场打断正在工作的编辑器闭环**(BENCHMARK G6)。
     `backends`/`prompt` 同样依赖旧脑的结构化 `output/squads/status`。
   - 理由(长期主义): 正确姿势是"coordinator.py 整体退役时, 连同扩展契约一起换", 而非
     为架构洁癖逐个切、留一地半成品。当下**会话主脑已统一**(Web/IM/automata/CLI), 收益已拿到。

2. **`--resume` → 保留旧 DAG checkpoint 语义(不动), 随 coordinator.py 一起退役。**
   - 理由(质量为王): 主脑下"恢复"=会话连续, 与 checkpoint 恢复是两种功能; 现在重写会**回归风险**
     且无法端到端验证。低频功能, 保持可用 > 半迁移。

3. **`headless`/`flow` → 作为"结构化/HITL 专用 DAG 子系统"保留, 退役另立 usage-audit 步骤。**
   - 理由(长期主义/功能之上): `veya-headless` 的价值就是机器间结构化输出; `flow` 是 Genesis HITL
     真实能力。不为洁癖删除working子系统; 退役前先审计调用方。

**总裁定**: 批次 C 的核心目标(消灭会话双头脑)**已达成**。剩余旧脑用户是合法的专用子系统 +
次级端点, 已在下表分类冻结。coordinator.py 的整体退役降级为"未来带扩展契约一起做"的独立事项。

## 批次 C 精确在库（旧脑 `coordinator.handle`/`resume` 的 live 调用点）

> 复审修正: IM(telegram/dingtalk)、automata **早已在主脑**；旧脑残留比初判集中。

| 位置 | 种别 | 处置 |
|---|---|---|
| `cli/main.py`（`veya` 交互/stdin）| 会话 CLI | ✅ 已切主脑（本次）|
| Web `routes/master.py` · IM `telegram/dingtalk` · `automata` | 会话 | ✅ 早已在主脑（复审确认）|
| `server/routes/prompt.py:44`（`prompt_router`, `{text,persona,model,provider}`）| 次级聊天 HTTP 端点 | ⏳ 可迁移, 属 HTTP 契约变更, 需确认 + 查调用方 |
| `server/routes/vscode.py:45/103/266` | 编辑器集成 HTTP/SSE | ⏳ 需确认（契约变更）|
| `server/backends.py:135`（`_run_builtin`, backend 抽象）| 结构化 backend 契约 | ⏳ 需确认 |
| `cli/headless.py`（`veya-headless`）| **机器间结构化 I/F + orchestrator 内部 `run_squad_headless`** | 🚫 非会话 swap; 属 DAG 子系统整体退役/保留决策 |
| `server/routes/flow.py:60`（Genesis HITL, `mode=requirement`→manifest）| **专用 DAG** | 🚫 对象外, 保留/另行重设计 |
| `routes/session.py:250` + `cli/main.py:103`（`coordinator.resume`）| resume 语义 | ⏳ 待用户决定语义 |

## 关键发现证据

- **双头脑（复审修正——比初判轻）**：会话链路（Web / IM / automata / CLI）已全部在主脑；
  旧 `coordinator.py` 剩余用户多为**结构化/专用子系统**（headless 机器 I/F、flow HITL、
  backends 抽象）或**次级端点**（prompt / vscode），并非一律可 swap。见上表分类。
- **编程 Agent 沿用主 key（设计如此，非缺陷）**：reasonix 与主 LLM 共用 `OPENCODE_API_KEY`
  （`deploy/reasonix-entrypoint.sh:9`）；其独立性在于进程隔离 + 接收主 LLM 下发的结构化编码
  指令，非 key 隔离。用户 2026-08-09 澄清确认。
- **Reasonix 双配置**：`reasonix_agent.py:48 REASONIX_MODEL=luna`（本地 subprocess）vs `reasonix-entrypoint.sh --model opencode-go`（云端 serve :8768）。云端为主 / 本地兜底。
- **子 LLM 分派半实现**：swarm 注入 MasterAgent（`coordinator_master.py:266`）但未在 `tool_registry.py` 注册为工具。
- **文档三重人格**：`AGENTS.md`（空壳 agents/）、`docs/architecture.md`（旧 DAG）、`ARCHITECTURE_STABLE.md`（权威）互相矛盾 → 见 [`graveyard.md`](graveyard.md)。

## 验证基线

- `tests/`（77 文件）+ `veya doctor --json` + 线上 `curl /api/v1/mcp/health`。
- 批次 C 灰度：同一 prompt 从 Web / CLI / Telegram 发出，断言同经 `coordinator_master`、返回一致。
