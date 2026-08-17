# 3O 环境内化：统一沙箱 + 补齐对 Orchard 的结构性弱项

> 状态：G0–G5 已落地并测绿（合同 + 三后端 + chat/pytest + harness + Broker + 可选 PTY）。`project_run_goal` v0.2 Spec Kit 已接线（§11）。包工头 v1.0 已落地（§12）。
> 主库：`platform/3O`（经 `veya.platform`）
> 红线：不改 Coordinator 路由；不加第二套任务队列；S1–S5 门禁不动。
> 主链工具名保持 `run_in_sandbox` / `hicode_run` / `browser_run` / `project_ask`。

Orchard 赢在**一条环境契约**（create / exec / files / patch / destroy）上挂任意镜像与 harness。
Veya 输在同一件事拆成四套互不相通的执行器，而且对隔离强度说了假话。
强化顺序：先统一契约，再换后端，最后才谈 K8s / 训练配方。

---

## 0. 弱项按重要性（只排 Veya 弱的）

判据：不补则后面所有「像 Orchard」的能力都建在沙子上；产品可靠性；可复用性。

| 序 | 弱项 | 为何排这 | 不补的后果 |
|---|---|---|---|
| **W1** | 没有统一环境契约，四套沙箱互不相通 | 总根因 | 后面任何 harness / 并行 / 评测都会再分叉一套 |
| **W2** | 隔离说了假话（chat 断网=清代理环境变量） | 安全与可复现 | 模型以为「无网验证」其实能出网；搜索奖励不稳 |
| **W3** | 没有多轮存活的沙箱（create 后 N 次 exec） | Orchard 的最小有用单位 | 装依赖、改文件、再跑测试做不到，只能一次 subprocess |
| **W4** | 文件 / git patch 不是环境原语 | 多轮工作面 | 沙箱看不见项目，或只靠 JSON 灌文件；hicode 自己管盘 |
| **W5** | harness 是「一引擎一适配器」，不是沙箱里的一条命令 | 可换 runtime | 加 Claude/Codex/pi 都要改 `engine_runner`；无法在同一环境里对比 |
| **W6** | 编码执行串行（hicode serve 一把锁） | 吞吐 | 一个长任务堵住整条编码腿 |
| **W7** | 无 PTY / 交互会话 | 终端类任务 | 只能非交互 exec |
| **W8** | 无 K8s 水平扩展 / 成本账 | 科研规模 | 单机够用产品，吃不下千级 rollout |
| **W9** | 无快照 / 分支（Orchard 也还在路线图） | 信用分配 | 47 步轨迹只能事后猜哪一步有用 |
| **W10** | 无 SFT/RL 配方、无 SWE-bench / WebVoyager、无公开轨迹 | 论文分 | **产品主链不靠这个活**；单独开研究栈，禁止焊进 Coordinator |

W1–W4 是内化主体。W5–W6 接在统一环境上。W7–W9 是环境增强。W10 永远不进主链。

---

## 1. 统一原则

1. **一套合同，多后端。** 调用方只看见 `Sandbox`。后端是 `process` / `netns` / `docker` /（以后）`k8s`。
2. **按隔离等级选后端，不按「谁写的客户端」选。** pytest、视频质检、chat 验证、loop 搜索、hicode 工作区都是同一合同的不同 `image` / `isolation` / `mounts`。
3. **诚实。** 结果里必须带 `isolation` 实际生效值。要 docker 却没有 docker → `ok=False`，禁止静默降成「假装无网的进程」。
4. **主库先落地，Veya 只换实现、不换工具名。** `run_in_sandbox` 仍是那一把工具，内部改走统一合同。
5. **视频镜像 ≠ pytest 镜像。** 统一的是 API，不是把 ffmpeg 和 pytest 塞进同一个 Dockerfile。

不统一的（故意留下）：

| 留下 | 原因 |
|---|---|
| 宿主工作区 `write_file` | 产品编辑的是用户项目，不是沙箱 tmp |
| Playwright `browser_run` | 浏览器是另一类环境（有显示/站点），只通过合同的 `exec` 调驱动，不把 Chromium 塞进 pytest 镜像 |
| hicode / reasonix 二进制 | 仍是外部 harness；环境只给它 workspace + exec |
| 主链 ReAct | 冻结。环境服务不是第二条主链 |

---

## 2. 3O 分层（主库 `platform/3O`）

