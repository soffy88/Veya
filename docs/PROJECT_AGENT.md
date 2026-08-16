# Project Agent（`project_ask`）落地记录

> 状态: **M1（Store）+ M2（builtin/hicode 闭环）+ M3（dsh adapter）+ Understand 门禁（U1-U4）已实施，U5 真机 smoke 已跑通（16/16，2026-08-16）**
> 代码: `server/project_store.py`（记忆 Store + 状态映射表 + 契约草案 + understand.json 读写）、
> `server/project_understand.py`（Understand 门禁：UnderstandResult + 判定 + 硬约束校验）、
> `server/project_ask.py`（唯一对外入口 + Understand 门禁接线 + hicode/dsh worker + tool 注册）

## 0. 三套「项目」概念，不要混用

这个仓库里有三个名字很像、语义完全不同的东西。写代码/写 prompt 前先确认在说哪一个：

| 概念 | 作用域 | 用途 | 代码位置 |
|---|---|---|---|
| `~/.veya/{loop,audit}` | 本机运行时根目录（home，跨项目共享） | Loop Plane 的事件流/审计日志持久化 | `services/loop-plane/app/config.py`、`server/audit.py` |
| `veya.agent_project`（`AgentLayout`） | 某个「目录即 Agent」的静态定义（instructions.md / skills / channels / schedules） | vercel/eve 风格的声明式 agent 目录扫描，与任务执行无关 | `veya/agent_project.py` |
| `.veya-project/`（本文档主题） | **单个用户项目目录内**，跨会话记忆 + 任务快照 | `project_ask` 的读写目标：STATE/DECISIONS/LESSONS + 队列镜像 + 运行产物 | `server/project_store.py` |

`.veya-project/` 故意不叫 `.veya/`：后者已经是本机运行时的共享约定（home 目录），命名相同会导致「项目内 vs 本机全局」两种作用域混淆，尤其是当某次任务的 `project_root` 恰好解析到 `$HOME` 时。

## 1. `project_ask` —— 唯一对外入口

**硬约束**：Coordinator 只调用 `project_ask` 这一个 tool；self-do（builtin）vs. 派工（hicode / dsh）的决策在 tool 实现内部完成，不暴露成多个可选 tool 让 Coordinator 做分支路由。这是为了不重新引入「多 Agent 意图 DAG」——Veya 主链路冻结的约束之一。

`project_status`（只读，见 §1.3）是唯一允许的第二个入口，因为它不做任何派工决策，只是查询。

```
project_ask(project_root: str, request: str, assignee_hint: str | None = None) -> str
```

### 1.1 `assignee_hint` 白名单

`None | "builtin" | "hicode" | "dsh"`。任何其它值 **直接 blocked**，不落到启发式、不派给任何 worker（`server/project_ask.py::project_ask` 顶部的白名单校验）。

缺省（不传 hint）时按关键词启发式在 `builtin`/`hicode` 之间选（`_EXEC_HINTS`：修复/实现/重构/fix/bug/test/deploy 等）；**`dsh` 只能靠显式 hint 触发，不参与启发式**，这是刻意的——新 worker 上线期先不参与自动判断，降低误伤面。

### 1.2 三种处理方式的语义边界

| 方式 | 会做什么 | 不会做什么 |
|---|---|---|
| `builtin` | 把 `request` 原样记成一条 `DECISIONS.md` 条目 | **不执行任何命令、不改任何代码**。调用一次不等于任务被完成，只是记录了意图 |
| `hicode` | 派给既有 `server.hicode_queue.HicodeTaskQueue`（唯一权威调度器，不是第二套队列），把 `.veya-project/` 记忆拼进任务上下文再提交 | 受 `HICODE_WORKSPACE` 沙箱边界约束（见 §2） |
| `dsh` | 拉起 `dsh --profile headless "<prompt>"` 子进程（cwd=`project_root`），软性请求输出末尾带 `VERDICT: completed\|blocked`（不保证遵守）；exit 0 + 有输出 → `completed` 兜底判定 | 不可用/超时/exit≠0 一律 `blocked`，**不会自动 fallback 到 hicode**（避免同一请求被两个 worker 双跑） |

