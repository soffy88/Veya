"""server.hicode_agent — Veya's HiCode coding-executor integration.

HiCode is the Veya product name for this integration.  The managed runtime is
Reasonix, an external MIT-licensed executable.  The Veya-owned adapter in
``server.hicode_runtime`` owns discovery, version health, configuration, and
the child-process boundary; this module owns Veya task/event orchestration.

- hicode_run   : 在隔离 workspace 里执行编程任务 (写/改代码、修 bug、跑测试)
- hicode_status: managed runtime / workspace / model availability diagnostic

3O 铁律: 机制 (hicode run 子进程协议) 在此装配层; 主脑只做路由决策
(系统提示 SOP 见 coordinator_master._HOST_SOP_APPEND)。

安全:
- workspace 限定 HICODE_WORKSPACE (默认 ~/.veya/hicode-workspace),
  工具参数里的绝对路径必须位于根内, 防逃逸;
- --auto 自动放行权限询问 (Reasonix 自身有 sandbox / checkpoint / 循环守卫);
- managed runtime 缺失或版本不匹配时 fail closed (工具返回明确诊断)。

Provider/model: resolved by the canonical ExecutorRegistry from the managed
runtime configuration. 配置由 Veya adapter 生成在 Veya runtime data root；不依赖用户全局
~/.reasonix 配置。Reasonix remains the underlying
MIT runtime; it is not claimed as Veya-native or fully internalized.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from server import exec_process
from server.hicode_cooldown import classify_upstream_failure, record_cooldown
from server.hicode_host_boundary import hicode_host_gate
from server.hicode_runtime import HicodeRuntimeError, get_hicode_executor
from server.process_guard import executor_spawn_kwargs
from veya.remote.executor_registry import get_executor_registry

logger = logging.getLogger("hicode")

# ── 配置 (env 可覆盖) ─────────────────────────────────────────────────
DEFAULT_WORKSPACE = os.environ.get(
    "HICODE_WORKSPACE", str(Path.home() / ".veya" / "hicode-workspace")
)
DEFAULT_MAX_STEPS = int(os.environ.get("HICODE_MAX_STEPS", "0"))  # 0 = 自动
DEFAULT_TIMEOUT_SEC = int(os.environ.get("HICODE_TIMEOUT_SEC", "1800"))
# 本地网关免鉴权 (10101: 无 Authorization 放行, 假 key 反而 403) —
# 不注入任何 api_key_env 占位值, Hicode 将不发 Authorization 头。
# 若将来 provider 配了真实 api_key_env, 环境变量自然透传。

# ── 容器内反代 (opencodex 按 Host 头白名单校验) ─────────────────────
# 容器访问宿主网关 192.168.16.1:10101 时 Host=192.168.16.1:10101 被拒
# (origin_rejected); 只有 Host=127.0.0.1:10100 放行。hicode 无法覆盖
# Host 头 → 在容器内起本地代理 127.0.0.1:HICODE_PROXY_PORT, 转发时
# 强制改写 Host。宿主环境 (base_url 直连 127.0.0.1:10100) 不起代理。
_PROXY_PORT = int(os.environ.get("HICODE_PROXY_PORT", "10103"))
_PROXY_UPSTREAM = os.environ.get("HICODE_PROXY_UPSTREAM", "http://192.168.16.1:10101")
_PROXY_UPSTREAM_HOST = os.environ.get("HICODE_PROXY_UPSTREAM_HOST", "127.0.0.1:10100")

_proxy_server: Any | None = None
_BOUND_WORKSPACE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "hicode_bound_workspace", default=None
)
_BOUND_EXECUTION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hicode_bound_execution_id", default=None
)


def _hicode_identity() -> tuple[str, str]:
    identity = get_executor_registry().identity("hicode")
    return identity.provider or "unknown", identity.model or "unknown"


@contextlib.contextmanager
def bound_hicode_workspace(workspace: str | Path):
    """Temporarily bind Hicode's resolver to one validated L1 worktree.

    The ordinary public ``hicode_run`` boundary remains constrained by the
    configured ``HICODE_WORKSPACE`` root.  Internal L1 execution already has a
    session-authorized, isolated worktree, which may be outside that default
    root; a context-local binding lets concurrent children use their own
    validated worktree without mutating process-global environment or policy.
    """

    token = _BOUND_WORKSPACE.set(Path(workspace).expanduser().resolve())
    try:
        yield
    finally:
        _BOUND_WORKSPACE.reset(token)


@contextlib.contextmanager
def bound_hicode_execution_id(execution_id: str):
    """Bind execution metadata without changing the Hicode call signature."""

    token = _BOUND_EXECUTION_ID.set(str(execution_id))
    try:
        yield
    finally:
        _BOUND_EXECUTION_ID.reset(token)


def _ensure_local_proxy() -> None:
    """惰性启动容器内反代 (幂等)。宿主环境不启动。"""
    global _proxy_server
    if _proxy_server is not None:
        return
    if not os.environ.get("HICODE_PROXY"):
        return
    import threading

    app = FastAPI()

    async def _proxy(request: Request) -> Response:
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
        }
        headers["Host"] = _PROXY_UPSTREAM_HOST
        client = httpx.AsyncClient(base_url=_PROXY_UPSTREAM, timeout=None)
        try:
            r = await client.request(
                request.method,
                request.url.path,
                content=body,
                headers=headers,
            )
            return Response(
                content=r.content,
                status_code=r.status_code,
                headers={
                    k: v
                    for k, v in r.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
                },
            )
        finally:
            await client.aclose()

    app.add_api_route(
        "/{path:path}", _proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    )
    cfg = uvicorn.Config(app, host="127.0.0.1", port=_PROXY_PORT, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    _proxy_server = server
    logger.info(
        "hicode local proxy on 127.0.0.1:%s → %s (Host=%s)",
        _PROXY_PORT,
        _PROXY_UPSTREAM,
        _PROXY_UPSTREAM_HOST,
    )


class HicodeUnavailable(RuntimeError):
    """二进制缺失或不可执行 (主脑应看到可操作的降级提示)。"""


class HicodeExecutionError(HicodeUnavailable):
    """Typed provider/runtime failure with bounded raw evidence."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        raw_evidence: dict[str, Any] | None = None,
        failure_class: str | None = None,
        retryable_immediately: bool | None = None,
        retry_not_before: float | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = str(code)
        self.detail = str(detail)
        self.raw_evidence = dict(raw_evidence or {})
        self.failure_class = failure_class or code
        self.retryable_immediately = retryable_immediately
        self.retry_not_before = retry_not_before


