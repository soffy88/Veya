# Veya × harness2 Phase 1 Report（真实 proposer）

> 范围：真实 proposer。Phase 1 把 Phase 0 的 `VeyaDemoProposer`（无模型调用）换成走
> Veya 现有 provider 配置的真实 LLM proposer，在 2 个 Veya fixture 任务上跑
> k=1 parallel，验证盲选 + 轨迹信号在真实模型下仍然成立。不回流主链路，不写持久
> Library 的自动激活路径，不改冻结主链。
>
> 前置：分支 `feat/harness2-phase0`，起点 `94e0cbba Merge branch 'main' into
> feat/harness2-phase0`。本报告对应的一次真实模型全循环见"跑通证据"。

## 做了什么

### 1. 真实 proposer：`server/harness/proposer.py`

`VeyaProposer` 实现 harness2 `core.proposer.Proposer` 的
`.call(mission, context_dir, label, harvest=...)` 协议，把"起 opencode/claude CLI
子进程"换成**复用 Veya 自己的 provider 配置**：

- 单次会话 = 一个 ReAct 循环，调 `veya.obase.llm.llm_call`（主脑同一入口），
  带 `tools=` 工具 schema；
- 返回 `harness2.core.proposer.Reply`，`workspace` 指向 harvest 出来的
  `context/harness` 副本——与 harness2 原生 Proposer 同一契约，
  `recursion._run_stage` 因此零改动；
- `cost_usd` 依次取响应里的 `cost_usd` / `usage.cost_usd`，否则用
  `veya.obase._llm_config.calc_cost` 按 provider 计价；token 用量单独累计
  （计价表缺该 provider 时成本为 0，token 不为 0）。

**工具面隔离**。proposer 只看到四个文件工具——`read_file` / `list_dir` /
`write_file` / `apply_patch`——全部以 `context_dir` 为根做路径夹持：

- 写只允许落在 `context/harness/`（harness2 的可编辑树，"你的编辑就是提案"）；
- 路径穿越（`../`、绝对路径）显式报错，不静默夹到根；
- 没有 master_tools、不能触网、不能执行命令。

### 2. proposer 通道与可用性判定：`server/harness/channel.py`

harness2 的 proposer 通道只有 `opencode`（bun 直调 CLI）和 `claude_cli` 两种，
都要外部 checkout 与凭据。Phase 1 不改 harness2，在 Veya 侧解析通道：

1. `HARNESS2_IMPROVER_MODEL` / `HARNESS2_IMPROVER_PROVIDER` 显式覆盖
   （两者必须同时给，只给一半直接报错）；
2. `opencode-go` 的 `deepseek-v4.1-flash`——Veya 主脑池第一档；
3. Veya 默认 provider 及其默认模型。

候选是否真的可用由 `probe_channel()` 判定：一次最小 `llm_call`，返回 stub 兜底
文案、`error=True` 或空回复都视为不可用，换下一个候选。全程**不读取、不返回、
不打印任何 key**。全部不可用时返回 `(None, records)`，调用方必须显式报错，
不静默换一个没验证过的模型。

### 3. 默认关闭的 feature flag

`server/feature_flags.py` 表尾追加（不改任何既有行）：

```python
# harness2 Phase 1: real (model-backed) proposer sessions ... (default OFF)
FeatureFlag("VEYA_HARNESS2_REAL_PROPOSER", False, "runtime", "2027-01-01"),
```

flag 关闭时：

- `VeyaProposer(...)` 构造即抛 `ProposerDisabled`，**连通道探测都不做**，一个
  LLM 请求都不发；
- `scripts/run_harness2_phase1.py` 直接退出并提示开启方式；
- 工具面字节不变：`server/tool_registry.py`、`server/coordinator_master.py`、
  `hooks/` 零改动。

### 4. 运行入口：`scripts/run_harness2_phase1.py`

与 Phase 0 同形，改为默认 2 个任务、k=1、mode=`agg`；`--dry-run` 只解析通道并
导出 base harness，不发任何模型请求。报告 `phase1-k1.json` 增加
`proposer_provider` / `proposer_model` / `proposer_source` / `proposer_calls` /
`proposer_tool_calls` / `proposer_usd`。**不写 Library**
（`experience=NO_ENTRY`），所以 Phase 1 不引入自动回流路径。

### 5. 测试

`tests/server/test_harness_proposer.py`（31 个）覆盖：

- flag 表尾追加、default `False`、`proposer_enabled()` 的开关语义；
- flag 关闭时构造抛 `ProposerDisabled` 且**零探测**（注入计数 probe 验证）；
- 通道探测拒绝 stub / `error=True` / 空回复 / 抛异常的候选；
- DeepSeek Flash 优先、env 覆盖、只给一半覆盖报错、全失败返回 `None`；
- 证据工具：读写列目录、写路径夹持、路径穿越拒绝；
- `apply_patch` 按上下文就地替换、行号过期仍命中、歧义上下文报错、
  无上下文补丁报错；
- 真实会话（注入假 `llm_call`，不触网）：返回带 `workspace` 的 `Reply`、
  落盘编辑经 harvest 成为提案、`harvest="context/harness"` 前缀剥离、
  空回复抛 harness2 的 `ProposerError`、轮数用尽回落最终文本轮、
  纯重复读触发提示而"读新文件"和"读完顺手改"不触发；
- 审计落盘不含任何 key 形态字符串。

## 跑通证据