这条边界必须在调用方（Coordinator 的 system prompt / tool description）侧写清楚，否则容易被模型误解成「调用了 `project_ask` 就等于代码已经改完」。`server/project_ask.py::wire_master_tools` 里注册的 tool description 已经用粗体强调了这一点。

### 1.3 `project_status` —— 唯一允许并存的只读入口

```
project_status(project_root: str, limit: int = 5) -> str
```

只读查看某项目的 `.veya-project/` 状态：最近 N 条任务记录（来自 `queue-mirror.json`）+ 是否已被 `project_ask` 处理过。**不做任何 self-do/派工决策，不执行任何命令，也不会仅因为被查询就把 `.veya-project/` 建出来**——查一个从未被 `project_ask` 处理过的项目会直接返回「尚不存在」，而不是创建目录。这是它能与 `project_ask` 并存而不违反单入口纪律的原因：它没有分支可选，Coordinator 调它不构成路由决策。

## 2. `HICODE_WORKSPACE` 沙箱边界 + `force_cli`（2026-08-15 真机 smoke 验证后修正）

`hicode` 执行仍受 `server.hicode_agent._resolve_workspace` 约束：`project_root` 必须落在 `HICODE_WORKSPACE`（默认 `~/.veya/hicode-workspace`，可用同名环境变量覆盖）内，否则 `hicode_task_queue.submit()` 会在这一层抛 `ValueError`，`project_ask` 捕获后映射为 `blocked + block_reason`，不会裸露成未处理异常。真机验证过：传一个 `HICODE_WORKSPACE` 外的路径，确实 `blocked`，不崩。

**但仅仅通过这个校验，不代表改动真的会落在 `project_root` 里。** 真机 smoke 第一次跑就发现了这个坑：`server.hicode_agent._execute_hicode_core` 默认优先走 `hicode serve`（一个单一持久会话，`HicodeServeClient.submit()` 只发 `{"input": spec}`，**没有 cwd/workspace 参数**）；传入的 `workspace` 在 serve 路径下只用来打一次任务前 git 快照，从不传给真正执行任务的地方。结果是：serve 健康时，不管调用方传哪个 `project_root`，代码改动都会落在 `hicode serve` 那一个固定会话目录里，跟调用方指定的目录毫无关系——第一次真机跑「创建 hello.py」，返回 `✅ completed` 且摘要写得有板有眼，但全盘搜索确认**没有任何文件被创建**，摘要是幻觉（当时这台机器上 `hicode serve` 用的还是 `~/.veya/config.json` 里的占位 API key `sk-demo`，处于 shim 应答状态，进一步放大了这个问题，但沙箱边界这个 gap 本身和 shim 无关，换真 key 一样存在）。

修复：`project_ask` 的 hicode 路径现在**强制 `force_cli=True`**（`server/project_ask.py::_run_hicode` → `hicode_task_queue.submit(..., meta={"force_cli": True})` → `HicodeTaskQueue._run_one` 读 `rec.meta["force_cli"]` → `_execute_hicode_core(..., force_cli=True)`），直接跳过 serve、走 CLI 路径（`--add-dir <workspace>`，真正把执行限定在传入目录内）。`force_cli` 默认 `False`，只有 `project_ask` 显式 opt-in——通用的 `hicode_run` 工具（单一会话、无多项目隔离需求）行为完全不变，仍然优先用 serve。

修复后重新真机验证：同一个「创建 hello.py」任务，`hello2.py` 确实出现在指定的 `project_root` 里，内容与请求一致，`completed` 状态这次是真的。

## 3. 终态契约：`completed` | `blocked`，不允许静默丢失

`server.project_store.to_project_status(status, error)` 把 `HicodeTaskQueue.TaskRecord` 的内部枚举（`queued/running/done/failed/cancelled`）收敛为项目侧只暴露的两个终态：

- `done` → `completed`
- `failed` / `cancelled` → `blocked` + `block_reason`（优先用原始 `error`，否则用默认原因，保证 reason 永不为空）
- `queued` / `running` 是非终态，原样透传（当前实现里 `project_ask` 会 `await` 到底，不会把非终态返回给调用方）

dsh 路径同样只有两个出口：解析到 `VERDICT: completed` → `completed`；`VERDICT: blocked`、无法解析出合法 verdict、二进制不可用、超时、派工异常——全部 → `blocked` + 具体原因。**没有第三种结局**，也没有跨 worker 的隐式 fallback。

