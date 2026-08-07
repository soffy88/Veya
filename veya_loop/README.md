# Veya Loop Engine / 维亚闭环引擎

> **因果闭环控制基板** — 把「诊断」变成「可计算的闭环控制」：
> 最优干预选择 → 硬化执行 → 真实反馈在线更新因果模型 → 持续演化策略。
> 每个关键节点自动落审计，可回放、可追责、可取证。

- **产品名**: Veya Loop Engine / 维亚闭环引擎
- **Python 包**: `veya_loop`
- **发行名**: `veya-loop`
- **版本**: 0.5.0
- **命名边界**: 3O 主库元素（oprim / oskill / omodul / obase）是独立主数据包，本包只装配不混用；Veya 业务项目通过依赖本包接入。

---

## 能力面（P1 神经符号 / Phase 2 / 3 / 4）

| Phase | 能力 | 导出 |
|---|---|---|
| P1 | 神经符号四闸门（IR 校验 / Z3 编译 / MaxSMT 可满足 / 回译） | `PlanIR` `parse_ir` `validate` `compile_ir` `check_feasible` `optimize` `diff_all` |
| P1 | 不可满足核 + 解释（MUS） | `shrink_to_mus` `explain` |
| P1 | 组合优化分配 + VCG 支付 + 策略证明 | `assign_one_to_one` `assign_with_capacity` `vcg` `check_strategyproof` `welfare` |
| P1 | 死锁检测（租约 / 资源序 / 等待图预检） | `LeaseManager` `ResourceOrder` `WaitForGraph` |
| P1 | 博弈论（纯纳什 / 帕累托 / 主导策略） | `Game` `pure_nash` `pareto_optimal` `dominant_strategies` |
| P1 | 拍卖账本（任务 / 工人 / 出价） | `Ledger` `Problem` `Task` `Worker` `Bid` |
| P1 | 沙箱推演（快照 / 稠密奖励 / PUCT / lookahead） | `SnapshotStore` `run_probes` `MCTS` `puct` `lookahead` |
| P1 | 动作执行面（计划应用 / 可逆性 / 补偿链） | `ActionPlan` `Applier` `Reversibility` `compensation_chain` `gate` |
| P1 | 隔离沙箱（命名空间 / 预热池） | `LocalSandbox` `SandboxPool` |
| 2 | 因果测谎仪（do-calculus 诊断 + 反事实干预） | `CausalGraphStore` `causal_fault_diagnose` `build_binary_failure_cpd_map` |
| 2 | 贝叶斯意图雷达 + 蜜罐反间谍 | `BayesianBeliefUpdater` `adversarial_honeypot_observe` |
| 3 | 反脆弱闭环（期望效用选择 + 在线 CPD 更新） | `closed_loop_intervene` `select_intervention` `expected_utility` `CategoricalCPD` `update_cpd` |
| 3 | 威胁模型演化 + 决策审计统一写出口 | `threat_model_evolve` `AuditEmitter` `JsonlSink` `CompositeSink` |
| 4 | 长视距反事实规划 + 策略自演化 | `multi_step_plan` `counterfactual_rollout` `StrategyEvolver` |
| L3 | 显式噪声 SCM / Cholesky 流 / Hybrid SCM / BO 规划 | `StructuralSCM` `CholeskyMechanism` `HybridSCM` `bayesian_optimize` `fit_deep_scm` |
| 可靠 | 代码可靠性闭环（沙箱 + 修复迭代） | `run_code_reliability_loop` `CodeTask` `CodeLoopResult` |
| 长程 | 事件溯源状态内核（goal/todo/gate/evidence + 链式校验 + 并发安全 + 迁移） | `AppendOnlyEventStore` `VerifyResult` |
| 长程 | 投影状态机 + 配额治理（跨天恢复 / 人工闸门 / 预算暂停恢复） | `GoalKernel` `Todo` `Gate` `Goal` `QuotaView` `QuotaTracker` |
| 自有 | 硬化执行 / 授权契约 / 干预派发 | `HardenedExecutor` `PermissionContract` `dispatch_intervention` |
| 自有 | 执行适配器模板（probe + 派发三合一） | `ExecutionAdapter` `RestartAdapter` `dispatch_via_adapter` |

