# 架构

## 执行模型：Coordinator → Squad → Engine

```
用户输入
   │
   ▼
Coordinator.handle()          server/coordinator.py
   │  veya.intent 分类 SIMPLE/COMPLEX
   ├─ SIMPLE → 单 squad 直接执行
   └─ COMPLEX → veya.plan 分解为 DAG（research → plan → execute）
        │
        ▼
  Squad 并行组（拓扑序）        _run_dag / parallel_executor
        │
        ▼
  Engine.run_turn()            veya/compat.py
        │  turn_handler → llm_caller
        ▼
  provider_call/stream          veya/llm.py (openai/anthropic/dashscope)
```

## 3O 范式基座（veya/obase）

`veya/obase/` 是只依赖标准库/第三方的基座层，**永不 import 业务层**（§7.4 铁律，
由 `scripts/check_obase_no_reverse_dep.py` 在 CI 强制）：

| 模块 | 职责 | 关键约束 |
|------|------|---------|
| `telemetry.py` | JSONL trace、ContextVar 通道、`@traced` 装饰器 | C1：共享可变对象走 ContextVar，子任务只 `.get()` |
| `authz.py` | 权限规则引擎 + 交互式确认门 | 规则按 allow/deny/ask/`*` 顺序匹配 |
| `__init__.py` | `__manifest__` 契约（7 元素） | `scripts/check_manifest.py` 校验 |

## 惰性初始化（G9）

`Coordinator` 的 16 个子系统全部通过 `functools.cached_property` 惰性构造；
重模块（plotly/networkx/matplotlib ≈56MB）延迟到首次访问才导入，
`Coordinator()` 构造增量内存 ≈ 0。

## 安全边界

- 沙箱子进程：POSIX `ulimit -v`（内存）+ `ulimit -t`（CPU）shell 前缀，**宿主进程
  rlimit 永不降低**（`veya/sandbox.py`）
- 危险命令前置拦截（`reject_dangerous=True` 默认）
- 权限门：`veya/obase/authz.py` + HTTP `/permission/*` + CLI 交互确认

## 流式协议

SSE 事件序列（session_id 维度）：

```
session_start → squad_start → text_delta* → squad_done
             → cost_update → task_done → [DONE]
```

## 可观测性

`veya/obase/telemetry.py`：每次 agent 执行写 JSONL trace（span 树），
`veya.obase.telemetry.latest_trace()` 读取最近 trace；`@traced` 自动记录
参数摘要（PII 脱敏 + 80 字符截断）与耗时。