```
oservi.SandboxBroker          可选本机守护：池、TTL、心跳、并发槽
        │
omodul.sandbox_session        生命周期 + 工作区同步 + 评测/harness 编排
omodul.eval_in_sandbox        pytest / 视频质检（原 reliability 客户端）
omodul.run_harness            在已有 Sandbox 上跑 codex/claude/pi/dsh/…
        │
oskill.isolation_policy       纯函数：用途 → isolation/image/limits（无 I/O）
oskill.harness_argv           纯函数：名字 + prompt → argv + 所需 env 键
oskill.pytest_payload         纯函数：files+args → 沙箱协议 payload
        │
oprim.sandbox_create/exec/    单次原子，失败结构化返回
     put_file/get_file/list
     apply_patch/destroy
     heartbeat
        │
obase.SandboxHandle           Protocol：资源账本、authz、是否允许出网
obase.NetPolicy               声明 block_network，不在 oprim 里写 iptables
```

依赖仍是 `obase ← oprim ← oskill ← omodul ← oservi`。
Veya `server/*` 只 `veya.platform.load(...)`，禁止再实现一套 Docker 客户端。

### 2.1 oprim 合同（对标 Orchard REST，单机先实现）

```text
sandbox_create(*, image="", isolation="process", block_network=True,
               cpu="1", memory="512m", timeout_s=3600,
               mounts=None, workspace=None, env=None) -> {ok, sandbox_id, isolation, error}

sandbox_exec(sandbox_id, argv, *, cwd="", env=None, timeout_s=30, pty=False)
    -> {ok, exit_code, stdout, stderr, timed_out, pty}

sandbox_put_file / sandbox_get_file / sandbox_list
sandbox_apply_patch(sandbox_id, patch)   # 沙箱内 git apply / 内置 patch
sandbox_destroy(sandbox_id)
sandbox_heartbeat(sandbox_id)            # 续 TTL；process 后端可 no-op
```

约束（沿用 oprim 纪律）：

- 每个函数一次 OS/容器动作；keyword-only 配置。
- 后端之间不互相调用。`docker` 不调 `process`。
- 不 import `server` / `veya.sandbox`。

`isolation` 枚举：

| 值 | 实现 | 真隔离是什么 |
|---|---|---|
| `process` | `asyncio`/`subprocess` + `ulimit` | 只限 CPU/内存/超时。**网络仍通。** |
| `netns` | `unshare -Urn` | user+net 命名空间。无 `unshare`/EPERM → **失败，不降级** |
| `docker` | 长生命周期 `docker create/start`（不是 `--rm` 一次跑完） | `--network=none` 当 `block_network`；cgroup 限额 |
| `k8s` | 后期；同一函数签名 | Orchard 形状，独立 milestone |

`pty=True` 是**一次带 TTY 的 exec**，不是 Orchard 式 attach / websocket：

| 后端 | `pty=True` |
|---|---|
| `process` / `netns` | `oprim.run_pty`（与 `spawn_pty` 共用 `openpty`），argv 不经 shell |
| `docker` | `docker exec -t`（容器内分配 TTY） |
| `memory` | `ok=False`，没有假 TTY |

结果里带 `pty` 实际生效值。主链不加默认工具。

一次性评测（旧 `docker run --rm`）做成 `sandbox_create` + `exec` + `destroy` 的短会话，不再单独维护客户端。

### 2.2 oskill（纯函数）

```text
isolation_policy(purpose) -> {isolation, image, block_network, cpu, memory}

purpose:
  chat_verify      → process, 无镜像, block_network=False（承认能出网）
  untrusted_exec   → docker, python:slim 或 veya-code-sandbox, block_network=True
  pytest_eval      → docker, veya-code-sandbox, block_network=True
  video_eval       → docker, veya-video-sandbox, block_network=True, mount=ro
  tree_search      → netns, 无镜像
  harness_run      → docker, veya-sandbox-tools（预装 CLI）或 process+host PATH
  hicode_workspace → docker 或 process+path jail（由 policy 配置）
```

`harness_argv(engine, prompt, extra)` 把现在 `engine_runner` 里的 argv 表搬进来，单测锁死，禁止 Coordinator 再拼命令。

### 2.3 omodul

`sandbox_session(purpose, **policy_override)`：

1. 调 `isolation_policy`
2. `sandbox_create`
3. 按需 `put_file` / bind mount
4. 把 `Sandbox` 句柄交给调用方或跑完 `destroy`
5. 上下文管理器保证释放

`eval_in_sandbox` 替换 `services/code_sandbox_client.py` 与 `video_sandbox_client.py`。
`run_harness` 替换 `engine_runner` 里「造 argv + subprocess」的那一段；探测凭据仍可留在 Veya 装配层。

### 2.4 oservi（W6 才需要）

`SandboxBroker`：进程内或本机 HTTP。职责只有池、TTL、并发槽、id → backend。
**不是** MasterAgent，不注册「下一步找谁」。
hicode 不再自己 `asyncio.Lock` 全局串行；改成 Broker 里「每 workspace 一把锁」或「N 个并行槽」。

---

## 3. 现有四套沙箱怎么并

