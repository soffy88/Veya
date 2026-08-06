"""server/runtimes/pi_bridge.py — L2 pi 工具链/CLI 桥 (subprocess 适配器)。

pi (pi-coding-agent, TS/Bun) 无法进 Python 进程 → subprocess 桥:
  init  : pi --version 探测
  dispatch/invoke : pi -p <task> [--model <model>] (非交互, 无 shell 注入面)
  lifecycle : 进程管理占位 (v1 无常驻)
  health : pi 可执行 + 版本

能力声明: plugin_registry.install(capabilities=["cli:pi"]) (装配期)。
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from server.runtimes.base import unavailable


class PiBridgeRuntime:
    """pi CLI 运行时桥。"""

    name = "pi_bridge"
    description = "pi (pi-coding-agent) CLI 桥: 极简/类型安全工具链, 统一多厂商 API"

    def __init__(self) -> None:
        self._bin: str | None = None
        self._version = ""

    def _find_bin(self) -> str | None:
        return shutil.which("pi")

    async def _run(self, args: list[str], timeout_s: float = 600.0) -> dict[str, Any]:
        assert self._bin is not None
        proc = await asyncio.create_subprocess_exec(
            self._bin, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "runtime": self.name, "error": f"pi 超时 ({timeout_s:.0f}s)"}
        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        if proc.returncode != 0:
            return {"ok": False, "runtime": self.name,
                    "error": err[-2000:] or f"exit={proc.returncode}", "output": out[-2000:]}
        return {"ok": True, "runtime": self.name, "output": out[-4000:]}

    # ── 协议 ──────────────────────────────────────────────────────────
    async def init(self, config: dict | None = None) -> dict[str, Any]:
        self._bin = self._find_bin()
        if self._bin is None:
            return unavailable(self.name, "pi CLI 未安装 (npm i -g @pi-coding/pi 或官方安装脚本)")
        r = await self._run(["--version"], timeout_s=15)
        self._version = r.get("output", "").strip() or "unknown"
        return {"ok": True, "runtime": self.name, "version": self._version,
                "bin": self._bin}

    async def dispatch(self, task: str, **kwargs: Any) -> dict[str, Any]:
        if self._bin is None:
            return unavailable(self.name, "pi CLI 未初始化 (先 init)")
        args = ["-p", task]
        model = kwargs.get("model") or kwargs.get("model_name")
        if model:
            args += ["--model", str(model)]
        cwd = kwargs.get("cwd")
        if cwd:
            import os

            old = os.getcwd()
            os.chdir(cwd)
            try:
                return await self._run(args, timeout_s=kwargs.get("timeout_s", 600.0))
            finally:
                os.chdir(old)
        return await self._run(args, timeout_s=kwargs.get("timeout_s", 600.0))

    async def invoke(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return await self.dispatch(prompt, **kwargs)

    async def lifecycle(self, action: str) -> dict[str, Any]:
        if action in ("health", "status"):
            return await self.health()
        return {"ok": True, "runtime": self.name, "action": action,
                "note": "pi bridge v1 按需子进程 (无常驻 daemon)"}

    async def health(self) -> dict[str, Any]:
        ok = self._find_bin() is not None
        return {"ok": ok, "runtime": self.name,
                "bin": self._find_bin(), "version": self._version or None}


pi_bridge = PiBridgeRuntime()
