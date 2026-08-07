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
import os
import shutil
from collections.abc import AsyncIterator

ENGINE_ALIASES = {
    "claude": "claude",
    "codex": "codex",
    "pi": "pi",
    "opencode": "opencode",
    "master": "master",
}

# 容器环境检测 (docker 镜像设 VEYA_WORKSPACE=/app; /.dockerenv 兜底)
# 容器里执行外部 CLI 引擎的前提 = 凭据 + 端点/出口双可达, 逐个精确探测:
#   pi     → ~/.pi/agent/auth.json + 二进制
#   claude → 凭据 + 二进制 + 出口代理可达 (Anthropic 拒容器直连 IP, 需宿主代理桥)
#   codex  → ~/.codex + 二进制 + 端点可达 (宿主本地代理需桥接)
_IN_CONTAINER: bool = bool(os.environ.get("VEYA_WORKSPACE")) or os.path.exists("/.dockerenv")


def _container_gateway_ip() -> str | None:
    """容器 → 宿主网关 IP (探测可达网段)。"""
    if not _IN_CONTAINER:
        return None
    import urllib.error
    import urllib.request

    for gw in ("192.168.16.1", "172.18.0.1", "172.17.0.1"):
        try:
            with urllib.request.urlopen(f"http://{gw}:10101/v1/models", timeout=0.5) as resp:
                if resp.status in (200, 401, 403):
                    return gw
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 401, 403):   # urlopen 对非 2xx 抛 HTTPError
                return gw
        except Exception:
            continue
    return None


def _container_proxy_env() -> dict[str, str]:
    """容器内外部引擎的代理 env: 宿主代理桥 (17890→7890) 经网关可达。

    Anthropic 拒绝容器直连出口 IP (403 Request not allowed) — 宿主 claude
    经本地代理 127.0.0.1:7890 成功; 容器内经桥 17890 走同一代理。
    NO_PROXY 排除本地 (opencodex 桥/内部服务不走代理, ws 不会被破坏)。
    """
    gw = _container_gateway_ip()
    if not gw:
        return {}
    return {
        "HTTP_PROXY": f"http://{gw}:17890",
        "HTTPS_PROXY": f"http://{gw}:17890",
        "http_proxy": f"http://{gw}:17890",
        "https_proxy": f"http://{gw}:17890",
        "NO_PROXY": "localhost,127.0.0.1,::1,.local,192.168.16.0/24,172.18.0.0/16",
        "no_proxy": "localhost,127.0.0.1,::1,.local,192.168.16.0/24,172.18.0.0/16",
    }


