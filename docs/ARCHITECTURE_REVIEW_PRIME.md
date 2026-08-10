# Veya × Prime Agent 架构对照评审

> 状态：**纯分析文档（2026-08）**。不涉及任何代码改动；可借鉴点仅供后续决策参考。
> 视角：用 Prime Agent 的 daemon / worker / kernel 三层边界审视 veya 现有架构，找出差距与可吸收的工程机制。

---

## 0. 评审对象

- **Prime Agent**：5 层职责分离（Client → AgentConnection → Daemon/Supervisor → Worker → Kernel/IPython），核心规则一条：**UI 可以富、可以 client 专属，但它不能拥有 agent 执行**。
- **veya**：容器内单体（FastAPI/uvicorn + master_coordinator 主脑）+ 引擎子进程（claude/codex/grok/pi/hicode）+ 物理沙箱（run_in_sandbox）。

---

## 1. 总体分层映射

```
Prime 分层            veya 对应                        判定
──────────────────────────────────────────────────────────────
Client 层             SvelteKit 前端 (ChatConsole/       ✅ 一致
                      Dashboard/四引擎工作台)             (只渲染 SSE 事件)
AgentConnection      legacy_agent / master 路由          ✅ 一致
                     (前端只发文本收 SSE)                 (意图表达不拥有执行)
Daemon(Supervisor)   — (无独立 daemon)                   ❌ 缺
                     FastAPI+uvicorn+coordinator 单例    (路由与执行同进程)
Worker               主脑 ReAct 循环 + 引擎子进程         ⚠️ 半隔离
                     + hicode 编码执行器                  (执行器隔离, 主脑未隔离)
Kernel(IPython)      — (无 model-facing 内核)            ❌ 缺
                     最近似: run_in_sandbox/hicode       (一次性沙箱, 非持久内核)
外部                 opencode-go / gpt-5.6-luna /         ✅ 同层
                     CLI 引擎 / 3O 子库
```

---

## 2. 三层边界对照表（完整版）

### 2.1 Daemon（Supervisor）层

| 维度 | Prime | veya（FastAPI + uvicorn + coordinator 单例） | 差距 |
|---|---|---|---|
| 拥有的 | socket/路由/attach/健康/消息投递/命令 journal | HTTP/SSE 路由（`/api/v1/agent/stream`）、请求→主脑派发、工具注册表（master_tools）、引擎子进程 spawn、SSE 事件投递 | ⚠️ 路由与执行同进程，无独立 daemon 职责面 |
| 不拥有的 | 一切具体业务执行与状态机计算 | 无——执行就在这个进程里（主脑 ReAct、工具执行、记忆读写全在 uvicorn 进程内） | ❌ 越权拥有了一切执行 |
| 故障域 | 挂 → 存活 Worker 监控 socket 消失，原子 Lease 竞选 replacement supervisor 接管 | 挂 → 容器重启，全部会话/进行中任务丢失，无接管、无竞选 | ❌ 无自愈机制 |

### 2.2 Worker 层

| 维度 | Prime（每 worker 一棵 root session tree） | veya（master_coordinator 主脑 + engine_runner 子进程） | 差距 |
|---|---|---|---|
| 拥有的 | root runtime + session + scheduler + kernel + children | ReAct 循环、会话历史（`_histories` 内存 + P1 SqliteHistoryStore）、工具调用、引擎 CLI spawn（claude/codex/grok/pi）、hicode 子进程、记忆/自动化/蜂群/RAG/Vault | ⚠️ 主脑自身是单体；引擎/编码执行已外置子进程 ✓ |
| 不拥有的 | 终端 UI 渲染、底层 OS 进程直接执行权 | 前端 UI 渲染（前端持 localStorage 会话）；部分 OS 执行在引擎子进程 | ✅ 基本对齐（UI 不拥有执行） |
| 故障域 | 挂 → 只死一棵 tree；250ms/1s/5s 三次重试恢复，reap 旧进程组 + transcript recovery marker，不重放不确定副作用 | 主脑挂 → 整个对话全挂（单进程内）；引擎子进程挂 → 只死该次引擎调用（`engine_error` 事件兜底）✓ | ⚠️ 隔离在"引擎层"，不在"主脑层" |

### 2.3 Kernel（IPython）层

| 维度 | Prime | veya（run_in_sandbox / hicode 工作区） | 差距 |
|---|---|---|---|
| 拥有的 | Python namespace + 代码执行（惰性创建、Jupyter multipart + HMAC、串行执行、状态快照 dill/json） | 一次性物理沙箱（run_in_sandbox）、hicode 隔离工作区（子进程） | ⚠️ 有"沙箱"但无"持久内核" |
| 不拥有的 | 任何权威操作（typed host request 汇报给 TS） | 工具结果回喂主脑（无权威操作） | ✅ 对齐 |
| 故障域 | 挂 → Worker(KernelManager) 拦截重启，重新注入 RLM shim，从快照恢复 namespace | sandbox/hicode 挂 → 该工具调用失败（有兜底/重试），无 namespace 快照恢复 | ⚠️ 无"状态可恢复的执行内核" |

---

## 3. 关键机制对照表

