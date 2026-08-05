"""
veya/omodul/browser_agent.py — Browser Agent Module (Layer 3).

End-to-end browser automation agent. Uses LLM + Playwright to:
1. Navigate websites autonomously
2. Extract structured information
3. Fill forms and interact with web apps
4. Take screenshots for visual feedback

Follows the 3O omodul contract: (config, input, output_dir) -> dict result.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from veya.oprim.browser import (
    BrowserAction,
    BrowserActionResult,
    action_click,
    action_extract_text,
    action_navigate,
    action_screenshot,
    action_type,
    build_selector,
)
from veya.oskill.browser import BrowserSession, run_browser_task


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class BrowserAgentState(StrEnum):
    IDLE = "idle"
    NAVIGATING = "navigating"
    INTERACTING = "interacting"
    EXTRACTING = "extracting"
    DONE = "done"
    ERROR = "error"


@dataclass
class BrowserTaskConfig:
    """Configuration for a browser agent task."""

    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720
    timeout_ms: int = 30000
    max_steps: int = 20
    wait_between_actions_ms: int = 500
    screenshot_interval: int = 5  # screenshot every N steps


@dataclass
class BrowserTaskResult:
    """Result of a browser agent task."""

    success: bool
    url: str = ""
    extracted_data: dict[str, Any] = field(default_factory=dict)
    text_content: str = ""
    screenshots: list[str] = field(default_factory=list)
    steps: int = 0
    duration_ms: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# Browser Agent
# ---------------------------------------------------------------------------


class BrowserAgent:
    """Autonomous browser agent — uses LLM guidance + Playwright execution.

    Can:
    - Navigate to URLs and follow links
    - Fill forms and submit data
    - Extract structured data from pages
    - Take screenshots for visual analysis
    - Handle login flows (with stored auth state)

    Example:
        >>> agent = BrowserAgent(BrowserTaskConfig())
        >>> result = await agent.run_task(
        ...     "https://news.ycombinator.com",
        ...     "Extract the top 5 story titles and their URLs",
        ... )
        >>> print(result.extracted_data)
    """

    def __init__(self, config: BrowserTaskConfig | None = None):
        self.config = config or BrowserTaskConfig()
        self._session: BrowserSession | None = None
        self.llm_handler: Any = None  # injected by caller

    async def run_task(
        self,
        url: str,
        instruction: str,
        *,
        pre_actions: list[BrowserAction] | None = None,
        extract_schema: dict | None = None,
    ) -> BrowserTaskResult:
        """Run a complete browser task.

        Args:
            url: Starting URL.
            instruction: Natural language instruction for the task.
            pre_actions: Actions to execute before the main task (e.g., login).
            extract_schema: JSON schema for structured data extraction.

        Returns:
            BrowserTaskResult.
        """
        start_time = time.time()
        screenshots: list[str] = []

        self._session = BrowserSession(
            headless=self.config.headless,
            viewport_width=self.config.viewport_width,
            viewport_height=self.config.viewport_height,
        )

        try:
            await self._session.start()

            # Execute pre-actions (e.g., login)
            if pre_actions:
                for action in pre_actions:
                    result = await self._session.execute_action(action)
                    if not result.success:
                        return BrowserTaskResult(
                            success=False,
                            error=f"Pre-action failed: {result.error}",
                            duration_ms=(time.time() - start_time) * 1000,
                        )

            # Navigate to target
            nav_result = await self._session.navigate(url)
            if not nav_result.success:
                return BrowserTaskResult(
                    success=False,
                    url=url,
                    error=nav_result.error,
                    duration_ms=(time.time() - start_time) * 1000,
                )

            # Take initial screenshot
            ss = await self._session.screenshot()
            if ss.screenshot_base64:
                screenshots.append(ss.screenshot_base64)

            # Get page content for LLM
            page_text = await self._session.get_text()
            page_html = await self._session.get_html()

            # Use LLM to plan actions (if handler available)
            actions: list[BrowserAction] = []
            if self.llm_handler:
                actions = await self._plan_actions_with_llm(
                    instruction, page_text, page_html, extract_schema
                )
            else:
                # Default: extract text content
                actions = [action_extract_text()]

            # Execute planned actions
            step = 0
            extracted_texts: list[str] = []
            for action in actions[:self.config.max_steps]:
                result = await self._session.execute_action(action)
                step += 1

                if result.text:
                    extracted_texts.append(result.text)
                if result.screenshot_base64 and step % self.config.screenshot_interval == 0:
                    screenshots.append(result.screenshot_base64)

                if not result.success:
                    break

                import asyncio
                await asyncio.sleep(self.config.wait_between_actions_ms / 1000)

            # Final screenshot
            final_ss = await self._session.screenshot()
            if final_ss.screenshot_base64:
                screenshots.append(final_ss.screenshot_base64)

            # Extract structured data if schema provided
            extracted_data: dict[str, Any] = {}
            if extract_schema and self.llm_handler:
                extracted_data = await self._extract_structured(
                    "\n".join(extracted_texts), extract_schema
                )

            return BrowserTaskResult(
                success=True,
                url=self._session.current_url,
                extracted_data=extracted_data,
                text_content="\n".join(extracted_texts),
                screenshots=screenshots,
                steps=step,
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return BrowserTaskResult(
                success=False,
                url=url,
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )
        finally:
            if self._session:
                await self._session.stop()
                self._session = None

    async def _plan_actions_with_llm(
        self,
        instruction: str,
        page_text: str,
        page_html: str,
        extract_schema: dict | None,
    ) -> list[BrowserAction]:
        """Use LLM to plan browser actions based on instruction."""
        prompt = f"""You are a browser automation agent. Given a web page and an instruction, plan a sequence of browser actions.

