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


# ===========================================================================
# 3. ADDENDUM T: Search fallback ladder
# ===========================================================================

from veya.obase.search_adapters import (
    SEARCH_LADDER_ORDER,
    run_search_ladder_adapter,
)
from veya.oskill.pure.search_ladder import (
    DEFAULT_SEARCH_BACKENDS,
    SEARCH_FAILURE_NOTICE,
    SearchHit,
    SearchLadderError,
    SearchPlan,
    format_search_results,
    merge_search_results,
    plan_search,
    should_fall_back,
)


def test_search_ladder_default_order() -> None:
    """Search order must be configured_provider -> model_native -> duckduckgo -> bing."""
    assert DEFAULT_SEARCH_BACKENDS == (
        "configured_provider",
        "model_native",
        "duckduckgo",
        "bing",
    )
    assert SEARCH_LADDER_ORDER == DEFAULT_SEARCH_BACKENDS


def test_plan_search_picks_configured_provider_when_available() -> None:
    """Picks configured provider first and exposes fallbacks."""
    available = {
        "configured_provider": True,
        "model_native": True,
        "duckduckgo": True,
        "bing": True,
    }
    plan = plan_search(available)
    assert plan.backend == "configured_provider"
    assert plan.fallback_backends == ("model_native", "duckduckgo", "bing")

    next_plan = plan.next_after_failure()
    assert next_plan is not None
    assert next_plan.backend == "model_native"


def test_plan_search_falls_back_when_configured_provider_missing() -> None:
    """Falls back to public search when provider and model native are absent."""
    available = {
        "configured_provider": False,
        "model_native": False,
        "duckduckgo": True,
        "bing": True,
    }
    plan = plan_search(available)
    assert plan.backend == "duckduckgo"
    assert plan.fallback_backends == ("bing",)


def test_plan_search_raises_when_no_backend_available() -> None:
    """Raises SearchLadderError when available is empty."""
    with pytest.raises(SearchLadderError):
        plan_search({})


def test_merge_search_results_deduplication() -> None:
    """Deduplicates hits by normalized URL preserving precedence order."""
    hits1 = [
        SearchHit(url="https://example.com/a/", title="A1", backend="configured_provider"),
        SearchHit(url="https://example.com/b", title="B1", backend="configured_provider"),
    ]
    hits2 = [
        SearchHit(url="https://example.com/a", title="A2 duplicate", backend="duckduckgo"),
        SearchHit(url="https://example.com/c", title="C1", backend="duckduckgo"),
    ]
    merged = merge_search_results(hits1, hits2)
    assert len(merged) == 3
    assert merged[0].url == "https://example.com/a/"
    assert merged[0].backend == "configured_provider"
    assert merged[1].url == "https://example.com/b"
    assert merged[2].url == "https://example.com/c"


def test_format_search_results_structure_and_backend_line() -> None:
    """Every search result block prepends 'Search backend: <name>'."""
    hits = [
        SearchHit(
            url="https://example.com/news",
            title="News Page",
            snippet="Breaking news today.",
            backend="duckduckgo",
        ),
    ]
    formatted = format_search_results(hits)
    assert formatted.startswith("Search backend: duckduckgo")
    assert "Title: News Page" in formatted
    assert "URL: https://example.com/news" in formatted
    assert "Snippet: Breaking news today." in formatted


def test_format_search_results_failure_notice_when_empty() -> None:
    """When no results found, instructs the model not to guess URLs."""
    assert format_search_results([]) == SEARCH_FAILURE_NOTICE
    assert "Do not guess or fabricate URLs" in SEARCH_FAILURE_NOTICE


@pytest.mark.asyncio
async def test_run_search_ladder_adapter_fallback() -> None:
    """run_search_ladder_adapter steps through backends until one succeeds."""
    with patch(
        "veya.obase.search_adapters.execute_search_backend",
        side_effect=[
            [],  # configured_provider fails
            [{"url": "https://bing.com/hit", "title": "Hit", "snippet": "Text"}],  # model_native succeeds
        ],
    ):
        winning_backend, hits, text = await run_search_ladder_adapter(
            "test query",
            backends=("configured_provider", "model_native"),
        )
        assert winning_backend == "model_native"
        assert len(hits) == 1
        assert "Search backend: model_native" in text
        assert "URL: https://bing.com/hit" in text


@pytest.mark.asyncio
async def test_run_search_ladder_adapter_all_failed() -> None:
    """When all backends fail, returns failure notice."""
    with patch("veya.obase.search_adapters.execute_search_backend", return_value=[]):
        winning_backend, hits, text = await run_search_ladder_adapter(
            "nonexistent query",
            backends=("configured_provider", "duckduckgo"),
        )
        assert winning_backend == "none"
        assert hits == []
        assert text == SEARCH_FAILURE_NOTICE


