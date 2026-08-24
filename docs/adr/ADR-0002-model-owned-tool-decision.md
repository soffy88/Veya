# ADR-0002: 工具面决策权归模型 (Model-Owned Tool Decision)

> 状态：accepted（既成事实，本 ADR 是补记，不改变现有行为）
> 依据：`docs/VEYA_10_OF_10_PLAN.md` §2 I-02（"必须把这个区别写成正式 ADR，
> 防止未来重新演化成 `_layer_tools`"——本文件就是那份 ADR）

## 决策

程序只能对**客观、可判定**的维度做决策：权限、schema、timeout、quota、
resource、deterministic policy、protocol、safety boundary。

程序不得替模型做**开放语义判断**："这是不是 research"、"用户是不是想编码"、
"该用哪个 agent"、"回答是否应该交给 Hicode" 一类问题必须留给模型在明确工具面上
自主决策，程序不能猜。

## 例外：`tool_search` 元路由

`tool_search`（`server/skill_hub.py::VeyaSkillHub`）允许模型主动发起工具发现，
这看起来像是"程序参与了工具面裁剪"，但性质不同：

```text
模型决定发现什么   ≠   程序猜模型需要什么
（tool_search 触发权在模型）  （_layer_tools 触发权在程序，按关键词猜）
```

`VEYA_MASTER_LITE_TOOLS=1` 开启时（默认关闭，生产行为零变化），主链每轮只把
`tool_search` + 极少常驻工具（`ask_user`/`project_ask`）+ 本会话已解锁工具塞进
`tools` 参数；模型通过 system prompt 里的一行式工具菜单知道有哪些能力存在，
自己调用 `tool_search` 按意图检索并解锁，下一轮即可原生 function-calling 调用
——解锁的发起方始终是模型，程序从不替它裁藏。见
`server/tool_registry.py:72-73` 的行内注释。

## 反面教材：`_layer_tools`

早期踩过的坑：程序按关键词猜该露出哪些工具（"工具面分层 `_layer_tools`"），
猜错了模型就看不到需要的工具，或者看到一堆不相关的。见
`docs/ARCHITECTURE_STABLE.md` §2.1（禁止模式）和 §2.5（AgentLoop 双轨期间
"同一个坑踩了两次"的记录）。任何新的工具面精简方案，凡是触发权在程序而非模型，
一律视为 `_layer_tools` 复发，不允许合入。

## 判定标准（给未来 PR review 用）

新增工具面相关改动时，问一个问题：**这次裁剪/过滤/排序，是模型主动请求触发的，
还是程序在没有模型明确指令的情况下自己决定的？** 前者合规，后者违反 I-02，
无论包装成什么名字（分层/路由/推荐/预测）。
