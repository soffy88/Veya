"""tests/test_nano_w10_d.py — Unit and integration tests for NANO-W10 Group D.

Covers:
- 主体 6: a11y-first observation ladder (VEYA_A11Y_FIRST)
- ADDENDUM R: DOM reader read_browser (VEYA_DOM_READER)
- ADDENDUM T: Search fallback ladder
- ADDENDUM V: Channel ladder for web reading (VEYA_CHANNEL_LADDER)
- 主体 7c: 6-step tool ladder expansion
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from server import feature_flags
from veya.obase.a11y import (
    capture_browser_a11y,
    capture_desktop_a11y,
    format_a11y_tree,
)
from veya.oskill.pure.observation_ladder import (
    OBSERVATION_TIERS,
    ObservationLadderError,
    ObservationPlan,
    plan_observation,
    screenshot_first_plan,
)


# ===========================================================================
# 1. 主体 6: a11y-first observation ladder
# ===========================================================================


def test_a11y_feature_flag_defaults_off() -> None:
    """Flag VEYA_A11Y_FIRST must default to False."""
    assert feature_flags.enabled("VEYA_A11Y_FIRST") is False


def test_observation_ladder_order_and_tiers() -> None:
    """Tiers must follow a11y -> trimmed_html -> dom_text -> dom_html -> screenshot."""
    assert OBSERVATION_TIERS == (
        "a11y",
        "trimmed_html",
        "dom_text",
        "dom_html",
        "screenshot",
    )


def test_plan_observation_picks_a11y_when_available() -> None:
    """When a11y is available, plan_observation selects a11y with structured fallbacks."""
    available = {
        "a11y": True,
        "trimmed_html": True,
        "dom_text": True,
        "dom_html": True,
        "screenshot": True,
    }
    plan = plan_observation(available)
    assert plan.tier == "a11y"
    assert "trimmed_html" in plan.fallback_tiers
    assert "screenshot" in plan.fallback_tiers

    # Next fallback
    next_plan = plan.next_after_failure()
    assert next_plan is not None
    assert next_plan.tier == "trimmed_html"


def test_plan_observation_falls_back_when_a11y_unavailable() -> None:
    """When a11y is missing, falls back to trimmed_html -> dom_text -> screenshot."""
    available = {
        "a11y": False,
        "trimmed_html": False,
        "dom_text": True,
        "screenshot": True,
    }
    plan = plan_observation(available)
    assert plan.tier == "dom_text"
    assert plan.fallback_tiers == ("screenshot",)


def test_plan_observation_empty_raises_error() -> None:
    """When no source is available, raises ObservationLadderError."""
    with pytest.raises(ObservationLadderError):
        plan_observation({})


def test_screenshot_first_plan_legacy_order() -> None:
    """Legacy order must start with screenshot."""
    available = {
        "a11y": True,
        "trimmed_html": True,
        "screenshot": True,
    }
    plan = screenshot_first_plan(available)
    assert plan.tier == "screenshot"
    assert "trimmed_html" in plan.fallback_tiers
    assert "a11y" in plan.fallback_tiers


def test_format_a11y_tree_formatting() -> None:
    """format_a11y_tree renders roles, names, values, and child indentation."""
    tree = {
        "role": "WebArea",
        "name": "Test Page",
        "children": [
            {
                "role": "heading",
                "name": "Welcome",
                "value": "1",
            },
            {
                "role": "button",
                "name": "Submit",
                "description": "Click to submit",
            },
        ],
    }
    rendered = format_a11y_tree(tree)
    assert '[WebArea] "Test Page"' in rendered
    assert '  [heading] "Welcome" value=1' in rendered
    assert '  [button] "Submit" (Click to submit)' in rendered


@pytest.mark.asyncio
async def test_capture_browser_a11y_with_mock_page() -> None:
    """capture_browser_a11y extracts tree from mock page.accessibility.snapshot."""
    mock_page = MagicMock()
    mock_snapshot = {
        "role": "RootWebArea",
        "name": "Home",
        "children": [{"role": "link", "name": "Docs"}],
    }
    mock_page.accessibility.snapshot = AsyncMock(return_value=mock_snapshot)

    result = await capture_browser_a11y(mock_page)
    assert result["available"] is True
    assert result["tree"] == mock_snapshot
    assert '[RootWebArea] "Home"' in result["formatted"]
    assert '[link] "Docs"' in result["formatted"]


@pytest.mark.asyncio
async def test_capture_browser_a11y_handles_none_or_missing() -> None:
    """Gracefully handles None page or page without accessibility."""
    res1 = await capture_browser_a11y(None)
    assert res1["available"] is False
    assert "no page" in res1["error"]

    res2 = await capture_browser_a11y(object())
    assert res2["available"] is False


def test_capture_desktop_a11y_returns_valid_structure() -> None:
    """Desktop capture always returns dict with standard keys."""
    res = capture_desktop_a11y()
    assert "available" in res
    assert "tree" in res
    assert "formatted" in res
    assert "error" in res
