# RFC-10: PR-06/PR-11 可观测性 — 范围决策

> 状态：已执行安全子集（2026-08-24）；核心接线（trace_id 进 SSE/audit）明确
> 不做，理由见 §3。
> 依据：docs/VEYA_10_OF_10_PLAN.md §6（EventSpec v1）+ §12（Observability）
> 范围：这两个 PR 本质是同一件事（给 tool/model/state 各种 span 一个统一
> correlation ID），一起决策。

## 1. 意外发现：可观测性基建已经建好了，只是零接线

调研前假设"这块完全没做"，读代码后发现假设错了：`veya/obase/telemetry.py`
已经是一套相当完整、OpenTelemetry 风格的 trace/span 机制——`TraceContext`
（trace_id + parent_id, span 层级）、`begin_trace`/`end_trace`（生命周期）、
`@traced` 装饰器（自动记录 enter/exit/error/duration）、`emit()`（写入当前
trace + 转发给注入的回调）、`set_emitter()`（服务层注入回调的挂钩点，专门
为了不让 obase 反向依赖 `server.events`）、JSONL 落盘 + `latest_trace()` 读取。
质量和完成度都不低。

`grep -rn "begin_trace\|set_emitter" server/ veya/` 之后发现：**这套机制在
真实调用链路里完全没被用过**。`set_emitter()` 全仓库零处调用（唯一命中是
`veya/obase/adapters.py` 里的一句注释）；`begin_trace()` 同样零处调用。跟
`rfc-05` 之前发现 `oskill.eval_suite` "建好了但零 I/O、从未接到真实结果"是
同一种模式——这次是可观测性版本。

## 2. 现有的另一套机制：`fire_step`/`on_step`（真实在用，但不是同一个东西）

`server/events.py::fire_step()` + `_on_step_ctx`（独立的 ContextVar，跟
`telemetry.py` 的 `_emitter_ctx`/`_current` 完全不共享）是**当前真实在生产
SSE 流里用的通知机制**——`MasterCoordinator.chat_stream()` 里 `on_step` 参数
经这条通道桥接。也就是说现在同时存在两套完全独立、互不相通的"事件通知"基础
设施：一套是真实在用但没有 trace_id 概念的 `fire_step`，一套是有完整 trace_id
概念但完全没人调用的 `telemetry.emit()`。

## 3. 决策：这次只做安全的子集，接线决策留到下一轮

把 `telemetry.begin_trace()`/`set_emitter()` 接进 `MasterCoordinator.
chat_stream()`（真正让每轮聊天产生一个 trace_id）需要先回答一个没有显然答案
的设计问题：**`telemetry.emit()` 的输出要不要也走 `fire_step` 转发给前端 SSE？
两套机制要合并成一套，还是保持"内部审计 trace"和"前端可见 on_step"两条平行
线？** 这个问题的答案会决定接线方式，而 `chat_stream()` 是这个仓库里改动风险
最高的热路径之一（这次会话里已经在这个函数上发现并修过好几个真实 bug）——
在没想清楚这个设计问题之前就往这个函数里加 `begin_trace`/`with trace:`，
要么做出一个后面要推倒重来的接线，要么在不该改控制流的地方勉强插入代码。

**这次做的（零风险，纯准备）**：`server/tool_registry.py::MasterToolRegistry.
execute()`——全仓库唯一的工具执行收口点——加了 `telemetry.emit()` 调用，
记录 `tool_execute` span（进/出/错误 + 工具名 + 耗时 + 状态)。因为现在没有
任何地方调用 `set_emitter()`/`begin_trace()`，`emit()` 内部逻辑
（`trace = _current.get(); if trace: ...` + `cb = _emitter_ctx.get(); if cb:
...`）两个分支都是 `None`，是完全的安全 no-op——**运行时验证过**（不是靠读
代码判断"应该没事"）：不绑定 emitter 时执行工具零异常、返回值不变；绑定
`telemetry.set_emitter()` + `begin_trace()` 后跑同一段代码，能收到真实的
`tool_execute` span（含成功/超时/失败三种状态），说明埋点是真的接对了地方，
不是摆设。

**这次不做的**：`chat_stream()` 里接 `begin_trace`/`end_trace`、决定
`telemetry.emit()` 输出要不要转发进 `fire_step`、`server/audit.py` 的读侧
是否要从"读 `server.events` 落的审计文件"改成"读 `telemetry.jsonl_write`
落的 trace 文件"。这几个都是需要先拍板"两套机制到底要不要合并"这个设计问题
才能动手的事，属于下一轮的范围。

## 4. 验证

- `server/tool_registry.py`：`ruff check` 干净；`mypy --follow-imports=skip`
  跟 `coordinator_master.py` 一起查 0 错误。
- 运行时验证（不是猜）：
  1. 不绑定 emitter 时调用 `MasterToolRegistry.execute()`（成功用例）——
     零异常，返回值不受影响。
  2. 绑定 `telemetry.set_emitter()` + `begin_trace()` 后跑一个成功用例 + 一个
     故意失败的用例——收到两条 `tool_execute` span（`status=completed` /
     `status=failed`），`trace.steps` 长度精确等于 2。
- `tests/test_master_tools.py` + `tests/test_coordinator_cognitive.py` +
  `tests/test_hosted_sandbox.py` + `tests/test_sandbox_cmd_normalize.py` +
  `tests/test_agent_eval_suite.py`：全部通过，证明埋点没有改变任何既有
  行为（见测试运行记录）。
