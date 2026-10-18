# 两段式按需工具加载（Lazy Tool Loading）

> 状态：已实现，默认关闭。`VEYA_LAZY_TOOLS=1` 显式开启。
> 分支：`feat/lazy-tools`（基于 `main`，未 push）。
> 测量环境：本 worktree，2026-10-18，`uv` 虚拟环境（Python 3.12.13），
> 本机 `~/.veya/skills` 已加载。

## 1. 结论

开启后，MasterAgent 每轮发送的 `tools` 参数从 **174 个 schema / 87,747 字符 /
约 26,127 token** 降到 **27 个 schema / 21,122 字符 / 约 5,972 token**
（初始轮，含 11 个不可卸载的 host 工具），即 **字符减少 75.9%，估算 token 减少
77.1%**。模型再搜索并加载 4 个工具后，仍有约 **72.3%** 的 token 节省。

关闭时（默认）`get_all_tool_schemas()`、system prompt、工具注册表、执行路径与
改动前逐字节一致；新增代码只在 `VEYA_LAZY_TOOLS=1` 时生效。

## 2. 设计

### 2.1 两段式

```text
第一段（常驻）: search_tools + 15 个高频/主路径工具
第二段（按需）: 模型调用 search_tools(query)
                → 返回命中的完整 OpenAI function schema
                → 命中项追加进本会话 loaded 集合
                → 下一轮起进入 tools 参数，并在本会话内保持
```

此外还有一条兜底：模型**直接调用**一个已注册但尚未加载的工具时，宿主先把它加入
本会话 loaded 集合，再走原有执行路径；不报"工具未加载"错误。只有真实注册过的
名字会被记录，模型幻觉出的工具名不会污染工具面。

### 2.2 常驻集合（16 个，含 `search_tools`）

依据本机 `~/.veya/trajectories`（3,167 个轨迹文件）的真实调用频次，以及
`docs/harness2-integration.md` §1.2 描述的主路径挑选：

| 工具 | 选择依据 |
|---|---|
| `search_tools` | 第二段发现入口，强制常驻 |
| `ask_user` | 澄清/交互主路径 |
| `coding_task_run` | 规范编码派工入口（宿主 SOP 明确要求） |
| `run_in_sandbox` | 轨迹频次第 1（710 次） |
| `read_file_ast` | 轨迹频次第 2（635 次） |
| `list_files` | 轨迹频次第 3（413 次） |
| `grep` | 轨迹频次第 5（167 次） |
| `read_hashline` | 轨迹频次第 6（131 次） |
| `write_file` | 轨迹频次第 4（218 次），写路径基础能力 |
| `fetch_url` | 轨迹频次第 12（84 次），URL 主路径 |
| `browser_run` | 轨迹频次第 11（103 次），浏览器主路径 |
| `assemble_code_context` | 宿主 SOP 指定的既有代码上下文入口 |
| `agent_loop_run` | 隔离执行/子任务入口 |
| `run_skill` | skill dispatcher 模式下的执行入口 |
| `memory_search` | 记忆检索主路径 |
| `skill_search` | skill 发现主路径 |

`system_*` 工具（reload / swarm / workspace search / secure exec / omni dispatch 等）
**不在常驻表里**，因为 oservi `get_system_schemas()` 把它们标为不可卸载：两种模式下
它们都固定出现在工具面最前面。把它们写进常驻表会重复计数、夸大收益。

常驻表可用 `VEYA_LAZY_RESIDENT_TOOLS=a,b,c` 覆盖；`search_tools` 会被自动补回，
无法被配置删掉。

### 2.3 会话与缓存语义

- loaded 集合按 `session_id` 隔离，存在进程内；同一会话跨轮次保持。
- 工具面顺序固定为：**host 不可卸载工具 → 常驻表顺序 → 首次加载顺序**。
  新工具只追加到尾部，已发送的前缀逐字节不变，避免每轮重排导致 prompt cache 失效。
- 会话跨用户复用时（`_bind_history_owner` 检测到 owner 变化）同时清空历史与
  loaded 集合，避免把上一个账号加载的能力面带给下一个账号。
- 进程重启后 loaded 集合丢失，回到"常驻 + host"初始面；这是有意的：能力面不进入
  持久历史，避免历史快照与当前注册表漂移。

### 2.4 发现语义

`search_tools(query, top_k=8)` 只做**模型显式触发**的检索：对工具名 + description
做子串与 CJK 二元组分词匹配，返回命中的完整 schema（不是摘要），并记录到会话。
它不读取用户任务、不预判意图、不按关键词自动决定该露出谁。

### 2.5 system prompt