INSTRUCTION: {instruction}

PAGE CONTENT (first 3000 chars):
{page_text[:3000]}

Available actions: navigate, click, type, press, screenshot, extract_text, extract_html, scroll, wait

For clicking, use text selectors like "text=Login" or CSS selectors.
For typing, specify the selector and the text value.
For extraction, use "extract_text" with optional CSS selector.

Return a JSON array of actions:
[
  {{"action": "click", "selector": "text=Login"}},
  {{"action": "type", "selector": "input[name=email]", "value": "user@example.com"}},
  {{"action": "extract_text", "selector": ".results"}},
]
"""
        if extract_schema:
            prompt += f"\nExtract data matching this JSON schema:\n{json.dumps(extract_schema, indent=2)}"

        if self.llm_handler:
            try:
                response = await self._call_llm(prompt)
                actions_data = self._parse_action_json(response)
                return self._to_browser_actions(actions_data)
            except Exception:
                pass

        return [action_extract_text()]

    async def _extract_structured(
        self, text: str, schema: dict
    ) -> dict[str, Any]:
        """Use LLM to extract structured data from page text."""
        prompt = f"""Extract structured data from this web page content matching the schema.

SCHEMA:
{json.dumps(schema, indent=2)}

CONTENT:
{text[:5000]}

Return ONLY valid JSON matching the schema."""

        if self.llm_handler:
            try:
                response = await self._call_llm(prompt)
                return self._parse_json(response)
            except Exception:
                pass
        return {}

    async def _call_llm(self, prompt: str) -> str:
        """Call the injected LLM handler."""
        if self.llm_handler is None:
            return "[]"
        messages = [{"role": "user", "content": prompt}]
        import asyncio
        if asyncio.iscoroutinefunction(self.llm_handler):
            result = await self.llm_handler(messages)
        else:
            result = self.llm_handler(messages)
        return result.get("content", "[]") if isinstance(result, dict) else str(result)

    @staticmethod
    def _parse_action_json(response: str) -> list[dict]:
        """Parse JSON action plan from LLM response."""
        try:
            # Try direct parse
            return json.loads(response)
        except json.JSONDecodeError:
            pass
        # Try to extract JSON from markdown code block
        import re
        match = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', response)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return []

    @staticmethod
    def _parse_json(response: str) -> dict:
        """Parse JSON from LLM response."""
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            import re
            match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', response)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
        return {}

    @staticmethod
    def _to_browser_actions(data: list[dict]) -> list[BrowserAction]:
        """Convert JSON action dicts to BrowserAction objects."""
        actions: list[BrowserAction] = []
        for item in data:
            action = item.get("action", "")
            selector = item.get("selector")
            value = item.get("value")
            if action in ("click", "type", "extract_text", "extract_html", "select", "hover"):
                actions.append(BrowserAction(action=action, selector=selector, value=value))
            elif action == "navigate":
                actions.append(action_navigate(value or ""))
            elif action == "screenshot":
                actions.append(action_screenshot(selector))
            elif action == "press":
                actions.append(BrowserAction(action="press", value=value))
            elif action in ("scroll", "wait"):
                actions.append(BrowserAction(action=action, value=value))
        return actions


# ---------------------------------------------------------------------------
# omodul interface
# ---------------------------------------------------------------------------


async def run_browser_automation(
    config: Any,
    input_data: Any,
    output_dir: Path = Path("/tmp/veya"),
) -> dict[str, Any]:
    """omodul contract: run browser automation from config + input.

    Args:
        config: SimpleNamespace/dict with BrowserTaskConfig fields.
        input_data: SimpleNamespace/dict with url, instruction, pre_actions, extract_schema.
        output_dir: Output directory for screenshots.

    Returns:
        Dict with success, extracted_data, text, screenshots, stats.
    """
    from types import SimpleNamespace

    if isinstance(input_data, dict):
        input_data = SimpleNamespace(**input_data)
    if isinstance(config, dict):
        config = SimpleNamespace(**config)

    url = getattr(input_data, "url", "https://example.com")
    instruction = getattr(input_data, "instruction", "Extract page content")
    pre_actions = getattr(input_data, "pre_actions", None)
    extract_schema = getattr(input_data, "extract_schema", None)

    task_config = BrowserTaskConfig(
        headless=getattr(config, "headless", True),
        viewport_width=getattr(config, "viewport_width", 1280),
        viewport_height=getattr(config, "viewport_height", 720),
        timeout_ms=getattr(config, "timeout_ms", 30000),
        max_steps=getattr(config, "max_steps", 20),
    )

    agent = BrowserAgent(task_config)
    llm_handler = getattr(input_data, "llm_handler", None)
    if llm_handler:
        agent.llm_handler = llm_handler

    result = await agent.run_task(
        url=url,
        instruction=instruction,
        pre_actions=pre_actions,
        extract_schema=extract_schema,
    )

    # Save screenshots
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for i, ss in enumerate(result.screenshots):
        (output_dir / f"screenshot_{i}.png").write_bytes(
            __import__("base64").b64decode(ss)
        )

    return {
        "status": "completed" if result.success else "error",
        "url": result.url,
        "extracted_data": result.extracted_data,
        "text_content": result.text_content[:5000],
        "screenshots_count": len(result.screenshots),
        "steps": result.steps,
        "duration_ms": result.duration_ms,
        "error": result.error,
    }
