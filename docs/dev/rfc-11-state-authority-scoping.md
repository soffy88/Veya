# RFC-11: PR-07 State Authority — 决策：这次不动代码

> 状态：§1-3 是 2026-08-24 当时的评估（阻塞点是"不敢动真实用户数据"）；
> 用户随后明确"不用考虑之前的数据"，解除了这个阻塞点——§4 记录第二轮实际
> 执行的范围（只做了会话历史这一项，session_tree/memory 仍未动）。

## 1. 现状核实

跟 PR-06/PR-11（`rfc-10`）不一样，这次没有"建好了没接线"的意外发现——`grep
canonical.*event|EventStore|EventLog` 只命中两个不相关的既有类名。计划文档
§3 自己的现状描述是准的：history、session tree、compacted history、memory、
goal event store、trace、replay projection 现在是真的分散成好几套独立存储，
不是名字不同但底层统一。

## 2. 为什么这次明确不写代码——不是能力问题，是这个决策不该由我单方面做

PR-07 跟这次会话做过的所有其他事在性质上不一样：

- **PR-01/08/09/10/06/11/20**：改的是 CI 配置、依赖声明、附加元数据、新测试、
  文档——都有一个共同点，出错了回滚成本低（改回 pyproject.toml 一行、删掉一个
  guarded import、撤销一份文档），而且改之前都能用运行时验证/mypy/测试拿到
  "这样做对不对"的直接反馈。
- **PR-07**：要动的是 `history_store`/`session_tree`/`memory_store` 这几个
  **真实承载用户对话历史的持久化存储本体**。把它们收敛成"canonical event log
  + projection"意味着：现有数据要迁移（旧格式的历史/记忆怎么变成新的
  canonical event，一次迁移写错就是真实用户数据损坏或丢失）、每一个读这几个
  存储的调用点都要跟着换读法（`coordinator_master.py`/`memory_bank.py`/
  `session tree` 相关路由——面广，逐个验证工作量大）、而且这类改动出错之后
  "运行测试立刻能看出问题"这件事不一定成立（数据迁移的错误可能是悄悄的、
  过一阵子才会在旧数据读不出来的时候暴露）。

这是一个真正需要跟你对齐设计再动手的决策，不是我能在"四原则"框架下替你决定
"做还是不做"的事——这次调研到这里为止就是正确的止损点：**知道现状是什么、
知道为什么难、不装作已经有答案**。

## 3. 如果以后要做，第一步该是什么

不是"写 canonical event schema"，是先回答这几个问题（需要你的输入，不是我能
单方面调研出来的）：

1. 现有用户数据（`~/.veya` 下的 history/memory/session 文件）能不能接受一次
   格式迁移？迁移失败的回滚方案是什么？
2. 是要一次性切换所有读写路径，还是先让新写入走 canonical event + projection、
   旧数据保持旧格式只读兼容一段时间？
3. `veya_loop`（独立包）自己的状态机制要不要也纳入统一，还是保持独立？

这几个问题没有唯一正确答案，是产品/工程权衡，回答清楚之后才适合再开一轮
"PR-07 落地"。

## 4. 第二轮：用户明确"不用考虑之前的数据"后实际做的

§3 的三个问题里，第 1 个（数据迁移方案）被用户直接解除——不用考虑之前的数据，
不需要迁移方案。这不代表第 2/3 个问题也一起解除，所以范围仍然收窄：**只改了
`veya/oservi/history_store.py`（会话历史），`session_tree`/`memory_store`
这次没动**——§3 问题 3（`veya_loop` 要不要纳入）跟问题 2（一次切换 vs 新旧
并存）本质都还是"这套模式要推广到多大范围"的产品决策，用户这句话解除的只是
"数据"这一个具体障碍，不是把整个 PR-07 的范围都交给我判断，混为一谈会是我
自己过度解读授权范围。

### 4.1 实际改动

