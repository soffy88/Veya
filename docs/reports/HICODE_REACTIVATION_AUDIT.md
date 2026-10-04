# HICODE_REACTIVATION_AUDIT — SF-HICODE P0

> **只读考古。未修改任何源码。**
> 提取源:`937184cd^`(hicode 最后一次在 `_KNOWN` 中的状态)
> 日期:2026-10-04

## 0. 首要更正:spec 的"退役前最后可用状态"前提有误

Spec §4 P0 写的是"从 `b7b1a7d4`(退役 commit)之前的最后可用状态提取"。
实测**该前提不成立**:`b7b1a7d4` 的父提交 `bb90ff90` 的 `_KNOWN` 里
**已经没有 hicode**。

```text
b7b1a7d4  2026-10-02  refactor(hicode): retire ... into legacy/   纯文件移动,0 增 0 删
  └─ bb90ff90         _KNOWN 不含 hicode  ← spec 以为的"最后可用状态"
937184cd  2026-09-28  feat(registry): land canonical Local2 registry (Phase A)
  └─ 937184cd^        _KNOWN = ("pi","hicode","codex","antigravity","opencode","dsh")  ← 真实最后可用状态
```

**hicode 的移除是两个独立事件**,相隔 5 天:

| 时间 | commit | 事件 |
|---|---|---|
| 2026-09-28 | `937184cd` | hicode 从 `_KNOWN` 移除(注册消失,实现文件仍在 `server/`) |
| 2026-10-02 | `b7b1a7d4` | 实现文件归档进 `legacy/`(纯移动) |

因此 P0 的提取源应为 `937184cd^`,**不是** `b7b1a7d4^`。若按 spec 原前提提取,
会得到一个"已不可用"的状态,后续 P1+ 会建立在一个从未真正工作的基线上。

## 1. 旧实现依赖的模块清单

`937184cd^:server/` 下 7 个模块,共 3340 行:

| 模块 | 行数 | 职责 |
|---|---|---|
| `hicode_agent.py` | 1326 | 工具面、代理循环、进度事件、失败分类 |
| `hicode_runtime.py` | 839 | **L2 runtime 解析**、proxy、managed python |
| `hicode_queue.py` | 433 | 任务队列(依赖 `server.goal_run.*`) |
| `hicode_serve.py` | 344 | serve 入口 |
| `hicode_cooldown.py` | 207 | 上游配额冷却 |
| `hicode_managed_entry.py` | 110 | managed 入口 |
| `hicode_host_boundary.py` | 81 | 宿主边界 |

内部依赖(均**未**依赖其他 hicode 模块之外的私有面):

```text
hicode_agent  → server.process_guard
              → veya.remote.executor_registry
              → veya.oprim / veya.obase
hicode_queue  → server.goal_run.leaf / server.goal_run.store
              → veya.oprim.fs
hicode_runtime→ veya.obase, veya.remote.executor_registry
hicode_serve  → server.hicode_host_boundary
```

外部契约面:

```text
config/hicode_model_mapping.json     provider/model 映射
deploy/hicode-entrypoint.sh          容器入口(当前引用已删模块)
deploy/build_hicode_python_runtime.py 构建 L2 python runtime
deploy/build_hicode_image.sh         构建镜像
deploy/host/install_managed_reasonix.sh  宿主安装 managed Reasonix
docs/evidence/hicode-provenance.json 来源证据
docs/evidence/hicode-upstream.json   上游证据
runtime/execution/adapters.py        delegate_result_from_hicode()
runtime/provider_reliability.py      上游可靠性
```

## 2. 旧 provider / model 映射

```python
# executor_registry._hicode_config()
provider = os.environ.get("HICODE_REASONIX_PROVIDER")
model    = os.environ.get("HICODE_REASONIX_MODEL") or os.environ.get("HICODE_MODEL")
# 回退到 config/hicode_model_mapping.json
```

`config/hicode_model_mapping.json` 全部内容(唯一一条):

```json
{
  "gemini-pro-agent": {
    "requested_model":    "gemini-pro-agent",
    "requested_provider": "cliproxy-google",
    "internal_provider":  "antigravity",
    "upstream_model":     "gemini-pro-default",
    "active": true,
    "updated_at": "2026-09-26T00:00:00Z"
  }
}
```

注意映射的方向:`gemini-pro-agent` → 内部走 **antigravity**。
即 hicode 与 antigravity **共用上游 provider**,不是独立 provider。

