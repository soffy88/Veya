"""server/omni_gateway.py — Omni-Channel Gateway: unified outbound dispatch.

Standardizes external distribution behind one LLM tool
(get_llm_schema()/execute_dispatch()) so a caller only needs to say "send this to
platform X" — the gateway decides whether that means a real API call
(server.channels.adapters.FeishuAdapter) or an RPA fallback
(server.channels.adapters.SocialMediaRPAAdapter).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from server.channels.adapters import ChannelAdapter, FeishuAdapter, SocialMediaRPAAdapter
from server.notification_center import global_notifier

DISPATCH_TOOL_NAME = "system_dispatch_omni_channel"


class OmniChannelGateway:
    """Manages registered ChannelAdapters and fans out dispatch requests."""

    def __init__(self) -> None:
        self.channels: dict[str, ChannelAdapter] = {
            "feishu_workgroup": FeishuAdapter(os.environ.get("FEISHU_WEBHOOK")),
            "xiaohongshu_official": SocialMediaRPAAdapter(
                "小红书", "https://creator.xiaohongshu.com/publish/publish"
            ),
            "x_twitter": SocialMediaRPAAdapter("X (Twitter)", "https://x.com/compose/post"),
        }

    def get_llm_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": DISPATCH_TOOL_NAME,
                "description": (
                    "Distribute content, summaries, or reports to external platforms "
                    "(e.g. Feishu, Xiaohongshu, Twitter). The system automatically handles "
                    "API formatting or RPA browser automation depending on the target."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "targets": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(self.channels.keys())},
                            "description": "Select one or multiple destination platforms.",
                        },
                        "title": {
                            "type": "string",
                            "description": "The title of the push message.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The formatted Markdown content to distribute.",
                        },
                    },
                    "required": ["targets", "title", "content"],
                },
            },
        }

    async def execute_dispatch(self, targets: list[str], title: str, content: str) -> str:
        """Fan out to each target adapter concurrently and assemble a per-channel report."""
        tasks = []
        for target in targets:
            adapter = self.channels.get(target)
            if adapter is None:
                tasks.append(self._unregistered(target))
            else:
                tasks.append(adapter.push(content, payload={"title": title}))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        report_lines = ["Omni-Channel Dispatch Report:"]
        any_failed = False
        for target, result in zip(targets, results, strict=True):
            if isinstance(result, Exception):
                any_failed = True
                report_lines.append(f"- [{target}]: ❌ {result}")
            else:
                report_lines.append(f"- [{target}]: {result}")
        report = "\n".join(report_lines)

        global_notifier.push(
            "ERROR" if any_failed else "SUCCESS",
            "全渠道分发完成" if not any_failed else "全渠道分发部分失败",
            f"{title}: {', '.join(targets)}",
            {"targets": targets, "title": title},
        )
        return report

    @staticmethod
    async def _unregistered(target: str) -> str:
        raise RuntimeError(f"渠道 {target} 未注册。")


omni_gateway = OmniChannelGateway()
