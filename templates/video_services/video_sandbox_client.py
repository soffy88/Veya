"""video_sandbox_client — veya-video-sandbox 的宿主侧客户端。

通过 docker run --rm -i --network=none -v <dir>:/work:ro 调沙箱执行质检,
或 (无 Docker 环境) 用 LocalVideoEvaluator 直接调 evaluate.py (dev/CI)。

返回 VideoEvalResult (与沙箱 stdout JSON 对齐)。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from veya_loop.omodul.video_reliability_loop import VideoEvalResult


class VideoSandboxClient:
    """Docker 沙箱客户端 (方案 C: 独立容器 + --network=none)。"""

    def __init__(
        self,
        image: str = "veya-video-sandbox:latest",
        docker_bin: str = "docker",
        mount_root: str = "/work",
        timeout_s: float = 180.0,
    ) -> None:
        self._image = image
        self._docker = docker_bin
        self._mount_root = mount_root
        self._timeout = timeout_s

    def evaluate(self, video_path: Path, spec: dict[str, Any]) -> VideoEvalResult:
        """把视频挂进沙箱只读目录, 跑 evaluate.py, 解析结果。"""
        video_path = Path(video_path)
        host_dir = str(video_path.parent)
        container_path = f"{self._mount_root}/{video_path.name}"

        payload = json.dumps({"video_path": container_path, "spec": spec}, ensure_ascii=False)
        cmd = [
            self._docker,
            "run",
            "--rm",
            "-i",
            "--network=none",
            "-v",
            f"{host_dir}:{self._mount_root}:ro",
            self._image,
        ]
        proc = subprocess.run(
            cmd, input=payload, capture_output=True, text=True, timeout=self._timeout
        )
        if proc.returncode != 0:
            return VideoEvalResult(
                passed=False,
                issues=[
                    {
                        "code": "SANDBOX_ERROR",
                        "message": f"docker exit={proc.returncode}: {proc.stderr[-300:]}",
                        "severity": "high",
                    }
                ],
                stderr=proc.stderr,
            )
        try:
            data = json.loads(proc.stdout or "{}")
            return VideoEvalResult.from_dict(data)
        except json.JSONDecodeError as exc:
            return VideoEvalResult(
                passed=False,
                issues=[
                    {
                        "code": "SANDBOX_ERROR",
                        "message": f"沙箱输出非 JSON: {exc}",
                        "severity": "high",
                    }
                ],
                stderr=proc.stdout[-500:],
            )


class LocalVideoEvaluator:
    """无 Docker 环境: 直接调沙箱 evaluate.py (dev/CI/本机)。"""

    def __init__(
        self,
        evaluate_script: Path | None = None,
        timeout_s: float = 180.0,
    ) -> None:
        self._script = Path(
            evaluate_script
            or (Path(__file__).resolve().parents[2] / "infra" / "video_sandbox" / "evaluate.py")
        )
        self._timeout = timeout_s

    def evaluate(self, video_path: Path, spec: dict[str, Any]) -> VideoEvalResult:
        payload = json.dumps(
            {"video_path": str(Path(video_path).resolve()), "spec": spec},
            ensure_ascii=False,
        )
        proc = subprocess.run(
            [os.sys.executable, str(self._script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if proc.returncode != 0:
            return VideoEvalResult(
                passed=False,
                issues=[
                    {
                        "code": "SANDBOX_ERROR",
                        "message": f"evaluate.py exit={proc.returncode}: {proc.stderr[-300:]}",
                        "severity": "high",
                    }
                ],
                stderr=proc.stderr,
            )
        try:
            return VideoEvalResult.from_dict(json.loads(proc.stdout or "{}"))
        except json.JSONDecodeError as exc:
            return VideoEvalResult(
                passed=False,
                issues=[
                    {
                        "code": "SANDBOX_ERROR",
                        "message": f"沙箱输出非 JSON: {exc}",
                        "severity": "high",
                    }
                ],
                stderr=proc.stdout[-500:],
            )


def make_eval_fn(client: Any) -> Any:
    """适配 evaluate_fn 签名: (task, artifact) -> VideoEvalResult。"""

    def _evaluate(task: Any, artifact: Any) -> VideoEvalResult:
        return client.evaluate(Path(artifact.video_path), task.spec.to_dict())

    return _evaluate


__all__ = ["LocalVideoEvaluator", "VideoSandboxClient", "make_eval_fn"]
