"""
Hands oskill — composite skills for desktop automation (VEYA_HANDS).

Combines hands oprims into reusable skills:
- hands_approval_gate: checks approval + records action log + screenshot
- hands_operator_loop_step: screenshot → VLM → action selection

Pure logic, no IO/persistence, LLM via injected LLMCaller protocol.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from veya.obase.errors import VeyaError


class HandsOskillError(VeyaError):
    """Error in hands oskill operations."""


@dataclass(frozen=True)
class HandsAction:
    """An action selected by the VLM for hands automation."""
    action: str  # "click", "type", "key", "scroll", "focus_window", "done"
    params: dict[str, Any]
    reasoning: str


class LLMCaller(Protocol):
    """Protocol for LLM/VLM calls (injected by caller)."""

    def __call__(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]: ...


def _encode_image_base64(image_path: str) -> str:
    """Encode image file as base64 data URI."""
    with open(image_path, "rb") as f:
        data = f.read()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _build_hands_tools() -> list[dict[str, Any]]:
    """Build the tool schema for hands actions."""
    return [
        {
            "type": "function",
            "function": {
                "name": "click",
                "description": "Click at screen coordinates",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer", "description": "X coordinate"},
                        "y": {"type": "integer", "description": "Y coordinate"},
                        "button": {"type": "integer", "default": 1, "description": "Mouse button (1=left, 2=middle, 3=right)"},
                    },
                    "required": ["x", "y"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "type",
                "description": "Type text at current cursor position",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text to type"},
                        "delay_ms": {"type": "integer", "default": 0, "description": "Delay between keystrokes in ms"},
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "key",
                "description": "Press a key or key combination",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key_sequence": {"type": "string", "description": "Key sequence (e.g., 'Return', 'ctrl+c', 'alt+Tab')"},
                    },
                    "required": ["key_sequence"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scroll",
                "description": "Scroll at current mouse position",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                        "amount": {"type": "integer", "default": 3, "description": "Number of scroll clicks"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "focus_window",
                "description": "Focus a specific window by ID",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "window_id": {"type": "string", "description": "Window ID from list_windows"},
                    },
                    "required": ["window_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "done",
                "description": "Task completed, no further action needed",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Summary of what was accomplished"},
                    },
                    "required": ["summary"],
                },
            },
        },
    ]


def _build_system_prompt() -> str:
    """Build the system prompt for the hands operator loop."""
    return """You are a desktop automation agent. You see the screen via screenshots and can control the mouse/keyboard.

Available actions:
- click(x, y, button=1): Click at screen coordinates
- type(text, delay_ms=0): Type text at current cursor position
- key(key_sequence): Press key combination (e.g., "Return", "ctrl+c", "alt+Tab")
- scroll(direction="down", amount=3): Scroll at current mouse position
- focus_window(window_id): Focus a specific window
- done(summary): Task completed

Workflow:
1. Analyze the screenshot to understand the current state
2. Decide what action to take next
3. If task is complete, call done() with a summary

