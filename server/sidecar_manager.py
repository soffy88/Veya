"""server/sidecar_manager.py — 通用 sidecar 进程管理器 (3O 业务装配层)。

统一编排"常驻能力进程" (officecli daemon / codebase-memory connector / MCP sidecar...):
  spawn → 健康检查 → circuit_breaker 兜底 → 统一回收。

用法:
    mgr = SidecarManager()
    mgr.start("officecli", command=["officecli", "daemon", "--pipe", "/tmp/oc.pipe"],
              health=lambda: _pipe_exists("/tmp/oc.pipe"))
    mgr.status()          # {name: running/error/open/stopped, pid, failures}
    mgr.stop("officecli")
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_OPEN_SECONDS = 60.0


@dataclass
class SidecarRecord:
    name: str
    command: list[str]
    proc: subprocess.Popen | None = None
    started_at: float = 0.0
    health: Callable[[], bool] = lambda: True
    failures: int = 0
    circuit_open_until: float = 0.0
    last_error: str = ""

    @property
    def state(self) -> str:
        if self.proc is None:
            return "stopped"
        if self.proc.poll() is not None:
            return "exited"
        if time.time() < self.circuit_open_until:
            return "open"
        if not self.health():
            return "unhealthy"
        return "running"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "command": self.command, "state": self.state,
            "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
            "failures": self.failures, "last_error": self.last_error[-200:],
        }


class SidecarManager:
    """sidecar 生命周期: start / stop / status / 熔断。"""

    def __init__(self) -> None:
        self._records: dict[str, SidecarRecord] = {}

    # ── 生命周期 ──────────────────────────────────────────────────────
    def start(self, name: str, command: list[str], *,
              health: Callable[[], bool] | None = None,
              ready_timeout_s: float = 15.0) -> SidecarRecord:
        """启动 sidecar: 二进制缺失 → 结构化错误; 启动后轮询健康。"""
        rec = self._records.get(name)
        if rec is not None and rec.proc is not None and rec.proc.poll() is None:
            return rec  # 已在跑

        if shutil.which(command[0]) is None:
            raise RuntimeError(
                f"sidecar '{name}' 不可用: 未找到 {command[0]} "
                f"(安装后重试; 或检查 PATH)")

        proc = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        rec = SidecarRecord(name=name, command=command, proc=proc,
                            started_at=time.time(),
                            health=health or (lambda: True))
        self._records[name] = rec

        # 健康轮询 (超时视为失败, 触发熔断计数)
        deadline = time.time() + ready_timeout_s
        while time.time() < deadline:
            if proc.poll() is not None:
                rec.last_error = f"进程提前退出 rc={proc.returncode}"
                rec.failures += 1
                self._maybe_open_circuit(name)
                raise RuntimeError(rec.last_error)
            if rec.health():
                rec.failures = 0
                return rec
            time.sleep(0.2)
        rec.last_error = f"健康检查超时 ({ready_timeout_s:.0f}s)"
        rec.failures += 1
        self._maybe_open_circuit(name)
        raise RuntimeError(rec.last_error)

    # ── 熔断 (circuit_breaker) ───────────────────────────────────────
    def _maybe_open_circuit(self, name: str) -> None:
        rec = self._records[name]
        if rec.failures >= CIRCUIT_FAIL_THRESHOLD:
            rec.circuit_open_until = time.time() + CIRCUIT_OPEN_SECONDS
            rec.last_error = f"熔断: 连续失败 {rec.failures} 次, 暂停 {CIRCUIT_OPEN_SECONDS:.0f}s"

    def stop(self, name: str) -> None:
        rec = self._records.get(name)
        if rec is None:
            return
        if rec.proc is not None and rec.proc.poll() is None:
            rec.proc.terminate()
            try:
                rec.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rec.proc.kill()
        rec.proc = None

    def stop_all(self) -> None:
        for name in list(self._records):
            self.stop(name)

    # ── 状态 ──────────────────────────────────────────────────────────
    def status(self) -> list[dict[str, Any]]:
        return [rec.to_dict() for rec in self._records.values()]

    def get(self, name: str) -> SidecarRecord | None:
        return self._records.get(name)


_default_manager = SidecarManager()


def get_sidecar_manager() -> SidecarManager:
    return _default_manager