| 机制 | Prime | veya | 差距评估 |
|---|---|---|---|
| 会话隔离 | worker 树 + session lease（canonical JSONL 路径为 key，并发 open → session_already_active） | 内存 dict + Sqlite + 前端 localStorage；无 lease | ⚠️ 并发 open 会打架 |
| 事件协议 | v4 versioned envelope + 能力协商 + 游标 {generation, sequence} + 快照 begin/chunk/end | SSE 无版本事件（text_delta/tool_call/hicode_progress） | ⚠️ 前端对事件结构硬编码 |
| Backpressure | attachment-local：单阻塞 client 只停自己的增量事件；supervisor 不保留 unbounded per-client queue | SSE 无背压（前端断连后端任务继续跑） | ⚠️ 孤儿任务风险 |
| 幂等 | append-only journal + clientId/commandId，重复执行返回已存结果，uncertain 不重放 | 无（hicode_queue 弱任务队列） | ❌ 重试可能重放 |
| 故障域粒度 | 一棵 tree 一个 worker | 单进程全有全无（容器级） | ⚠️ 粒度粗 |
| 子代理 | RLM children（深度默认 1，children 不能建 grandchildren）+ 成本折叠 | Genesis/swarm（有等效物，非 RLM 树） | ⚠️ 有等效物 |
| 成本归属 | child usage 异步折叠进 parent turn，树级对账 | 单次 `cost_calculator`，无树级对账 | ⚠️ 可审计性弱 |
| 内核沙箱 | IPython + 信任边界明确（非安全沙箱；凭据永不全量进 Python） | run_in_sandbox（物理沙箱，网络隔离） | ⚠️ 取向不同（物理 vs 协议） |
| 协调更新 | 两阶段：workers 并行 checkpoint → supervisor 原子 manifest → commit | 无 | ❌ |

---

## 4. 工程启发对照（veya 现状）

| Prime 启发 | veya 现状 | 结论 |
|---|---|---|
| **① 爆炸半径控制**：UI 离线可关、Python kernel 是"耗材"可杀可重建、Daemon 挂了 Worker 竞选接管——高价值长任务不因通讯层抖动前功尽弃 | 引擎子进程（claude/codex/grok/pi/hicode）隔离 ✓——LLM 死循环只杀该子进程；但主脑 ReAct 在单进程内——坏工具/记忆读写影响全局 | ⚠️ 半隔离：执行器隔离了，主脑没隔离 |
| **② 控制流反向依赖/死锁规避**：Python 阻塞时经 Jupyter control 通道带外回传 Handle，绝不复用数据通道做控制流 | veya 用 OpenAI function calling（工具 = 异步 await），**不存在 Jupyter 串行通道死锁问题**——模型→工具走协议内，天然无阻塞 | ✅ 无此风险（架构取向不同，无需借鉴） |
| **③ 成本与权限树状归属**：孙子 agent 生命周期归属直接父级，token 异步折叠到 root 对账——每一分钱可审计 | 有单次 `cost_calculator`（LLM 调用计费），无树级对账——Genesis/swarm 子代理消耗未折叠 | ⚠️ 可审计性弱于 Prime |

---

## 5. 差距清单与可借鉴点（按优先级）

### 低改动、高收益（适合 veya 轻量单容器服务）
1. **请求幂等键**：SSE 请求带幂等键（clientId/commandId），重复提交不重放——前端重试/网络抖动场景。借鉴 Prime 的 append-only journal 思想（简化为幂等键 + 结果缓存）。
2. **孤儿任务清理**：SSE 断开时 kill 对应引擎子进程/hicode 任务（当前 claude/codex 子进程可能继续跑，浪费订阅额度）——借鉴 attachment-local + 进程组清理。
3. **事件协议版本化**：SSE 事件带 `schema_version`，前端按版本解析，兼容演进——借鉴 v4 envelope（不做完整游标/快照，只加版本号）。

### 中改动（veya 变"多进程"方向）
4. **主脑执行外置为 worker 子进程**：主脑 ReAct loop 放进独立子进程（类似 hicode 模式），uvicorn 只做路由/SSE 转发——崩溃隔离 + 热恢复，直接获得 Prime 的"爆炸半径控制"。
5. **会话持久化为主 + lease**：从内存 dict 迁到 Sqlite（P1 已在做），session lease 防止多客户端并发改同一会话。

### 高改动（veya 单容器部署不建议照搬）
6. 完整 daemon/worker/kernel 三层 + Supervisor 竞选自愈 + 协调更新两阶段提交——分布式机制对单容器服务属于过度设计，借鉴思想即可。

---

## 6. 结论

1. **veya 的分层哲学（UI 不拥有执行）已经正确**，与 Prime 核心规则一致。
2. **主要差距在执行拥有者的健壮性**：Prime 用"每 worker 一棵树 + 进程隔离 + 幂等 + 背压"，veya 用"容器内单体 + 子进程工具"——veya 轻量够用，但**孤儿任务、并发会话冲突、无幂等、成本不可树级对账**是真实脆弱点。
3. **最值得吸收的 3 个低改动机制**：幂等键、孤儿清理、事件版本化。
4. **中期方向**：主脑执行 worker 化（hicode 化子进程）获得崩溃隔离。
5. **完整 daemon/worker/kernel 对 veya 单容器部署是过度设计**；Jupyter 控制通道死锁规避对 veya 无意义（无 kernel 模型）。

---

*参考：Prime Agent 架构深析（daemon/worker/kernel 三层边界 + 工程启发），veya 实测架构（2026-08，工具面 22 + dispatcher、主脑 opencode-go 直连、引擎子进程、hicode 执行器）。*