def _safe_failure_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<max-depth>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if any(
                secret in name.lower()
                for secret in ("authorization", "api_key", "apikey", "password", "secret", "token")
            ):
                out[name] = "<redacted>"
            else:
                out[name] = _safe_failure_value(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_safe_failure_value(item, depth=depth + 1) for item in value[-20:]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _empty_assistant_reply(value: Any) -> bool:
    if isinstance(value, dict):
        role = str(value.get("role") or "").lower()
        if role == "assistant" and value.get("content") is None and value.get("tool_calls") == []:
            return True
        return any(_empty_assistant_reply(item) for item in value.values())
    if isinstance(value, list):
        return any(_empty_assistant_reply(item) for item in value)
    return False


def _structured_failure_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Return typed failure metadata only for explicit structured failure signals."""

    kind = str(ev.get("kind") or ev.get("type") or "").strip().lower()
    explicit_kinds = {
        "error",
        "provider_error",
        "model_error",
        "round_error",
        "turn_error",
        "round_failed",
        "turn_failed",
    }
    if ev.get("is_error") is not True and kind not in explicit_kinds:
        return None
    raw_error = ev.get("error")
    if isinstance(raw_error, dict):
        code = raw_error.get("code") or ev.get("code")
        detail = raw_error.get("message") or raw_error.get("detail")
    else:
        code = ev.get("code")
        detail = raw_error
    code_text = str(code or "HICODE_PROVIDER_ROUND_FAILURE")
    detail_text = str(detail or ev.get("message") or code_text)[:4000]
    round_index = None
    for key in ("round_index", "round", "turn", "turn_index"):
        value = ev.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            round_index = value
            break
    return {
        "code": code_text,
        "detail": detail_text,
        "round_index": round_index,
        "raw_evidence": _safe_failure_value(ev),
    }


def _hicode_result_error(
    result: dict[str, Any] | None,
    *,
    raw_events: list[dict[str, Any]],
    stderr_tail: str,
    exit_code: int | None,
) -> HicodeExecutionError | None:
    """Classify one Reasonix terminal result without using assistant narration."""

    evidence = {
        "result": _safe_failure_value(result),
        "recent_events": raw_events[-10:],
        "stderr_tail": str(stderr_tail)[-2000:],
        "exit_code": exit_code,
    }
    quota = classify_upstream_failure({"result": result, "stderr": stderr_tail})
    if quota is not None:
        record_cooldown(quota)
        return HicodeExecutionError(
            "MODEL_COOLDOWN",
            "Hicode model quota exhausted; caller must wait before retrying",
            raw_evidence={
                **evidence,
                "failure_class": quota.failure_class,
                "provider": quota.provider,
                "model": quota.model,
                "upstream_evidence": quota.upstream_evidence,
                "upstream_reset_seconds": quota.upstream_reset_seconds,
                "effective_retry_not_before": quota.cooldown_until,
            },
            failure_class=quota.failure_class,
            retryable_immediately=False,
            retry_not_before=quota.cooldown_until,
        )
    if result is None:
        return HicodeExecutionError(
            "HICODE_NO_STRUCTURED_RESULT",
            f"hicode returned no structured result (exit={exit_code})",
            raw_evidence=evidence,
        )
    raw_tool_calls = result.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        tool_call_count = len(raw_tool_calls)
    else:
        try:
            tool_call_count = int(raw_tool_calls or 0)
        except (TypeError, ValueError):
            tool_call_count = 0
    try:
        model_request_count = int(result.get("model_requests") or result.get("num_turns") or 0)
    except (TypeError, ValueError):
        model_request_count = 0
    body = result.get("result")
    has_empty_assistant = _empty_assistant_reply(result) or any(
        _empty_assistant_reply(event) for event in raw_events
    )
    semantically_empty = (
        not str(body or "").strip() and tool_call_count == 0 and model_request_count > 0
    )
    if result.get("subtype") != "managed_bootstrap" and (has_empty_assistant or semantically_empty):
        return HicodeExecutionError(
            "EMPTY_MODEL_RESPONSE",
            "provider returned an empty assistant response with no tool calls",
            raw_evidence=evidence,
        )
    if bool(result.get("is_error")):
        detail = str(
            result.get("error")
            or result.get("result")
            or result.get("subtype")
            or "Hicode provider error"
        )[:4000]
        return HicodeExecutionError("HICODE_PROVIDER_ERROR", detail, raw_evidence=evidence)
    return None


def _resolve_bin() -> str:
    try:
        return get_hicode_executor().resolve_binary()
    except HicodeRuntimeError as exc:
        raise HicodeUnavailable(str(exc)) from exc


def _bin_version() -> str | None:
    try:
        return get_hicode_executor().probe_version(_resolve_bin())
    except Exception:
        return None


def _sandbox_profile() -> str:
    """Read the host profile without importing any 3O package."""

    raw = os.environ.get("VEYA_SANDBOX_PROFILE", "local").strip().lower()
    return "hosted" if raw in {"hosted", "host", "cloud", "prod"} else "local"


def _current_owner_id() -> str:
    try:
        from server.auth import current_user

        return str(current_user().get("user_id") or "anonymous")
    except Exception:
        return "anonymous"


def _safe_owner_segment(owner_id: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in owner_id)
    return cleaned or "anonymous"


def _workspace_root() -> Path:
    bound = _BOUND_WORKSPACE.get()
    if bound is not None:
        return bound
    root = Path(DEFAULT_WORKSPACE).expanduser().resolve()
    if _sandbox_profile() == "hosted":
        return (root / "users" / _safe_owner_segment(_current_owner_id())).resolve()
    return root


def _resolve_workspace(name_or_path: str | None) -> Path:
    """解析工具参数里的 workspace → 根内绝对路径 (防逃逸)。"""
    root = _workspace_root()
    if not name_or_path:
        return root
    p = Path(name_or_path).expanduser()
    if p.is_absolute():
        rp = p.resolve()
        if rp != root and root not in rp.parents:
            raise ValueError(f"workspace 必须位于 HICODE_WORKSPACE 内 ({root}); 收到: {rp}")
        return rp
    # 相对名 → 根下的子目录
    return (root / name_or_path).resolve()


async def _run_hicode(
    args: list[str],
    *,
    workspace: Path,
    timeout: int,
    on_event: Callable[[dict], None] | None = None,
    continue_: bool = False,
    resume_id: str | None = None,
    on_process: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """执行一次 hicode 子进程, 流式解析 stream-json 事件, 返回最终结果对象。

    --output-format stream-json 每行一个 JSON: 中间是 kind 事件 (turn_started /
    tool_dispatch / tool_result / usage), 结尾是 {"type":"result", ...}。
    中间事件逐行实时回调 on_event (→ SSE 进度); 不脱敏, 含真实工具名/参数。
    stdout 逐行读 (不缓冲), stderr 并发收集仅作报错尾部。
    """
    bin_path = _resolve_bin()
    try:
        runtime = get_hicode_executor()
        runtime.ensure_runtime_preflight()
        env = runtime.execution_environment()
        runtime.run_managed_bootstrap(
            request={
                "execution_id": _BOUND_EXECUTION_ID.get() or "",
                "workspace": str(workspace),
                "objective": " ".join(args[-1:])[:2000],
                "model": _hicode_identity()[1],
                "provider": _hicode_identity()[0],
                "context": {"bootstrap": "pre-model-request"},
            }
        )
        if os.environ.get("HICODE_BOOTSTRAP_ONLY", "").strip() == "1":
            return {
                "subtype": "managed_bootstrap",
                "is_error": False,
                "result": "managed Hicode bootstrap ready",
                "num_turns": 0,
                "model_requests": 0,
                "tool_calls": 0,
            }
    except HicodeRuntimeError as exc:
        raise HicodeUnavailable(str(exc)) from exc
    workspace.mkdir(parents=True, exist_ok=True)
    _ensure_local_proxy()
    time.sleep(0.3)  # 代理首次启动等待
    try:
        cmd = runtime.run_command(
            args,
            model=_hicode_identity()[1],
            timeout=timeout,
            executable=bin_path,
        )
    except HicodeRuntimeError as exc:
        raise HicodeUnavailable(str(exc)) from exc
    cmd.extend(["--dir", str(workspace)])
    if continue_:
        cmd.append("--continue")
    if resume_id:
        cmd.append(f"--resume={resume_id}")
    logger.info("hicode cmd: %s", " ".join(cmd[:6]) + " ...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        **executor_spawn_kwargs(),
    )
    exec_process.record_current(os.environ.get(exec_process.PIDFILE_ENV, ""), proc, str(workspace))
    if on_process is not None:
        with contextlib.suppress(OSError):
            on_process(int(proc.pid), int(os.getpgid(proc.pid) or proc.pid))
    stderr_lines: list[str] = []

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_lines.append(line.decode("utf-8", "replace"))

    stderr_task = asyncio.create_task(_drain_stderr())
    result: dict[str, Any] | None = None
    raw_events: list[dict[str, Any]] = []
    try:
        assert proc.stdout is not None
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                ev = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                raw_events.append(_safe_failure_value(ev))
                del raw_events[: max(0, len(raw_events) - 20)]
            if ev.get("type") == "result":
                result = ev
                break
            _emit_event(ev, on_event)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HicodeUnavailable(
            f"hicode 执行超过 {timeout}s 被终止 (可调 timeout_sec / max_steps)。"
        )
    finally:
        stderr_task.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    error = _hicode_result_error(
        result,
        raw_events=raw_events,
        stderr_tail="".join(stderr_lines),
        exit_code=proc.returncode,
    )
    if error is not None:
        raise error
    assert result is not None
    return result


def _tool_brief(name: str, args: dict) -> str:
    """工具调用摘要 (进度徽章用, 截断防 SSE 帧膨胀)。"""
    try:
        if name in ("write_file", "create_file", "edit_file", "patch"):
            p = args.get("path") or args.get("file_path") or ""
            fn = Path(p).name or p
            content = args.get("content") or ""
            return f"写入 {fn}" + (f" ({len(str(content))}B)" if content else "")
        if name in ("bash", "terminal", "run_command", "run"):
            return f"运行: {str(args.get('command') or args.get('cmd') or '')[:80]}"
        if name in ("search", "grep", "glob"):
            return f"搜索: {str(args.get('query') or args.get('pattern') or args.get('path') or '')[:60]}"
        if name in ("read", "read_file"):
            return f"读 {Path(str(args.get('path') or '')).name}"
        if name in ("ls", "list_dir", "list_directory"):
            return f"列表: {str(args.get('path') or '.')[:60]}"
        brief = json.dumps(args, ensure_ascii=False)
        return f"{name}: {brief[:80]}"
    except Exception:
        return name


def _emit_event(ev: dict, on_event: Callable[[dict], None] | None) -> None:
    """stream-json 中间事件 → 精简进度事件 (→ SSE hicode_progress)。"""
    if on_event is None:
        return
    failure = _structured_failure_event(ev)
    if failure is not None:
        on_event(
            {
                "stage": "provider_failure",
                "tool": None,
                "detail": failure["detail"],
                "code": failure["code"],
                "round_index": failure["round_index"],
                "raw_evidence": failure["raw_evidence"],
            }
        )
        return
    kind = ev.get("kind")
    if kind == "turn_started":
        on_event({"stage": "planning", "tool": None, "detail": "Hicode 规划中…"})
    elif kind == "tool_dispatch":
        tool = ev.get("tool") or {}
        # partial=true 的 dispatch 只是意图预告 (args 为空) — 等完整 args 事件
        if tool.get("partial"):
            return
        name = str(tool.get("name") or "tool")
        args = tool.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args[:60]}
        on_event({"stage": "executing", "tool": name, "detail": _tool_brief(name, args)})
    elif kind == "tool_result":
        tool = ev.get("tool") or {}
        name = str(tool.get("name") or "tool")
        ms = tool.get("durationMs")
        on_event(
            {
                "stage": "executing",
                "tool": name,
                "detail": f"{name} 完成" + (f" ({ms}ms)" if ms else ""),
            }
        )
    elif kind == "usage":
        u = ev.get("usage") or {}
        pt, ct = u.get("promptTokens"), u.get("completionTokens")
        if pt or ct:
            on_event({"stage": "stats", "tool": None, "detail": f"tokens: in={pt} out={ct}"})


# ── 工具实现 ──────────────────────────────────────────────────────────


def _ensure_on_event(
    on_event: Callable[[dict], None] | None,
) -> Callable[[dict], None] | None:
    """on_event 为空时, 自动桥接当前 SSE 请求上下文 (fire_step → hicode_progress)。

    模型调用 hicode_run 时无法传入 on_event (工具 schema 无此参数);
    此处从 contextvar 取当前请求的 on_step (SSE 队列), 把 hicode 进度事件
    包装成前端可渲染的 {"type": "hicode_progress", stage/tool/detail} 实时发出。
    无请求上下文 (后台/CLI 直调) 时保持 None (行为不变)。
    """
    if on_event is not None:
        return on_event
    try:
        from server.events import _on_step_ctx

        cb = _on_step_ctx.get()
    except Exception:
        return None
    if cb is None:
        return None

    def _bridge(ev: dict) -> None:
        with contextlib.suppress(Exception):
            cb(
                {
                    "type": "hicode_progress",
                    "stage": ev.get("stage"),
                    "tool": ev.get("tool"),
                    "detail": ev.get("detail"),
                }
            )

    return _bridge


async def _execute_hicode_core(
    task: str,
    workspace: str | None = None,
    max_steps: int = 0,
    timeout_sec: int = 0,
    session_id: str | None = None,
    continue_: bool = False,
    on_event: Callable[[dict], None] | None = None,
    force_cli: bool = False,
    on_process: Callable[[int, int], None] | None = None,
) -> str:
    """真正执行一个 hicode 编程任务 (默认 serve 优先, CLI 兜底)。

    continue_=True → --continue (接着上次未完成会话); session_id 指定 →
    --resume=<machine id> (恢复历史会话, 见 hicode_sessions)。任务前自动
    打 git 快照 (checkpoint) → 可用 hicode_rollback 回滚。
    on_event 用于实时进度回调。

    force_cli=True → 跳过 hicode serve, 强制走 CLI 路径。原因: serve 是
    单一持久会话 (HicodeServeClient.submit 不带 workspace 参数), 传入的
    workspace 只用于任务前 git 快照, 不会真正约束执行发生的目录 —— 多项目
    场景 (如 project_ask) 必须走 CLI (`--add-dir <workspace>`) 才能保证
    改动真的落在调用方指定的目录内 (2026-08-15 真机 smoke 验证发现)。
    """
    return await _execute_hicode_core_inner(
        task,
        workspace=workspace,
        max_steps=max_steps,
        timeout_sec=timeout_sec,
        session_id=session_id,
        continue_=continue_,
        on_event=on_event,
        force_cli=force_cli,
        on_process=on_process,
    )


def _build_hicode_spec(user_prompt: str) -> str:
    """Build the Hicode task envelope without importing the Veya main brain."""

    return (
        "# 任务\n"
        f"{user_prompt.strip()}\n\n"
        "# 执行规范\n"
        "1. 在隔离工作区完成, 只改动完成任务所需的最小文件集。\n"
        "2. 优先交付可运行代码; 写完后必须实际运行验证, 不能只写不跑。\n"
        "3. 完成后报告: 改了哪些文件、运行了什么命令、验证输出是什么。\n"
        "4. 若任务有歧义, 选最合理实现并在报告中说明假设。\n"
    )


def _format_hicode_result(result: dict[str, Any]) -> str:
    """Format a serve result without loading coordinator/3O modules."""

    if result.get("status") == "error":
        return f"⚠ hicode 执行失败: {result.get('error')}"
    body = (result.get("result") or "").strip()
    turns = result.get("turns") or 0
    tools = result.get("tool_calls") or []
    usage = result.get("usage") or {}
    head = f"✅ hicode 执行完成 (轮次={turns}, 工具调用={len(tools)})"
    if usage.get("promptTokens") or usage.get("completionTokens"):
        head += f", in={usage.get('promptTokens', 0)} out={usage.get('completionTokens', 0)}"
    return f"{head}\n{body[:8000]}"


async def _execute_hicode_core_inner(
    task: str,
    workspace: str | None = None,
    max_steps: int = 0,
    timeout_sec: int = 0,
    session_id: str | None = None,
    continue_: bool = False,
    on_event: Callable[[dict], None] | None = None,
    force_cli: bool = False,
    on_process: Callable[[int, int], None] | None = None,
) -> str:
    # 新编程任务 → 优先 hicode serve (独立 oservi, HTTP+SSE 进度回流);
    # serve 不可达/失败 → 回退 CLI (功能等价, 含 checkpoint/续做/回滚)。
    # 续做/恢复仍走 CLI (会话状态在 workspace)。force_cli 时也直接跳过。
    if not force_cli and not continue_ and not session_id:
        try:
            from server.hicode_serve import get_serve_client

            client = get_serve_client()
            if await client.health():
                ws0 = _resolve_workspace(workspace)
                # 短锁: 只护住快照本身, 在调 client.run_task 前释放——run_task
                # 内部会再拿一次同一把 (按路径 key 的) 锁, 顺序 acquire 不是
                # 嵌套, 不会死锁 (phase 互斥: 防跟别的 session 的 CLI 路径撞
                # 同一工作目录的 git 操作)。
                async with hicode_host_gate().async_workspace(str(ws0)):
                    _snapshot_workspace(ws0, task)  # checkpoint (回滚可用)
                res = await client.run_task(
                    _build_hicode_spec(task),
                    on_event=on_event,
                    timeout=timeout_sec or 900,
                    workspace=str(ws0),
                )
                if res.get("status") != "error":
                    return _format_hicode_result(res)
                quota = classify_upstream_failure(res)
                if quota is not None:
                    record_cooldown(quota)
                    raise HicodeExecutionError(
                        "MODEL_COOLDOWN",
                        "Hicode model quota exhausted; caller must wait before retrying",
                        raw_evidence={
                            "failure_class": quota.failure_class,
                            "provider": quota.provider,
                            "model": quota.model,
                            "upstream_evidence": quota.upstream_evidence,
                            "upstream_reset_seconds": quota.upstream_reset_seconds,
                            "effective_retry_not_before": quota.cooldown_until,
                        },
                        failure_class=quota.failure_class,
                        retryable_immediately=False,
                        retry_not_before=quota.cooldown_until,
                    )
        except Exception as exc:
            if (
                isinstance(exc, HicodeExecutionError)
                and exc.failure_class == "UPSTREAM_QUOTA_EXHAUSTED"
            ):
                raise
            logger.info("hicode serve 不可用, 回退 CLI: %s", exc)

    # ── CLI 路径 (续做 / serve 不可达时的兜底) ──
    try:
        ws = _resolve_workspace(workspace)
    except ValueError as e:
        return f"错误: {e}"
    try:
        _resolve_bin()  # 提前失败给出安装指引
    except HicodeUnavailable as e:
        if force_cli:
            raise
        return f"hicode 不可用: {e}"

    args = ["--max-steps", str(max_steps or DEFAULT_MAX_STEPS)]
    timeout = timeout_sec or DEFAULT_TIMEOUT_SEC
    if workspace:
        args += ["--add-dir", str(ws)]
    args.append(task)

    # phase 互斥: 快照+执行整段包在同一把工作区锁里, 防跟别的 session 的 CLI
    # 路径 (或 hicode_rollback) 并发撞同一个工作目录的 git/文件操作。
    async with hicode_host_gate().async_workspace(str(ws)):
        # 任务前 git 快照 (checkpoint) — 失败不阻塞执行 (无 git 时回滚不可用)
        checkpoint = _snapshot_workspace(ws, task)
        try:
            run_kwargs: dict[str, Any] = {
                "workspace": ws,
                "timeout": timeout,
                "on_event": on_event,
                "continue_": continue_,
                "resume_id": session_id,
            }
            if on_process is not None:
                run_kwargs["on_process"] = on_process
            r = await _run_hicode(args, **run_kwargs)
        except HicodeExecutionError:
            raise
        except HicodeUnavailable as e:
            if force_cli:
                raise
            return f"hicode 执行失败: {e}"
        except Exception as e:
            if force_cli:
                raise
            logger.exception("hicode_run unexpected error")
            return f"hicode 执行异常: {e}"

    subtype = r.get("subtype", "unknown")
    ok = not r.get("is_error", False)
    cost = r.get("total_cost", 0)
    currency = r.get("currency", "")
    usage = r.get("usage") or {}
    body = (r.get("result") or "").strip()
    head = "✅ hicode 执行完成" if ok else "⚠ hicode 执行失败"
    extra = []
    if r.get("num_turns") is not None:
        extra.append(f"轮次={r['num_turns']}")
    if cost:
        extra.append(f"成本={cost}{currency}")
    if usage.get("input_tokens") or usage.get("output_tokens"):
        extra.append(f"in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}")
    if r.get("session_id"):
        extra.append(f"session={r['session_id']}")
    meta = f" ({', '.join(extra)})" if extra else ""
    summary = f"{head}{meta} @ {ws}\n{subtype}: {body[:8000]}"
    if len(body) > 8000:
        summary += f"\n\n[截断, 完整输出见 hicode session {r.get('session_id')}]"
    if checkpoint:
        summary += f"\n\n🛟 checkpoint: {checkpoint[:12]} (任务前快照; 回滚: 对我说「回滚最近一次」或 hicode_rollback)"
    if continue_ or session_id:
        summary += "\n[本次为续做/恢复会话]"
    return summary


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ws), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _snapshot_workspace(ws: Path, task: str) -> str | None:
    """任务前 git 快照 (懒 init)。返回 commit hash; 失败返回 None (不阻塞)。"""
    try:
        if not (ws / ".git").exists():
            _git(ws, "init", "-q")
        _git(ws, "add", "-A")
        # 临时 git 身份 (容器/CI 无 user 配置时 commit 也能成功, 不污染全局)
        r = _git(
            ws,
            "-c",
            "user.name=veya-hicode",
            "-c",
            "user.email=veya@local",
            "commit",
            "-q",
            "-m",
            f"pre-task: {task[:80]}",
        )
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            logger.warning("snapshot commit failed: %s", (r.stdout + r.stderr)[-200:])
            return None
        r2 = _git(ws, "rev-parse", "HEAD")
        if r2.returncode != 0:
            return None
        return r2.stdout.strip() or None
    except Exception:
        logger.warning("snapshot failed for %s: git 不可用?", ws, exc_info=True)
        return None


async def hicode_rollback(workspace: str | None = None, ref: str | None = None) -> str:
    """回滚工作区到最近一次任务前快照 (或指定 ref)。

    每次 hicode_run 前自动打 git 快照 (pre-task commit)。默认回滚最近
    一次 (HEAD~1); ref 可指定 commit/hash (见 hicode_sessions 的 checkpoint)。
    """
    try:
        ws = _resolve_workspace(workspace)
    except ValueError as e:
        return f"错误: {e}"
    try:
        # phase 互斥: reset --hard 是破坏性操作, 跟正在跑的 snapshot/run 同一把
        # 工作区锁, 防止冲掉别的会话进行中的改动。
        async with hicode_host_gate().async_workspace(str(ws)):
            if not (ws / ".git").exists():
                return "工作区还没有 git 快照 (没有执行过任务)。"
            target = ref or "HEAD~1"
            r = _git(ws, "rev-parse", "--verify", target)
            if r.returncode != 0:
                return f"找不到回滚目标 {target!r}。"
            target_hash = r.stdout.strip()
            before = _git(ws, "rev-parse", "HEAD").stdout.strip()[:12]
            _git(ws, "reset", "--hard", target_hash)
            return (
                f"✅ 已回滚 {ws} 到 {target_hash[:12]} (此前 HEAD={before}).\n"
                f"工作区文件已恢复到任务前状态。"
            )
    except Exception as e:
        return f"回滚失败: {e}"


# ── 会话感知 (供 Stop 端点定位当前会话的 hicode 任务) ──────────────


def _current_sid() -> str | None:
    """从 contextvar 读当前 SSE 会话 id。

    生产者是 chat_stream 绑定到 _on_step_ctx 的闭包, 会话 id 挂在
    ``veya_session_id`` 上; 旧的 bound-method 形态 (``__self__.sid``) 保留
    为兼容回退。
    """
    try:
        from server.events import _on_step_ctx

        cb = _on_step_ctx.get()
        sid = getattr(cb, "veya_session_id", None)
        if sid:
            return str(sid)
        owner = getattr(cb, "__self__", None)
        return getattr(owner, "sid", None) or None
    except Exception:
        return None


def _register_session_task(tid: str) -> None:
    """把任务 id 关联到当前会话 (Stop 端点据此真正停止)。"""
    sid = _current_sid()
    if sid:
        try:
            from server.coordinator_master import _session_task

            _session_task[sid] = tid
        except Exception:
            pass


async def hicode_run(
    task: str,
    workspace: str | None = None,
    max_steps: int = 0,
    timeout_sec: int = 0,
    session_id: str | None = None,
    continue_: bool = False,
    on_event: Callable[[dict], None] | None = None,
) -> str:
    """执行一个真正的编程任务 (Hicode 编码执行器)。返回执行摘要。

    新任务、续做与历史会话恢复统一进入后台任务队列 → GoalRun。
    队列持久化恢复参数并保留停止/断线恢复语义；CLI 只是 GoalRun 叶子的 provider 路径。
    on_event 用于实时进度回调。
    """
    on_event = _ensure_on_event(on_event)
    # 新任务按需附带 Graft 地图 + 历史规则 (不是每轮预注入; 续做不再扫盘)
    if not continue_ and not session_id:
        try:
            from server.graft_autocontext import attach_to_task

            task = attach_to_task(task)
        except Exception:
            pass
    # 所有新任务/续做/恢复都进入同一 GoalRun 队列；resume 参数作为 durable
    # instruction envelope 的一部分持久化，避免进程重启后语义丢失。
    from server.hicode_queue import hicode_task_queue

    tid = await hicode_task_queue.submit(
        task,
        workspace=workspace,
        meta={
            "timeout_sec": timeout_sec or 900,
            "max_steps": max_steps,
            "session_id": session_id,
            "continue_": continue_,
            "force_cli": bool(continue_ or session_id),
            "sid": _current_sid(),
        },
    )
    _register_session_task(tid)
    try:
        rec = await hicode_task_queue.wait(tid, on_progress=on_event)
    except asyncio.CancelledError:
        # 会话断开/被 Stop → 等待被打断, 但 worker 继续后台执行 (不丢)
        return (
            f"已提交后台任务 #{tid} (继续后台执行中)。"
            f"可查看 hicode_tasks 或说「停止任务 #{tid}」中断。"
        )
    if rec.status == "cancelled":
        return f"任务 #{tid} 已停止 ({rec.error or 'user stop'})。"
    if rec.status == "failed":
        return f"任务 #{tid} 失败: {rec.error or '未知错误'}"
    if rec.status == "blocked":
        return f"任务 #{tid} blocked: {rec.error or rec.summary or 'MODEL_COOLDOWN'}"
    return rec.summary or f"任务 #{tid} 已完成 (无摘要)。"


async def hicode_sessions(limit: int = 8) -> str:
    """列出最近 hicode 会话 (可续做 / 查看 checkpoint)。"""
    try:
        bin_path = _resolve_bin()
        runtime = get_hicode_executor()
        runtime.ensure_runtime_preflight()
        runtime.ensure_compatible(bin_path)
        env = runtime.execution_environment()
        ws = _workspace_root()
        ws.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            bin_path,
            "session",
            "list",
            "--json",
            cwd=str(ws),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out_b, _err_b = await asyncio.wait_for(proc.communicate(), timeout=30)
        data = json.loads((out_b or b"").decode("utf-8", "replace") or "{}")
    except Exception as e:
        return f"无法列出会话: {e}"
    sessions = data.get("sessions", [])
    if not sessions:
        return "暂无 hicode 会话 (执行过编程任务后会有; 对我说「继续上次」可续做)。"
    lines = [f"最近 {min(limit, len(sessions))} 个 hicode 会话:"]
    for s in sessions[:limit]:
        updated = (s.get("updated_at") or "")[:19].replace("T", " ")
        lines.append(
            f"- {s.get('id')}  turns={s.get('turns')} state={s.get('state')} updated={updated}"
        )
    lines.append("续做: 对我说「继续上次」; 指定会话: hicode_run(session_id=<id>)。")
    return "\n".join(lines)


async def hicode_status() -> str:
    """诊断 managed Reasonix / workspace / model (不泄露凭证)。"""
    runtime = get_hicode_executor()
    try:
        fingerprint = runtime.ensure_runtime_preflight()
    except HicodeRuntimeError as exc:
        return f"hicode 不可用: reason={exc}"
    status = runtime.status()
    if not status.healthy:
        return (
            "hicode 不可用: managed_reasonix_available="
            f"{str(status.managed_reasonix_available).lower()} "
            f"version={status.managed_reasonix_version or 'unknown'} "
            f"compatible={str(status.managed_reasonix_compatible).lower()} "
            f"reason={status.error or 'unhealthy'}"
        )
    root = _workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    return (
        "✅ hicode 可用\n"
        f"  managed_reasonix_available: {str(status.managed_reasonix_available).lower()}\n"
        f"  二进制: {status.executable}\n"
        f"  版本: {status.managed_reasonix_version}\n"
        f"  compatible: {str(status.managed_reasonix_compatible).lower()}\n"
        f"  runtime fingerprint: {fingerprint['runtime_fingerprint']}\n"
        f"  runtime config: {status.config_path}\n"
        f"  workspace: {root}\n"
        f"  模型: {_hicode_identity()[1]} (Veya-managed runtime config)\n"
        f"  最大步数: {DEFAULT_MAX_STEPS or '自动'}, 超时: {DEFAULT_TIMEOUT_SEC}s"
    )


# ── AI 代码评审 (CLI review 子命令) ────────────────────────────────


async def hicode_review(
    base: str = "HEAD",
    commit: str = "",
    instructions: str = "",
    workspace: str | None = None,
    timeout_sec: int = 300,
) -> str:
    """对 hicode 工作区最近改动做 AI 代码评审 (Hicode review 子代理)。

    base: 对比基准 ref (默认 HEAD = 评审未提交的 working-tree 改动);
    commit: 评审指定 commit 引入的改动 (与 base 互斥);
    instructions: 附加评审重点 (如「重点看并发与内存泄漏」)。
    返回评审结论纯文本 (问题列表 / 风险 / 建议)。
    """
    try:
        ws = _resolve_workspace(workspace)
        bin_path = _resolve_bin()
        runtime = get_hicode_executor()
        runtime.ensure_runtime_preflight()
        runtime.ensure_compatible(bin_path)
    except (ValueError, HicodeUnavailable, HicodeRuntimeError) as e:
        return f"错误: {e}"
    ws.mkdir(parents=True, exist_ok=True)
    try:
        env = get_hicode_executor().execution_environment()
    except HicodeRuntimeError as exc:
        return f"错误: {exc}"
    _ensure_local_proxy()
    review_args: list[str] = []
    if commit:
        review_args += ["--commit", commit]
    elif base and base != "HEAD":
        review_args += ["--base", base]
    if instructions:
        review_args += ["--instructions", instructions]
    cmd = runtime.review_command(review_args, model=_hicode_identity()[1], executable=bin_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(ws),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        text = out.decode("utf-8", "replace").strip()
        err_txt = err.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not text:
            detail = err_txt or text or f"exit {proc.returncode}"
            return f"评审失败: {detail[:300]}"
        return text
    except TimeoutError:
        return f"评审超时 ({timeout_sec}s), 可加大 timeout_sec 重试"
    except Exception as exc:
        return f"评审失败: {exc}"


async def hicode_tasks(limit: int = 12) -> str:
    """列出 Hicode 后台任务队列 (排队/执行中/完成/取消)。

    编程任务入队后立即返回 task id; 用本工具查看进度与结果。
    用户说「任务停掉/别跑了」时用 hicode_stop 停止指定任务。
    """
    from server.hicode_queue import hicode_task_queue

    tasks = hicode_task_queue.list(limit=limit)
    if not tasks:
        return "Hicode 任务队列为空 (暂无后台任务)。"
    lines = []
    for t in tasks:
        lines.append(
            f"#{t['id']} [{t['status']}] 提交={t['created_at']:.0f} "
            f"{t['summary'][:60] if t['summary'] else ''}"
        )
    return "\n".join(lines)


async def hicode_stop(task_id: str) -> str:
    """停止一个 Hicode 后台任务 (真正中断执行, 不只断前端)。

    执行中任务 → serve /cancel 中断当前 turn; 排队中 → 直接取消。
    """
    from server.hicode_queue import hicode_task_queue

    ok = await hicode_task_queue.stop(task_id)
    if not ok:
        return f"未找到任务 #{task_id} (可能已完成)。"
    rec = hicode_task_queue.get(task_id)
    return f"已请求停止 #{task_id} (状态: {rec.status if rec else '?'})。"


# ── 注册 ──────────────────────────────────────────────────────────────


async def wire_master_tools() -> int:
    """把 hicode 工具注册进 master_tools (幂等)。返回新注册数量。"""
    from server.tool_registry import SideEffect, master_tools

    added = 0
    tools: list[tuple[str, str, dict, Any, int]] = [
        (
            "hicode_run",
            "在隔离编码工作区执行真正的编程任务（写/改代码、修 bug、跑测试、重构、"
            "实现功能、搭建项目）。这是 veya 的编码执行器（Hicode）：它有独立的"
            "规划器/执行器/沙箱/检查点，会自己读代码、改文件、运行命令、验证结果，"
            "完成后返回执行摘要与成本。**需要实际改动代码文件的任务请直接用本工具**，"
            "不要在对话里手搓代码。耗时可能数分钟，属于长任务。纯问答/解释类任务不要用。",
            {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "要完成的编程任务，写明目标与验收标准（如：修复 login.py 的登录失败，跑 pytest 通过）。",
                    },
                    "workspace": {
                        "type": "string",
                        "description": f"可选。工作子目录名或绝对路径（必须位于 {_workspace_root()} 内）。缺省用根工作区。",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "可选。工具调用轮次上限，0=自动（默认）。",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": "可选。超时秒数，默认 1800。",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "可选。恢复指定历史会话（hicode_sessions 列出的 machine id）。与 continue_ 互斥。",
                    },
                    "continue_": {
                        "type": "boolean",
                        "description": "可选。true = 接着上次未完成的会话继续做（跨轮续做）。",
                    },
                },
                "required": ["task"],
            },
            hicode_run,
            20000,
        ),
        (
            "hicode_sessions",
            "列出最近的 hicode 编码会话（id/轮次/状态/更新时间）。跨轮续做或查看历史执行记录时调用；用户说「继续上次」时配合 hicode_run(continue_=true) 使用。",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "可选。返回条数，默认 8。"}
                },
            },
            hicode_sessions,
            4000,
        ),
        (
            "hicode_rollback",
            "回滚 hicode 工作区到最近一次任务前快照（或指定 commit）。每次 hicode_run 前自动打 git 快照；用户说「回滚/撤销最近一次改动」时调用。",
            {
                "type": "object",
                "properties": {
                    "workspace": {
                        "type": "string",
                        "description": f"可选。工作目录（必须位于 {_workspace_root()} 内）。",
                    },
                    "ref": {
                        "type": "string",
                        "description": "可选。回滚目标 commit/ref，默认 HEAD~1（最近一次任务前快照）。",
                    },
                },
            },
            hicode_rollback,
            2000,
        ),
        (
            "hicode_status",
            "诊断 hicode 编码执行器是否可用（二进制/版本/工作区/模型）。执行编程任务前或收到 hicode 不可用提示时可调用。",
            {"type": "object", "properties": {}},
            hicode_status,
            2000,
        ),
        (
            "hicode_review",
            "对 hicode 工作区最近改动做 AI 代码评审（读 diff + 子代理评审，输出问题/风险/建议）。"
            "编程任务完成后、或用户要求「评审/审查一下代码」时调用。",
            {
                "type": "object",
                "properties": {
                    "base": {
                        "type": "string",
                        "description": "可选。对比基准 ref，默认 HEAD（评审未提交的改动）。",
                    },
                    "commit": {
                        "type": "string",
                        "description": "可选。评审指定 commit 引入的改动（与 base 互斥）。",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "可选。附加评审重点，如「重点看并发安全与内存泄漏」。",
                    },
                    "timeout_sec": {"type": "integer", "description": "可选。超时秒数，默认 300。"},
                },
            },
            hicode_review,
            6000,
        ),
        (
            "hicode_tasks",
            "列出 Hicode 后台任务队列（排队/执行中/完成/取消）及摘要。编程任务入队后立即返回 task id，用本工具查询进度/结果。",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "可选。返回条数，默认 12。"}
                },
            },
            hicode_tasks,
            4000,
        ),
        (
            "hicode_stop",
            "停止一个 Hicode 后台任务（真正中断执行，不只断前端连接）。用户说「任务停掉/别跑了/取消」时调用。",
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "任务 id（hicode_tasks 列出的 #id）。",
                    }
                },
                "required": ["task_id"],
            },
            hicode_stop,
            1000,
        ),
    ]
    _read_only = {"hicode_sessions", "hicode_status", "hicode_tasks"}
    for name, desc, params, func, limit in tools:
        if master_tools.has(name):
            continue
        side_effect = SideEffect.PURE_READ if name in _read_only else None
        master_tools.register(
            name, desc, params, func, max_result_chars=limit, side_effect=side_effect
        )
        added += 1
    if added:
        status = get_hicode_executor().status()
        logger.info(
            "wire hicode: 注册 %d 个工具 (managed_reasonix_available=%s version=%s)",
            added,
            status.managed_reasonix_available,
            status.managed_reasonix_version or "unknown",
        )
    return added