# ===========================================================================
# 4. ADDENDUM V: Channel ladder for web reading
# ===========================================================================

import json
from cli.product import run_doctor
from server.channel_tools import tool_read_channel
from veya.obase.channel_probes import (
    WEB_CHANNEL_LADDERS,
    get_cached_channel_statuses,
    probe_all_channels,
    probe_backend,
    probe_channel,
)
from veya.oskill.pure.channel_ladder import (
    LOGIN_REQUIRED_BACKENDS,
    WEB_READING_CHANNELS,
    ChannelLadderError,
    WebChannelPlan,
    plan_web_channel_backend,
    reorder_channel_backends,
)


def test_channel_ladder_flag_defaults_off() -> None:
    """Flag VEYA_CHANNEL_LADDER must default to False."""
    assert feature_flags.enabled("VEYA_CHANNEL_LADDER") is False


def test_web_reading_channel_ladders_definition() -> None:
    """All 9 web reading channels must have their ordered backend ladders."""
    expected_channels = {
        "web",
        "youtube",
        "rss",
        "github",
        "bilibili",
        "reddit",
        "twitter",
        "xiaohongshu",
        "linkedin",
    }
    assert expected_channels.issubset(set(WEB_READING_CHANNELS.keys()))
    assert WEB_READING_CHANNELS["web"] == ("jina", "dom_reader", "http_fetch")
    assert WEB_READING_CHANNELS["youtube"] == ("yt_dlp", "browser")
    assert WEB_READING_CHANNELS["rss"] == ("feedparser", "http_fetch")
    assert WEB_READING_CHANNELS["github"] == ("gh_cli", "api")


def test_plan_web_channel_backend_order_and_fallback() -> None:
    """plan_web_channel_backend follows the channel ladder."""
    # When jina is available, picks jina
    plan = plan_web_channel_backend(
        "web",
        {"jina": True, "dom_reader": True, "http_fetch": True},
    )
    assert plan.active_backend == "jina"
    assert plan.fallback_backends == ("dom_reader", "http_fetch")

    # When jina fails, falls back to dom_reader
    fallback = plan.next_after_failure()
    assert fallback is not None
    assert fallback.active_backend == "dom_reader"
    assert fallback.fallback_backends == ("http_fetch",)


def test_plan_web_channel_backend_login_required() -> None:
    """Identifies backends requiring login/credentials."""
    plan = plan_web_channel_backend(
        "xiaohongshu",
        {"cli_xiaohongshu": True, "browser": True},
    )
    assert plan.active_backend == "cli_xiaohongshu"
    assert plan.needs_login is True
    assert "cli_xiaohongshu" in LOGIN_REQUIRED_BACKENDS


def test_reorder_channel_backends_pure() -> None:
    """Reordering backends changes preference order without modifying code."""
    new_order = reorder_channel_backends("web", ["dom_reader", "http_fetch", "jina"])
    assert new_order == ("dom_reader", "http_fetch", "jina")

    # Partial reorder puts listed backends first
    partial = reorder_channel_backends("web", ["http_fetch"])
    assert partial[0] == "http_fetch"
    assert "dom_reader" in partial
    assert "jina" in partial


def test_reorder_channel_backends_invalid_backend() -> None:
    """Reordering with an invalid backend raises ChannelLadderError."""
    with pytest.raises(ChannelLadderError):
        reorder_channel_backends("web", ["invalid_backend"])


@pytest.mark.asyncio
async def test_probe_backend_and_channel() -> None:
    """probe_backend and probe_channel return structured probe results."""
    # Probe http_fetch which is always ok
    status, hint = await probe_backend("http_fetch")
    assert status == "ok"
    assert hint == ""

    # Probe channel web (will hit http_fetch as working backend)
    res = await probe_channel("web")
    assert res["channel"] == "web"
    assert res["status"] == "ok"
    assert res["active_backend"] in ("jina", "dom_reader", "http_fetch")
    assert "last_probe_time" in res


@pytest.mark.asyncio
async def test_probe_login_channels_report_needs_login() -> None:
    """Login channels report needs-login by default."""
    res = await probe_channel("xiaohongshu")
    assert res["channel"] == "xiaohongshu"
    # Unless env cookie is set, reports needs-login or fallback
    assert res["status"] in ("needs-login", "ok", "broken")
    if res["status"] == "needs-login":
        assert "Login required" in res["fix_hint"]


