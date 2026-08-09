"""server.reasonix_agent — Reasonix 编码执行器集成装配层。

把 Reasonix (Go 编码 Agent: 独立 planner / executor / sandbox / checkpoint,
单二进制, MIT) 作为 veya 主脑的"代码动手执行器"接入:

- reasonix_run   : 在隔离 workspace 里执行编程任务 (写/改代码、修 bug、跑测试)
- reasonix_status: 二进制 / workspace / 模型可用性诊断

3O 铁律: 机制 (reasonix run 子进程协议) 在此装配层; 主脑只做路由决策
(系统提示 SOP 见 coordinator_master._HOST_SOP_APPEND)。

安全:
- workspace 限定 REASONIX_WORKSPACE (默认 ~/.veya/reasonix-workspace),
  工具参数里的绝对路径必须位于根内, 防逃逸;
- --auto 自动放行权限询问 (Reasonix 自身有 sandbox / checkpoint / 循环守卫);
- 二进制缺失时优雅降级 (工具返回安装指引, 不阻塞服务启动)。

Provider: 复用 veya 本地 opencode 网关 (127.0.0.1:10100/v1, OpenAI 兼容,
免鉴权, 模型 gpt-5.6-luna)。配置在 ~/.reasonix/reasonix.toml。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

logger = logging.getLogger("reasonix")

# ── 配置 (env 可覆盖) ─────────────────────────────────────────────────
DEFAULT_BIN_HINT = os.environ.get(
    "REASONIX_BIN", ""
)  # 显式指定二进制; 空 = 自动解析 (PATH → ~/.nvm)
DEFAULT_WORKSPACE = os.environ.get(
    "REASONIX_WORKSPACE", str(Path.home() / ".veya" / "reasonix-workspace")
)
DEFAULT_MODEL = os.environ.get("REASONIX_MODEL", "luna")
DEFAULT_MAX_STEPS = int(os.environ.get("REASONIX_MAX_STEPS", "0"))  # 0 = 自动
DEFAULT_TIMEOUT_SEC = int(os.environ.get("REASONIX_TIMEOUT_SEC", "1800"))
# 本地网关免鉴权 (10101: 无 Authorization 放行, 假 key 反而 403) —
# 不注入任何 api_key_env 占位值, Reasonix 将不发 Authorization 头。
# 若将来 provider 配了真实 api_key_env, 环境变量自然透传。

# ── 容器内反代 (opencodex 按 Host 头白名单校验) ─────────────────────
# 容器访问宿主网关 192.168.16.1:10101 时 Host=192.168.16.1:10101 被拒
# (origin_rejected); 只有 Host=127.0.0.1:10100 放行。reasonix 无法覆盖
# Host 头 → 在容器内起本地代理 127.0.0.1:REASONIX_PROXY_PORT, 转发时
# 强制改写 Host。宿主环境 (base_url 直连 127.0.0.1:10100) 不起代理。
_PROXY_PORT = int(os.environ.get("REASONIX_PROXY_PORT", "10103"))
_PROXY_UPSTREAM = os.environ.get(
    "REASONIX_PROXY_UPSTREAM", "http://192.168.16.1:10101"
)
_PROXY_UPSTREAM_HOST = os.environ.get("REASONIX_PROXY_UPSTREAM_HOST", "127.0.0.1:10100")

_proxy_server: Any | None = None


def _ensure_local_proxy() -> None:
    """惰性启动容器内反代 (幂等)。宿主环境不启动。"""
    global _proxy_server
    if _proxy_server is not None:
        return
    if not os.environ.get("REASONIX_PROXY"):
        return
    import threading

    app = FastAPI()

    async def _proxy(request: Request) -> Response:
        body = await request.body()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length")}
        headers["Host"] = _PROXY_UPSTREAM_HOST
        client = httpx.AsyncClient(base_url=_PROXY_UPSTREAM, timeout=None)
        try:
            r = await client.request(
                request.method, request.url.path,
                content=body, headers=headers,
            )
            return Response(content=r.content, status_code=r.status_code,
                            headers={k: v for k, v in r.headers.items()
                                     if k.lower() not in ("transfer-encoding",
                                                          "content-encoding",
                                                          "content-length")})
        finally:
            await client.aclose()

    app.add_api_route("/{path:path}", _proxy, methods=["GET", "POST", "PUT",
                                                      "PATCH", "DELETE",
                                                      "OPTIONS", "HEAD"])
    cfg = uvicorn.Config(app, host="127.0.0.1", port=_PROXY_PORT,
                         log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    _proxy_server = server
    logger.info("reasonix local proxy on 127.0.0.1:%s → %s (Host=%s)",
                _PROXY_PORT, _PROXY_UPSTREAM, _PROXY_UPSTREAM_HOST)


class ReasonixUnavailable(RuntimeError):
    """二进制缺失或不可执行 (主脑应看到可操作的降级提示)。"""


def _resolve_bin() -> str:
    if DEFAULT_BIN_HINT:
        p = Path(DEFAULT_BIN_HINT)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        raise ReasonixUnavailable(f"REASONIX_BIN 指向的二进制不可执行: {p}")
    found = shutil.which("reasonix")
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
    raise ReasonixUnavailable(
        "reasonix 未安装。安装: npm i -g reasonix (或设置 REASONIX_BIN 指向二进制)。"
    )


def _bin_version() -> str | None:
    try:
        import subprocess

        r = subprocess.run(
            [_resolve_bin(), "--version"],
            capture_output=True, text=True, timeout=10,
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
            raise ValueError(
                f"workspace 必须位于 REASONIX_WORKSPACE 内 ({root}); 收到: {rp}"
            )
        return rp
    # 相对名 → 根下的子目录
    return (root / name_or_path).resolve()


async def _run_reasonix(
    args: list[str],
    *,
    workspace: Path,
    timeout: int,
) -> dict[str, Any]:
    """执行一次 reasonix 子进程, 解析 --output-format json 的最终结果对象。

    合并 stdout/stderr 逐行扫描, 取最后一个 {"type":"result" 开头的 JSON 行
    (reasonix 可能把 skill warning 打到 stdout, 必须跳过)。
    """
    bin_path = _resolve_bin()
    workspace.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    _ensure_local_proxy()
    time.sleep(0.3)  # 代理首次启动等待
    cmd = [bin_path, "run", *args, "--output-format", "json", "--model", DEFAULT_MODEL,
           "--auto", "--dir", str(workspace)]
    logger.info("reasonix cmd: %s", " ".join(cmd[:6]) + " ...")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(workspace), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ReasonixUnavailable(
            f"reasonix 执行超过 {timeout}s 被终止 (可调 timeout_sec / max_steps)。"
        )
    text = (out_b or b"").decode("utf-8", "replace") + "\n" + (err_b or b"").decode(
        "utf-8", "replace"
    )
    result: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('{"type":"result"'):
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                continue
    if result is None:
        tail = text[-1500:]
        raise ReasonixUnavailable(
            f"reasonix 无结构化结果 (exit={proc.returncode}):\n{tail}"
        )
    return result


# ── 工具实现 ──────────────────────────────────────────────────────────

async def reasonix_run(task: str, workspace: str | None = None,
                       max_steps: int = 0, timeout_sec: int = 0) -> str:
    """执行一个真正的编程任务 (Reasonix 编码执行器)。返回执行摘要。"""
    try:
        ws = _resolve_workspace(workspace)
    except ValueError as e:
        return f"错误: {e}"
    try:
        _resolve_bin()  # 提前失败给出安装指引
    except ReasonixUnavailable as e:
        return f"reasonix 不可用: {e}"

    args = ["--max-steps", str(max_steps or DEFAULT_MAX_STEPS)]
    timeout = timeout_sec or DEFAULT_TIMEOUT_SEC
    if workspace:
        args += ["--add-dir", str(ws)]
    args.append(task)
    try:
        r = await _run_reasonix(args, workspace=ws, timeout=timeout)
    except ReasonixUnavailable as e:
        return f"reasonix 执行失败: {e}"
    except Exception as e:  # noqa: BLE001 — 工具边界兜底, 回喂主脑
        logger.exception("reasonix_run unexpected error")
        return f"reasonix 执行异常: {e}"

    subtype = r.get("subtype", "unknown")
    ok = not r.get("is_error", False)
    cost = r.get("total_cost", 0)
    currency = r.get("currency", "")
    usage = r.get("usage") or {}
    body = (r.get("result") or "").strip()
    head = "✅ reasonix 执行完成" if ok else "⚠ reasonix 执行失败"
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
    return f"{head}{meta} @ {ws}\n{subtype}: {body[:8000]}" + (
        f"\n\n[截断, 完整输出见 reasonix session {r.get('session_id')}]"
        if len(body) > 8000 else ""
    )


async def reasonix_status() -> str:
    """诊断 reasonix 执行器可用性 (二进制 / workspace / 模型)。"""
    try:
        bin_path = _resolve_bin()
        version = _bin_version() or "?"
        root = _workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        return (
            "✅ reasonix 可用\n"
            f"  二进制: {bin_path}\n"
            f"  版本: {version}\n"
            f"  workspace: {root}\n"
            f"  模型: {DEFAULT_MODEL} (本地网关; 容器内经反代 127.0.0.1:{_PROXY_PORT} 改写 Host)\n"
            f"  最大步数: {DEFAULT_MAX_STEPS or '自动'}, 超时: {DEFAULT_TIMEOUT_SEC}s"
        )
    except ReasonixUnavailable as e:
        return f"reasonix 不可用: {e}"


# ── 注册 ──────────────────────────────────────────────────────────────

async def wire_master_tools() -> int:
    """把 reasonix 工具注册进 master_tools (幂等)。返回新注册数量。"""
    from server.tool_registry import master_tools

    added = 0
    tools: list[tuple[str, str, dict, Any, int]] = [
        (
            "reasonix_run",
            "在隔离编码工作区执行真正的编程任务（写/改代码、修 bug、跑测试、重构、"
            "实现功能、搭建项目）。这是 veya 的编码执行器（Reasonix）：它有独立的"
            "规划器/执行器/沙箱/检查点，会自己读代码、改文件、运行命令、验证结果，"
            "完成后返回执行摘要与成本。**需要实际改动代码文件的任务请直接用本工具**，"
            "不要在对话里手搓代码。耗时可能数分钟，属于长任务。纯问答/解释类任务不要用。",
            {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "要完成的编程任务，写明目标与验收标准（如：修复 login.py 的登录失败，跑 pytest 通过）。"},
                    "workspace": {"type": "string", "description": f"可选。工作子目录名或绝对路径（必须位于 {_workspace_root()} 内）。缺省用根工作区。"},
                    "max_steps": {"type": "integer", "description": "可选。工具调用轮次上限，0=自动（默认）。"},
                    "timeout_sec": {"type": "integer", "description": "可选。超时秒数，默认 1800。"},
                },
                "required": ["task"],
            },
            reasonix_run,
            20000,
        ),
        (
            "reasonix_status",
            "诊断 reasonix 编码执行器是否可用（二进制/版本/工作区/模型）。执行编程任务前或收到 reasonix 不可用提示时可调用。",
            {"type": "object", "properties": {}},
            reasonix_status,
            2000,
        ),
    ]
    for name, desc, params, func, limit in tools:
        if master_tools.has(name):
            continue
        master_tools.register(name, desc, params, func, max_result_chars=limit)
        added += 1
    if added:
        logger.info("wire reasonix: 注册 %d 个工具 (bin=%s)", added, _resolve_bin())
    return added