## 4. 记忆写回与 `.veya-project/` 布局

```
<project_root>/.veya-project/
  PROJECT_STATE.md      # 自由 Markdown, 固定章节模板 (Goal/Current status/Key artifacts/Open risks)
  DECISIONS.md           # 追加式; builtin 请求 + 未来可能的显式决策记录都写这里
  LESSONS.md              # 追加式; 当前版本没有自动写入路径 (留给未来 result-ingest 语义)
  queue-mirror.json       # 非权威快照; 每次 project_ask 调用都会追加一条 (最多保留 200 条)
  workers/                # 预留 (未来 worker 专属记忆, 当前未使用)
  output/                  # 预留 (未来产物归档, 当前未使用)
  runs/<task_id>/
    brief.md               # 派工时拼装的完整上下文 (STATE/DECISIONS/LESSONS + Task)
    worker.log              # hicode/dsh 的执行摘要或错误详情
```

`queue-mirror.json` 明确是**镜像，不是权威源**——真正的任务状态永远以 `HicodeTaskQueue`（内存态，进程重启会丢）为准；镜像只是给 `.veya-project/` 一份可离线查看的历史快照，不用于恢复队列状态。

## 5. dsh 调用形态（2026-08 对照 apps/cli/README 核实）

`dsh` 官方没有 `run --brief-file` 这种子命令；一次性任务走 headless profile，任务是**位置参数字符串**，不是文件路径：

```bash
cd "$project_root"                 # cwd = workspace root, 与 project_root 一致
export DEEPSEEK_API_KEY=...        # dsh 子进程继承 server 进程的环境变量
dsh --profile headless "在当前目录新建 hello.py，写 print('hi')"
```

`server/project_ask.py::_dsh_exec` 按这个形态调用：`[dsh_bin, "--profile", "headless", prompt]`，`prompt` 是 `.veya-project/记忆 + Task` 拼成的完整字符串（超过 `_DSH_PROMPT_CHAR_LIMIT`=6000 字符会截断，避免 argv 超限；未截断的完整版本仍落盘到 `runs/<task_id>/brief.md` 供审计）。headless 退出前 stdout 主要是最后一段 assistant 文本，**不保证**出现 `VERDICT:` 页脚，所以判定逻辑分两层：

1. 输出里如果确实有合法 `VERDICT: completed|blocked` 行 → 按它判定（`_parse_dsh_verdict`）。
2. 没有的话，`exit code == 0` 且 stdout 非空 → 兜底判定为 `completed`；`exit code != 0` → `blocked`，原因取 stderr（为空则退回 stdout）。

这个兜底逻辑不是「跨 worker fallback」——它仍然只在 dsh 这一条路径内部判定，不会去调用 hicode。

如果真机验证发现 headless 的实际输出格式（例如带 ANSI 颜色码、JSON 包裹等）和这里假设的不一样，需要调整 `_parse_dsh_verdict` 或兜底判定逻辑；先跑 `dsh --help` 和一次手动 `dsh --profile headless "<task>"` 对照 `runs/<task_id>/worker.log` 里的真实 stdout/stderr 再改。

### 5.1 真机验证记录（2026-08-15）

`npm install -g @deepseek-ai/dsh`，`dsh --help` 输出与假设的 `--profile headless "<prompt>"` 形态一致。手动跑一次（无 VERDICT 请求，纯裸任务）：`dsh --profile headless "在当前目录新建 dsh_hello.txt..."` → exit 0，文件真的创建，stdout 只有最终 assistant 文本、**没有** `VERDICT:` 行——证实了「headless 不保证页脚」的假设，兜底判定分支是必要的，不是过度设计。

再跑 `project_ask(..., assignee_hint="dsh")`（brief 里带软性 VERDICT 请求）：这次 dsh 输出末尾确实带了 `VERDICT: completed`，走的是 §5 判定逻辑里的第 1 层（显式 verdict），不是兜底分支；文件 `dsh_hello_ask.txt` 落在正确的 `project_root` 下，内容与请求一致，`queue-mirror.json` 记录 `assignee: dsh, status: completed`。两层判定逻辑（显式 VERDICT 优先，无 VERDICT 时按 exit code 兜底）都已被真实路径覆盖到，暂不需要因为这次验证再调整 `_parse_dsh_verdict`。

