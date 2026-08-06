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

## 能力面（Phase 2 / 3 / 4）

| Phase | 能力 | 导出 |
|---|---|---|
| 2 | 因果测谎仪（do-calculus 诊断 + 反事实干预） | `CausalGraphStore` `causal_fault_diagnose` `build_binary_failure_cpd_map` |
| 2 | 贝叶斯意图雷达 + 蜜罐反间谍 | `BayesianBeliefUpdater` `adversarial_honeypot_observe` |
| 3 | 反脆弱闭环（期望效用选择 + 在线 CPD 更新） | `closed_loop_intervene` `select_intervention` `CategoricalCPD` `update_cpd` |
| 3 | 威胁模型演化 + 决策审计统一写出口 | `threat_model_evolve` `AuditEmitter` `JsonlSink` |
| 4 | 长视距反事实规划 + 策略自演化 | `multi_step_plan` `counterfactual_rollout` `StrategyEvolver` |
| 自有 | 硬化执行 / 授权契约 / 干预派发 | `HardenedExecutor` `PermissionContract` `dispatch_intervention` |

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
