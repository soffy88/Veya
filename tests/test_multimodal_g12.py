"""G12 — multimodal vision messages wired into the LLM provider layer.

Covers:
- ImageProcessor.to_content_block (data-URI content blocks)
- MultimodalProcessor.build_vision_messages (text + images → provider messages)
- veya.llm.prepare_messages_for_provider:
    * anthropic: image_url blocks → native image blocks (base64 + url sources)
    * openai/dashscope: content blocks pass through unchanged
- provider_call request bodies (mock transport) carry image blocks for both providers
- offline stub fallback with vision messages
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from veya import llm as hllm
from veya.multimodal import ImageProcessor, MultimodalProcessor

# 1x1 red PNG (valid image bytes for base64 encoding)
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def png_file(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(_TINY_PNG)
    return str(p)


# ---------------------------------------------------------------------------
# ImageProcessor / MultimodalProcessor
# ---------------------------------------------------------------------------


def test_to_content_block_data_uri(png_file):
    block = ImageProcessor().to_content_block(png_file)
    assert block is not None
    assert block["type"] == "image_url"
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert url.split(",", 1)[1] == base64.b64encode(_TINY_PNG).decode()


def test_to_content_block_missing_file_returns_none():
    assert ImageProcessor().to_content_block("/nope/missing.png") is None


def test_to_content_block_unsupported_format(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    assert ImageProcessor().to_content_block(str(f)) is None


def test_build_vision_messages_text_plus_images(png_file, tmp_path):
    second = tmp_path / "second.jpg"
    second.write_bytes(_TINY_PNG)
    proc = MultimodalProcessor()
    messages = proc.build_vision_messages("看图说话", [png_file, str(second)])
    assert len(messages) == 1
    content = messages[0]["content"]
    assert content[0] == {"type": "text", "text": "看图说话"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_build_vision_messages_skips_missing(png_file):
    messages = MultimodalProcessor().build_vision_messages("hi", [png_file, "/nope/x.png"])
    blocks = messages[0]["content"]
    assert len(blocks) == 2  # text + 1 image, missing one dropped
    assert all(b["type"] in ("text", "image_url") for b in blocks)


def test_build_vision_messages_system_prefix(png_file):
    messages = MultimodalProcessor().build_vision_messages("hi", [png_file], system="你是视觉助手")
    assert messages[0] == {"role": "system", "content": "你是视觉助手"}
    assert messages[1]["role"] == "user"


# ---------------------------------------------------------------------------
# prepare_messages_for_provider
# ---------------------------------------------------------------------------


def _data_uri(media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{base64.b64encode(_TINY_PNG).decode()}"


def test_anthropic_converts_base64_image():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": _data_uri("image/png")}},
            ],
        }
    ]
    out = hllm.prepare_messages_for_provider(messages, "anthropic")
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    img = blocks[1]
    assert img["type"] == "image"
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/png"
    assert img["source"]["data"] == base64.b64encode(_TINY_PNG).decode()


def test_anthropic_converts_plain_url():
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://x/a.png"}}],
        }
    ]
    out = hllm.prepare_messages_for_provider(messages, "anthropic")
    img = out[0]["content"][0]
    assert img["source"] == {"type": "url", "url": "https://x/a.png"}


def test_anthropic_leaves_string_content_untouched():
    messages = [{"role": "user", "content": "plain text"}]
    assert hllm.prepare_messages_for_provider(messages, "anthropic") == messages


def test_openai_passthrough():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": _data_uri()}},
            ],
        }
    ]
    assert hllm.prepare_messages_for_provider(messages, "openai") == messages
    assert hllm.prepare_messages_for_provider(messages, "dashscope") == messages


# ---------------------------------------------------------------------------
# provider_call request bodies (mock transport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_call_anthropic_sends_image(png_file, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "it's a red square"}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
                "stop_reason": "end_turn",
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    messages = MultimodalProcessor().build_vision_messages("what is this?", [png_file])
    data = await hllm.provider_call(
        client, "anthropic", model="claude-3-5-sonnet", messages=messages
    )
    assert "red square" in data["choices"][0]["message"]["content"]

    body = captured["body"]
    sent = body["messages"][0]["content"]
    assert sent[0]["type"] == "text"
    assert sent[1]["type"] == "image"
    assert sent[1]["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_provider_call_openai_sends_image_url(png_file, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "choices": [{"message": {"role": "assistant", "content": "red"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    messages = MultimodalProcessor().build_vision_messages("what is this?", [png_file])
    data = await hllm.provider_call(client, "openai", model="gpt-4o-mini", messages=messages)
    assert data["choices"][0]["message"]["content"] == "red"

    sent = captured["body"]["messages"][0]["content"]
    assert sent[1]["type"] == "image_url"
    assert sent[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_provider_call_dashscope_accepts_blocks(png_file, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash-test")
    messages = MultimodalProcessor().build_vision_messages("hi", [png_file])
    await hllm.provider_call(client, "dashscope", model="qwen-vl-max", messages=messages)
    assert captured["body"]["messages"][0]["content"][1]["type"] == "image_url"


# ---------------------------------------------------------------------------
# llm_call offline stub with vision messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_call_vision_offline_stub(png_file, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("VEYA_LLM_PROVIDER", raising=False)
    messages = MultimodalProcessor().build_vision_messages("what is this?", [png_file])
    out = await hllm.llm_call(messages, default_content="no vision offline")
    assert out["choices"][0]["message"]["content"] == "no vision offline"
    assert out["usage"]["total_tokens"] == 0


@pytest.mark.asyncio
async def test_llm_stream_vision_offline_stub(png_file, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    messages = MultimodalProcessor().build_vision_messages("hi", [png_file])
    events = [e async for e in hllm.llm_stream(messages)]
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
    assert "not configured" in text