```
                    omodul.sandbox_session
                            │
                 oprim.sandbox_*  (一个合同)
        ┌───────────┼────────────┬─────────────┐
     process      netns        docker          k8s (后)
        │           │             │
  run_in_sandbox  observer    pytest 评测
  chat 30s 验证   openrsi     视频质检
  诚实「有网」    高频搜索     hicode 可选箱
```

| 今天的实现 | 并入后 |
|---|---|
| `veya/obase/sandbox.py` `ProcessSandbox` + `run_in_sandbox` | `purpose=chat_verify`，backend=`process`。危险命令检测留在 oskill 或 tool_guard |
| `oprim.LocalSandbox` + `SandboxPool` | 成为 `netns` 后端 + Broker 池，不再另起一套 API |
| `services/code_sandbox_client.py` `docker run --rm` | `sandbox_session(pytest_eval)`：create → put files → exec pytest → get junit → destroy |
| `video_sandbox_client.py` | 同上，换 image + ro mount |
| `obase.local_sandbox_pool` honeypot | 暂不并。对抗审计是另一用途，避免污染默认池 |
| `oprim.DockerSandbox`（几乎无调用方） | 删或改成 `docker` 后端的薄包装 |
| hicode workspace + git checkpoint | 先当 `process`+path jail；W6 再可选 `docker` bind-mount 同一目录 |
| Playwright | 不并进容器合同；`record_browser_gif` 已是 oprim。以后 `harness_run` 若要无头浏览，用独立 browser image 走同一 `sandbox_exec` |

迁移纪律：每并一套，旧客户端改成 10 行委托，测试从旧入口跑仍绿，再删旧实现。

---

## 4. W5 harness 内化（合同稳定之后）

今天：`engine_runner.ENGINE_ALIASES` + 各引擎一段 argv，CLI 从宿主机 bind-mount。

目标（Orchard 的「换命令不换镜像」在单机上的最小版）：

1. `oskill.harness_argv` 单源。
2. `omodul.run_harness(sandbox, engine, prompt)` 在**已有** Sandbox 里 `exec`。
3. 可选镜像 `veya-sandbox-tools`：预装 `claude`/`codex`/`pi`/`dsh`/`opencode`（有许可证的装，没有的标明缺）。宿主机 bind-mount 作为过渡。
4. `engine=master` **不进沙箱**——它是产品主链，不是 harness。
5. 前端引擎选择器语义不变：选的是「谁执行」，实现从「Veya 直接 subprocess」改为「环境里 exec 这条命令」。

禁止：为每个引擎再写一套 Docker 客户端。

---

## 5. W6 并行

- Broker 默认槽位：`process` 4、`netns` 4、`docker` 2（单机保守）。
- hicode：按 `workspace` 互斥，不同项目可并行。
- 不把「并行谁」暴露给 Coordinator。模型仍只调 `hicode_run`；排队在 Broker。

---

## 6. W7–W10（刻意靠后）

| 项 | 做法 | 何时 |
|---|---|---|
| W7 PTY | `sandbox_exec(..., pty=True)` 走 `oprim.run_pty`（与 `spawn_pty` 同一 `openpty`）；主链不加默认工具 | ✅ G5：一次 TTY exec，stdout/stderr 合并；memory 拒绝；docker `exec -t` |
| W8 K8s | 新 backend，函数签名不动。部署另栈，默认产品仍 docker/process | 有集群且要吃公开配方时 |
| W9 快照 | 先做 workspace 目录快照（已有 `oprim._snapshot`）；进程/容器快照另议 | 研究栈 |
| W10 训练 | 独立 `trainer/` 或外部 slime，只依赖环境合同。**禁止**进 `coordinator_master` / 默认工具面 | 单独立项 |

---

## 7. 里程碑

| 门 | 内容 | 完成定义 |
|---|---|---|
| **G0** | 主库 oprim 合同 + 假 backend（内存/字典）+ 单测 | ✅ create/exec/files/patch/destroy；越狱失败 |
| **G1** | `process` + `netns` + `docker` 三后端；`isolation_policy`；诚实失败 | ✅ 无 docker / 无 unshare 返回 `ok=False` |
| **G2** | `run_in_sandbox` 与 pytest 客户端改走统一合同 | ✅ 工具名不变；video 仍走旧客户端（缺 mount 原语） |
| **G3** | `harness_argv` + `run_harness`；`engine_runner` 变薄 | ✅ argv 单源；聚合 run 走 harness_host；前端选项不变 |
| **G4** | `SandboxBroker` + hicode 按 workspace 锁 | ✅ 同 workspace 互斥；hicode serve 仍 1 槽（单 serve 诚实限制） |
| **G5** | 可选 PTY；文档 `docs/O3_ENV_UNIFY.md` 与实现对照 | ✅ 无主链新工具；`pty=True` 是一次 TTY exec，不是 attach |
| **G6** | （可选）K8s backend | 与 G1 同一测试夹具，换 backend 开关 |
| **G7** | （可选，另仓/另目录）SWE/GUI 配方 | 不碰冻结主链 |

