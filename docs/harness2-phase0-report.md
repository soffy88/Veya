# Veya × harness2 Phase 0 Report

> 范围：流程验证。Phase 0 只证明 Veya 可以作为 harness2 的 executor adapter 跑通
> k=1 Propose-Probe-Compose 全循环；不验证真实 proposer 质量，不回流主链路，不写持久
> Library。

## 做了什么

### 1. `VeyaBenchmarkAdapter`

新增 `server/harness/adapter.py`，实现 harness2 `core.spec.BenchmarkAdapter` 的完整协议：

- `tasks()` / `domain_of()` / `task_prompt()` / `task_files()`
- `execute()` / `delivered()` / `trajectory()` / `deliverable_files()` /
  `deliverable_text()`
- `base_harness()` / `validate_tree()` / `score()`

Phase 0 的 `execute()` 使用 Veya 已有的隔离执行原语
`server.agent_loop_bridge.run_strict_chat`，注入：

- 确定性 `_ScriptedLlm`，按 fixture 发出固定 tool calls；
- 只读 fixture 工具面（`compute_sum` / `read_text` / `to_upper` /
  `write_deliverable`）；
- 独立 `session_id` 和 `~/.veya/harness/loop/*.db` 会话库。

因此每个 rollout 走真实的 Veya AgentLoop 工具循环，但不发网络请求、不调用模型、
不读写主链会话历史。`score()` 返回 `None`，明确表示 Phase 0 没有 judge，而不是
伪造 0 分。

### 2. 只读 base harness 导出器

新增 `server/harness/export_base_harness.py`：

- 读取当前 `MASTER_SYSTEM_PROMPT`（经 `_slim_master_prompt`）、`_HOST_SOP_APPEND`、
  工具 schema、`~/.veya/skills/*/manifest.json`、`~/.veya/global_memory.json` 和
  hooks 源码摘要；
- 写出符合 harness2 OpenCode surface 的 `base/`；
- 同级写出完整只读 `snapshot/`，保留未截断的 prompt、工具与技能清单；
- 默认每次构造 adapter 时重建快照，避免复用旧缓存；显式传入
  `base_harness_dir` 时尊重调用方目录。

完整 Veya 工具面远超 harness2 OpenCode surface 的 24,000 字符 always-on 上限，因此
`base/` 是 surface 合规的精简摘要，`snapshot/` 是完整证据。两者都是只读产物，没有
回写到 `master_agent`、`tool_registry` 或 hooks。

### 3. 确定性 proposer 与运行入口

- `VeyaDemoProposer`：编辑阶段原样保留 base harness；`experience` 阶段返回
  `NO_ENTRY`，不写持久 Library。
- `scripts/run_harness2_phase0.py`：调用 harness2 原生
  `schedule.run_domain(..., k=1, modes=("agg",))`，默认输出到
  `~/.veya/harness/`。

### 4. 测试

新增 `tests/server/test_harness_adapter.py`，覆盖：

- Veya 数据到 OpenCode surface 的映射；
- base/snapshot 导出及 harness2 tree 校验；
- 大输入下 base 不超过 24,000 字符；
- 两个 fixture 任务的真实 AgentLoop 执行与交付物；
- `BenchmarkAdapter` Protocol 完整性；
- 默认 base 快照刷新；
- k=1 `agg` 全循环的 summary、ledger 与 selection。

## 跑通证据

最终运行命令：

```bash
python3 scripts/run_harness2_phase0.py
```

输出：

```text
[veya-phase0] 1/2 ok           sum-1
[veya-phase0] 2/2 ok           upper-1
Phase 0: 2 done, 0 failed; executions=8, proposer_calls=8
report: /home/soffy/.veya/harness/phase0-k1.json
```

`~/.veya/harness/phase0-k1.json` 记录 `n_tasks=2`、`done=2`、`failed=0`、
`execution_count=8`、`proposer_calls=8`。

两个任务的 `summary.json` 均为：

- `status=ok`，`errors=[]`；
- `ledger.R=4`、`expected_R=4`、`ledger.P=3`、`expected_P=3`；
- `inv1` / `dom1` / `agg1` 均 `fell_back=false`、`kept_base=true`、
  `attempts=1`；