oservi 原始 prompt 末尾会逐条渲染所有工具的 `name — description`，与 `tools`
参数重复。lazy 模式下保留其前面的 SOP、潜意识、宿主指令**原样不动**，只把最后
那段工具清单替换为 name-only 目录 + 使用说明。

实测：system prompt 从 **38,054 字符降到 17,422 字符**（减少 20,632 字符，约
54.2%）。默认模式下仍使用原始 prompt（本次测量默认 37,752 字符，差异来自
`~/.veya` 潜意识内容随会话变化）。

## 3. 与冻结架构的关系

冻结架构（`docs/ARCHITECTURE_STABLE.md` §1/§2.1/§2.6）要求：唯一入口是
MasterAgent ReAct、默认全量工具面、零程序裁藏、零语义路由；ADR-0002 进一步明确
"模型主动触发工具发现"合规，而"程序猜模型需要什么"（`_layer_tools`）不合规。

本次改动逐条对齐：

| 约束 | 如何满足 |
|---|---|
| 单一主链路 | 没有新增 agent/loop/路由；仍是 `MasterCoordinator.chat_stream` → `MasterAgent.chat_stream` |
| 默认全量、行为不变 | 开关关闭时 `get_all_tool_schemas()` 直接返回原始全量快照，prompt/执行路径不变；有回归测试 |
| 零程序语义判断 | 没有任何任务分类、关键词路由、URL 预抓；初始常驻表是静态配置，不是按请求推断 |
| 工具面裁剪的触发权 | 第二段由模型显式调用 `search_tools` 触发；直接调用兜底也只响应模型已做出的选择 |
| 不新增公开主面 | 仅新增一个 opt-in 工具 `search_tools` 与三个环境开关；无新端点、无前端改动 |
| 前缀稳定/缓存 | 顺序为 host → 常驻 → 加载顺序，只追加不重排 |

需要说明的边界：这仍然是"工具面小于全量"，因此它**不是默认行为**。它属于冻结
架构中 ADR-0002 已认可的 `tool_search` 例外模式，但必须由用户显式开启并接受
"模型可能不搜索"的风险；默认路径保持全量。

## 4. 前后对比数字

测量命令（可复现）：

```bash
# 默认（全量）
.venv/bin/python scripts/measure_lazy_tools.py

# 开启 lazy + 指标
VEYA_LAZY_TOOLS=1 VEYA_TOOL_SCHEMA_METRICS=1 \
  .venv/bin/python scripts/measure_lazy_tools.py
```

| 指标 | 默认（关闭） | Lazy 初始轮 | Lazy 搜索 4 个工具后 |
|---|---:|---:|---:|
| 发送的 schema 数 | 174 | 27 | 31 |
| schema 字符数 | 87,747 | 21,122 | 26,001 |
| 估算 token | 26,127 | 5,972 | 7,243 |
| 相对默认字符节省 | — | 66,625（75.9%） | 61,746（70.4%） |
| 相对默认 token 节省 | — | 20,155（77.1%） | 18,884（72.3%） |
| system prompt 字符 | 37,752 | 17,422 | 17,422 |

说明：

- lazy 模式下全量基线是 **175** 个 schema（比关闭时多 1 个 `search_tools` 本身）。
  以同一 lazy 进程内的全量基线计：27/175 与 31/175，字符节省 76.1%/70.6%，
  token 节省 77.3%/72.4%。上表"相对默认"用的是关闭模式的 174/87,747/26,127，
  更贴近线上切换前后的真实差异。
- token 为仓库既有 CJK-aware 确定性估算（CJK 字符按 1 token、其它约 4 字符 1 token），
  不是特定 provider 的 tokenizer；用于相对比较，不用于账单预测。
- 运行时指标由 `VEYA_TOOL_SCHEMA_METRICS=1` 打开，每个 LLM 轮次记录一条
  `[lazy-tools] schema metrics` 日志，并写入 `MasterCoordinator._lazy_schema_metrics`
  （包含 full/sent/saved 的字符与估算 token）。lazy 模式默认开启该指标。

## 5. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 模型不调用 `search_tools` | 需要的能力不在常驻表 → 模型直答或失败 | 直接调用未加载工具会自动加载执行；常驻表覆盖高频路径；system prompt 明确写出全部能力名与发现方式 |
| 检索未命中 | 模型拿不到正确 schema | 命中为空时返回排序后的前 `top_k` 作为兜底；返回完整 schema；模型可直接调用任意已注册工具触发兜底加载 |
| 首次加载破坏缓存 | 工具列表变化导致该轮 prefix 失效 | 只追加尾部、不重排；已加载工具在会话内保持；接受"首次加载一次缓存失效"的成本 |
| 兜底模型上下文更小 | 本地 `gpt-5.6-luna` 兜底使用裁剪工具面 | lazy 模式让裁剪更小，对兜底是减负；无新增依赖 |
| 会话状态增长 | loaded 集合与 host 工具名在进程内累积 | 每会话只存名字；跨用户复用会清空；进程重启自然重置；未引入持久化 |
| 配置覆盖写错 | 常驻表为空/名字不存在 | `search_tools` 强制补回；不存在的名字会被跳过，不会报错；启动时不做隐式裁剪 |
| 与其它工具面开关叠加 | 可能同时出现两套裁剪 | 只读取 `VEYA_LAZY_TOOLS`；不改其它 flag 的语义 |

