"""Docker-based pytest sandbox client (scheme C).

主进程侧: 把 文件集 + 测试参数 送入独立沙箱容器跑 pytest, 返回结构化 TestResult。

支持两种 backend:
  - "docker" (默认, 规格方案 C): 无外网 + 资源限制 + 只读 + tmpfs
  - "local"  (回退, 无 docker 环境用): 直接调 run_tests.py 协议 (同样走
    subprocess + 超时; 隔离性由调用方保证, 仅用于开发/验收)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from veya_loop.omodul.code_reliability_loop import TestResult

_SANDBOX_RUNNER = Path(__file__).resolve().parent.parent / "infra" / "code_sandbox" / "run_tests.py"


class CodeSandboxClient:
    def __init__(
        self,
        image: str = "veya-code-sandbox:latest",
        timeout_sec: int = 60,
        docker_bin: str = "docker",
        network: str = "none",
        memory: str = "512m",
        cpus: str = "1",
        backend: str = "docker",
    ):
        self.image = image
        self.timeout_sec = timeout_sec
        self.docker_bin = docker_bin
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.backend = backend
        if backend not in ("docker", "local"):
            raise ValueError(f"backend 必须是 docker|local, 收到 {backend!r}")

    def run(self, files: dict[str, str],
            test_args: list[str] | None = None) -> TestResult:
        payload = {
            "files": files,
            "test_args": test_args or ["-q", "--tb=short"],
            "timeout_sec": self.timeout_sec,
        }
        if self.backend == "local":
            return self._run_local(payload)
        return self._run_docker(payload)

    # ── docker 后端 (方案 C) ─────────────────────────────────────────
    def _run_docker(self, payload: dict) -> TestResult:
        cmd = [
            self.docker_bin, "run", "--rm", "-i",
            f"--network={self.network}",
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            "--read-only",
            "--tmpfs", "/work:rw,exec,size=128m,uid=10001",
            "--tmpfs", "/tmp:rw,size=64m,uid=10001",
            self.image,
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload).encode(),
                capture_output=True,
                timeout=self.timeout_sec + 30,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False, n_failed=1, stderr="sandbox outer timeout",
                metadata={"timeout": True},
            )

        stdout = proc.stdout.decode(errors="replace")
        stderr = proc.stderr.decode(errors="replace")
        data: dict = {}
        import contextlib
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(stdout or "{}")

        if "passed" not in data:
            # docker/infra 错误 (镜像缺失等)
            return TestResult(
                passed=False, n_failed=1,
                stderr=(stderr or stdout)[:2000] or "sandbox error",
                metadata={"env_error": True},
            )
        return TestResult.from_dict(data)

    # ── local 回退后端 (无 docker 环境; 同一协议) ────────────────────
    def _run_local(self, payload: dict) -> TestResult:
        try:
            proc = subprocess.run(
                [sys.executable, str(_SANDBOX_RUNNER)],
                input=json.dumps(payload).encode(),
                capture_output=True,
                timeout=self.timeout_sec + 30,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False, n_failed=1, stderr="sandbox outer timeout",
                metadata={"timeout": True},
            )
        stdout = proc.stdout.decode(errors="replace")
        try:
            data = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return TestResult(
                passed=False, n_failed=1,
                stderr=f"bad sandbox json: {stdout[:500]}",
                metadata={"env_error": True},
            )
        return TestResult.from_dict(data)


def build_sandbox_image(context: str = "infra/code_sandbox",
                        tag: str = "veya-code-sandbox:latest") -> None:
    subprocess.check_call(["docker", "build", "-t", tag, context])


def docker_available() -> bool:
    """探测 docker 是否可用 (镜像构建/验收前检查)。"""
    try:
        r = subprocess.run(["docker", "version"], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False
