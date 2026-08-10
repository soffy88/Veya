# graph-engineer 深度理解 & veya 完成度对照

> 版本: 2026-08 | 上游: https://github.com/Ranteck/graph-engineer (design-stage, 未 end-to-end dogfood)
> veya 对照基线: `system_graph_cycle` (server/graph_engineer.py, commit 0b0d4358) + 状态内核 (plan_todo/quota/gate_check)

---

## 一、graph-engineer 是什么（深度理解）

一个 **Claude Code skill**：让 Claude 当编排者/仲裁者，Codex 当实现者/批判者/修复者，
跑一个**自纠正状态机循环**。本质是 Anthropic 两个官方模式的嵌套：

- **Orchestrator-Workers**：Claude 编排（spec/仲裁/验证），Codex 干活（写/修）
- **Evaluator-Optimizer**：CRITIQUE→REFACTOR→VERIFY 循环，直到停止条件为真

**铁律：Claude 从不编辑实现文件**（只写 PROJECT_CONTEXT.md 契约），只有 Codex 通过
单一入口 `codex:codex-rescue` subagent（--write / --resume-last / read-only）动代码。

### 8 节点状态机

```
0 PRE-FLIGHT (Claude)  安全检查: clean tree + 非main分支, QG resolution 持久化, elevated 评估
1 SPEC      (Claude)   契约写 PROJECT_CONTEXT.md (按 feature 命名空间分节)
2 IMPL      (Codex)    实现 (--write)
3 QUALITY GATE (Claude) 机械门: lint/format/type/build; 禁 mutating 命令; 每激活≤3失败; 环境失败立即升级
4 CRITIQUE  (Codex)    对抗式只读审查 (sandbox 强制 read-only); --resume-last 状态连续性
5 DEBATE    (Claude)   三分类: valid→REFACTOR / debatable→反证 reinject / false-positive→一行理由
6 REFACTOR  (Codex)    按 triage 列表修复 (--resume-last --write) → 回 QUALITY GATE
7 VERIFY    (Claude)   功能测试+验收 (与 QG 机械门分离); 失败→回 CRITIQUE 分类根因(4类)
DONE
```

**返回边**（图 vs 管线）：
- QUALITY GATE 失败 → 回写者 (IMPL 或 REFACTOR)，同激活共享 3 次计数
- VERIFY 失败 → 回 CRITIQUE（不是快速修复），先分类根因
- CRITIQUE 用 `--resume-last` 记住自己先前的发现 + Claude 的 triage 决定（防重复抱怨）

### 关键机制（非明显但决定成败）

1. **`--resume-last` 状态连续性**：后续 CRITIQUE 带上下文（先前发现 + triage 决定 +
   VERIFY 失败），不是每次 fresh——否则批判会重复已裁决的发现 → 振荡
2. **Anti-loop cutoff**：连续两轮 Claude 判定"同一底层抱怨 + 无净代码变更" → **停止升级用户**，
   绝不假造解决。这是硬地板，覆盖 /goal 的软迭代上限
3. **QUALITY GATE 严格性**：只收机械检查（lint/type/build），功能测试绝不进机械门（避免
   测试失败混入机械重试通道）；拒绝 mutating/auto-fix 命令（`--write`/`--fix`/裸 `prettier`）；
   跑前跑后 git 快照对比（side-effect 检测）；环境失败（缺依赖/超时/OOM/只读）立即升级不计数
4. **read-only 是硬保证**：CRITIQUE 的只读不是 prompt 软要求——插件 sandbox 按
   `request.write ? workspace-write : read-only` 在 OS 层强制。写者自评偏差是已知局限
   （IMPL/CRITIQUE 同模型），靠 Claude 的 Read/Grep 验证缓解
5. **DEBATE 防振荡**：debatable 发现带反证 reinject 回 Codex（"你说 X，但 Y 因为 Z，
   坚持吗？"），其回复必须等待；false-positive 必须写一行理由（绝不静默接受/拒绝）
6. **VERIFY 根因分类**：失败必须分 4 类之一（implementation-defect / test-defect /
   contract-mismatch / environmental），绝不说"just make the test pass"；环境阻塞直接升级
7. **三种模式**：full 8-node（写）/ review-only（只读报告，无 SPEC/IMPL/QG/REFACTOR/VERIFY）/
   refactor-only（已有代码上写，无新 SPEC/IMPL）
8. **Elevated assurance（opt-in）**：高风险触发（auth/支付/删除/并发）→ 3 个 fresh 独立
   lens + fan-in 规范化 + canonicalization + exit challenger（改后必重跑），有模型调用预算
9. **PRE-FLIGHT 安全检查**：clean tree + 非 main 分支硬检查，失败 abort 不清脆继续；
   QG resolution 持久化（resolution 不是结果），每次写后廉价 revalidate 不重新探测
10. **/goal 停闸**：Claude Code 内置 stop-gate，条件为真才让 turn 结束；模板覆盖
    带测试/无测试/refactor-only/review-only

### 已知局限（作者诚实声明）

- IMPL 与 CRITIQUE 同一 Codex 模型 → 批判非独立验证（"second pass by the same model"）
- 设计 stage：8 节点循环从未在真实仓库 end-to-end 跑过（"reviewed on paper"）
- 3 lens 角度多样性 ≠ 独立验证；token 节省是估算未测
- 依赖 codex-plugin-cc v1.0.6 的具体命令面/flags——插件升级可能静默破坏