## 3. 旧 credential 要求 —— 第三种假阳性

```python
# executor_registry._discover(),hicode 分支
elif executor_id == "hicode":
    provider, model, source = _hicode_config()
    auth = bool(provider and model)          # ← 不是凭据检查
    try:
        from server.hicode_runtime import get_hicode_executor
        launcher = get_hicode_executor().resolve_binary()
    except Exception:
        launcher = None                       # ← 静默吞掉全部失败
```

**这是与 SF-CRED 已修的两种都不同的第三种假阳性**:

| 假阳性类型 | 机制 | SF-CRED 状态 |
|---|---|---|
| 文件存在即已认证 | `claude_code`/`codex` | **已修**(P2a 结构校验) |
| 缺省即无效 | `antigravity` | **已修**(真实验收纠正) |
| **有 provider/model 映射即已认证** | **hicode** | **未修 —— 本次考古发现** |

`authenticated = bool(provider and model)` 与凭据**毫无关系**:只要映射文件里有
一条记录,即使没有任何可用凭据,hicode 也会报 `auth_state="AUTHENTICATED"`。

另外 `except Exception: launcher = None` 静默吞掉 L2 runtime 解析的任何异常,
使"runtime 缺失"与"runtime 存在但不可执行"不可区分。

旧 `hicode_agent.py` 明确**不注入任何 api_key_env**:

```python
# 不注入任何 api_key_env 占位值, Hicode 将不发 Authorization 头。
# 若将来 provider 配了真实 api_key_env, 环境变量自然透传。
```

⇒ **旧设计本身就没有凭据路径**。凭据由 Hicode 进程经 proxy 自行获取,
veya 侧只透传环境变量。SF-CRED 的 credential 体系对 hicode 的适用性
需要重新裁决 —— 它可能根本不属于同一模型。

## 4. 旧 capability 声明

```python
executor_kind = "internal_hicode" if executor_id == "hicode" else "l1_worker"
capabilities  = frozenset()        # 空
reachable     = launcher is not None
status        = "READY" if reachable and authenticated
                else "DEGRADED" if reachable else "UNAVAILABLE"
```

**`executor_kind="internal_hicode"` 是关键历史事实**:
hicode 从来不是 `l1_worker`。它没有走 `WORKER_CAPABILITIES` 那套 L1 能力声明,
`capabilities=frozenset()` 为空。