## 6. 上线建议

1. **默认保持关闭**。先以 `VEYA_LAZY_TOOLS=1` 在单个非关键会话/灰度实例验证
   模型是否稳定先 `search_tools` 再执行，观察 `tool.schema_metrics` 日志与
   工具失败率。
2. 建议灰度顺序：内部会话 → 单用户 → 小流量。重点看三类指标：
   工具调用成功率、`search_tools` 调用率、每轮 saved token。
3. 若模型明显不搜索或漏调能力，先扩充 `VEYA_LAZY_RESIDENT_TOOLS`，再考虑回退。
4. **回滚**：删除 `VEYA_LAZY_TOOLS=1`（或设为 `0`）并重启进程即可；关闭路径
   不依赖任何新增持久状态，行为与改动前一致。
5. 不建议在未验证的情况下把它设为默认值；冻结架构的默认全量面仍是当前稳定基线。

## 7. 验证记录

```text
.venv/bin/python -m pytest -q tests/test_lazy_tools.py
15 passed

.venv/bin/python -m pytest -q tests/test_lazy_tools.py \
  tests/test_tool_event_boundary.py \
  tests/architecture/test_single_intelligence_authority.py \
  tests/test_coordinator_safety.py tests/test_react_loop_guard.py \
  tests/test_master_tool_concurrency.py tests/test_master_checkpoint_resume.py \
  tests/test_skill_hub.py
64 passed

ruff check server/lazy_tools.py server/coordinator_master.py   # pass
ruff format --check（改动文件）                                 # pass
mypy server/coordinator_master.py server/tool_registry.py      # 与 main 基线相同的 3 条既存错误，无新增
```

已知既存问题（未在本次改动中修复，避免扩大范围）：

- `tests/test_master_tools.py` 有 5 条失败，在干净 `main` 上同样失败（已用 stash
  对照复跑确认）。
- `ruff check server/tool_registry.py` 的 `RUF059`/`RUF100` 在 `main` 上同样存在。

## 8. 默认开启（Default-On Transition）

### 8.1 改动内容
- **翻转默认行为**：`server/lazy_tools.py::enabled()` 现在默认返回 `True`。两段式按需加载（常驻核心工具 + `search_tools`）成为 MasterAgent 的默认行为。
- **一键回退开关**：保留并明确 `VEYA_LAZY_TOOLS=0`（或 `false`, `off`, `no`）作为一键回退到完整工具面的杠杆。当设置 `=0` 时，系统自动回退至全量工具面与原始 prompt，字节级对齐冻结架构基线。
- **常驻集补全**：在 `DEFAULT_RESIDENT_TOOLS` 中追加了计划三件套 `create_plan` / `plan_status` / `update_todo`，确保多步任务规划和进度管理在常驻面中直接可用，减少不必要的 discovery round-trip。

### 8.2 测试结果
- **单元与架构测试**：
  - `.venv/bin/python -m pytest -q tests/test_lazy_tools.py tests/architecture/test_single_intelligence_authority.py`：24 passed。
  - 核心契约、直接调用兜底、指标收集、回退校验全部通过。
- **静态检查与格式化**：
  - `ruff check server/lazy_tools.py server/coordinator_master.py tests/test_lazy_tools.py`：pass。
  - `ruff format --check`：pass。

### 8.3 回退方法
如需在任意环境或单次运行中完全恢复旧版的全量工具面：
```bash
export VEYA_LAZY_TOOLS=0
```
或在调用前置于环境变量中。重启或重载后即恢复至改动前的 174/175 工具全量形态。

### 8.4 合并到 main 前的检查清单
1. 默认状态验证：未设 `VEYA_LAZY_TOOLS` 时 `lazy_tools.enabled()` 为 `True`。
2. 回退状态验证：`VEYA_LAZY_TOOLS=0` 时 `get_all_tool_schemas()` 与原始全量面逐字节一致。
3. 护栏与契约测试全绿（`tests/test_lazy_tools.py` + `tests/architecture/test_single_intelligence_authority.py`）。
4. 确保未修改 `~/projects/veya` 主工作区，且本分支无任何 `git push`。

DONE2