第一次跑时用了项目 `.env` 里的 `DEEPSEEK_API_KEY`，被 dsh 判定 invalid（可能是内部网关专用 key，不是 dsh 认的公开 API key）；换一把有效 key 后才通过——用 dsh 前确认 key 对 dsh 本身有效，不要想当然复用 veya 内部网关的 key。

## 6. 已知边界（非 bug，设计如此）

- 启发式关键词匹配会有误伤/漏判；不想赌启发式时用显式 `assignee_hint`。
- 队列非持久化：`HicodeTaskQueue` 是进程内内存态，服务重启会丢失进行中任务（继承自 `hicode_queue.py` 的既有行为，`project_ask` 未改变这一点）。
- dsh 的「exit 0 + 有输出 → completed」兜底判定比显式 VERDICT 弱——如果 dsh 提前退出但没真正完成任务，只要 exit code 是 0 就会被误判为 completed。真机验证时需要重点看这一点是否成立。
- `hicode serve` 若配置了占位/无效的 provider key（这台机器上一度是 `~/.veya/config.json` 的 `sk-demo`），会返回看起来正常但其实是 shim 的完成响应，不代表真的执行了什么——`project_ask` 已经用 `force_cli` 绕开了 serve（见 §2），不受这个影响；但如果以后有代码改成不强制 CLI、又走回 serve，这个 gotcha 会原样复现，务必先查 provider key 是否真实。

## 7. Understand 门禁（`server/project_understand.py`，2026-08-16）

在 §1 的派工决策（builtin/hicode/dsh 三选一）**之前**，`project_ask` 现在先跑一次轻量判定：能确信实现方案与验收标准就直接执行（走既有三条腿）；有歧义就只追问、不产生任何业务副作用——不建代码变更、不派工、不改 `DECISIONS.md` 之外的东西。这条门禁不是第二个 tool，仍在 `project_ask` 内部完成，符合 §1 的单入口硬约束。

### 7.1 三种 `mode`（新增可选参数，默认 `auto`）

| mode | 行为 |
|---|---|
| `auto`（默认，可用 `PROJECT_ASK_DEFAULT_MODE` 覆盖） | 跑一次 `understand()` 判定；`decision=ask` → 早退追问；`decision=act` → 走执行腿 |
| `act_eager` | 跳过判定，直接构造 `decision=act` 放行执行（调用方需明确知道自己在跳过澄清，风险自负） |
| `ask_only` | 强制只追问、永不执行；即便判定为 `act` 也会转成一条确认问 |

### 7.2 判定结果 `UnderstandResult` 与硬约束

单次 LLM 调用（`server/project_understand.py::understand`，可注入 `_llm` 桩，测试不依赖真模型）产出 `decision(act|ask) / confidence / interpretation / assumptions / questions / risk_flags / reasons`。任何不自洽都会被 `validate_or_force_ask()` **安全降级为 ask**，不会放行一个不确定的 act：

- `decision=act` 但 `questions` 非空 → 降级为 ask
- `decision=act` 但 `interpretation` 为空 → 降级为 ask
- `decision=act` 但 `confidence` 低于阈值（`PROJECT_UNDERSTAND_CONFIDENCE_MIN`，默认 0.75）→ 降级为 ask
- LLM 输出解析失败 / LLM 调用本身抛异常 → 降级为 ask（原因写进 `reasons`）
- `decision=ask` 时 `questions` 会被裁剪到 1-3 条（`PROJECT_UNDERSTAND_MAX_QUESTIONS`）；空则补一条默认追问

### 7.3 `need_clarification` 不是执行失败

`decision=ask` 时，`project_ask` 返回 `status=blocked, block_reason=need_clarification, phase=understood_ask`，并把追问文案渲染在结果里。这条 `blocked` 语义是「等人回答」，不是任务失败——前端/调用方应据此展示追问 UI，而不是当报错处理。判定结果落盘到 `runs/<task_id>/understand.json`（供审计与续答链读取），但**不会**触碰 `hicode_task_queue`、不会调用 dsh、不会写 `DECISIONS.md`。

### 7.4 续答协议：`parent_task_id`

