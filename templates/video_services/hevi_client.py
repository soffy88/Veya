"""hevi_client — 视频可靠性闭环的真实 hevi 出片客户端。

调 hevi `/api/lite/generate` 同步端点 (同机 dev 直连; 生产走服务内网)。
把 VideoSpec + FailureSignature 合成为 Lite 管线输入 (cues/分辨率/旁白量),
返回本地视频路径, 供沙箱质检。

generate_fn 契约: generate_with_hevi(prompt, failure_context) -> Path
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from veya_loop.omodul.video_reliability_loop import VideoSpec

DEFAULT_BASE_URL = "http://127.0.0.1:8000"  # hevi API (同机 dev / 服务内网)


class HeviGenerateClient:
    """hevi Lite 出片客户端 (调同步端点, 支持同机路径直用)。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 1800.0,
        download_dir: Path | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._download_dir = download_dir or Path("/tmp/hevi_downloads")
        self._download_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self, prompt: str, spec: VideoSpec, failure_context: dict[str, Any] | None = None
    ) -> Path:
        """真实出片: prompt → cues → hevi Lite 管线 → 本地视频路径。"""
        failure_context = failure_context or {}
        width, height = self._aspect_size(spec)
        cues = self._build_cues(prompt, spec, failure_context)

        payload = {
            "task_id": f"veya_{int(time.time() * 1000)}",
            "topic": prompt,
            "cues": cues,
            "voice": "edge_tts_zh",
            "width": width,
            "height": height,
            "fps": 24,
            "output_name": "final.mp4",
            # 失败上下文注入: 时长不够 → 增加旁白量/提示保持时长。
            "options": {
                "duration_hint_s": round(spec.min_duration_s, 1),
                "failure_kind": failure_context.get("kind", ""),
            },
        }
        req = urllib.request.Request(
            f"{self._base}/api/lite/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read().decode())
        except Exception as exc:
            raise RuntimeError(f"hevi /api/lite/generate 失败: {exc}") from exc

        if result.get("status") != "completed":
            raise RuntimeError(f"hevi 出片失败: {result.get('status')} {result.get('error', '')}")
        video_path = result.get("video_path")
        if not video_path:
            raise RuntimeError("hevi 返回 completed 但无 video_path")
        return self._materialize(Path(video_path))

    # ── 内部 ──────────────────────────────────────────────────────────
    def _materialize(self, video_path: Path) -> Path:
        """同机 dev 直接用宿主路径; 远端路径下载到本地。"""
        if video_path.is_file():
            return video_path
        # 容器/远端路径: 尝试下载 (hevi 需暴露文件端点时启用)。
        local = self._download_dir / video_path.name
        try:
            urllib.request.urlretrieve(f"{self._base}/api/files/{video_path.name}", local)
            return local
        except Exception:
            # 不可达时返回原路径, 让沙箱以 FILE_MISSING 兜底并给出清晰签名。
            return video_path

    def _aspect_size(self, spec: VideoSpec) -> tuple[int, int]:
        ratio = (spec.aspect_ratios or ["9:16"])[0]
        base = max(spec.min_width, spec.min_height)
        if ratio == "9:16":
            return 720, max(1280, base * 16 // 9 // 16 * 16)
        if ratio == "16:9":
            return max(1280, base * 16 // 9), 720
        if ratio == "1:1":
            return max(720, base), max(720, base)
        # 未知比例: 兜底 16:9
        return max(1280, base), 720

    def _build_cues(
        self, prompt: str, spec: VideoSpec, failure_context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """prompt → 若干条 Lite cue。

        时长目标: cue 数随 min_duration_s 增加 (旁白量驱动时长)。
        返工轮 (spec_or_duration): 明确提示生成侧保持目标时长。
        """
        import re

        sentences = [s.strip() for s in re.split(r"[。！？!?；;\n]", prompt) if s.strip()]
        if not sentences:
            sentences = [prompt]
        n_cues = max(1, min(len(sentences), int(max(1, spec.min_duration_s / 2.5))))
        if len(sentences) > n_cues:
            sentences = sentences[:n_cues]
        keep_duration_hint = (
            " 全片保持目标时长。" if failure_context.get("kind") == "spec_or_duration" else ""
        )
        cues: list[dict[str, Any]] = []
        for i, sentence in enumerate(sentences):
            title = sentence[:18] + ("…" if len(sentence) > 18 else "")
            cues.append(
                {
                    "index": i,
                    "narration": f"{sentence}{keep_duration_hint}",
                    "template": "card",
                    "props": {"title": title, "body": sentence, "fullscreen": False},
                }
            )
        return cues


__all__ = ["DEFAULT_BASE_URL", "HeviGenerateClient"]
