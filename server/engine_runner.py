"""server/engine_runner.py — 多引擎执行路由 (Claude Code / Codex / Pi)。

聊天框选择引擎 = 选择"谁执行任务", 不只是换 LLM provider:

    engine=master  → 现有主脑 (ReAct + 工具 + 记忆)
    engine=claude  → claude CLI (Claude Code, 非交互 -p)
    engine=codex   → codex CLI (OpenAI Codex, exec)
    engine=pi      → pi CLI (pi-coding-agent, -p)

执行契约: 引擎 CLI 为可信本机工具, 直接 subprocess 执行 (argv 传参, 无 shell);
统一超时与错误捕获; prompt 不拼接进 shell, 无注入面。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator

ENGINE_ALIASES = {
    "claude": "claude",
    "codex": "codex",
    "pi": "pi",
    "master": "master",
}


def available_engines() -> dict[str, str]:
    """本机可用的引擎及版本 (探测 which)。"""
    out: dict[str, str] = {}
    for eng, bin_name in ENGINE_ALIASES.items():
        if eng == "master":
            out[eng] = "builtin"
            continue
        path = shutil.which(bin_name)
        if path:
            out[eng] = path
    return out


def build_argv(engine: str, prompt: str, *,
                model: str | None = None,
                streaming: bool = False) -> list[str]:
    """构造引擎 CLI 非交互 argv (无 shell, 无注入面)。

    streaming=True 时 claude 用 stream-json (逐事件解析); run 聚合模式用普通文本输出。
    """
    engine = ENGINE_ALIASES.get(engine, engine)
    if engine == "claude":
        argv = ["claude", "-p", prompt]
        if streaming:
            # stream-json 在 --print 模式下要求 --verbose
            argv += ["--output-format", "stream-json", "--verbose"]
        if model:
            argv += ["--model", model]
        return argv
    if engine == "codex":
        # 非 git 仓库跳过信任检查 + 全自动(非交互不等待确认)
        argv = ["codex", "exec", "--skip-git-repo-check", "--full-auto", prompt]
        if model:
            argv += ["-m", model]
        return argv
    if engine == "pi":
        argv = ["pi", "-p", prompt]
        if model:
            argv += ["--model", model]
        return argv
    raise ValueError(f"未知引擎: {engine!r}; 可选 {sorted(ENGINE_ALIASES)}")


async def run_engine(
    engine: str,
    prompt: str,
    *,
    model: str | None = None,
    cwd: str | None = None,
    timeout_s: float = 600.0,
) -> dict[str, object]:
    """聚合执行: 引擎跑完返回全文 (run 契约)。"""
    if engine == "master":
        raise ValueError("master 引擎走主脑 chat_stream, 不经 engine_runner")
    if shutil.which(engine) is None:
        return {"ok": False, "error": f"引擎 {engine} 不可用: CLI 未安装 (可用: {sorted(available_engines())})"}
    argv = build_argv(engine, prompt, model=model, streaming=False)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutExpired:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": f"引擎 {engine} 超时 ({timeout_s:.0f}s)",
                "duration_s": timeout_s}
    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    if proc.returncode != 0:
        return {"ok": False, "error": err[-2000:] or f"exit={proc.returncode}",
                "output": out[-4000:], "duration_s": 0.0}
    return {"ok": True, "output": out, "duration_s": 0.0}


async def stream_engine(
    engine: str,
    prompt: str,
    *,
    model: str | None = None,
    cwd: str | None = None,
    timeout_s: float = 600.0,
) -> AsyncIterator[dict[str, object]]:
    """流式执行: 逐行产出 {type: text_delta|engine_done|engine_error, ...}。

    claude stream-json 输出逐行 JSON, 提取 text 块; 其余引擎按行透传。
    """
    if engine == "master":
        raise ValueError("master 引擎走主脑 chat_stream, 不经 engine_runner")
    # CLI 缺失 → 结构化错误事件 (而非 subprocess 抛异常 → 500/520)
    if shutil.which(engine) is None:
        yield {
            "type": "engine_error",
            "engine": engine,
            "error": f"引擎 {engine} 不可用: CLI 未安装 (可用: {sorted(available_engines())})",
        }
        return
    argv = build_argv(engine, prompt, model=model, streaming=True)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _read() -> None:
        assert proc.stdout is not None
        line = await proc.stdout.readline()
        while line:
            text = line.decode(errors="replace").rstrip("\n")
            if engine == "claude" and text.strip().startswith("{"):
                # stream-json: {"type":"content_block_delta","delta":{"text":...}}
                try:
                    evt = json.loads(text)
                    if evt.get("type") == "content_block_delta":
                        delta = (evt.get("delta") or {}).get("text", "")
                        if delta:
                            yield {"type": "text_delta", "engine": engine, "delta": delta}
                except json.JSONDecodeError:
                    yield {"type": "text_delta", "engine": engine, "delta": text}
            else:
                yield {"type": "text_delta", "engine": engine, "delta": text}
            line = await proc.stdout.readline()

    try:
        async for evt in asyncio.wait_for(_read(), timeout=timeout_s):
            yield evt
        code = await proc.wait()
        yield {"type": "engine_done", "engine": engine, "status": "success" if code == 0 else "failed"}
    except asyncio.TimeoutExpired:
        proc.kill()
        await proc.wait()
        yield {"type": "engine_error", "engine": engine, "error": f"引擎超时 ({timeout_s:.0f}s)"}