---

## 二、veya 完成度对照矩阵

| # | graph-engineer 能力 | veya 现状 | 完成度 |
|---|---|---|---|
| 1 | 8 节点循环骨架 (实现→批判→仲裁→修复→验证) | `system_graph_cycle` 简化循环 | 🟡 60% |
| 2 | 角色分离: 编排者不编辑实现 | 主脑不写实现, 引擎写 | 🟢 90% |
| 3 | **不同模型在批判路径** (独立仲裁) | implement=codex, critique=claude **不同模型** | 🟢 **超出原版** |
| 4 | 状态显式化 (PROJECT_CONTEXT.md) | plan_todo JSON + 计划看板 UI | 🟢 **超出原版** |
| 5 | 迭代上限 | max_iterations 默认 3 (可配 5) | 🟢 100% |
| 6 | **CRITIQUE 状态连续性 (--resume-last)** | ❌ 每次 fresh, 会重复已裁决发现 | 🔴 0% |
| 7 | **DEBATE 三分类 (valid/debatable/false-positive)** | 缺陷关键词启发式 (无反证 reinject/无理由) | 🔴 20% |
| 8 | **Anti-loop cutoff** (同抱怨+无变更→停止) | ❌ 无, 靠迭代上限硬停 (可能假解决) | 🔴 0% |
| 9 | **VERIFY 真功能测试 + 根因分类** | ❌ 无缺陷→直接 done, 无真实验证 | 🔴 10% |
| 10 | **机械 QUALITY GATE** (lint/type/build, 禁 mutating) | 仅"实现输出非空", 无真实机械检查 | 🔴 15% |
| 11 | **PRE-FLIGHT 安全检查** (clean tree/分支/QG resolution) | ❌ 无 | 🔴 0% |
| 12 | read-only 硬保证 (sandbox 强制) | 批判引擎 read-only 靠 prompt (软) | 🟡 30% |
| 13 | 三种模式 (full/review-only/refactor-only) | 仅 full | 🟡 40% |
| 14 | 命名空间隔离 (按 feature 分节) | plan 天然隔离 (每 plan 独立 JSON) | 🟢 100% |
| 15 | Elevated assurance (3 lens + exit challenger) | ❌ 无 | 🔴 0% |
| 16 | /goal 停闸 + 模板 | plan quota/boundary_scan (状态内核) 类似物 | 🟡 50% |
| 17 | 成本/风险透明 | 工具 description 注明外部引擎费用 | 🟢 90% |
| 18 | 质量门失败回写者 (capped retry) | 实现无输出→重试 (同激活) | 🟡 50% |
| 19 | VERIFY 失败回 CRITIQUE 而非快速修 | ❌ (无 VERIFY) | 🔴 0% |
| 20 | 证据链 (每步可审计) | todo evidence + 看板 + 日志 | 🟢 100% |

**总体**: 骨架 60% 落地, 但**防振荡机制 (6/7/8) 和真实验证 (9/10) 是核心缺口**——
graph-engineer 的成败关键不在"有循环", 而在"循环不振荡/不假解决"。

### veya 独有优势（原版没有）

1. **真独立批判**：codex 写 + claude 审 = 不同模型；原版 IMPL/CRITIQUE 同是 Codex
   （作者明示 self-preference bias 无结构解法）。veya 天生解决
2. **状态可视化**：plan_todo + 计划看板 UI（原版纯 markdown 文件无 UI）
3. **四引擎可选**（claude/codex/grok/pi），不锁死 2 模型
4. **状态内核**：quota_spend_slot / gate_check / boundary_scan（原版无对应物）
5. **多轮对话编排**：主脑可对话式引导循环（原版 /goal 单轮自动）

---

## 三、关键 gap 增强计划（按价值排序）

| 优先级 | 增强 | 对应原版机制 | 工作量 |
|---|---|---|---|
| P0 | CRITIQUE 状态连续性: 批判 prompt 携带先前发现+triage, 存 todo evidence | --resume-last | 小 |
| P0 | DEBATE 三分类: 主脑/仲裁引擎做 valid/debatable/false-positive, debatable 反证 reinject | DEBATE | 中 |
| P0 | Anti-loop cutoff: 同底层抱怨+无净变更→停止升级, 不假解决 | Anti-loop cutoff | 小 |
| P1 | VERIFY 真验证: 无缺陷后跑功能测试/验收, 失败分类根因(4类)回批判 | VERIFY | 中 |
| P1 | 机械质量门: 可配置检查命令 (lint/build), 禁 mutating, 前后快照 | QUALITY GATE | 中 |
| P1 | PRE-FLIGHT: 可选 clean tree/分支检查 | PRE-FLIGHT | 小 |
| P2 | review-only / refactor-only 模式 | 三种模式 | 中 |
| P2 | Elevated assurance (3 lens) | opt-in | 大 |

**实施原则**（与既有约束一致）：
- 纯新增逻辑，主脑零改动（冻结架构）
- 状态继续走 plan_todo（看板可视化）
- 增强可开关（参数），默认保守

---

*对照基线: graph-engineer @ main (2026-08), veya @ 0b0d4358*
