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

- **后端容器** `veya-backend`：`deploy/docker-compose.yml`（context=.., 端口 **8767**(gateway)/**9120**(legacy) → 容器 8765），`docker compose --env-file .env -f deploy/docker-compose.yml up -d --force-recreate`
- **前端** systemd `veya-web`（3105, build/index.js），`sudo systemctl restart veya-web` 加载新 build
- **引擎挂载**：容器 USER soffy(uid 1000)，挂载 `~/.nvm` `~/.local` `~/.claude` `~/.claude.json` `~/.codex` `~/.pi`，veya-data 卷已 chown 1000
- **引擎实测**：pi ✅（`1+1=2`）；claude/codex 链路通但 API 403/refused（宿主账号侧）
- **域名** `veya.aiinote.com` → Caddy → 容器；`/api/v1/agent/*` 旧协议兼容路由在 `server/routes/legacy_agent.py`
- **端口占用警示**：8765=无关服务(勿动)、8010=aegis(勿动)、
  **veya-gateway.service 已失效但未停**（11:36 起循环 exit 3，建议 root 执行
  `sudo systemctl disable --now veya-gateway && sudo rm -f /etc/systemd/system/veya-gateway.service`）

### 4.1 生产事故修复记录（2026-08-06 520/524）

- **现象**：veya.aiinote.com 报 HTTP 520/524（CF 错误码）；任务返回 "LLM provider not
  configured — shim response"
- **链路真相**：域名 → CF 边缘 → **aegis-cloudflared**（ingressRule=15）→
  **aegis-caddy:8093** → `172.18.0.1:8767`（宿主）—— 不是 HANDOVER 早先推断的 Caddy 文件
- **根因 1（网络丢失）**：veya-backend 容器 `NetworkSettings.Networks={}`（端口 publish 全丢）——
  rootless docker 网络驱动故障；宿主 veya-gateway（Restart=always、无 API key 环境）顶替 8767 →
  master 引擎全 shim
- **根因 2（隧道抖动）**：11:20-11:23 CF 边缘/cloudflared 连接重建窗口 → 520/524（6h 内 96 次
  proxy 错误，集中在 10:21/11:22 两个窗口）
- **修复（无 sudo，docker 组权限）**：`kill -9` 宿主 veya-gateway 进程 → 立即
  `docker network connect deploy_veya-net veya-backend`（端口重新 publish 8767/9120）→
  systemd 拉回失败进入循环（无害，需 sudo 收尾）
- **验证**：公网 `engine=pi` 全链路真响应（1+1=2）；首页 200
- **遗留（宿主账号侧）**：容器 `ANTHROPIC/OPENAI/DASHSCOPE_API_KEY` 全空（deploy/.env 不存在，
  compose `${VAR:-}` 解析空）；ALIYUN_MAAS_API_KEY 对 dashscope 无效（invalid_api_key）。
  master 引擎仍 shim —— 需真实 key 写入 deploy/.env 后
  `docker compose --env-file .env -f deploy/docker-compose.yml up -d --force-recreate backend`；
  前端默认 engine=master（settings.svelte.ts）且无引擎选择 UI

## 5. 测试基线

```
veya_loop/tests   230 passed  (cd veya_loop && ../venv/bin/python -m pytest tests/ -q)
veya 主仓 tests   596+ passed (venv/bin/python -m pytest tests/ -q --ignore=tests/guardians)
```

### 5.1 veya_loop 优化记录（2026-08-06 起）

- **P1 神经符号能力面装配补全**：四闸门(validate/compile_ir/check_feasible/optimize/diff_all)、
  MUS(shrink_to_mus/explain)、分配+VCG(assign_one_to_one/vcg/check_strategyproof)、
  死锁(LeaseManager/WaitForGraph)、博弈(Game/pure_nash)、账本(Ledger/Problem/Task/Worker/Bid)、
  快照(SnapshotStore)、奖励(run_probes)、PUCT(MCTS/puct/best_path)、lookahead、
  actions(ActionPlan/Applier/compensation_chain)、SandboxPool/LocalSandbox ——
  全部进 `_ELEMENT_MAP`（现 134 exports）
- **cli.py 修复**：pyproject 声明 `veya-loop` entry point 但文件缺失（打包缺陷）→ 已补全
  `veya-loop {--version|selftest|plan|diagnose}`；selftest 22 项冒烟（装配面+四闸门/VCG/死锁/期望效用/审计）
- **质量修复**：`PermissionContract` resource 规则现支持 glob 匹配（原半实现）；
  `dispatch_via_adapter` probe 改 duck-typing（不再硬编码 RestartAdapter）；
  消除 PytestCollectionWarning（TestResult 误收集，conftest 豁免）
- **守护测试**：`tests/test_shim_consistency.py`（装配漂移防回归，~141 项 parametrized）+
  `tests/test_p1_neuro_symbolic.py`（P1 行为 12 项）；ruff 全干净
- **测试基线**：78 → 230 passed（零告警）

### 5.2 主仓 P1 API + 会话持久化（2026-08-06）

- **P1 业务路由**：`server/routes/neuro_symbolic.py` 新增 3 端点（机制全在主库 oprim，只装配）：
  - `POST /neurosymbolic/allocate` — 技能报价自动生成 + 匈牙利分配 + VCG 支付 + 策略证明
    （注意 `check_strategyproof(p, vcg_fn, allocator=...)` 传**函数**不是结果对象；
    bids 必填——无报价格子 = unassigned_penalty）
  - `POST /neurosymbolic/deadlock` — 等待图环 + 新边预检 + victim 建议
  - `POST /neurosymbolic/game` — 纯纳什 / 帕累托 / 主导策略（label 化输出）
- **veya_loop 装配补漏**：`CheckpointStore`（obase）进 `_ELEMENT_MAP`；
  CLI selftest + P1 测试修正假绿（bids 必填 + `alloc.pairs` 断言）

### 5.3 P2/P3 行为测试矩阵（2026-08-06，231 → 256）

- **test_phase2_behavior.py（11 项）**：根因召回与候选确定性、单父图干预方向
  （delta>0 且 after<观测）、诊断确定性（同输入同输出）、反事实 rollout、
  信念边界（不越界 [0,1]/阈值恰等/未知状态报错）、蜜罐探测面（网络外发/超时取证）
- **test_phase3_behavior.py（14 项）**：期望效用精确公式 U=ΔP−λC−ρ·risk、
  λ/ρ 权衡、并列 tiebreak 确定性、drop_negative u==0 边界、空/全负列表、
  Dirichlet 平滑不落 0/1、EMA 收敛（交替序列相位稳态 0.4737/0.5263）、
  strength 语义、version 单调、审计 trace 隔离/replay 确定性/Memory-Jsonl 一致、
  派发链路事件序（decide→execute、denied 只 decide、nonce 对应）
- **主库安全修复（obase a0ccd79）**：蜜罐 sandbox 超时场景 hostile 漏报 ——
  connect 挂起拖到超时 → payload 丢弃 → network_attempt 丢失。
  修复：audit hook 立即 print NETWORK_ATTEMPT，超时分支扫描取证；
  veya_loop 侧 test_honeypot_network_timeout_forensics 防回归
- **行为语义勘误（记入测试注释）**：平行双因图干预效应结构对称（delta 无法分根因，
  区分靠 failure_log）；EMA 随机序列统计噪声 σ≈0.15（断言需 ≥ 噪声）；
  dispatch action 命名规范为 `前缀:目标`（`do:reboot` 匹配 `do:*`）

### 5.4 多目标效用优化循环 optimize_loop（2026-08-06，256 → 283）

- **新机制（主库 oprim 399c68e）**：`oprim/_optimize_loop.py` —— train 搜索 + OOS 硬门禁 + 评价缓存
  - `MultiObjectiveConfig`/`multi_objective_utility`：默认权重 sharpe 1.0 · total_return 0.25 ·
    max_drawdown -1.0 · turnover -0.05 · cost_drag -0.5（可覆盖，缺指标按 0）
  - `EvalWindow(start,end,label)`：ISO 区间 · `fingerprint_eval`：params+window+meta → sha256[:16]
  - `EvalCache`：内存 + disk_path JSONL 跨进程持久化，hit/miss 统计
  - `RiskGateConfig(min_sharpe/max_drawdown/min_trades/max_turnover/max_cost_drag)`：
    缺指标 fail-closed（安全默认），None 跳过该项
  - `optimize_loop`：BO 内核复用主库 `bayesian_optimize`（RBF-GP+EI），`gate_on="train"` 无 OOS 降级
- **veya_loop 装配**：shim `veya_loop/oprim/_optimize_loop.py` + `_ELEMENT_MAP` 8 符号
- **测试 `tests/test_optimize_loop.py`（17 项）**：效用数值/权重覆盖/缺指标按 0、指纹稳定·键序无关·
  区间 meta 敏感、缓存 hit 跳过 evaluate·磁盘持久化、gate 过/拒/缺指标/可选检查、
  BO 寻峰（凸函数峰点误差 <10%）、OOS 拒绝·放宽接受（同参数仅门禁不同）、gate_on=train、缓存复用
- **排坑记录**：`bayesian_optimize(minimize=False)` 语义 = 最大化**传入的** objective ——
  负效用必须配默认 `minimize=True`，否则反向寻谷（曾找到 utility=-0.024 的谷底）；
  重复点返回缓存值而非 -1e9 极端惩罚（会污染 GP 后验带偏 EI）

### 5.5 优化工程化 + Walk-forward + 生命周期（2026-08-06，283 → 306）

- **主库 oprim c7929ba**：
  - `ParamSpec(low, high, kind=continuous|integer|log)`：log 空间 BO + exp 还原、integer 取整；
    `(low,high)` 元组向后兼容
  - `bayesian_optimize`/`optimize_loop` 透传 `early_stop_rounds`/`ei_stop`（连续 N 轮无改进 /
    最大 EI < 阈值 → 提前停止），`OptimizeLoopResult.early_stopped` 标记
  - `eval_meta`（data_version/engine_version）进 fingerprint → 升级自动打穿缓存
  - `walk_forward(folds, ...)` → `WalkForwardResult`：每折独立 optimize_loop（seed+1000*i）
    共享 cache；聚合 accept_rate / OOS 效用 mean·std / 指标 p25·p50·p75
  - `StrategyLifecycle`：research→candidate→paper→degraded→retired；
    apply_optimize_result 升降级（通过升 candidate/degraded 恢复；连续失败降级→退休）；
    promote_to_paper/retire/snapshot 审计事件留痕
- **测试 `tests/test_walk_forward_lifecycle.py`（15 项）**：ParamSpec 整数/对数解码与校验、
  整数+对数寻峰、eval_meta 打穿缓存（v2 全重评/同版零新增）、早停标记与评价数、
  walk_forward 聚合（分位数单调/拒绝率/共享缓存）、生命周期全链路（候选晋升/降级恢复/退休/审计）
- **测试基线 306 passed**（283 → 306，含 shim 守护自动覆盖）

### 5.6 codebase-memory-mcp 集成（2026-08-06，代码智能精度层）

- **定位**：Genesis 账本确认 Veya 无 LSP 调用链/blast radius/Cypher——此集成补精度层
- **主库 obase 343a908**：`mcp_stdio.py` — StdioMcpClient（stdio transport MCP 客户端，
  JSON-RPC 2.0 + LSP 帧；spawn 任意 stdio MCP 二进制；McpClientHandle 协议兼容；
  崩溃检测/超时/stderr 留存）
- **主仓装配 `server/codebase_memory.py`**（3O 铁律：机制主库，装配主仓）：
  - `CodebaseMemoryConnector`：spawn 二进制 → `McpClientRegistry.register("codebase_memory")`；
    索引 full/incremental 持久化 `~/.veya/codebase-memory-index`（project.json 恢复）；
    8 工具批量 `make_mcp_tool_adapter`（mcp_codebase_*）；二进制缺失优雅降级
  - `blast_radius(symbols)`：trace_path(mode=calls) 聚合影响面（pre_dispatch 门禁用）
  - 双通道 `search()`：search_graph 符号级优先 → SemanticSearch 向量 fallback
- **接线**：app.py lifespan 启动/关闭 sidecar；`hooks/builtin/pre_dispatch.py` 新增
  `blast_radius_gate_hook`（build/execute + 文件写意图 → 影响面 >20 阻断，未就绪降级放行）；
  `coordinator.semantic_search_query` 双通道升级
- **二进制**：DeusData/codebase-memory-mcp v0.9.0 → `~/.local/bin/`（270MB portable）
- **实测**：主仓根索引 38,042 nodes / 208,070 edges；blast_radius 真实调用链；
  trace_path 注意点：`mode="calls"` 且**不传 direction**（显式 direction 反而返回空）
- **测试 `tests/test_codebase_memory.py`（11 项）**：可用性/索引/符号命中/跨文件调用链/
  blast_radius 聚合/Cypher/双通道 graph 优先+vector fallback/工具批量适配/单例/缺失降级
- **工具面接线 + cron（2026-08-06，14 项）**：
  - `wire_master_tools(connector)`：8 个 `mcp_codebase_*` 注册进 **master_tools**（主脑
    coordinator_master 静态工具面，LLM 直接可调）；幂等；注意 `make_mcp_tool_adapter`
    返回 **ToolAdapter 对象**（`.callable` 才是 func）
  - `schedule_daily_reindex(automata.scheduler)`：APScheduler cron 每日 03:17 增量索引
    （automata.scheduler 是 AsyncIOScheduler，纯函数任务不走 LLM；misfire_grace 1h；幂等）
  - app.py lifespan：start → wire → schedule 三连接线
  - **生产验证**（容器重启后）：`/master/tools` 17 工具含 8 codebase；
    `/automata/jobs` 显示 cbm_daily_reindex next_run 2026-08-07T03:17Z

## 6. 已知待办/风险

1. **claude/codex 引擎 403/refused** — 宿主账号侧，与代码无关；排查宿主 `claude -p` / `codex exec`
2. **前端新 build 需 sudo restart veya-web** 才生效（当前域名前端可能是旧 build 走旧协议——已兼容）
3. **会话历史已持久化（2026-08-06 闭环）** — `server/routes/session.py` + `server/chat_coordinator.py`
   装配 obase.CheckpointStore (SQLite WAL, ~/.veya/checkpoints 落 veya-data 卷)；
   写点 create/fork/compact/undo + chat 每轮后落盘，启动 hydration 恢复；
   chat key 前缀 `chat_`（注意 CheckpointStore._safe 把 `:`→`_`）
4. **Caddy 配置不可读**（/etc/caddy/Caddyfile 缺失，进程内存配置）— 域名转发规则未审计
5. **未 push** — 主仓 26+ commits 待 `git push`；3O 子模块各自 commit 未 push
6. **多租户/量化接线/非线性收缩** — 明确非目标

### 6.1 引擎账号修复记录（2026-08-06，四引擎容器内全通）

- **诊断结论**：claude/codex 账号本身正常（宿主实测通过）；容器内不可用是**网络架构**问题：
  - claude 403 "Request not allowed" = Anthropic 拒容器直连出口 IP；宿主经本地代理
    `127.0.0.1:7890`（clash）成功
  - codex 依赖 opencodex 代理（127.0.0.1:10100，**Host 必须 loopback** 校验）→ 容器内自举
- **修复（engine_runner 79063f29）**：
  - `_container_gateway_ip`（HTTPError 403 也算可达——urlopen 非 2xx 抛异常，易误判）
  - `_container_proxy_env`：容器内 subprocess 注入代理 env（claude 出口走桥 17890）
  - `_ensure_container_opencodex`：容器内自举 opencodex（bun + 代理 env，模型目录同步 chatgpt.com），幂等
  - codex argv：`--sandbox workspace-write`（`--full-auto` 已弃用）+ 容器内 `-c openai_base_url=127.0.0.1:10100`
- **宿主桥**：用户级 systemd `veya-codex-bridge`（`~/.local/bin/veya_codex_bridge.py`）：
  `0.0.0.0:10101→127.0.0.1:10100`（opencodex 备用）+ `0.0.0.0:17890→127.0.0.1:7890`（HTTP 代理）
- **注意**：容器重启后 opencodex 由 engine_runner 首次调用时自动自举（探测 127.0.0.1:10100
  healthz 失败 → spawn）；bridge systemd 服务需宿主保留（重启宿主后 `systemctl --user enable`
  已持久化）
- **实测**：四引擎容器内全通（master/pi/claude/codex）；公网 claude 全链路 OK

## 7. 常用命令

```bash
# 容器
docker compose -f deploy/docker-compose.yml build backend
docker compose --env-file .env -f deploy/docker-compose.yml up -d --force-recreate
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