`SqliteHistoryStore`（`turns` 表）从"`save()` = DELETE 整个 session 再
INSERT"改成"`save()` = INSERT 一条新的不可变修订（revision 单调递增，永不
删除/覆盖旧行）"：

- `load_sync()`：外部行为完全不变——仍是"读这个 session 当前应该看到的消息
  列表"，内部改成读最新一条 revision。
- `save_sync()`：外部签名不变，内部从覆盖改成追加。
- 新增 `replay_sync()`/`replay()`：读完整的不可变修订序列（`[{"revision",
  "messages", "ts"}, ...]`），这是新增的能力，之前没有任何方式能看到"这个
  session 历史上某次 checkpoint 的样子"，只能看到最新状态。
- `list_sessions_sync()`：从"按行聚合"（旧 schema 一行一条消息）改成"每个
  session 只取最新一条 revision"（窗口函数），因为新 schema 一行是一次
  save() 的完整快照，聚合口径必须跟着变，否则 `msg_count` 会算成历史修订
  总数而不是当前消息数。

`MasterCoordinator._persist_history`/`_restore_history`（`server/
coordinator_master.py`）零改动——它们只调用 `store.save(sid, messages)`/
`store.load(sid)`，这两个方法的外部契约完全没变，所以调用方不用跟着改，这是
故意这样设计的（§2 提到的"每一个读这几个存储的调用点都要跟着换读法"这个风险
被这个设计规避掉了，不是靠"不用考虑数据"解决的，是靠保持外部 API 形状不变
解决的）。

### 4.2 已知取舍：没做修订保留策略

`save()` 不再删除旧数据，意味着长会话频繁 checkpoint
（`VEYA_CHECKPOINT_INTERVAL_S`）会让同一 session 的行数持续增长。没有做
"只保留最近 N 条 + 归档更早的"这类保留策略——现在不知道合适的保留窗口该多大，
这个数字应该等真实用量数据出来后再定，硬猜一个比不设限更容易做错决定（见文件
头注释）。这是记录下来的已知 gap，不是遗漏。

### 4.3 验证

- `tests/test_history_store_async.py`：新增 2 条直接测试不可变性——
  `test_save_is_append_only_old_revisions_survive`（save 三次，验证 `load()`
  只看到最新一条，但 `replay()` 三条修订都还在且顺序正确）、
  `test_replay_respects_limit_and_session_isolation`。原有 2 条并发/WAL 测试
  （其中一条直接对 `turns` 表跑原始 SQL）全部不改代码就通过——证明表名/关键
  列名兼容，SQLite WAL 并发语义没被破坏。全量 4/4 通过。
- 间接依赖 history_store 的集成测试：`test_master_checkpoint_resume.py` +
  `test_coordinator_safety.py` + `test_resume_idempotent.py` +
  `test_session_tree_mirror.py` + `test_legacy_agent_sessions_sync.py` +
  `test_omodul_core.py`（78 项）+ `test_coordinator_cognitive.py` +
  `test_g13_resume.py` + `test_g7_e2e.py` + `test_checkpoint_isolation.py`
  （34 项）全部通过，合计 112 项零回归——包括真实走 `MasterCoordinator`
  checkpoint/resume 全链路的测试，不只是 history_store 自身的单测。
- `mypy --follow-imports=skip veya/oservi/history_store.py`：只剩一处
  pre-existing（`_uid()` 的 `no-any-return`，这次没碰的代码）；`ruff check`
  干净。

### 4.4 仍然没做的（保持 §3 的范围判断）

`session_tree`（`veya/omodul/session_tree.py`）和 `memory_store` 要不要
同样从"覆盖式"改成"追加不可变修订"，没有跟着做——这两个是否要统一进同一套
canonical event 模型、要不要跟 history_store 共享同一个 revision 概念，
仍然是 §3 问题 2/3 那类需要先谈清楚范围的产品决策，"不用考虑之前的数据"
解除的是数据迁移这一个具体障碍，不是把这两个的范围也一起授权掉。
