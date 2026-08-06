"""browser_use 技能包 — 自然语言驱动浏览器执行目标。

安全约定:
  - 本技能做真实网络操作 (LLM 决定点击/输入), 不得放入 run_in_sandbox;
  - 凭证走 ~/.veya/browser-profiles/ (登录态持久化), 不接收对话内 token;
  - URL 由调用方保证已过 SSRF 白名单 (上层 redact/security hook)。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROFILES_DIR = Path.home() / ".veya" / "browser-profiles"


def main(goal: str, url: str = "", max_steps: int = 10, **_: Any) -> dict[str, Any]:
    """执行浏览器目标, 返回结构化结果。browser_use 未安装时给出安装指引。"""
    try:
        from browser_use import Agent
    except ImportError as exc:
        raise RuntimeError(
            "browser_use 未安装。安装: pip install browser-use "
            f"(且 playwright 浏览器已就绪: playwright install chromium)。({exc})"
        ) from exc

    # LLM 复用 Veya provider 链 (环境变量 ANTHROPIC/OPENAI/DASHSCOPE_API_KEY
    # 或 VEYA_LLM_ENDPOINT 指向 OpenAI 兼容本地端点)。
    from browser_use import Browser
    from browser_use.llm import LLM  # type: ignore[attr-defined]

    import asyncio

    async def _run() -> dict[str, Any]:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        browser = Browser(
            headless=True,
            # 登录态复用: 独立 profile, 不与系统浏览器互扰
            user_data_dir=str(PROFILES_DIR / "default"),
        )
        llm = LLM(provider="litellm", model=os.environ.get("VEYA_LLM_MODEL") or "anthropic/claude-sonnet-4-6")
        agent = Agent(task=goal, llm=llm, browser=browser, max_steps=max_steps)
        result = await agent.run()
        await browser.close()
        return {
            "status": "done",
            "steps": getattr(result, "step_count", None) or len(getattr(result, "history", [])),
            "final": str(getattr(result, "final_result", "") or "")[:2000],
        }

    return asyncio.run(_run())
