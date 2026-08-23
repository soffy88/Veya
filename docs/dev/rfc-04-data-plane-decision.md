# RFC-04: Data Plane 决策（P3 前置，Long-Term Memory 的存储底座）

> 状态：proposed（决策文档，不改变生产行为）
> 依据：[`VEYA_3.0_GAP_AUDIT.md`](../VEYA_3.0_GAP_AUDIT.md) §3.1（"现在不引入 Qdrant"前提不成立）
> 范围：P3 的前置门禁——`VEYA_3.0_GAP_AUDIT.md` §7 明确要求"数据底座 RFC 产出前，
> 不得开始 MemoryRecord schema 的实现型 PR"，本文件就是那份 RFC。

## 1. 问题

2.0 文档 19.1 节假设 MemoryRecord 的存储底座是 Postgres + pgvector（已经在用，
只是加 schema）。§3.1 审计发现这个前提不成立：veya 实际是 DuckDB(元数据默认) +
LanceDB/Tantivy(知识混合检索) + Postgres(可选，本部署未启用) 三头格局。P3 动手前
必须先决定 MemoryRecord 用哪个。

## 2. 现状实测（本轮新增核实，此前只知道"三头格局"，不知道 Postgres 有没有真的在跑）

- `platform/3O/oprim/oprim/meta_db/__init__.py`：`META_DB_BACKEND` 默认 `duckdb`；
  `postgres` 分支的模块注释写"路由到共享 aii-postgres，让 oskill 的 ingest 落进
  跟 stratum 读取同一个存储层"——这是**为另一个项目/场景设计的选项**，不是
  veya 专属默认。
- `grep -rn "META_DB_BACKEND\|STRATUM_PG\|POSTGRES" .env deploy/.env`：**零命中**。
  本部署从未配置 Postgres，`META_DB_BACKEND` 从未被设成 `postgres`。
- `platform/3O/obase/obase/persistence/vector.py::vector_search`：pgvector HNSW
  查询，**硬依赖一个真实 `PgPool` 连接**（`obase.persistence.pool.PgPool`）——
  这段代码在当前部署里**完全不可用**，不是"能用但没优化"，是没有 Postgres
  实例可连。
- `platform/3O/oskill/oskill/hybrid_search.py::hybrid_search`：LanceDB(dense) +
  Tantivy(BM25) + RRF 融合，是**当前真实在跑**的检索路径，但绑定
  "corpus_id/view_id/substrate"这套知识语料摄入模型（`oskill.knowledge._context`），
  不是通用的"任意 memory record 存进去就能查"接口——把 MemoryRecord 硬套进去
  需要先搞清楚 substrate 摄入契约，本轮未深入到这一步。

## 3. 决策

**MemoryRecord 元数据用 JSON 单文件存储（`~/.veya/vaom_memory_records.json`），
不引入 Postgres，不改造 LanceDB/hybrid_search 摄入管线。search() 先做关键词/
结构化过滤，不做向量语义检索。**

理由：

1. **不为没有的基础设施背书**。当前部署没有 Postgres，引入它是新增一个要部署/
   运维的服务，只为满足 2.0 文档一个已经证明站不住脚的假设——这正是 2.0 文档
   自己 19.1 节"早期规模下不引入 Qdrant"同一条原则的对称应用：不该引入的不只是
   Qdrant，还包括当前用不上的 Postgres。
2. **不越权改造 hybrid_search 的摄入契约**。它服务于一个明确的知识语料场景
   （corpus/substrate），MemoryRecord 是不同粒度的对象（个体记忆条目，非语料
   文档）。把两者绑在一起需要先跟 oskill 维护者对齐 substrate 契约能不能承载
   这种用法——这本身就是另一个决策，不该在这份 RFC 里顺带拍板。
3. **JSON 单文件是 veya 自己反复验证过的低成本模式**。`memory_bank.py`、
   `capability_model.py::_JsonRegistryStore` 都是这个模式，P1/P2 已经证明够用。
   MemoryRecord 现阶段的规模（个体记忆条目，不是语料库）用这个模式没有明显
   风险。
4. **关键词检索不是永久方案，是诚实的起点**。2.0 文档要求的语义检索能力真正
   需要时（规模到了、真实召回质量不够了），再评估怎么接 hybrid_search 或独立
   引入向量库——不在没有真实压力信号时先做。这也是"§19.1 不在真实规模需求
   出现前引入 Qdrant"原则本身要求的行为，不是妥协。

## 4. 何时重新评估

- 如果本部署未来真的启用 `META_DB_BACKEND=postgres`（比如因为别的子系统需要），
  MemoryRecord 可以迁移过去——JSON 单文件到 Postgres 表是可逆的（导出/导入，
  不是不可回滚的抉择）。
- 如果 MemoryRecord 条目数增长到 JSON 单文件明显吃力（这个模式在
  `memory_bank.py` 目前有容量上限 `MAX_PREFERENCES=200` 做参照，量级类似），
  或者关键词检索的召回质量在真实使用中被证明不够用，再单独评估向量检索方案。

## 5. 不做什么

- 不部署/配置任何 Postgres 实例。
- 不修改 `oprim/meta_db`、`oskill/hybrid_search.py`、`obase/persistence/vector.py`
  一行代码。
- 不承诺"以后一定要迁移到 Postgres"——这句话本身也是一个需要证据支撑才能做的
  决策，不是既定路线图。