def _ensure_container_opencodex() -> bool:
    """容器内确保 opencodex 代理活着 (探测 → 无则 spawn, 幂等)。

    opencodex 是 codex 引擎的本地翻译层 (ChatGPT oauth → OpenAI 兼容);
    容器内实例需代理 env (模型目录同步 chatgpt.com)。
    """
    if not _IN_CONTAINER:
        return True
    import subprocess
    import time
    import urllib.request

    def _healthz() -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:10100/healthz", timeout=0.5) as r:
                return r.status == 200
        except Exception:
            return False

    if _healthz():
        return True
    bun = ("/home/soffy/.nvm/versions/node/v26.4.0/lib/node_modules/"
           "@bitkyc08/opencodex/node_modules/bun/bin/bun.exe")
    cli = ("/home/soffy/.nvm/versions/node/v26.4.0/lib/node_modules/"
           "@bitkyc08/opencodex/src/cli/index.ts")
    if not (os.path.isfile(bun) and os.path.isfile(cli)):
        return False
    gw = _container_gateway_ip() or "192.168.16.1"
    env = {
        **os.environ,
        "HTTPS_PROXY": f"http://{gw}:17890",
        "HTTP_PROXY": f"http://{gw}:17890",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }
    subprocess.Popen([bun, cli, "start", "--port", "10100"], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        if _healthz():
            return True
    return False


def _container_pi_usable() -> bool:
    """容器内 pi 精确探测: 凭据目录 + agent/auth.json + 二进制 PATH 全在。"""
    if not _IN_CONTAINER:
        return True
    pi_dir = os.path.expanduser("~/.pi")
    creds = os.path.join(pi_dir, "agent", "auth.json")
    return (
        os.path.isdir(pi_dir)
        and os.path.isfile(creds)
        and shutil.which("pi") is not None
    )


def _container_claude_usable() -> bool:
    """容器内 claude 精确探测: 凭据 + 全局配置 + 二进制 + 代理桥可达。

    Anthropic 拒容器直连出口 IP (403) — 需宿主代理桥 (17890) 经网关可达;
    桥不可达时诚实拒绝 (探测 _container_gateway_ip)。
    """
    if not _IN_CONTAINER:
        return True
    home = os.path.expanduser("~")
    return (
        os.path.isfile(os.path.join(home, ".claude", ".credentials.json"))
        and os.path.isfile(os.path.join(home, ".claude.json"))
        and shutil.which("claude") is not None
        and _container_gateway_ip() is not None
    )


def _container_codex_usable() -> bool:
    """容器内 codex 精确探测: 配置 + 二进制 + opencodex 自举可达。

    codex 依赖 opencodex 代理 (ChatGPT oauth 翻译层) — 容器内自举实例
    (带代理 env), 探测失败时诚实拒绝。
    """
    if not _IN_CONTAINER:
        return True
    home = os.path.expanduser("~")
    if not (os.path.isfile(os.path.join(home, ".codex", "config.toml"))
            and os.path.isfile(os.path.join(home, ".codex", "auth.json"))
            and shutil.which("codex") is not None):
        return False
    return _ensure_container_opencodex()


def _container_codex_base_url() -> str | None:
    """容器内 codex 应使用的 opencodex 端点 (容器内自举实例, loopback 直连)。"""
    if not _IN_CONTAINER:
        return None
    if _ensure_container_opencodex():
        return "http://127.0.0.1:10100/v1"
    return None


def available_engines() -> dict[str, str]:
    """本机可用的引擎及版本 (探测 which + 凭据/端点)。

    容器环境: master + 逐个精确探测 (pi/claude/codex 凭据+端点齐全才放行)。
    """
    if _IN_CONTAINER:
        out = {"master": "builtin"}
        for eng, probe in (("pi", _container_pi_usable),
                           ("claude", _container_claude_usable),
                           ("codex", _container_codex_usable)):
            if probe():
                out[eng] = shutil.which(eng)
        return out
    out: dict[str, str] = {}
    for eng, bin_name in ENGINE_ALIASES.items():
        if eng == "master":
            out[eng] = "builtin"
            continue
        path = shutil.which(bin_name)
        if path:
            out[eng] = path
    return out


def _container_engine_block(engine: str) -> str | None:
    """容器内非 master 引擎 → 按精确探测返回拒绝原因, 否则 None。"""
    if _IN_CONTAINER and engine != "master":
        probes = {"pi": _container_pi_usable,
                  "claude": _container_claude_usable,
                  "codex": _container_codex_usable}
        probe = probes.get(engine)
        if probe and probe():
            return None
        return f"容器环境不支持外部 CLI 引擎 '{engine}' (凭据/端点未就绪, 仅 master 可用)"
    return None


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
        # 非 git 仓库跳过信任检查 + workspace-write 沙箱 (--full-auto 已弃用)
        argv = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", prompt]
        if model:
            argv += ["-m", model]
        if _IN_CONTAINER:
            # 容器内覆盖 opencodex 端点 (宿主桥 10101, config.toml 写死 127.0.0.1 不可达)
            base = _container_codex_base_url()
            if base:
                argv += ["-c", f"openai_base_url={base}"]
        return argv
    if engine == "pi":
        argv = ["pi", "-p", prompt]
        if model:
            argv += ["--model", model]
        return argv
    if engine == "opencode":
        # opencode-go 网关模型 (系统内已有凭据)
        argv = ["opencode", "run", prompt,
                "--model", model or "opencode-go/deepseek-v4-flash"]
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
    if reason := _container_engine_block(engine):
        return {"ok": False, "error": reason}
    if shutil.which(engine) is None:
        return {"ok": False, "error": f"引擎 {engine} 不可用: CLI 未安装 (可用: {sorted(available_engines())})"}
    argv = build_argv(engine, prompt, model=model, streaming=False)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env={**os.environ, **(_container_proxy_env() if _IN_CONTAINER else {})},
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
    if reason := _container_engine_block(engine):
        yield {"type": "engine_error", "engine": engine, "error": reason}
        return
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
        env={**os.environ, **(_container_proxy_env() if _IN_CONTAINER else {})},
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
