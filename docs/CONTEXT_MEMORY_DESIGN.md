# Veya 强上下文 + 个人记忆 设计（P1–P4）

> 状态: 实施中 · 2026-08-09 · 权威主链路见 [`ARCHITECTURE_STABLE.md`](ARCHITECTURE_STABLE.md)。
> 用户已授权执行 P1–P4；按 P1→P2→P4→P3 顺序落地，一期一验证。

## 实施进度

| 期 | 状态 | 证据 / 说明 |
|---|---|---|
| **P1** 历史持久化 | ✅ **完成·已验证** | `veya/history_store.py`(SqliteHistoryStore) + `coordinator_master` 冷启恢复/落盘。验证: 实例A落盘 → 全新实例B(模拟重启)恢复含用户事实, PASS |
| **P2** SOP 理解优先门 | ✅ **veya 侧完成** | `_HOST_SOP_APPEND` 加 "UNDERSTAND-FIRST GATE"(理解前禁调外部工具, 例外=缺料/执行)。oservi:136 "possess cross-session memory" 因 P1 已做实, 保留不改(暂不动子库) |
| **P4** 记忆蒸馏 | ✅ **机械部分完成·已验证** / ⚠️ **质量待生产验证** | `veya/memory_store.py`(存储+关键词检索) + `veya/memory_distill.py`(蒸馏) + `coordinator_master` 检索注入/后台蒸馏。验证: distill 解析/retrieve/inject 不重复/distill_and_store 落库 全 PASS。**注**: 蒸馏抽取质量、真实语义检索需真实模型 + 生产环境验证; kill-switch `VEYA_MEMORY=0` |
| **P3** 跨设备同步 | ✅ **完成** (前端待运行时验证) | 后端 `/api/v1/agent/history/{sid}` 返回 P1 messages(已验证) + 去掉 `chat_stream` 公共桶默认。前端 `sessionStore.hydrate()`(svelte-check 0 error, 运行时需真实前后端联调) |


## 0. 目标 / 非目标

