"""Tests for HARN S2 and S3 fixes (dom_reader title, raw tool output filtering, memory update receipt filtering, turn queue S3 coalescing)."""

import asyncio
import pytest
from server.bot_team.turns import extract_reply
from server.goal_run.bot_identity import BotSpec
from veya.oskill.pure.html_to_markdown import read_html


def test_dom_reader_title_inclusion():
    html = "<html><head><title>Example Domain</title></head><body><h1>Hello</h1></body></html>"
    doc = read_html(html, url="https://example.com")
    assert "Title: Example Domain" in doc.markdown
    assert "Source URL: https://example.com" in doc.markdown


def test_extract_reply_filters_raw_tool_outputs():
    bot = BotSpec(bot_id="test_bot", name="TestBot")

    # S2 raw tool output case
    resp_s2 = {
        "reply": "[Channel: web | Backend: dom_reader]\n<!-- EXTERNAL DATA: ... -->\n\nTitle: Example Domain\n\nSource URL: https://example.com\n\nThis domain is for use...",
        "status": "completed",
    }
    extracted_s2 = extract_reply(resp_s2, bot=bot)
    assert "[Channel:" not in extracted_s2
    assert "EXTERNAL DATA" not in extracted_s2

    # S3 memory update receipt case
    resp_s3 = {
        "reply": "✅ 永久记忆已更新: Always call the user 小林 (Xiao Lin) in conversation.",
        "status": "completed",
    }
    extracted_s3 = extract_reply(resp_s3, bot=bot)
    assert "✅ 永久记忆已更新" not in extracted_s3