建议实施顺序：**G0 → G1 → G2** 先打通「一个合同吃掉四套沙箱」，再 G3/G4。不要先做 K8s 或 RL。

---

## 8. Veya 装配（只接线）

| 文件 | 变化 |
|---|---|
| `server/tool_registry.py` `_tool_run_in_sandbox` | 调 `omodul.sandbox_session("chat_verify")` |
| `services/code_sandbox_client.py` | 委托 `eval_in_sandbox` |
| `templates/video_services/video_sandbox_client.py` | 同上 |
| `server/engine_runner.py` | argv 来自 oskill；exec 经 session（`master` 除外） |
| `server/hicode_serve.py` | 锁上交 Broker（G4） |
| `server/observer.py` / `openrsi.py` | 池改为 Broker.netns |
| **不改** | `coordinator_master.py`、默认模型、前端交互、`project_ask` 派工语义 |

`PROJECT_ASK_AUTO_GATES` 仍然默认关。环境内化与工程门禁正交。

---

## 9. 测试

- oprim：假 backend 全合同；docker/netns 用 `which` 跳过或 fixture。
- oskill：`isolation_policy` 表驱动；`harness_argv` 快照。
- omodul：session 退出必 destroy（即便 exec 抛错）；pytest_eval 不碰业务源码。
- Veya：`run_in_sandbox` 工具名仍在；`test_project_ask` 零回归；引擎列表不变。
- 守护：禁止 `server/` 再 `subprocess` 拼 `docker run`（G2 后加 guardian）。

---

## 10. 非目标

- 不把 Orchard Env 整仓 vendoring 进 Veya。
- 不把 Coordinator 改成「环境编排器」。
- 不把四套镜像合成一个胖镜像。
- 不在 chat 默认路径上启用 K8s。
- 不把 W10 训练配方伪装成产品功能。

---

## 11. project_run_goal v0.2（Spec Kit 驱动）

> 状态：3O 契约 + Veya 薄接线已落地并测绿。工具名仍是 `project_run_goal`。
> 不改 Coordinator 路由；不平行第二套任务队列。

`.speckit/constitution.md` + `.speckit/tasks.md` 是可选 SSOT。存在则替换黑盒 G1；否则走原规则/LLM 计划。

| 层 | 落点 |
|---|---|
| obase | `SpecKitPaths`、`TaskNode` |
| oprim | `load_speckit_artifacts` / `save_taskgraph`；`compose_constitution_brief` + PathJail |
| oskill | `compile_spec_to_dag` / `validate_taskgraph_dag`；`detect_constitution_violation` |
| omodul | `phase_spec_driven_plan`、`phase_verify_leaf_task`；health monitor 宪法红线先于 L1/L2/L3 |
| oservi | `SpecDrivenGoalEngine`（装配用；不改工具名） |
| Veya | `g1_plan(project_root=)` 有 artifacts 时走 3O plan；leaf 前缀 constitution；runner 用 monitor 拦截违宪 |

刻意不做：Nexu / LoRA / Spec Kit 生成 UI（不在 3O）；交互 attach / `sandbox_exec_stream`（推后）。

---

## 12. 包工头 v1.0-Boss（G0 意图 + G1 派发 + G2 证据验收）

> 状态：3O 契约 + Veya Layer 4 薄接线已落地。工具名仍是 `project_run_goal`。
> Veya 是分诊中枢 / 包工头：不亲自写代码。叶子仍由 hicode / dsh 执行。
> Spec Kit 路径（§11）不变：有 `.speckit` 时仍走 `compile_spec_to_dag`。
> 默认 `project_run_goal` 行为不变；包工头走 `project_run_goal_boss_mode`。

| 层 | 落点 |
|---|---|
| obase | `WorkspaceSnapshot` / `WorkspaceInspector`；`IntentBrief` |
| oprim | `call_llm_for_intent` / planning / verification（caller 注入） |
| oskill | `assemble_intent_context` / `assemble_boss_context(brief=)`；`validate_intent_brief` / `validate_leaf_contract`；DAG 查环 |
| omodul | `phase_intent_triage`（G0）；`phase_closed_loop_plan`（G1，吃 brief + 叶子合同）；`phase_evidence_verify`（G2） |
| oservi | `BossOrchestrationEngine`：G0 →（ask/refuse 停）→ G1 → 叶子 → G2 |
| Veya | `server/boss_entrypoint.py` → `project_run_goal_boss_mode` |

刻意不做：改 Coordinator 路由；第二套任务队列；替换 Spec Kit G1；改 `project_run_goal` 默认行为；加厚 G2。
