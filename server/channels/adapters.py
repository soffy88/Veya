"""Veya Channels: standardized outbound-dispatch adapters.

Two shapes: a real-API adapter (FeishuAdapter, plain custom-bot webhook — no app
registration needed, distinct from veya/im/feishu.py's full tenant-token Bot API,
which solves a different problem: replying inside an ongoing bot conversation by
chat_id) and an RPA-fallback adapter (SocialMediaRPAAdapter) for platforms with no
open API, driven by veya.omodul.browser_agent.BrowserAgent's real Playwright engine.
"""

from __future__ import annotations

import abc
import logging
from typing import Any

logger = logging.getLogger("omni_channel")


class ChannelAdapter(abc.ABC):
    """Standardized interface for an outbound dispatch target."""

    @abc.abstractmethod
    async def push(self, content: str, payload: dict[str, Any] | None = None) -> str:
        """Push content out. Returns a human-readable status string; raises on failure."""


class FeishuAdapter(ChannelAdapter):
    """Feishu custom-bot group webhook (plain POST, no app registration)."""

    def __init__(self, webhook_url: str | None):
        self.webhook_url = webhook_url

    async def push(self, content: str, payload: dict[str, Any] | None = None) -> str:
        if not self.webhook_url:
            raise RuntimeError("FeishuAdapter: FEISHU_WEBHOOK is not configured")

        import httpx

        title = (payload or {}).get("title", "Veya 自动分发")
        card_msg = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [[{"tag": "text", "text": content}]],
                    }
                }
            },
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(self.webhook_url, json=card_msg)
            res.raise_for_status()
        return "✅ 已成功分发至飞书群组。"


async def _browser_llm_handler(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Adapts veya.llm.llm_call's OpenAI-shaped response into BrowserAgent's
    expected {"content": str} handler contract.

    These prompts embed raw scraped page text (see veya/omodul/browser_agent.py's
    _plan_actions_with_llm) — a real prompt-injection surface, since a target page
    could contain adversarial instructions. Firewall every user-role message before
    it reaches the model.
    """
    from server.firewall import VeyaFirewall
    from veya.llm import llm_call

    sanitized_messages = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            result = VeyaFirewall.sanitize(msg["content"], source="browser_scrape")
            if not result["safe"]:
                logger.warning("[Omni-Gateway] browser_llm_handler blocked: %s", result["reason"])
            msg = {**msg, "content": result["sanitized_content"]}
        sanitized_messages.append(msg)

    response = await llm_call(sanitized_messages)
    content = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return {"content": content}


class SocialMediaRPAAdapter(ChannelAdapter):
    """RPA fallback for platforms with no open API: drives a real headless browser
    via veya.omodul.browser_agent.BrowserAgent. Requires an llm_handler to actually
    plan click/type/submit actions — without one, run_task degrades to a read-only
    text extraction, which would silently fail to publish anything."""

    def __init__(self, platform_name: str, target_url: str):
        self.platform_name = platform_name
        self.target_url = target_url

    async def push(self, content: str, payload: dict[str, Any] | None = None) -> str:
        from veya.omodul.browser_agent import BrowserAgent, BrowserTaskConfig

        logger.info(
            "[Omni-Gateway] %s 无开放 API，启动 RPA 无头浏览器降级分发...", self.platform_name
        )

        agent = BrowserAgent(BrowserTaskConfig())
        agent.llm_handler = _browser_llm_handler

        instruction = f"Publish the following post to this platform: {content}"
        image_path = (payload or {}).get("image_path")
        if image_path:
            instruction += f" Attach the image at {image_path} if there is an upload option."

        result = await agent.run_task(self.target_url, instruction)
        if not result.success:
            raise RuntimeError(f"RPA dispatch to {self.platform_name} failed: {result.error}")
        return f"✅ 已通过 RPA 自动化引擎在 {self.platform_name} 发布完毕（steps={result.steps}）。"
