# Veya Loop 交接文档（HANDOVER）

> 生成时间: 2026-08-06 · 交接给下一窗口的完整上下文
> 主线: **3O 范式（主库固机制 / veya_loop 装配 / veya 业务接线）→ 因果闭环 → 可靠性 → 多引擎部署**

---

## 1. 一句话定位

veya_loop 是独立包（`veya-loop 0.5.0`），把 3O 主库（oprim/oskill/omodul/obase/oservi，`platform/3O/` 子模块）的机制原子装配成「因果闭环控制基板」，veya 主仓通过它获得诊断→决策→执行→学习→审计 全链路。

## 2. 架构分层（写代码前必读）

```
veya 主仓 (业务)          server/ routes/ cli/ services/ infra/ deploy/
veya_loop (装配包)        veya_loop/src/veya_loop/{__init__,hardened,execution_adapters,oprim/,omodul/}
3O 主库 (机制, 单一来源)  platform/3O/{oprim,oskill,omodul,obase,oservi}/.../...
```

**铁律（3O 单一来源 §1.4）**：业务/机制逻辑进主库；veya_loop 只转发（shim 模式：`veya_loop/oprim/_xxx.py` 里 `from .._assembly import oprim; X = _oprim._xxx.X`）；veya 主仓只装配。禁止在主仓 server/ 里重实现主库已有机制。

## 3. 已交付能力（全部有测试）

| 阶段 | 能力 | 关键文件 | 测试 |
|---|---|---|---|
| P1 神经符号 | O1 四闸门(Z3 校验/回译/MUS/MaxSMT)、O2 分配(匈牙利/VCG/死锁)、O3 沙箱推演(PUCT/快照/稠密奖励) | `oprim/_plan_ir.py` `_ir_compile.py` `_mus.py` `_ir_solve.py` `_backtranslate.py` `_allocate.py` `_payments.py` `_deadlock.py` `_games.py` `_snapshot.py` `_reward.py` `_mcts.py` `_lookahead.py` `_actions.py` `_sandbox.py`; `omodul/neuro_symbolic.py` `operator_center.py` `observer.py` | 30+ |
| 性能 | 推理 LRU 缓存、DAG 路径 DP、残差缩放近似 | `oprim/_inference_cache.py`; `_do_calculus_intervention` `_counterfactual_rollout` `causal_fault_diagnose` 改造 | test_perf 9 |
| P3 反脆弱 | 期望效用选择、在线 CPD 更新、闭环事务、威胁模型 | `oprim/_expected_utility_select.py`; `oskill/_online_cpd_update.py`; `omodul/closed_loop_intervene.py` `threat_model_evolve.py` | test_phase3 16 |
| 审计 | AuditEmitter 统一写出口 (JSONL) | `oprim/_audit_emit.py`; 挂 closed_loop/multi_step_plan | test_audit 8 |
| P4 长视距 | multi_step_plan、因果诊断、CPD 版本号 | `omodul/multi_step_plan.py` `causal_fault_diagnose.py` `counterfactual_diagnose.py` | — |
| L3 反事实 | 显式噪声 SCM、条件 Cholesky 流、hybrid SCM、BO 规划 | `oprim/_structural_counterfactual.py` `_cholesky_flow.py` `_coupling_flow.py` `_hybrid_scm.py` `_bayes_opt_plan.py` `_deep_scm_train.py`; `omodul/cholesky_scm.py` | test_advanced_flows 9 + L3 8 |
| 稳定性 | diag_floor/offdiag_scale/κ正则/Ledoit-Wolf | `_cholesky_flow.py` 内 | — |
| 代码可靠性 | 方案A+C: 沙箱容器 + 修复闭环 | `omodul/code_reliability_loop.py`; `infra/code_sandbox/`; `services/code_sandbox_client.py` `code_agent_reliability.py` | test_code_reliability 7 |
| 多引擎 | Master/Claude/Codex/Pi 引擎执行 | `server/engine_runner.py`; `routes/legacy_agent.py` | — |

**当前主脑状态**：零限制（无 tool_choice 强控、无 HITL 语义，max_rounds=32 仅护栏）+ 连续对话历史（session_id 持久化，进程内 LRU）。

## 4. 部署状态（生产在跑）

- **后端容器** `veya-backend`：`deploy/docker-compose.yml`（context=.., 端口 **8767**(gateway)/**9120**(legacy) → 容器 8765），`docker compose -f deploy/docker-compose.yml up -d --force-recreate`
- **前端** systemd `veya-web`（3105, build/index.js），`sudo systemctl restart veya-web` 加载新 build
- **引擎挂载**：容器 USER soffy(uid 1000)，挂载 `~/.nvm` `~/.local` `~/.claude` `~/.claude.json` `~/.codex` `~/.pi`，veya-data 卷已 chown 1000
- **引擎实测**：pi ✅（`1+1=2`）；claude/codex 链路通但 API 403/refused（宿主账号侧）
- **域名** `veya.aiinote.com` → Caddy → 容器；`/api/v1/agent/*` 旧协议兼容路由在 `server/routes/legacy_agent.py`
- **端口占用警示**：8765=无关服务(勿动)、8010=aegis(勿动)、systemd veya-gateway 已 dead（建议 root 执行 `sudo systemctl disable veya-gateway && sudo rm -f /etc/systemd/system/veya-gateway.service`）

## 5. 测试基线

```
veya_loop/tests   ~78 passed  (cd veya_loop && ../venv/bin/python -m pytest tests/ -q)
veya 主仓 tests   596+ passed (venv/bin/python -m pytest tests/ -q --ignore=tests/guardians)
```

## 6. 已知待办/风险

1. **claude/codex 引擎 403/refused** — 宿主账号侧，与代码无关；排查宿主 `claude -p` / `codex exec`
2. **前端新 build 需 sudo restart veya-web** 才生效（当前域名前端可能是旧 build 走旧协议——已兼容）
3. **会话历史为进程内内存** — 容器重启丢历史；要持久化接 obase store
4. **Caddy 配置不可读**（/etc/caddy/Caddyfile 缺失，进程内存配置）— 域名转发规则未审计
5. **未 push** — 主仓 26+ commits 待 `git push`；3O 子模块各自 commit 未 push
6. **多租户/量化接线/非线性收缩** — 明确非目标

## 7. 常用命令

```bash
# 容器
docker compose -f deploy/docker-compose.yml build backend
docker compose -f deploy/docker-compose.yml up -d --force-recreate
docker logs veya-backend --tail 50
docker exec veya-backend sh -c "claude --version; codex --version; pi --version"

# 测试
cd veya_loop && ../venv/bin/python -m pytest tests/ -q
venv/bin/python -m pytest tests/ -q --ignore=tests/guardians

# 前端构建
cd apps/web && pnpm build && sudo systemctl restart veya-web
```

## 8. 交接给下一窗口的首件事

用户当前主线 = **veya_loop 能力落地与稳定运行**。优先：
1. 确认用户反馈的 500/404 已闭环（近期最后修的是 stream 500）
2. 若用户提新需求 → 先看本 HANDOVER 第 3 节是否已有
3. 涉及主库机制 → 改 `platform/3O/*`；涉及装配 → `veya_loop/`；涉及部署 → `deploy/`