真实模型全循环（**fresh root**，不复用缓存）：

```bash
VEYA_HARNESS2_REAL_PROPOSER=1 python3 -u scripts/run_harness2_phase1.py \
  --root /data/soffy/h2-p1-fresh
```

输出：

```text
[veya-phase1] candidate: opencode-go/deepseek-v4.1-flash answered a minimal probe
[veya-phase1] proposer channel: opencode-go/deepseek-v4.1-flash (source: deepseek-flash)
  [veya-phase1] 1/2 ok           sum-1
  [veya-phase1] 2/2 ok           upper-1
Phase 1: 2 done, 0 failed; executions=8, proposer_calls=8, proposer_usd=0.0
report: /data/soffy/h2-p1-fresh/phase1-k1.json
```

`--dry-run`（只解析通道、导出 base，零模型请求）：

```text
[veya-phase1] proposer channel: opencode-go/deepseek-v4.1-flash (source: deepseek-flash)
[veya-phase1] dry run ok; report: .../phase1-dry-run.json
```

四个 proposer 会话的审计（`proposer-audit/<label>/meta.json`）：

| label | ok | 模型 | tool_calls | wall_s |
|---|---|---|---|---|
| `inv1.a1` | true | opencode-go/deepseek-v4.1-flash | 18 | 64.7 |
| `dom1.a1` | true | 同上 | 14 | 24.3 |
| `agg1.a1` | true | 同上 | 31 | 47.0 |
| `experience.agg1` | true | 同上 | 17 | 83.3 |

两处值得记录的实测观察：

- **proposer 会真读证据再答**。`agg1.a1` 读了 task、8 个 base harness 文件、
  base rollout 与交付物、两个候选的 rollout/交付物/diff、两个决策记录，然后给出
  KEEP_BASE 结论，理由是三个候选 harness 的 `harness.diff` 全空、三条 rollout 逐
  字相同、交付物都正确——没有可附着的编辑时机。这是盲选/轨迹信号可用的一次正面
  观察：模型没有为了"产出编辑"而编造差异。
- **experience 阶段给了 NO_ENTRY**，并明确指出 k=1 单候选池无法区分 harness 变体。
  与 Phase 0 一致，没有写 Library。

`proposer_usd=0.0`：`opencode-go` 不在 `calc_cost` 的计价表里，成本按 0 记录，
token 用量单独累计。这是已知缺口，不是本次运行免费。

测试与 lint：

```text
python3 -m pytest -q tests/server/test_harness_proposer.py
31 passed

python3 -m pytest -q tests/server/test_harness_adapter.py tests/server/test_harness_tools.py tests/server/test_harness_proposer.py
40 passed

ruff check server/harness/ scripts/run_harness2_phase1.py tests/server/test_harness_proposer.py
ruff format --check server/harness/ scripts/run_harness2_phase1.py tests/server/test_harness_proposer.py
```

两者均通过。全仓 `ruff check .` 当前仍有既有失败，失败点不在本轮新增文件。

## 遇到的问题

1. **"轮次用尽"会把一次花钱的会话判成失败。** 实跑中模型反复重读同一批证据，
   烧到轮数上限后空回复 → 整个提案回退 base。修复：轮数用尽时再给一次**不带工具**
   的机会逼出最终 `<notes>` 文本，而不是直接抛 `ProposerError`。
2. **重复调用检测误伤正常取证。** 最初按工具**名**判重，读第二个不同文件也算
   "重复"，于是模型还没看完证据就被反复催着回答。修复：按**同名同参**判重
   （`_call_signature`），只有整批调用都是已经做过的读操作才提示；"读新文件"和
   "读完顺手改一处"都不触发。
3. **`harvest` 的 `context/` 前缀。** harness2 传 `harvest="context/harness"`，
   而我们的根就是 `context_dir`，不剥离前缀会去找 `<context>/context/harness`，
   提案被判成"harness 没回来"。修复：`_harvest()` 去掉前导的 `context/`。
4. **`apply_patch` 不能信 hunk 行号。** 模型手里的行号常相对于它上一次读到的版本，
   中间已有别的编辑；照行号落点会**复制**而不是就地替换。修复：每个 hunk 用
   context/removed 行去原文找唯一匹配，找不到或有多个候选就显式报错，绝不猜。
5. **`server/harness/__init__.py` 的 `__all__` 里有一个从未实现的
   `resolve_proposer_model`。** 那是早期设计残留（Phase 0 提交时就在）。它让
   `from server.harness import resolve_proposer_model` 直接 ImportError。本轮**只
   删除这一个失效条目**，其余不动；同一文件里 `VeyaBenchmarkAdapter` 等既有条目
   同样未实现，属 Phase 0 遗留，不在本轮范围。
6. **`--root` 放 `/tmp` 会因磁盘配额失败。** `/tmp` 是 16G tmpfs 且已用 81%，
   base harness 导出时报 `OSError: [Errno 122] Disk quota exceeded`。属环境问题，
   换到 `/data` 即可。
7. **`run_domain` 会 resume 已有 run 目录。** 同一个 `--root` 重跑会把任务标成
   `skipped`（复用上次的 chosen harness），不代表本次 proposer 被调用。要新鲜的
   全循环证据必须换 root。
8. **CI 依赖未决定。** 新测试依赖外部 harness2 checkout（`HARNESS2_REPO`，
   默认 `~/projects/harness2`）；本轮没有擅自加入 required CI suite。

DONE
