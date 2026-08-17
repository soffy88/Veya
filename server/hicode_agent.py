"""server.hicode_agent — Hicode 编码执行器集成装配层。

把 Hicode (Go 编码 Agent: 独立 planner / executor / sandbox / checkpoint,
单二进制, MIT) 作为 veya 主脑的"代码动手执行器"接入:

- hicode_run   : 在隔离 workspace 里执行编程任务 (写/改代码、修 bug、跑测试)
- hicode_status: 二进制 / workspace / 模型可用性诊断

3O 铁律: 机制 (hicode run 子进程协议) 在此装配层; 主脑只做路由决策
(系统提示 SOP 见 coordinator_master._HOST_SOP_APPEND)。

安全:
- workspace 限定 HICODE_WORKSPACE (默认 ~/.veya/hicode-workspace),
  工具参数里的绝对路径必须位于根内, 防逃逸;
- --auto 自动放行权限询问 (Hicode 自身有 sandbox / checkpoint / 循环守卫);
- 二进制缺失时优雅降级 (工具返回安装指引, 不阻塞服务启动)。

Provider: 复用 veya 本地 opencode 网关 (127.0.0.1:10100/v1, OpenAI 兼容,
免鉴权, 模型 gpt-5.6-luna)。配置在 ~/.reasonix/config.toml (外部 reasonix CLI 契约, 不改名)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

logger = logging.getLogger("hicode")

# ── 配置 (env 可覆盖) ─────────────────────────────────────────────────
DEFAULT_BIN_HINT = os.environ.get("HICODE_BIN", "")  # 显式指定二进制; 空 = 自动解析 (PATH → ~/.nvm)
DEFAULT_WORKSPACE = os.environ.get(
    "HICODE_WORKSPACE", str(Path.home() / ".veya" / "hicode-workspace")
)
DEFAULT_MODEL = os.environ.get("HICODE_MODEL", "luna")
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


def _resolve_bin() -> str:
    if DEFAULT_BIN_HINT:
        p = Path(DEFAULT_BIN_HINT)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        raise HicodeUnavailable(f"HICODE_BIN 指向的二进制不可执行: {p}")
    found = shutil.which("reasonix")  # 外部 CLI 仍名 reasonix (hicode = veya 侧名称)
    if found:
        return found
    # systemd 服务 PATH 可能不含 nvm bin → 兜底扫描
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        candidates = sorted(
            (d / "bin" / "reasonix" for d in nvm_root.iterdir() if d.is_dir()),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for c in candidates:
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)
    raise HicodeUnavailable(
        "hicode (reasonix CLI) 未安装。安装: npm i -g @reasonix/cli "
        "(或设置 HICODE_BIN 指向二进制)。"
    )


def _bin_version() -> str | None:
    try:
        import subprocess

        r = subprocess.run(
            [_resolve_bin(), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (r.stdout or r.stderr).strip().splitlines()[-1] or None
    except Exception:
        return None


def _workspace_root() -> Path:
    return Path(DEFAULT_WORKSPACE).expanduser().resolve()


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
) -> dict[str, Any]:
    """执行一次 hicode 子进程, 流式解析 stream-json 事件, 返回最终结果对象。

    --output-format stream-json 每行一个 JSON: 中间是 kind 事件 (turn_started /
    tool_dispatch / tool_result / usage), 结尾是 {"type":"result", ...}。
    中间事件逐行实时回调 on_event (→ SSE 进度); 不脱敏, 含真实工具名/参数。
    stdout 逐行读 (不缓冲), stderr 并发收集仅作报错尾部。
    """
    bin_path = _resolve_bin()
    workspace.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    _ensure_local_proxy()
    time.sleep(0.3)  # 代理首次启动等待
    cmd = [
        bin_path,
        "run",
        *args,
        "--output-format",
        "stream-json",
        "--model",
        DEFAULT_MODEL,
        "--auto",
        "--dir",
        str(workspace),
    ]
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
    )
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
            if ev.get("type") == "result":
                result = ev
                break
            _emit_event(ev, on_event)
    except asyncio.TimeoutError:
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
    if result is None:
        tail = "".join(stderr_lines)[-1500:] or "(无 stderr)"
        raise HicodeUnavailable(f"hicode 无结构化结果 (exit={proc.returncode}):\n{tail}")
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
    except Exception:  # noqa: BLE001
        return None
    if cb is None:
        return None

    def _bridge(ev: dict) -> None:
        try:
            cb(
                {
                    "type": "hicode_progress",
                    "stage": ev.get("stage"),
                    "tool": ev.get("tool"),
                    "detail": ev.get("detail"),
                }
            )
        except Exception:  # noqa: BLE001 — SSE 推送失败绝不拖垮任务
            pass

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
    # 新编程任务 → 优先 hicode serve (独立 oservi, HTTP+SSE 进度回流);
    # serve 不可达/失败 → 回退 CLI (功能等价, 含 checkpoint/续做/回滚)。
    # 续做/恢复仍走 CLI (会话状态在 workspace)。force_cli 时也直接跳过。
    if not force_cli and not continue_ and not session_id:
        try:
            from server.coordinator_master import (
                _build_hicode_spec,
                _format_hicode_result,
            )
            from server.hicode_serve import get_serve_client

            client = get_serve_client()
            if await client.health():
                ws0 = _resolve_workspace(workspace)
                _snapshot_workspace(ws0, task)  # checkpoint (回滚可用)
                res = await client.run_task(
                    _build_hicode_spec(task),
                    on_event=on_event,
                    timeout=timeout_sec or 900,
                    workspace=str(ws0),
                )
                if res.get("status") != "error":
                    return _format_hicode_result(res)
        except Exception as exc:  # noqa: BLE001 — serve 路径失败 → CLI 兜底
            logger.info("hicode serve 不可用, 回退 CLI: %s", exc)

    # ── CLI 路径 (续做 / serve 不可达时的兜底) ──
    try:
        ws = _resolve_workspace(workspace)
    except ValueError as e:
        return f"错误: {e}"
    try:
        _resolve_bin()  # 提前失败给出安装指引
    except HicodeUnavailable as e:
        return f"hicode 不可用: {e}"

    args = ["--max-steps", str(max_steps or DEFAULT_MAX_STEPS)]
    timeout = timeout_sec or DEFAULT_TIMEOUT_SEC
    if workspace:
        args += ["--add-dir", str(ws)]
    args.append(task)
    # 任务前 git 快照 (checkpoint) — 失败不阻塞执行 (无 git 时回滚不可用)
    checkpoint = _snapshot_workspace(ws, task)
    try:
        r = await _run_hicode(
            args,
            workspace=ws,
            timeout=timeout,
            on_event=on_event,
            continue_=continue_,
            resume_id=session_id,
        )
    except HicodeUnavailable as e:
        return f"hicode 执行失败: {e}"
    except Exception as e:  # noqa: BLE001 — 工具边界兜底, 回喂主脑
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
    except Exception:  # noqa: BLE001 — 快照失败不阻塞执行
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
        if not (ws / ".git").exists():
            return "工作区还没有 git 快照 (没有执行过任务)。"
        target = ref or "HEAD~1"
        r = _git(ws, "rev-parse", "--verify", target)
        if r.returncode != 0:
            return f"找不到回滚目标 {target!r}。"
        target_hash = r.stdout.strip()
        before = _git(ws, "rev-parse", "HEAD").stdout.strip()[:12]
        _git(ws, "reset", "--hard", target_hash)
        after = _git(ws, "rev-parse", "HEAD").stdout.strip()[:12]
        return (
            f"✅ 已回滚 {ws} 到 {target_hash[:12]} (此前 HEAD={before}).\n"
            f"工作区文件已恢复到任务前状态。"
        )
    except Exception as e:  # noqa: BLE001
        return f"回滚失败: {e}"


# ── 会话感知 (供 Stop 端点定位当前会话的 hicode 任务) ──────────────


def _current_sid() -> str | None:
    """从 contextvar 读当前 SSE 会话 id (on_step 是 SSEQueue 的 bound method)。"""
    try:
        from server.events import _on_step_ctx

        cb = _on_step_ctx.get()
        q = getattr(cb, "__self__", None)
        return getattr(q, "sid", None) or None
    except Exception:  # noqa: BLE001
        return None


def _register_session_task(tid: str) -> None:
    """把任务 id 关联到当前会话 (Stop 端点据此真正停止)。"""
    sid = _current_sid()
    if sid:
        try:
            from server.coordinator_master import _session_task

            _session_task[sid] = tid
        except Exception:  # noqa: BLE001
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

    新任务 → 后台任务队列 (可停止/断线不丢, 结果留在队列可查);
    续做 (continue_=True) / 恢复 (session_id) → 直接 CLI 执行。
    on_event 用于实时进度回调。
    """
    on_event = _ensure_on_event(on_event)
    # 新任务按需附带 Graft 地图 + 历史规则 (不是每轮预注入; 续做不再扫盘)
    if not continue_ and not session_id:
        try:
            from server.graft_autocontext import attach_to_task

            task = attach_to_task(task)
        except Exception:  # noqa: BLE001 — 上下文装配失败不挡编码
            pass
    # 续做/恢复: 会话状态在 workspace, 走 CLI 同步执行 (低频管理操作)
    if continue_ or session_id:
        return await _execute_hicode_core(
            task,
            workspace=workspace,
            max_steps=max_steps,
            timeout_sec=timeout_sec,
            session_id=session_id,
            continue_=continue_,
            on_event=on_event,
        )
    # 新任务 → 后台队列: 并发提交/串行执行/可停止/断线不丢
    from server.hicode_queue import hicode_task_queue

    tid = await hicode_task_queue.submit(
        task,
        workspace=workspace,
        meta={"timeout_sec": timeout_sec or 900, "sid": _current_sid()},
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
    return rec.summary or f"任务 #{tid} 已完成 (无摘要)。"


async def hicode_sessions(limit: int = 8) -> str:
    """列出最近 hicode 会话 (可续做 / 查看 checkpoint)。"""
    try:
        bin_path = _resolve_bin()
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
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=30)
        data = json.loads((out_b or b"").decode("utf-8", "replace") or "{}")
    except Exception as e:  # noqa: BLE001
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
    """诊断 hicode 执行器可用性 (二进制 / workspace / 模型)。"""
    try:
        bin_path = _resolve_bin()
        version = _bin_version() or "?"
        root = _workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        return (
            "✅ hicode 可用\n"
            f"  二进制: {bin_path}\n"
            f"  版本: {version}\n"
            f"  workspace: {root}\n"
            f"  模型: {DEFAULT_MODEL} (本地网关; 容器内经反代 127.0.0.1:{_PROXY_PORT} 改写 Host)\n"
            f"  最大步数: {DEFAULT_MAX_STEPS or '自动'}, 超时: {DEFAULT_TIMEOUT_SEC}s"
        )
    except HicodeUnavailable as e:
        return f"hicode 不可用: {e}"


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
    except (ValueError, HicodeUnavailable) as e:
        return f"错误: {e}"
    ws.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    _ensure_local_proxy()
    cmd = [bin_path, "review", "--model", DEFAULT_MODEL]
    if commit:
        cmd += ["--commit", commit]
    elif base and base != "HEAD":
        cmd += ["--base", base]
    if instructions:
        cmd += ["--instructions", instructions]
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
    except asyncio.TimeoutError:
        return f"评审超时 ({timeout_sec}s), 可加大 timeout_sec 重试"
    except Exception as exc:  # noqa: BLE001
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
    from server.tool_registry import master_tools

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
    for name, desc, params, func, limit in tools:
        if master_tools.has(name):
            continue
        master_tools.register(name, desc, params, func, max_result_chars=limit)
        added += 1
    if added:
        logger.info("wire hicode: 注册 %d 个工具 (bin=%s)", added, _resolve_bin())
    return added
