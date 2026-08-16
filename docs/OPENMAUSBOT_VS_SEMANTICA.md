# OpenMausBot vs Semantica — 全面能力对比

> 版本: 2026-08-16 | 上游: [milind-soni/OpenMausBot](https://github.com/milind-soni/OpenMausBot)（用户原链接拼写 OpenMausBoth，实际仓库名 OpenMausBot）、[semantica-agi/semantica](https://github.com/semantica-agi/semantica) v0.6.5
> 结论速览: **两者不是竞品，是不同层的两个系统**——OpenMausBot 是「Agent 的聊天界面 + 运行时」（交互层/编排层），Semantica 是「Agent 的图原生记忆/审计/推理底座」（知识层/治理层）。可叠加使用，不冲突。

---

## 一、一句话定位

| | OpenMausBot | Semantica |
|---|---|---|
| 定位 | **开源版 Grok Bot**：一个聊天 App 里养一支真实 AI bot 团队 | **开源版 Palantir**：图原生上下文 + 可审计 AI 基础设施 |
| 核心承诺 | 自带 agent（BYO）：本地 claude/codex/grok CLI 变成可聊天的联系人 | 每个 AI 决策都是一等公民：可追溯、可查先例、可因果分析 |
| 本质 | Agent 交互范式 + 多 agent 运行时（Electron 桌面 App） | 确定性知识层（Python 库 + 服务），不依赖 LLM 建图 |
| 许可证 | MIT | MIT |
| 语言/栈 | TypeScript（strict）/ React 19 / Electron / Node 24+ | Python 3.8+ / 5000+ 行核心 + 全套服务 |

---

## 二、目标用户与场景

| 维度 | OpenMausBot | Semantica |
|---|---|---|
| 目标用户 | 个人开发者/极客，想用自己已有的 Claude/Codex/Grok 订阅，把 agent 当联系人用 | 受监管企业（金融/医疗/法律/政府/国防）、AI 平台团队、数据/知识工程师 |
| 核心场景 | 「跟 agent 聊天、看着它干活、审批它的动作」——Telegram 式多 bot 协作 | 「把碎片化企业数据变成可查询知识图谱 + 每个 AI 决策过得了监管的 why」 |
| 典型痛点 | 一个盒子一个助手是错的形状；agent 应该是通讯录里的联系人 | 向量库只存相似度不存意义；决策无法审计；冲突被静默覆盖 |
| 部署形态 | 本地优先桌面 App（macOS/Windows 发布版；Ubuntu beta）+ 嵌入式 harness server | 自托管 Python 包/服务（REST/MCP/CLI），docker-compose 可选 |

---

## 三、核心架构

### OpenMausBot — 两个进程，零传输耦合

```
App (React + Tailwind)          Harness server (127.0.0.1:8799)       Agents (本机 CLI)
  Chat UI · model picker   →      Driver registry → Event bus(SSE) →   claude CLI
  computer panel                Permission broker (Allow/Deny)         codex CLI
  voice/call                    Box API → 云电脑 (box.ascii.dev)      grok CLI
  apps marketplace              Composio Connect → Gmail/Slack/GitHub…
```

- **Driver SPI 极小**（`server/contracts.ts`）：一个 provider 一个文件 + 一行注册；未知 driver 降级「unavailable」不崩舰队
- 每个 provider 的原生协议（stream-JSON / JSON-RPC / ACP）归一为一条 canonical runtime event stream，按 thread 落 NDJSON
- App 零客户端传输：只发 HTTP 命令，收一条 SSE 流
- 密钥 write-only：UI 只见「已配置」标记，TTS key 只在 harness 侧

### Semantica — 端到端确定性知识管线

```
Sources → Ingest → Parse → Normalize → Split → Extract → Conflict → Dedup
   → Knowledge Graph → [ Ontology · Reasoning · Provenance · Decisions ] → Enriched KG
   → Vector Store + Polyglot Graph Store (RDF & LPG) → Export / Visualize / REST · MCP · CLI
```

- 每阶段都是可独立 import 的模块（`semantica.ingest` / `parse` / `semantic_extract` / `kg` / `context` / `provenance`…）
- 推理引擎（forward chaining / Rete / Datalog / SPARQL）与 KG 构建、溯源层**全部确定性**，不需要 LLM
- 双时态事实（bi-temporal）+ 任意时间点图快照（`state_at()`）
- 存储 polyglot：RDF（Oxigraph/Blazegraph/Jena/RDF4J）+ LPG（Neo4j/FalkorDB/AGE/Neptune）+ 向量库，换后端不改业务代码

---

## 四、能力全景对比

| 能力域 | OpenMausBot | Semantica |
|---|---|---|
| **交互形态** | Telegram 式聊天：bot=联系人，右键 pin/置顶/复制会话 ID；群聊 + @提及 + bot 间对话 | 无聊天 UI；Python API + REST(100+ 端点) + CLI(20+ 命令组) + 浏览器 Knowledge Explorer |
| **Agent 运行** | 本地 CLI 驱动（claude/codex/grok），每 bot 独立模型/人格/线程记忆；云电脑（Box）+ 本机 Mac 可选 | 不运行 agent；作为 agent 的记忆/审计底座被集成（Agno、CrewAI 原生支持） |
| **工具/应用** | Composio Connect 500+ 应用（Gmail/Slack/GitHub/Notion/Linear），OAuth 一次全体 bot 可用 | 工具面以 MCP 12 个工具 + REST 12 组端点提供（extract/record_decision/query_decisions/get_causal_chain/run_reasoning…） |
| **上下文/记忆** | 每 bot 独立线程记忆（thread transcripts，NDJSON 落盘） | **Context Graph**：图遍历记忆（回答"什么相连、为什么、怎么连"）+ 语义检索 + 时间点快照；AgentContext 高层 API |
| **知识图谱** | 无 | 完整 KG 管线：多源摄入 → NER/关系/事件/三元组 → 冲突检测（不静默覆盖）→ 语义去重 → 图构建（双时态） |
| **决策溯源** | 审批卡片有记录（approval 事件） | **一等公民**：`record_decision()` + 因果链 + 先例检索 + 影响图 + W3C PROV-O 导出（监管可交） |
| **推理** | 无独立推理；agent 各自推理 | 确定性推理：forward chaining / Rete / Datalog / SPARQL，可解释路径；因果链分析 |
| **治理/合规** | 权限 broker（shell/文件编辑/提问 → Allow/Deny 卡片）；auto-approve 规则 | SHACL 约束 + OWL 生成 + SKOS 词表管理 + 策略规则引擎（`check_decision_rules`）+ 冲突检测 |
| **存储** | `~/.openmausbot`（本地 NDJSON 事件日志，key 本地持久） | 多后端：RDF 三元组库 + LPG 图库 + 向量库（FAISS/Qdrant/Weaviate/Milvus/PgVector…） |
| **语音/多媒体** | ElevenLabs TTS（回复朗读/通话）+ macOS 本地听写 + 通话模式（bot 边干活边口述） | 无 |
| **云电脑/浏览器** | Box 云桌面（实时屏幕预览、浏览器接管）、本机屏幕捕获与控制（macOS 先行） | 无 |
| **多 agent 协作** | 群聊路由（@提及/全员）、Chief of Staff 委派（主脑系统提示 + 成员名册）、branching 子线程 | 无；但「多 agent 共享单一智能层」是其卖点（各 agent 读同一 Context Graph） |
| **扩展机制** | Driver SPI（新增 provider = 1 文件）；Routines 占位 | 插件域技能 17 种 + 专用 agent 包（kg-assistant/decision-advisor/explainability）；Claude Code/Cursor/Codex/Windsurf/Cline/VS Code/OpenClaw 插件 |
| **生态入口** | 桌面 App（HTTP + SSE 本机） | MCP server（30 秒接入 Claude Desktop/Windsurf/Cline/VS Code）+ REST + CLI + docker |
| **企业数据接入** | 无（靠 Composio 应用） | 强：Databricks（Unity Catalog+Delta+lineage）、Snowflake（key-pair/OAuth）、PostgreSQL/MySQL/SQLite/Oracle、Kafka/Kinesis、Git、IMAP、MCP 资源、Parquet/Arrow |
| **性能基准** | 未发布（early but real） | 11.8 万节点图上：节点搜索 24ms→0.004ms（6000×）、语义去重 6.98×、候选生成快 63.6% |
| **成熟度** | Early but real：macOS/Windows 有发布版，Ubuntu beta；routines 占位、sidebar 未完成、通话仅 macOS | v0.6.5 活跃迭代；有 CI/测试套件/性能基准；1.6k+ 行 README 文档 + 完整模块参考 |
| **依赖外部服务** | Box（云电脑，付费）、Composio（应用市场）、ElevenLabs（语音）——均为可选 | 全部自托管；无强制外部 SaaS（去第三方是其卖点） |

---

## 五、深挖：六个关键差异

### 1. 对「agent」的定义完全不同
- **OpenMausBot**：agent = 你本机已有的 CLI 进程（claude/codex/grok），系统只是给它们一个聊天壳 + 统一事件流 + 审批层。**agent 是实体，UI 是壳。**
- **Semantica**：agent 是被集成方（Agno/CrewAI/任何 MCP client）。Semantica 提供的是 agent **之下**的确定性记忆/审计层。**agent 是消费者，Semantica 是底座。**

### 2. 记忆范式：线程日志 vs 图
- OpenMausBot：每 bot 一条线程（chat history 式记忆），多 bot 之间**没有共享记忆**（除了群聊上下文）
- Semantica：单一共享 Context Graph，跨 agent 共享；图遍历能发现「3 跳之外的连接」，向量检索发现不了；且带溯源和时点快照

### 3. 决策处理：审批卡片 vs 一等公民记录
- OpenMausBot：审批是**执行前**的权限门（Allow/Deny），记录在事件流里
- Semantica：决策是**执行后**的知识节点（category/scenario/reasoning/outcome/confidence），可查询/可作先例/可因果链追溯/可导出 PROV-O 交监管

### 4. 治理深度
- OpenMausBot：权限 broker（人盯着机器）
- Semantica：机器层面的规则引擎 + SHACL 约束 + 冲突检测 + 双时态（**机器自治理**，人的干预在规则层）

### 5. 与 LLM 的关系
- OpenMausBot：**完全依赖** LLM agent CLI（无 CLI 就没 bot）
- Semantica：**不依赖** LLM 做建图/推理/溯源（确定性）；LLM 只是可选的上游（被它治理的 agent）

### 6. 边界哲学
- OpenMausBot：本地优先，**依赖外部服务可选**（Composio/Box/ElevenLabs）
- Semantica：企业自托管，**拒绝第三方 SaaS**（"can't send their data to someone else's SaaS"）

---

## 六、互补性与结合方式

两者完全不冲突，可以叠成一个完整方案：

```
┌─────────────────────────────────────────────┐
│  OpenMausBot（交互层）                        │
│  聊天 UI · 审批卡片 · 云电脑 · 语音 · 群聊     │
│  ┌───────────────┐  ┌────────────────────┐  │
│  │ claude CLI    │  │ codex CLI          │  │
│  └───────┬───────┘  └─────────┬──────────┘  │
└──────────┼────────────────────┼─────────────┘
           │ MCP client         │
           ▼                    ▼
┌─────────────────────────────────────────────┐
│  Semantica（知识层）                          │
│  Context Graph · 决策记录 · PROV-O 审计       │
│  MCP server (12 tools) · REST (100+ 端点)    │
└─────────────────────────────────────────────┘
```

- **OpenMausBot 的 bot 通过 Semantica 的 MCP server** 调用 `record_decision` / `query_decisions` / `get_causal_chain`——聊天里的每个 agent 动作获得可审计的决策记录
- **Semantica 的图谱反哺 OpenMausBot 的审批**：`check_decision_rules` 可作为 auto-approve 的策略源
- 对称地，**Semantica 缺交互面**（无聊天、无审批 UI），OpenMausBot 缺知识底座——互相补位

---

## 七、与 veya 的关系（参考）

| veya 现状 | 可借鉴点 |
|---|---|
| 单主脑 + 全量工具面（~171 tools），冻结架构禁止程序化路由 | OpenMausBot 的 **Driver SPI**（小接口 + 降级不崩）值得参考；但其多 bot 联系人范式与 veya 单主脑冲突，不建议架构对齐 |
| `mcp_*` 工具面 + knowledge 端点 + 记忆注入桥（memory/graft context providers） | Semantica 的 **MCP server** 可作为外部知识工具直接挂进 veya 工具面（模型自主决定何时调用，不违反冻结架构）；决策记录/溯源是 veya 当前没有的能力 |
| project_ask 门禁（Understand/ask-act 判定） | Semantica 的 `record_decision` + 因果链可为门禁的追问/续答提供结构化记忆（现为 understand.json 单层链） |

> 注：veya 仓库内（docs/、代码、git 历史）**没有**任何 Semantica 对齐记录——用户确认过，本次对比是首次系统性记录。

---

## 八、选择建议（一句话）

- 想要「**跟 agent 聊天的桌面体验**、复用已有 CLI 订阅、看着它们干活」→ **OpenMausBot**
- 想要「**知识图谱 + 决策审计 + 确定性推理**、让 AI 决策过得了监管」→ **Semantica**
- 两者都想要 → OpenMausBot 做脸，Semantica 做脑的底座，中间用 MCP 粘合

---

## 九、内化集成落地记录（2026-08-16）

按用户指示将两库优势能力内化为 veya 自持模块（非外部依赖）：

| 内化能力 | 来源 | veya 模块 | 说明 |
|---|---|---|---|
| 决策账本（Decision Intelligence） | Semantica | `server/decision_ledger.py` | record/trace(因果链)/similar(先例)/impact(影响)/rules(策略门)/export(审计)，SQLite 持久化 |
| 上下文图（Context Graph 轻量） | Semantica | `server/context_graph.py` | 实体/关系邻接表 + BFS 遍历 + 软删时点快照，SQLite |
| project_ask 审计闭环 | Semantica | `server/project_ask.py::_record_decision` | 每次门禁判定/派工自动入账 + 图节点/因果边 |
| 提问卡片（bots ask before they act） | OpenMausBot | `server/user_control.py::ask_question` + `/api/v1/agent/answer` | bot 执行中提问 → 卡片 → 文字回答回填，超时给默认假设提示 |

**工具面**（master_tools 新增 5 个，模型自主选择，零程序路由，不裁藏）：
`ask_user` · `decision_record` · `decision_query` · `graph_store` · `graph_query`

**合规**：不改变「入口只有一个大模型」；记录/查询是护栏与审计，不参与路由判断；既有工具全部保留。

**测试**：`tests/test_internalized_graph.py`（10 用例，含提问卡片超时路径）。