@pytest.mark.asyncio
async def test_probe_all_channels() -> None:
    """probe_all_channels collects statuses for all web reading channels."""
    all_res = await probe_all_channels()
    assert len(all_res) == len(WEB_CHANNEL_LADDERS)
    for ch in ("web", "youtube", "rss", "github"):
        assert ch in all_res
        assert "status" in all_res[ch]
        assert "active_backend" in all_res[ch]


def test_veya_doctor_channels_cli(capsys: pytest.CaptureFixture[str]) -> None:
    """veya doctor channels prints readable table."""
    code = run_doctor(["channels"])
    assert code == 0
    captured = capsys.readouterr()
    assert "网页阅读频道阶梯自检" in captured.out
    assert "web" in captured.out
    assert "youtube" in captured.out
    assert "github" in captured.out


def test_veya_doctor_channels_json_cli(capsys: pytest.CaptureFixture[str]) -> None:
    """veya doctor channels --json prints valid json dictionary."""
    code = run_doctor(["channels", "--json"])
    assert code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, dict)
    assert "web" in data
    assert "status" in data["web"]


@pytest.mark.asyncio
async def test_tool_read_channel_disabled_when_flag_off() -> None:
    """read_channel returns disabled notice when flag is OFF."""
    with patch("server.feature_flags.enabled", return_value=False):
        res = await tool_read_channel("web", "https://example.com")
        assert "disabled" in res
        assert "VEYA_CHANNEL_LADDER is OFF" in res


@pytest.mark.asyncio
async def test_tool_read_channel_with_provenance() -> None:
    """read_channel marks output with firewall provenance."""
    with patch("server.feature_flags.enabled", return_value=True):
        with patch("server.channel_tools.probe_channel", return_value={"status": "ok", "active_backend": "test_mock"}):
            res = await tool_read_channel("web", "https://example.com/data")
            assert "EXTERNAL DATA" in res
            assert "[Channel: web | Backend: test_mock]" in res


def test_read_channel_registered_in_master_tools() -> None:
    """read_channel must be registered in master_tools."""
    assert master_tools.has("read_channel") is True
    schemas = [s for s in master_tools.get_all_schemas() if s["function"]["name"] == "read_channel"]
    assert len(schemas) == 1
    assert "read_channel" in schemas[0]["function"]["name"]


# ===========================================================================
# 5. 主体 7c: 6-step tool ladder expansion
# ===========================================================================

from veya.oskill.cli_hub import (
    FULL_TOOL_LADDER_ORDER,
    full_6step_tool_ladder,
)


def test_full_tool_ladder_6step_order() -> None:
    """Full ladder must order: context -> connector_api -> web_search -> browser -> desktop -> ask_human."""
    assert FULL_TOOL_LADDER_ORDER == (
        "context",
        "connector_api",
        "web_search",
        "browser",
        "desktop",
        "ask_human",
    )


def test_full_6step_tool_ladder_prefers_context() -> None:
    """When context tools are present, context is chosen first with zero overhead."""
    sources = {
        "context": ["in_memory_recent_turn"],
        "api": ["http_api"],
        "web_search": ["duckduckgo"],
        "browser": ["read_browser"],
        "desktop": ["hands_screenshot"],
        "ask_human": ["escalate_to_user"],
    }
    result = full_6step_tool_ladder(["solve_task"], sources)
    assert result["chosen"] == "context"
    assert result["ranked"][0]["step"] == "context"
    assert "zero overhead" in result["ranked"][0]["reason"]


def test_full_6step_tool_ladder_step_by_step_fallback() -> None:
    """Ladder steps fall back in exact descending order."""
    # Without context -> connector_api wins
    sources = {
        "api": ["weather_api"],
        "web_search": ["duckduckgo"],
        "browser": ["read_browser"],
    }
    res = full_6step_tool_ladder(["fetch_weather"], sources)
    assert res["chosen"] == "connector_api"

    # Without API -> web_search wins
    sources = {
        "web_search": ["bing"],
        "browser": ["read_browser"],
    }
    res = full_6step_tool_ladder(["news"], sources)
    assert res["chosen"] == "web_search"

    # Without search -> browser wins
    sources = {
        "browser": ["read_browser"],
        "desktop": ["hands"],
    }
    res = full_6step_tool_ladder(["scrape"], sources)
    assert res["chosen"] == "browser"

    # Without browser -> desktop wins
    sources = {
        "desktop": ["hands"],
        "ask_human": ["escalate"],
    }
    res = full_6step_tool_ladder(["gui_task"], sources)
    assert res["chosen"] == "desktop"

    # Only ask_human
    sources = {
        "ask_human": ["escalate"],
    }
    res = full_6step_tool_ladder(["ambiguous"], sources)
    assert res["chosen"] == "ask_human"

    # Empty sources -> none
    res = full_6step_tool_ladder(["empty"], {})
    assert res["chosen"] == "none"
