# Veya Loop 架构说明

> 文档版本: 0.5.0 · 产品: Veya Loop Engine / 维亚闭环引擎

## 1. 命名与边界

| 名称 | 角色 | 说明 |
|---|---|---|
| 3O 主库元素 | 主数据（独立，不混用） | oprim / oskill / omodul / obase —— 机制原子与事务本体所在 |
| **Veya Loop** | 因果闭环控制基板（本引擎） | 装配 3O 元素 + 自有业务组件（硬化执行/授权/派发），v0.5.0 |
| Veya 业务项目 | 业务接线 | 通过依赖接入本包，按业务故障图与执行适配器接线 |

依赖方向严格单向：**Veya 业务 → veya_loop → 3O 主库**。

## 2. 分层

```
┌──────────────────────────────────────────────────────────────┐
│  Veya 业务项目 (故障图 / 执行适配器 / 业务策略)                  │
├──────────────────────────────────────────────────────────────┤
│  veya_loop (本包)                                             │
│    ├─ hardened.py     HardenedExecutor / PermissionContract / │
│    │                  dispatch_intervention (自有组件)         │
│    ├─ _assembly.py    3O 主库装配器 (pip 优先, submodule 兜底)  │
│    └─ __init__.py     公共 API 面 (惰性导出)                   │
├──────────────────────────────────────────────────────────────┤
│  3O 主库元素 (主数据)                                          │
│    ├─ oprim   纯原子: do-calculus / 反事实 rollout / 效用选择 /  │
│    │                 AUDIT / 沙箱 / MCTS                       │
│    ├─ oskill  技能: CPD 在线更新 / ToM 信念 / 策略演化           │
│    ├─ omodul  事务: 因果诊断 / 闭环干预 / 威胁演化 / 多步规划     │
│    └─ obase   存储: CausalGraphStore (带结构版本号)             │
└──────────────────────────────────────────────────────────────┘
```

## 3. 闭环数据流（诊断 → 决策 → 执行 → 学习）

```
故障信号 ──► causal_fault_diagnose ──► CausalDiagnosisReport (ΔP 定量)
                │                              │
                │         select_intervention (U = ΔP − λC − ρ·risk)
                ▼                              ▼
         multi_step_plan / closed_loop_intervene
                │                              │
                ├─► PermissionContract (规则 → nonce)
                ├─► HardenedExecutor (unshare 沙箱 + 环境冻结 + 超时)
                ├─► 真实反馈 (实现态观测) ──► CPD 在线更新 (Dirichlet/EMA)
                └─► AuditEmitter 五节点落笔 (JSONL, trace_id 贯穿)
                                │
                                └─► 下一轮 ΔP 估计自动变准 (策略演化闭环)
```

## 4. 确定性主张

同输入 + 同版本 + 同种子 → 同输出，且可离线重放：

- `CausalGraphStore.version` — 结构变更自增（审计记「用的哪版因果图」）
- `CategoricalCPD.version` — 每次更新自增（审计记「用的哪版 CPD」）
- `plan_id` / `decision_id` / `replay_key` — 内容寻址，三个月后可重跑复现
- 效用选择 / 分配 / 排名全部字典序打桩，输入顺序不影响结果

## 5. 审计规范（统一 Schema）

`AuditEmitter` 在 diagnose → plan → decide → execute → learn 每节点写一条：

| 字段 | 含义 |
|---|---|
| `audit_id` / `trace_id` | 单条记录 ID / 一次故障链路 ID |
| `inputs.graph_version` / `cpd_version` | 当时用的哪版模型 |
| `inputs.threat_level` | 当时威胁水平 |
| `decision.chosen_strategy` / `utilities` | 为什么选这个动作（含效用全排序） |
| `execution.primitive` / `status` / `capability_nonce` | 执行了什么 / 谁授权的 |
| `learning.cpd_version_after` | 学到了什么（版本推进） |

## 6. 安全边界

- 硬化执行: `unshare -Urn`（seccomp 禁用时功能探测并回落，不假装隔离）；
  环境变量全清空 + `PYTHONHASHSEED=0` / `TZ=UTC` 冻结不确定性；超时强制杀。
- 授权: deny-by-default 规则集 + 单次消费 nonce（防重放）。
- 审计: 只记录已发生的决策，不做决策；`capability_nonce` 贯穿执行与审计。
