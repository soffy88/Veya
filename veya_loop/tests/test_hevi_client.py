"""HeviGenerateClient 单测 — 真实出片客户端的请求/响应契约。

不依赖 hevi API 实例: 用本地 ThreadingHTTPServer 扮演 hevi 同步端点,
验证 payload 构造 (cues/分辨率注入) 与响应解析 (video_path / 失败传播)。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "templates" / "video_services"))

from hevi_client import HeviGenerateClient
from veya_loop.omodul.video_reliability_loop import VideoSpec


class _FakeHevi:
    """扮演 hevi /api/lite/generate: 记录请求, 按状态返回。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = "completed"
        self.video_path = "/tmp/fake_video.mp4"

    def handle(self, payload: dict) -> dict:
        self.requests.append(payload)
        if self.status == "completed":
            return {
                "task_id": payload.get("task_id", ""),
                "status": "completed",
                "video_path": self.video_path,
                "progress": 100,
            }
        return {
            "task_id": payload.get("task_id", ""),
            "status": "failed",
            "error": "lite pipeline raised: boom",
        }


@pytest.fixture()
def fake_hevi() -> _FakeHevi:
    state = _FakeHevi()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode() or "{}")
            body = json.dumps(state.handle(payload)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_generate_returns_local_path(fake_hevi):
    """completed + 本地路径 → 直接返回 Path。"""
    state, base = fake_hevi
    Path("/tmp/fake_video.mp4").touch()
    client = HeviGenerateClient(base_url=base)
    spec = VideoSpec(min_duration_s=6.0, min_width=720, min_height=720, aspect_ratios=["9:16"])
    path = client.generate("测试提示", spec, None)
    assert Path(path).name == "fake_video.mp4"
    req = state.requests[0]
    assert req["width"] == 720 and req["height"] == 1280  # 9:16 注入
    assert req["cues"] and req["cues"][0]["narration"]
    assert req["topic"] == "测试提示"
    Path("/tmp/fake_video.mp4").unlink()


def test_generate_failure_raises(fake_hevi):
    """hevi 返回 failed → RuntimeError (闭环 eval 层转 ENV 签名)。"""
    state, base = fake_hevi
    state.status = "failed"
    client = HeviGenerateClient(base_url=base)
    with pytest.raises(RuntimeError, match="hevi 出片失败"):
        client.generate("x", VideoSpec(), None)


def test_failure_context_injects_duration_hint(fake_hevi):
    """返工轮 (spec_or_duration) → payload 带 duration_hint 与提示。"""
    state, base = fake_hevi
    client = HeviGenerateClient(base_url=base)
    spec = VideoSpec(min_duration_s=8.0)
    client.generate("示例", spec, {"kind": "spec_or_duration", "preferred_action": "ADJUST_PROMPT"})
    req = state.requests[0]
    assert req["options"]["duration_hint_s"] == 8.0
    assert req["options"]["failure_kind"] == "spec_or_duration"
    assert "保持目标时长" in req["cues"][0]["narration"]