⇒ 复活时**不能**把它当成又一个 L1 worker 注册。命名空间需要裁决
(spec §3 前置 #6),因为 `HARNESS_ENGINES` 曾用 `hicode` 指另一套东西。

## 5. 旧 policy / permission 分类

```python
_read_only = {"hicode_sessions", "hicode_status", "hicode_tasks"}
# 注册时:
side_effect = SideEffect.PURE_READ if name in _read_only else None
```

工具面共 **7 个**(代码中另有 2 个 ContextVar `hicode_bound_workspace` /
`hicode_bound_execution_id`,与 1 个事件类型 `hicode_progress`,均非工具):

```text
PURE_READ(3)  hicode_sessions, hicode_status, hicode_tasks
无声明  (4)    hicode_run, hicode_rollback, hicode_review, hicode_stop
```

**4 个写/执行类工具完全没有 effect 声明** —— 这正是 SF-001 记录的问题
("38 个非只读声明全部 ALLOW")在 hicode 上的重演。复活时若沿用
`side_effect=None`,这 5 个工具会默认放行。

## 6. 旧 receipt 形状

失败证据(有界,可审计):

```python
{
  "code": code_text,                 # 或 "HICODE_PROVIDER_ROUND_FAILURE"
  "detail": detail_text,
  "round_index": round_index,
  "raw_evidence": _safe_failure_value(ev),     # 有界截断
  "result": _safe_failure_value(result),
  "recent_events": raw_events[-10:],
  "stderr_tail": str(stderr_tail)[-2000:],
  "exit_code": exit_code,
}
```

配额冷却证据:

```python
{
  "failure_class": quota.failure_class,
  "provider": quota.provider,
  "model": quota.model,
  "upstream_evidence": quota.upstream_evidence,
  "upstream_reset_seconds": quota.upstream_reset_seconds,
  "effective_retry_not_before": quota.cooldown_until,
}
```

成功结果形状(claude-code 式 JSONL 流的末尾 `{"type":"result", ...}`):

```python
{
  "execution_id", "workspace", "objective",
  "model", "provider",
  "context": {"bootstrap": "pre-model-request"},
  "subtype": "managed_bootstrap",
  "is_error": False,
  "result": "managed Hicode bootstrap ready",
  "num_turns": 0, "model_requests": 0, "tool_calls": 0,
}
```

其中 `EMPTY_MODEL_RESPONSE` 的判定条件(值得保留的设计):

```python
not str(body or "").strip() and tool_call_count == 0 and model_request_count > 0
```

即"发过模型请求、没有任何工具调用、响应体为空" → 空响应。
`RemoteErrorCode.EMPTY_MODEL_RESPONSE` 现为无任何代码 raise 的枚举成员,
**这个判定随 `legacy/` 一起归档了**。

## 7. L2 runtime 路径(当前全部不存在)

```python
# hicode_runtime.resolve_binary()
configured = os.environ.get("HICODE_MANAGED_PYTHON", "").strip()
# 必须匹配 canonical managed manifest
if not all(reasonix_info.get(k) for k in ("version", "binary", "commit")):
    ...
# 否则:
raise HICODE_RUNTIME_INCOMPATIBLE: "Reasonix is not the canonical managed binary"
```

相关路径与环境:

```text
HICODE_MANAGED_PYTHON           (宿主) /home/soffy/.veya/hicode-runtime   不存在
which hicode                                                    无
proxy port                     HICODE_PROXY_PORT=10103
proxy upstream                  HICODE_PROXY_UPSTREAM=http://192.168.16.1:10101
proxy upstream host             HICODE_PROXY_UPSTREAM_HOST=127.0.0.1:10100
manifest 必需字段               version / binary / commit 三者齐全
```

`192.168.16.1:10101` 与当前 LLM 层记录中的宿主桥地址一致 ——
说明 hicode 当年经**宿主桥**取模型,与 opencode 走公网网关是不同路径。

## 8. 前置条件现状复核(spec §3)

| # | 前置 | 状态 |
|---|---|---|
| 1 | L2 runtime 实际存在 | **仍未满足** —— 二进制、managed python、manifest 全缺 |
| 2 | `executor_retirement` 退役被显式解除 | 未做,`is_retired("hicode")` 仍为 True |
| 3 | `provider-registry.json` 的 `retired: true` 更新 | 未做,写明 "Do not re-admit." |
| 4 | SF-CRED 已落地 | **已满足**(`c6bee266`/`fc10f879`/`2f56c73d`/`91f77a69`);但见 §3 的适用性疑问 |
| 5 | `hicode-entrypoint.sh` 不再引用已删模块 | 未做,仍调 `server.hicode_runtime` |
| 6 | 命名空间裁决 | **未做,且更复杂** —— 见 §9 |

**结论:STOP。前置 1/2/3/5/6 未满足,不推进 registration。**

## 9. 新增阻塞项(P0 发现,spec 未列)

| # | 新阻塞 | 说明 |
|---|---|---|
| 7 | credential 模型可能不适用 | 旧设计无凭据路径,`authenticated` 由 provider/model 映射决定。SF-CRED 的 `credential_present`/`credential_valid` 对一个"无凭据注入"的 executor 语义不明 |
| 8 | 与 antigravity 共用上游 | 映射指向 `internal_provider: antigravity`。同时复活两者会共享同一上游配额与故障域,failover 独立性存疑 |
| 9 | `executor_kind` 命名空间 | 旧值 `internal_hicode` 与现行 `l1_worker` 是不同层。新 canonical registry 只有 `l1_worker`。归入哪一层需要裁决,不能默认按 L1 处理 |
| 10 | `EMPTY_MODEL_RESPONSE` 判定丢失 | 空响应检测逻辑随 `legacy/` 归档,枚举成员变为死代码 |
| 11 | 4 个写/执行工具无 effect 声明 | `hicode_run`/`rollback`/`review`/`stop` 若沿用 `side_effect=None` 会默认放行,与 SF-001 同源。7 个工具中仅 3 个有声明 |

## 10. Deliverables

```text
docs/reports/HICODE_REACTIVATION_AUDIT.md   本文件
```

未产出实现代码 —— spec P0 明确"不得改源码",前置 1/2/3/5/6 未满足,
按 §3 末尾"任何一条不满足 → STOP 并报告阻塞点"。