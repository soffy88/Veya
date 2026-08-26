# Veya Execution Runtime Upgrade

状态：`0.9.x` execution slice implemented; `1.0.0` release gates remain open.

本文把 FrontierAgent 的长任务经验内化为 Veya 的 deterministic capability，服从
[`ARCHITECTURE_STABLE.md`](ARCHITECTURE_STABLE.md)：用户仍只有
`MasterCoordinator → MasterAgent ReAct → tools` 一条主链。`GoalRun` 只负责持久化、调度、验收
和收口；`AgentLoop` 仍然只是 MasterAgent 可调用的隔离 delegate，不拥有用户 session 或最终答案。

## 已实现

- `runtime/execution/models.py`：`DelegateRequest`、`DelegateResult`、`AcceptanceCriterion`、`SharedTaskContext`、统一 stop reason、Evidence、Assertion、Artifact、checkpoint 数据契约。
- `runtime/execution/spawn_guard.py`：深度、并发槽、token/cost reservation、root wall、child timeout、取消安全和 RAII 释放。满槽排队，不拒绝。
- `runtime/execution/scheduler.py`：continuous ready scheduling；显式 `parallel=True` 才允许并行，快任务结束后立即补位，非并行任务独占。
- `runtime/execution/fanin.py`：complete/partial/failed/paused/cancelled 分类，保留失败 child 已产生的证据、断言和产物，并按 source/content/artifact fingerprint 去重。
- `runtime/execution/artifacts.py`：`.veya/runs/<task_id>/{inputs,workspace,outputs,evidence,checkpoints,trajectories}` 与 manifest；只有 `outputs/` 默认是最终交付。
- `runtime/execution/finalization.py`：reserve 计算、`FinalizationObserver` 与一次性收口触发；支持 wall、预算、无进展、上下文和 operator stop 信号。
- `SpawnGuard` 在 delegate 返回值中读取实际 prompt/completion tokens 与 cost，完成 reservation reconciliation。
- GoalRun：新增 `finalizing`、`partial_completed`，叶子结果/证据/未完成项持久化，continuous scheduler、Fan-In、manifest、checkpoint 和取消入口已接入。
- AgentLoop：`agent_loop_run` 通过同一 `DelegateRequest → SpawnGuard → DelegateRuntime → DelegateResult` 边界运行；未知 stop reason 按 partial 处理。
- SSE/UI：`scheduler.*`、`delegate.*`、`fanin.*`、`finalization.*`、`artifact.*` 事件只展示真实执行状态。

## 运行时不变量

1. MasterAgent 是唯一语义权威；runtime 不做意图分类、persona 路由、自然语言重解释或最终答案合成。
2. 未声明 `parallel=True` 的任务不得并发。
3. 未知 stop reason 不得升级为 `complete`。
4. child 失败、超时或取消不删除已经产生的 evidence/artifact。
5. 进入 `finalizing` 后不得新建 Goal task、research branch 或 delegate；只收集、验收、写 manifest 并交回 MasterAgent 收口。
6. permission、sandbox、capability scope 和 destructive operation fail-closed；报告格式化、审查和 telemetry fail-open。

## 部署

`runtime/` 已加入 backend image COPY，并加入 Compose 的只读实时挂载。源码或挂载内容变更后，生产 backend 只需重新创建/重启容器即可加载；只有修改镜像层依赖时才需要 `--build`。前端仍需 `pnpm run build` 后重启 `veya-web`。

## 验证

- Runtime 单测覆盖并发上限、排队、RAII、取消、深度、连续补位、Fan-In、部分证据、收口和 checkpoint。
- GoalRun 集成覆盖连续补位、失败 sibling 保留、`partial_completed`、manifest 和 operator cancel。
- 现有 `tests/goal_run` 与新 `tests/runtime` 在生产容器中通过；前端 `svelte-check` 0 error/0 warning，生产 build 通过。

## 后续 1.0 gate

仍需独立完成并验证：remote worker adapter、跨进程 durable queue、resume 时 dangling child 的幂等恢复、required acceptance subset 的明确结构化契约、shadow/dual-run 对比指标以及长期指标导出。它们不能通过增加第二条用户主链来实现。