- `selection.ok=true`、`chosen_label=agg1`、`trivial=true`；
- 交付物分别为 `sum(1..10)=55\n` 和 `HELLO FROM VEYA\n`。

产物位置：

```text
~/.veya/harness/phase0-k1.json
~/.veya/harness/runs/veya-oc-agg1/sum-1/summary.json
~/.veya/harness/runs/veya-oc-agg1/upper-1/summary.json
~/.veya/harness/base/
~/.veya/harness/snapshot/
```

本次导出的 always-on 字符数：`base/` 为 20,040，`snapshot/` 为 78,532。

测试命令与结果：

```text
python3 -m pytest -q tests/server/test_harness_adapter.py
8 passed

python3 -m pytest -q tests/server/test_harness_tools.py tests/server/test_coding_tools.py tests/test_memory_bank.py
22 passed

ruff check server/harness scripts/run_harness2_phase0.py tests/server/test_harness_adapter.py
ruff format --check server/harness scripts/run_harness2_phase0.py tests/server/test_harness_adapter.py
```

两者均通过。全仓 `ruff check .` / `ruff format --check .` 当前仍有既有失败
（本次运行分别报告 331 个 lint 问题和 72 个待格式化文件），失败点不在本轮新增
文件；本报告不把全仓基线失败算作 Phase 0 验证通过。

## 遇到的问题

1. **OpenCode surface 上限与 Veya 全量 harness 冲突。** Veya 的完整 prompt、工具说明
   和技能清单远高于 24,000 字符；最初直接导出会让每个 stage 因超限回退。最终采用
   `base/`（surface 合规摘要）+ `snapshot/`（完整只读证据）两层。
2. **harness2 是平行 checkout。** Phase 0 通过 `HARNESS2_REPO`（默认
   `~/projects/harness2`）延迟 import，没有 vendoring，也没有加入 Veya 依赖。
3. **读取 Veya 源会触发模块级装配。** `read_veya_sources()` import
   `server.coordinator_master` 时会构造模块级 coordinator 单例并接线工具；Phase 0
   只在显式导出/运行入口调用，不进入服务常规 import 路径，但 Phase 1 应改为 AST
   读取或子进程采集。
4. **Phase 0 不是真实模型任务。** fixture 使用 scripted LLM 和确定性工具，验证的是
   adapter 契约、证据树、ledger、选择与落盘流程，不代表真实 Veya 任务的成功率。
5. **没有写 Library。** `experience=NO_ENTRY`，因此 Phase 0 只验证运行与选择，没有
   验证 harness 经验提炼和回流。
6. **CI 依赖尚未决定。** 新测试依赖外部 harness2 checkout；当前没有擅自加入
   required CI suite。

## Phase 1 前需决定的事

1. **完整工具面如何映射。** 是保留摘要 + snapshot 指针，还是把工具面分组、做成
   按需 evidence，或扩展 harness2 surface 上限。这个决定会直接影响 proposer 能看到
   多少 Veya 真实能力。
2. **真实 executor 的隔离边界。** Phase 1 是否使用 opt-in 的真实模型/CLI 执行器；
   若使用，必须定义独立预算、超时、worktree/容器隔离和失败回退，不能把实验流量
   混入主链会话。
3. **proposer 通道与模型。** 是否使用 harness2 原生 `Proposer`、哪个独立模型通道、
   如何记录成本和缓存；不能用 Veya 主脑默认配置隐式承担实验成本。
4. **经验回流闸门。** `reflect_adoption` 产出的 experience 是只进候选库，还是进入
   PersonalRuntime 人工审核；任何回写 base harness 的路径都需要单独批准。
5. **验证范围。** 是否在 Phase 1 同时跑 `agg` 与 `seq`、增加真实任务数量，以及如何
   定义“流程通过”以外的质量信号。
6. **外部依赖策略。** 继续依赖平行 harness2 checkout，还是固定 commit、vendor
   `core/` 或在 CI 中显式安装；同时决定新测试是否进入 required CI suite。

DONE
