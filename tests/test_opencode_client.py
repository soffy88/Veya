"""opencode serve 常驻客户端测试 — 会话缓存 / SSE 解析 / 消息发送。

纯逻辑: mock httpx (假 serve), 不触网。SSE 事件解析与增量切片为确定性逻辑。
"""

from __future__ import annotations

import json
import sys
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "server")
import opencode_client as oc

# ── 协议纯函数 ────────────────────────────────────────────────────────────


def test_message_body_model_partition():
    body = oc._message_body("你好", "opencode-go/deepseek-v4-flash")
    assert body["parts"] == [{"type": "text", "text": "你好"}]
    assert body["model"] == {"providerID": "opencode-go",
                             "modelID": "deepseek-v4-flash"}
    # 无 provider 前缀 → 默认 opencode-go
    body2 = oc._message_body("x", "deepseek-v4-flash")
    assert body2["model"] == {"providerID": "opencode-go",
                              "modelID": "deepseek-v4-flash"}


def test_parse_sse_data():
    assert oc._parse_sse_data('data: {"type": "session.idle"}') == {
        "type": "session.idle"}
    assert oc._parse_sse_data("event: foo") is None  # 非 data 行
    assert oc._parse_sse_data("data: not-json") is None  # 坏 JSON


def test_assistant_text_extracts_text_parts():
    data = {"parts": [
        {"type": "text", "text": "第一段"},
        {"type": "tool", "tool": "x"},
        {"type": "text", "text": "第二段"},
    ]}
    assert oc._assistant_text(data) == "第一段\n第二段"


def test_assistant_text_handles_empty():
    assert oc._assistant_text({}) == ""
    assert oc._assistant_text({"parts": []}) == ""


# ── 会话缓存 (LRU) ────────────────────────────────────────────────────────


def test_session_cache_lru_eviction():
    oc._session_cache.clear()
    for i in range(oc._SESSION_CACHE_MAX + 2):
        oc._cache_put(f"model-{i}", f"ses_{i}")
    assert len(oc._session_cache) == oc._SESSION_CACHE_MAX
    # 最早插入的被逐出
    assert oc._cache_get("model-0") is None
    assert oc._cache_get(f"model-{oc._SESSION_CACHE_MAX + 1}") == f"ses_{oc._SESSION_CACHE_MAX + 1}"


def test_session_cache_drop():
    oc._session_cache.clear()
    oc._cache_put("m", "s1")
    oc._cache_drop("m")
    assert oc._cache_get("m") is None


# ── 消息发送 (mock httpx) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_uses_cached_session(monkeypatch):
    oc._session_cache.clear()
    oc._cache_put("opencode-go/deepseek-v4-flash", "ses_cached")

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={"parts": [{"type": "text",
                                                        "text": "回复内容"}]})

    class FakeClient:
        instances: ClassVar[list[FakeClient]] = []

        def __init__(self, *a, **kw):
            self.posts = []
            FakeClient.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            self.posts.append(url)
            return fake_resp

    monkeypatch.setattr(oc, "ensure_server", lambda timeout_s=10.0: True)
    monkeypatch.setattr(oc, "base_url", lambda: "http://127.0.0.1:18765")

    with patch("httpx.AsyncClient", FakeClient):
        r = await oc.send_message("你好", "opencode-go/deepseek-v4-flash")
        # 用缓存会话 → 不调 POST /session, 只发消息
        assert FakeClient.instances[-1].posts == ["/session/ses_cached/message"]
        assert r["output"] == "回复内容"
        assert r["session_id"] == "ses_cached"


@pytest.mark.asyncio
async def test_send_message_creates_session_when_no_cache(monkeypatch):
    oc._session_cache.clear()

    fake_session = MagicMock()
    fake_session.json = MagicMock(return_value={"id": "ses_new"})
    fake_msg = MagicMock()
    fake_msg.raise_for_status = MagicMock()
    fake_msg.json = MagicMock(return_value={"parts": [{"type": "text",
                                                       "text": "ok"}]})

    class FakeClient:
        def __init__(self, *a, **kw):
            self.posts = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            self.posts.append(url)
            if url == "/session":
                return fake_session
            return fake_msg

    monkeypatch.setattr(oc, "ensure_server", lambda timeout_s=10.0: True)
    with patch("httpx.AsyncClient", FakeClient):
        r = await oc.send_message("你好", "opencode-go/deepseek-v4-flash")
        assert r["session_id"] == "ses_new"
        assert r["output"] == "ok"


