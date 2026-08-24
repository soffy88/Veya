# RFC-11: PR-07 State Authority — 决策：这次不动代码

> 状态：范围评估，不执行任何代码改动（2026-08-24）
> 依据：docs/VEYA_10_OF_10_PLAN.md §9（State / Session / Memory 做到 10/10）

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
