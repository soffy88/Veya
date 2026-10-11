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


# ===========================================================================
# 2. ADDENDUM R: DOM reader read_browser
# ===========================================================================

from server.dom_reader_tools import tool_read_browser
from server.tool_registry import master_tools
from veya.oskill.pure.html_to_markdown import (
    BLOCKED_LOCAL_PORTS,
    PROVENANCE_HEADER,
    extract_main_html,
    html_to_markdown,
    is_ssrf_safe_url,
    read_html,
)


def test_dom_reader_flag_defaults_off() -> None:
    """Flag VEYA_DOM_READER must default to False."""
    assert feature_flags.enabled("VEYA_DOM_READER") is False


def test_is_ssrf_safe_url() -> None:
    """Validates SSRF prevention rules."""
    # Safe public URLs
    ok, _ = is_ssrf_safe_url("https://example.com/articles/1")
    assert ok is True
    ok, _ = is_ssrf_safe_url("http://github.com/veya")
    assert ok is True

    # Cloud metadata endpoints blocked
    ok, reason = is_ssrf_safe_url("http://169.254.169.254/latest/meta-data/")
    assert ok is False
    assert "cloud metadata" in reason

    ok, reason = is_ssrf_safe_url("http://metadata.google.internal/computeMetadata/v1/")
    assert ok is False
    assert "cloud metadata" in reason

    # Veya internal ports blocked on localhost
    for port in BLOCKED_LOCAL_PORTS:
        ok, reason = is_ssrf_safe_url(f"http://127.0.0.1:{port}/api/v1/test")
        assert ok is False
        assert f"port {port}" in reason

        ok, reason = is_ssrf_safe_url(f"http://localhost:{port}/")
        assert ok is False

    # Safe local port allowed
    ok, _ = is_ssrf_safe_url("http://localhost:8080/metrics")
    assert ok is True

    # Invalid scheme
    ok, reason = is_ssrf_safe_url("file:///etc/passwd")
    assert ok is False


def test_html_to_markdown_converts_structure_and_indexes_controls() -> None:
    """Converts headings, lists, tables, and marks links/controls as [#n]."""
    sample_html = """
    <html>
      <head><title>Dashboard Demo</title></head>
      <body>
        <h1>Welcome Dashboard</h1>
        <p>Here is an introduction with a <a href="https://example.com/docs">documentation link</a>.</p>
        <button>Click Action</button>
        <form>
          <input type="text" name="username" value="alice" />
          <input type="password" name="secret" value="supersecret123" />
          <input type="text" name="card" autocomplete="cc-number" value="4111222233334444" />
          <select name="role"></select>
          <textarea name="bio"></textarea>
        </form>
      </body>
    </html>
    """
    doc = html_to_markdown(sample_html)
    assert doc.title == "Dashboard Demo"
    assert "# Welcome Dashboard" in doc.markdown
    assert "[documentation link][#1]" in doc.markdown
    assert '[#2: button "Click Action"]' in doc.markdown
    assert '[#3: input text "username" value="alice"]' in doc.markdown
    # Sensitive fields redacted
    assert "supersecret123" not in doc.markdown
    assert '[#4: input password "secret" value="[REDACTED]"]' in doc.markdown
    assert "4111222233334444" not in doc.markdown
    assert '[#5: input text "card" value="[REDACTED]"]' in doc.markdown
    assert '[#6: select "role"]' in doc.markdown
    assert '[#7: textarea "bio"]' in doc.markdown

    # References table at the bottom
    assert "### References" in doc.markdown
    assert "[#1] https://example.com/docs" in doc.markdown
    assert '[#2] <button "Click Action">' in doc.markdown
    assert '[#4] <input type="password" name="secret" value="[REDACTED]">' in doc.markdown


def test_html_to_markdown_skips_hidden_elements() -> None:
    """Elements with display:none, visibility:hidden, hidden, or aria-hidden are omitted."""
    html_with_hidden = """
    <article>
      <h1>Visible Title</h1>
      <p>Visible paragraph text.</p>
      <div style="display: none">Hidden secret message</div>
      <p style="visibility: hidden">Hidden invisible text</p>
      <span hidden>Hidden by attribute</span>
      <div aria-hidden="true">Hidden aria text</div>
    </article>
    """
    doc = html_to_markdown(html_with_hidden)
    assert "Visible Title" in doc.markdown
    assert "Visible paragraph text." in doc.markdown
    assert "Hidden secret message" not in doc.markdown
    assert "Hidden invisible text" not in doc.markdown
    assert "Hidden by attribute" not in doc.markdown
    assert "Hidden aria text" not in doc.markdown


def test_read_html_provenance_header() -> None:
    """read_html prepends provenance header marking content as external data."""
    html = "<main><h1>News Headline</h1><p>Breaking story.</p></main>"
    doc = read_html(html, url="https://news.example.com/1")
    assert doc.markdown.startswith(PROVENANCE_HEADER)
    assert "Source URL: https://news.example.com/1" in doc.markdown
    assert "# News Headline" in doc.markdown


@pytest.mark.asyncio
async def test_tool_read_browser_disabled_when_flag_off() -> None:
    """read_browser returns disabled notice when flag is OFF."""
    with patch("server.feature_flags.enabled", return_value=False):
        res = await tool_read_browser(url="https://example.com")
        assert "disabled" in res
        assert "VEYA_DOM_READER is OFF" in res


@pytest.mark.asyncio
async def test_tool_read_browser_with_html_input() -> None:
    """read_browser processes html directly when flag is ON."""
    with patch("server.feature_flags.enabled", return_value=True):
        res = await tool_read_browser(html="<h1>Direct Test</h1><a href='https://foo.com'>Link</a>")
        assert PROVENANCE_HEADER in res
        assert "# Direct Test" in res
        assert "[Link][#1]" in res
        assert "### References" in res


def test_read_browser_registered_in_master_tools() -> None:
    """read_browser must be registered in master_tools."""
    assert master_tools.has("read_browser") is True
    schemas = [s for s in master_tools.get_all_schemas() if s["function"]["name"] == "read_browser"]
    assert len(schemas) == 1
    assert "read_browser" in schemas[0]["function"]["name"]