**目标**
- G1 主脑**永不失忆**：本轮/历史轮/重启/部署/换 worker/**换设备**都记得。
- G2 **先理解后动手**：理解阶段不反射式调外部工具（呼应用户 2026-08-09 原则）。
- G3 **个人智能感（pi 那样）**：聊很久还记得你、能**主动想起**相关事实/偏好，而非倒带全量。

**非目标（诚实划界）**
- 不做人格温度 / 语音 / 低延迟（那是另一条产品线，非记忆架构）。
- 不引入 PostgreSQL（当前部署无 PG，见 §2）。

## 1. 根因回顾（为什么现在失忆）

```
前端(sessionStore, localStorage 有全量历史) --只发 {text, session_id}--> 后端
后端 MasterAgent._histories[sid]  = 纯进程内 dict (master_agent.py:278)
                                    ↑ git pull → docker compose up -d 重启即清空
                                    ↑ 换设备/换 worker = 另一个进程, 天生为空
SOP:136 "You possess cross-session memory"  ← 空头支票 → 模型去扫 stratum(未建表) → 放弃
```
**割裂大脑**：前端有历史不发、后端持有历史但易失，两边都不为"把对话喂给模型"负责到底。

## 2. 存储决策（关键，先定地基）

| 候选 | 结论 | 理由 |
|---|---|---|
| `obase.persistence`（PG+pgvector）| ❌ 不用 | 强绑 PostgreSQL；**当前部署无 PG**，`docker-compose` 只有 backend + `veya-data` 卷 |
| `obase.interaction_history` | ❌ 现状不可用 | **空 stub，无 API** |
| **`~/.veya/` 下 SQLite（veya-data 卷）** | ✅ **选定** | 卷是**命名卷**（`veya-data:/home/soffy/.veya`）→ 重启不丢；单文件零运维；契合现有 `~/.veya/*.json(l)` idiom；支持按 sid 并发追加 + 查询 |

**存储位置**：`~/.veya/sessions/history.db`（SQLite，随 veya-data 卷持久）。
检索用嵌入向量放 `~/.veya/memory/`（见 P4），不依赖 pgvector。

## 3. 数据模型

```
-- P1: 对话历史 (逐轮消息)
turns(sid TEXT, idx INT, role TEXT, content TEXT, ts INT, tool_calls JSON,
      PRIMARY KEY(sid, idx))
sessions(sid TEXT PRIMARY KEY, title TEXT, created_ts INT, updated_ts INT,
         user_id TEXT, summary TEXT)     -- summary 由 P4 回填

-- P4: 蒸馏记忆
memories(id TEXT PK, user_id TEXT, kind TEXT,   -- fact | preference | rule
         text TEXT, salience REAL, source_sid TEXT, ts INT, embedding BLOB)
```
`user_id` 走既有 `veya.im.pseudo.anonymize_user_id`（L4 已用），支持多用户隔离 + 跨设备（同 user 不同设备共享）。

---

## P1 — 对话历史持久化（进程无关）🎯 地基

**改动点**
1. **oservi MasterAgent**（`master_agent.py`）：把 `_histories`（进程内 dict）背后接一个 `HistoryStore` 协议（load(sid)/append(sid, msg)/save）。
   - `chat_stream` 取历史：`self._history_store.load(sid)`（miss 时空历史 + system），追加后 `append`。
   - 进程内 dict 降级为**热缓存**（快路径不变），SQLite 为**权威源**（冷启动/换进程从它恢复）。
2. **veya 装配层**（`coordinator_master.py`）：注入 veya 侧 `SqliteHistoryStore(~/.veya/sessions/history.db)` 作为 `HistoryStore` 实现（鸭子类型，符合 3O "机制在主库、装配在 veya"）。
3. **隐雷标注**：`routes/master.py:39` 每请求新建 `MasterCoordinator` → 天生失忆。web 走的是 `/api/v1/agent/stream`（单例，`chat_stream.py`）不受影响；但 `/master/chat` 端点要么复用单例、要么废弃，避免误用。

**为什么这样**：机制（历史抽象）留在主库 oservi，具体存储（SQLite 路径）在 veya 注入——与 key/工具面同一装配范式。

**验收**：起容器 → 聊 3 轮 → `docker compose restart backend` → 第 4 轮仍记得前 3 轮（当前会失忆）。

## P2 — SOP 对齐现实（理解优先门 + 诚实声明）

**改动点**
1. **理解优先工具门**（写进 oservi MASTER SOP + veya `_HOST_SOP_APPEND`）：
   > 在对「本条消息 + 已有对话上下文」形成理解前，**默认不调外部工具**。工具仅在两种时机：
   > (a) **理解缺料**——读懂本条消息本身需要我还没有的外部内容（如用户贴的 URL/仓库）；
   > (b) **执行阶段**——已有计划，工具是手脚。
   > 「扫状态/翻记忆」不属于 (a)：先看眼前的对话上下文。
2. **删空头支票**：`master_agent.py:136 "You possess cross-session memory"` —— 仅在 P1+P4 做实后才保留此句；否则改为描述实际能力。

**验收**：对已建历史的会话说「按你建议执行」→ 模型直接读上文执行，**零状态查询工具调用**（当前会连开 7 个）。

## P3 — 跨设备真同步

**改动点**
1. **后端**：把 `/api/v1/agent/history/{sid}`（`veya/server/app.py:522`，现在返回 decision trail）扩成**也能返回会话 messages**（从 P1 的 SQLite 取）。
2. **前端**（`sessionStore.svelte.ts`）：换设备/新加载时，若本地无该 sid → 调 history 端点 hydrate；本地有 → 以后端为准做一次对账。去掉 `chat_stream.py:37 sid = session_id or "chat_stream"` 的公共桶默认（改为必须显式 sid，防串味）。

**验收**：设备 A 聊 → 设备 B 打开同 sid → 看到完整历史并可续聊。

## P4 — 记忆蒸馏回路（pi 那样的"懂你"）

> 这是"长上下文"跃迁到"个人智能"的关键层。核心：**不倒带全量，而是蒸馏 + 检索注入**。

**四个子件**
1. **蒸馏（distill）**：每会话结束 / 每 N 轮，跑一次蒸馏（用主模型或本地小模型）：
   - 抽取**持久事实**（关于用户/项目）→ `memories(kind=fact)`；
   - 抽取**偏好/规则** → 复用既有 `MemoryProtocol.add_preference`（`master_agent.py:557`，别另造）；
   - 生成**滚动会话摘要** → 回填 `sessions.summary`。
2. **检索（retrieve）**：新一轮开始时，按"当前消息 + 近期上下文"对 `memories` 做相关性检索（嵌入余弦 + recency + salience 加权），取 top-K。
   - 检索引擎优先复用 **stratum**（`mcp_stratum_*`，本就是 BM25+向量的"知识管家"）——**但前置：先把 stratum 的 `notes/memories` 表建起来**（转录里 `notes 表未初始化` 正是此坑）；
   - stratum 未就绪时，退到 `~/.veya/memory/` 的本地嵌入索引（不依赖 pgvector）。
3. **注入（inject）**：把 top-K 记忆压成一个紧凑 `# MEMORY（关于用户）` 块，注入 system 上下文——**不是把 transcript 倒给模型**。
4. **上下文窗口管理**（贯穿 P1/P4）：喂给模型的 = 最近 K 轮逐字 + 更早轮的滚动摘要 + 检索到的长期记忆。**有界 token**，根治"撑爆 free 池网关上下文"（冻结文档 §2.2 的老坑）。

**验收**：
- 新会话（无历史回放）里，模型能主动引用几周前会话里说过的某个持久事实/偏好；
- 超长会话不再随轮数线性涨 token（摘要+检索封顶）。

---

## 4. 分期依赖与顺序

```
P1(历史持久) ──┬─► P2(SOP: 有上文了才配谈"先理解")
               ├─► P3(跨设备: 复用 P1 的 SQLite)
               └─► P4(记忆蒸馏: 建在 P1 的会话数据 + stratum 建表之上)
```
**强烈建议顺序 P1 → P2 → P4 → P3**（P3 可选，跨设备需求不急时后置）。

## 5. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 改 oservi（3O 子库）blast radius | HistoryStore 设为协议 + 默认内存实现；SQLite 实现在 veya 注入，主库行为可回退到纯内存 |
| SQLite 并发写（多请求同 sid）| 单 writer + WAL 模式；sid 级串行（reasonix serve 已有同款串行锁可借鉴） |
| 蒸馏成本/延迟（P4）| 异步后台跑（automata 定时），不阻塞对话主链路 |
| stratum 建表影响现网 | 幂等建表脚本 + 只读探活先行；建表失败退本地嵌入索引 |
| 历史无限增长 | 摘要压缩 + 老 turns 归档（保 summary，转冷存） |

## 6. 触及冻结主链路清单（须逐项审批）

| 期 | 触及 | 类型 |
|---|---|---|
| P1 | `oservi/master_agent.py`（3O 子库）、`coordinator_master.py` 装配 | 机制 + 装配 |
| P2 | MASTER SOP（3O）、`_HOST_SOP_APPEND`（veya） | 行为规范 |
| P3 | `veya/server/app.py` history 端点、`chat_stream.py`、前端 `sessionStore` | 端点 + 前端 |
| P4 | 蒸馏/检索/注入新模块、stratum 建表、`memories` 存储 | 新增能力 |

**审批规则**：每期实施前先出"具体到行"的实现方案 → 获同意 → 动手 → 验收演示。