# ── SSE 真流式 (mock httpx 事件流) ────────────────────────────────────────


def _sse_line(evt: dict) -> str:
    return f"data: {json.dumps(evt)}"


async def _fake_stream(lines: list[str]):
    for ln in lines:
        yield ln


def _part_event(session_id: str, text: str, msg_id: str = "msg_assistant") -> dict:
    return {
        "id": "evt_x", "type": "message.part.updated",
        "properties": {"sessionID": session_id,
                       "part": {"id": "p1", "type": "text", "messageID": msg_id,
                                "sessionID": session_id, "text": text}},
    }


@pytest.mark.asyncio
async def test_stream_send_yields_incremental_deltas(monkeypatch):
    oc._session_cache.clear()
    sid = "ses_flow"

    # 事件流: user echo → 文本增量 ×2 → idle
    events = [
        {"id": "e0", "type": "message.updated",
         "properties": {"sessionID": sid,
                        "info": {"id": "msg_user", "role": "user"}}},
        _part_event(sid, "你好", msg_id="msg_user"),          # user echo → 跳过
        _part_event(sid, "我是"),                              # 增量 1
        _part_event(sid, "我是 opencode"),                     # 增量 2
        {"id": "e9", "type": "session.idle",
         "properties": {"sessionID": sid}},
    ]

    class FakeClient:
        def __init__(self, *a, **kw):
            self._stream_events = events
            self._posted = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if url == "/session":
                return MagicMock(json=MagicMock(return_value={"id": sid}))
            self._posted.append(url)
            # POST message 挂起到流结束 → 直接返回完成消息
            return MagicMock(json=MagicMock(return_value={"parts": [
                {"type": "text", "text": "我是 opencode"}]}))

        def stream(self, method, url):
            async def _it():
                for evt in self._stream_events:
                    yield _sse_line(evt)
                yield ""
            resp = AsyncMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.aiter_lines = lambda: _it()
            return AsyncMock(__aenter__=AsyncMock(return_value=resp),
                             __aexit__=AsyncMock(return_value=False))

    monkeypatch.setattr(oc, "ensure_server", lambda timeout_s=10.0: True)
    monkeypatch.setattr(oc, "base_url", lambda: "http://127.0.0.1:18765")
    with patch("httpx.AsyncClient", FakeClient):
        deltas = []
        async for d in oc.stream_send("你好", "opencode-go/deepseek-v4-flash",
                                      timeout_s=5):
            deltas.append(d)
    # user echo 被跳过, 只产出 assistant 增量
    assert "".join(deltas) == "我是 opencode"
    assert deltas == ["我是", " opencode"]


@pytest.mark.asyncio
async def test_stream_send_falls_back_to_full_response(monkeypatch):
    """SSE 无事件 → finally 用 POST 完整响应兜底 (保证有输出)。"""
    oc._session_cache.clear()
    sid = "ses_fb"

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if url == "/session":
                return MagicMock(json=MagicMock(return_value={"id": sid}))
            return MagicMock(json=MagicMock(return_value={"parts": [
                {"type": "text", "text": "兜底完整回复"}]}))

        def stream(self, method, url):
            resp = AsyncMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.aiter_lines = lambda: _fake_stream([])  # 空事件流
            return AsyncMock(__aenter__=AsyncMock(return_value=resp),
                             __aexit__=AsyncMock(return_value=False))

    monkeypatch.setattr(oc, "ensure_server", lambda timeout_s=10.0: True)
    with patch("httpx.AsyncClient", FakeClient):
        got = []
        async for d in oc.stream_send("你好", "opencode-go/deepseek-v4-flash",
                                      timeout_s=5):
            got.append(d)
    assert "".join(got) == "兜底完整回复"