Be precise with coordinates. Use list_windows to find window IDs if needed.
Prefer API/MCP tools when available; hands is a last resort for apps without API."""


async def hands_operator_loop_step(
    *,
    llm: LLMCaller,
    objective: str,
    previous_actions: list[HandsAction] | None = None,
    screenshot_path: str | None = None,
    max_tokens: int = 4096,
) -> HandsAction:
    """
    One step of the hands operator loop: screenshot → VLM → action.

    Args:
        llm: Injected LLM/VLM caller
        objective: High-level task description
        previous_actions: History of actions taken so far
        screenshot_path: Optional pre-captured screenshot path
        max_tokens: Max tokens for LLM response

    Returns:
        HandsAction with the selected action and parameters.
    """
    from veya.oprim import hands_screenshot, hands_list_windows

    # Capture screenshot if not provided
    if screenshot_path is None:
        shot_result = hands_screenshot()
        screenshot_path = shot_result["path"]

    # Encode screenshot for VLM
    image_b64 = _encode_image_base64(screenshot_path)

    # Build message history
    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": f"Objective: {objective}"},
    ]

    # Add previous actions as context
    if previous_actions:
        history = "\n".join(
            f"  {a.action}({a.params}) — {a.reasoning}"
            for a in previous_actions
        )
        messages.append({
            "role": "assistant",
            "content": f"Previous actions:\n{history}",
        })

    # Current screenshot
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": "Current screen:"},
            {"type": "image_url", "image_url": {"url": image_b64}},
        ],
    })

    # Call VLM
    response = llm(
        messages=messages,
        tools=_build_hands_tools(),
        max_tokens=max_tokens,
    )

    # Parse tool call
    tool_calls = response.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
    if not tool_calls:
        # No tool call, treat as done
        return HandsAction(
            action="done",
            params={},
            reasoning=response.get("choices", [{}])[0].get("message", {}).get("content", "No tool call returned"),
        )

    tool_call = tool_calls[0]
    func_name = tool_call["function"]["name"]
    import json
    func_args = json.loads(tool_call["function"]["arguments"])

    return HandsAction(
        action=func_name,
        params=func_args,
        reasoning=f"VLM selected {func_name} with {func_args}",
    )


async def hands_approval_gate(
    *,
    action: HandsAction,
    goal_run_id: str,
    bot_id: str,
    approval_resolver: Any = None,
    audit_writer: Any = None,
    side_effect_ledger: Any = None,
) -> dict[str, Any]:
    """
    Run a hands action through the approval gate.

    Uses the existing ActionGatewayAdapter (W6) for approval.
    Records action log + screenshot as GoalRun artifacts.

    Args:
        action: The action to execute
        goal_run_id: Current GoalRun ID
        bot_id: Bot performing the action
        approval_resolver: Optional async approval resolver
        audit_writer: Optional audit writer
        side_effect_ledger: Optional SideEffectLedger

    Returns:
        Result dict with approval decision and execution result.
    """
    from veya.oprim import (
        hands_click, hands_type, hands_key, hands_scroll, hands_focus_window, hands_screenshot
    )

    # Map action to oprim
    action_map = {
        "click": lambda: hands_click(action.params["x"], action.params["y"], button=action.params.get("button", 1)),
        "type": lambda: hands_type(action.params["text"], delay_ms=action.params.get("delay_ms", 0)),
        "key": lambda: hands_key(action.params["key_sequence"]),
        "scroll": lambda: hands_scroll(direction=action.params.get("direction", "down"), amount=action.params.get("amount", 3)),
        "focus_window": lambda: hands_focus_window(action.params["window_id"]),
    }

    if action.action not in action_map:
        return {
            "approved": False,
            "reason": f"Unknown action: {action.action}",
            "executed": False,
        }

    # Build approval request
    approval_request = {
        "action": f"hands.{action.action}",
        "arguments": action.params,
        "reasoning": action.reasoning,
        "goal_run_id": goal_run_id,
        "bot_id": bot_id,
    }

    # Check approval
    approved = True
    reason = "auto-approved (no resolver)"

    if approval_resolver is not None:
        import inspect
        result = approval_resolver(approval_request)
        if inspect.isawaitable(result):
            result = await result
        approved = bool(result)
        reason = "approval granted" if approved else "approval denied"

    if not approved:
        return {
            "approved": False,
            "reason": reason,
            "executed": False,
        }

    # Execute the action
    try:
        result = action_map[action.action]()
        executed = True
        exec_result = result
    except Exception as exc:
        executed = False
        exec_result = {"error": str(exc)}

    # Capture post-action screenshot for audit
    try:
        post_shot = hands_screenshot()
        screenshot_path = post_shot["path"]
    except Exception:
        screenshot_path = None

    # Record in audit log
    audit_record = {
        "action": f"hands.{action.action}",
        "arguments": action.params,
        "reasoning": action.reasoning,
        "approved": approved,
        "executed": executed,
        "result": exec_result,
        "screenshot": screenshot_path,
        "goal_run_id": goal_run_id,
        "bot_id": bot_id,
    }

    if audit_writer is not None:
        import inspect
        result = audit_writer(audit_record)
        if inspect.isawaitable(result):
            await result

    # Record in SideEffectLedger if available
    if side_effect_ledger is not None and executed:
        try:
            await side_effect_ledger.execute(
                goal_run_id=goal_run_id,
                work_item_id=goal_run_id,
                operation_key=f"hands.{action.action}",
                operation_type="hands_action",
                target_ref=action.action,
                request=audit_record,
                provider="hands",
                capability="manual_only",
                bot_id=bot_id,
            )
        except Exception:
            pass  # Non-fatal

    return {
        "approved": approved,
        "reason": reason,
        "executed": executed,
        "result": exec_result,
        "screenshot": screenshot_path,
    }


async def hands_pause_resume(
    *,
    goal_run_id: str,
    paused: bool,
    side_effect_ledger: Any = None,
) -> dict[str, Any]:
    """
    Pause or resume a hands session (human takeover).

    Args:
        goal_run_id: Current GoalRun ID
        paused: True to pause, False to resume
        side_effect_ledger: Optional SideEffectLedger

    Returns:
        Status dict.
    """
    if side_effect_ledger is not None:
        try:
            await side_effect_ledger.execute(
                goal_run_id=goal_run_id,
                work_item_id=goal_run_id,
                operation_key=f"hands.{'pause' if paused else 'resume'}",
                operation_type="hands_control",
                target_ref="session",
                request={"paused": paused},
                provider="hands",
                capability="manual_only",
                bot_id="",  # Filled by caller
            )
        except Exception:
            pass

    return {"status": "paused" if paused else "resumed", "goal_run_id": goal_run_id}


__all__ = [
    "HandsOskillError",
    "HandsAction",
    "LLMCaller",
    "hands_operator_loop_step",
    "hands_approval_gate",
    "hands_pause_resume",
]