若上一轮返回了追问，调用方把用户对追问的回答作为新的 `request`、`parent_task_id` 设为上一轮返回的 `task_id` 再调一次即可。`project_ask` 会读取该 `task_id` 对应的 `understand.json`（`server/project_ask.py::_load_chain`，目前只看直接 parent 这一层，不做多跳），把上一轮的 `request/interpretation/questions` 拼进本轮判定的 prompt。

### 7.5 Act 阶段的透明化

`decision=act` 时，`interpretation`（复述将要做什么）和 `assumptions`（采用的默认假设）会前置写进 hicode/dsh 的 `brief.md` 顶部（`server/project_ask.py::_understand_prefix`），再拼 `.veya-project/` 记忆和任务本身——worker 执行时能看到「门禁认为这次要做什么」，而不只是原始 request。`ProjectAskResponse` 也把 `interpretation/assumptions/confidence` 透出给调用方，方便前端做「执行假设」的展示。

### 7.6 与完整 Grill/Spec 全流程的关系

这条门禁是**日常轻量判定**，不是 Matt Grill 式的完整需求建模/设计树。它不强制 `to-spec`/`to-tickets`，不做全域 `CONTEXT.md` 建模，只回答一个问题：「这次请求够不够明确到可以直接动手」。需要重型设计评审时，仍应走独立的规划/评审流程，不指望 `project_ask` 的 Understand 门禁替代它。

### 7.7 已知边界

- `_load_chain` 只读直接 parent 一层，不递归多跳；连续追问多轮时，第 N 轮只看得到第 N-1 轮的 interpretation/questions，看不到更早的轮次。
- `act_eager` 完全跳过判定，等价于回到 M1-M3 时代的行为——用于调用方已经很确信、或者上层已经做过澄清的场景，不要在不确定时用它当「加速通道」。

### 7.8 U5 真机 smoke（2026-08-16 跑通，16/16）

真机验证（真实 opencode-go LLM + 真实 reasonix/dsh 二进制，`~/.veya/hicode-workspace/u5-smoke-*`）：

| 用例 | 输入 | 结果 | 验证点 |
|---|---|---|---|
| A 模糊指令 | 「优化一下这个项目」（auto，无 hint） | `need_clarification` + 3 条追问 | 零副作用：无 hicode 派工、项目根无文件、runs 目录只有 understand.json、DECISIONS.md 无业务条目 |
| B 明确指令 | 「在当前目录写代码创建 u5_smoke.py，文件内容为 print('U5 OK')」（auto + hint=hicode） | `completed` | `u5_smoke.py` 真实创建、内容一致（不是幻觉摘要）；brief.md 前置 Understand interpretation |
| C 续答链 | B 同构明确指令 + `parent_task_id`=A 的 task_id | `completed` | `u5_chain.txt` 落盘；brief.md 含 Prior round（上一轮追问进入 worker 上下文） |
| D dsh 回归 | 「在当前目录创建 u5_dsh.txt…」（hint=dsh） | `blocked: dsh: AUTH: … api key invalid` | 门禁正确放行到执行腿（blocked 是 dsh 侧 key 问题，非门禁误伤） |

**真机抓到的 bug（已修复）**：`server/project_understand.py::_default_llm` 原本用**无参** `llm_call`，默认 provider 是 dashscope，本机无其 key → 返回 shim 文本（`LLM provider not configured…`）→ `_parse` 判解析失败 → **永远安全降级 ask**（任何指令都不会被执行）。修复：显式 `provider="veya1.1"` 走 opencode-go 直连主链路（候选重试 + gpt-5.6-luna 兜底不变）。此 bug 只有真机能抓到——单测注入 `_llm` 桩，永远发现不了默认路径接错。

**边界确认**：续答轮曾出现 `confidence 0.72 < 0.75` 被硬约束降级 ask（LLM 已正确复述任务但置信差 0.03）——这是 §7.2 设计行为（宁可多问），不是 bug，未放宽阈值。

**已知遗留**：dsh 路径仍受 §5.1 的 key 问题影响——`.env` 的 `DEEPSEEK_API_KEY` 对 dsh 无效（`dsh: AUTH: Authentication Fails`），门禁放行正常，要跑通 dsh 需换一把 dsh 认的公开 key（`~/.reasonix/.env` 的 `OPENCODE_API_KEY` 与 opencode 网关 key 是同一把，可尝试复用它）。