## 命令行（veya-loop）

```bash
veya-loop --version           # 版本
veya-loop selftest            # 冒烟: 装配面 + 关键机制链路 (31 项)

# 多步规划 / 因果诊断: 用 "graph" 字段传入因果拓扑 (nodes 可带 p_fail)
veya-loop diagnose --json '{
  "failure_log": "db timeout after api gateway 5xx",
  "graph": {"nodes": [{"name": "db", "p_fail": 0.3}, {"name": "api", "p_fail": 0.1},
                       "task_outcome"],
            "edges": [["db", "api"], ["api", "task_outcome"]]}}'
veya-loop plan --json '{"failure_log": "...", "graph": {"nodes": [...], "edges": [...]}}'
```

## 安装

```bash
# 完整能力面（含求解器与数据面）
pip install -e /path/to/veya-loop"[all]"
```

### 接线 3O 主库

3O 主库（oprim/oskill/omodul/obase）是独立主数据包，两种接线方式：

```bash
# 方式 A（推荐，生产）: 直接 pip 安装各主库包
pip install obase oprim oskill omodul

# 方式 B（开发）: git submodule 挂载到仓库 platform/3O/ 下
git clone --recursive https://github.com/helios-plat/veya.git   # 本包随库挂载
```

装配器（`veya_loop._assembly`）按序解析：先试直接 import，失败则注入
`platform/3O/*` submodule 路径 —— 两种接线方式对业务代码透明。

## 快速开始

```python
from veya_loop import (
    CausalGraphStore,
    multi_step_plan,
    closed_loop_intervene,
    HardenedExecutor,
    PermissionContract,
    dispatch_intervention,
)

# ── Phase 4: 一条调用完成「感知-规划-行动-学习」 ──────────────────────
store = CausalGraphStore()
store.add_node("api_gateway", p_fail=0.3)
store.add_node("db", p_fail=0.2)
store.add_node("task_outcome")
store.add_edge("api_gateway", "task_outcome")
store.add_edge("db", "task_outcome")

report = multi_step_plan(
    "task failed: db timeout after api gateway 5xx",
    store=store,
    threat_level=0.12,
    execute=True,
    repair_callback=lambda node: 0.6,
    audit_path="/var/log/veya-loop/audit.jsonl",
)
print(report.strategy, report.recommended_actions)

# ── 硬化派发: 授权 → 执行 → 审计 ─────────────────────────────────────
contract = PermissionContract()
contract.grant("do:*")                      # 授权所有干预动作
with HardenedExecutor() as executor:
    result = dispatch_intervention(
        "do(db=ok)", ["python3", "-c", "print('repair ok')"],
        contract=contract, executor=executor,
    )
print(result.status, result.nonce)          # approved_executed cap_xxxx
```

## 文档

- [架构说明](docs/ARCHITECTURE.md) — 3O 分层、闭环数据流、审计规范
- 测试: `pytest tests/`（Phase 2/3/4 完备性门禁）

## 审计（可回放 / 可追责 / 可取证）

`AuditEmitter` 在 diagnose → plan → decide → execute → learn 每个关键节点
自动写一条统一 Schema 记录（JSONL）：

```json
{"audit_id": "...", "trace_id": "...", "event_type": "decide",
 "inputs": {"graph_version": 3, "cpd_version": 5, "threat_level": 0.12},
 "decision": {"chosen_strategy": "aggressive_repair", "utilities": {"do(x)": 0.21}},
 "execution": {"primitive": "circuit_break", "status": "ok", "capability_nonce": "..."}}
```

事后可回答：为什么选这个动作 / 用的哪版因果图·CPD / 谁授权的 / 蜜罐是否正确隔离。
