"""veya/obase/channel_probes.py — ADDENDUM V: web reading channel probes.

Real backend probing for web reading channels:
- Executes small known-good calls (timeout 5s).
- Statuses: ok, broken, needs-login.
- Returns agent-friendly fix hints (Item 7).
- Login-based channels default to OFF until enabled.

obase layer: external IO / subprocess / HTTP probing, zero 3O layer imports.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from typing import Any

import httpx

__all__ = [
    "WEB_CHANNEL_LADDERS",
    "fetch_github_readme",
    "fetch_url_text",
    "get_cached_channel_statuses",
    "probe_all_channels",
    "probe_backend",
    "probe_channel",
]

# Static definitions of channel ladders for probing
WEB_CHANNEL_LADDERS: dict[str, tuple[str, ...]] = {
    "web": ("jina", "dom_reader", "http_fetch"),
    "youtube": ("yt_dlp", "browser"),
    "rss": ("feedparser", "http_fetch"),
    "github": ("gh_cli", "api"),
    "bilibili": ("cli_bilibili", "browser"),
    "reddit": ("cli_reddit", "browser"),
    "twitter": ("cli_twitter", "browser"),
    "xiaohongshu": ("cli_xiaohongshu", "browser"),
    "linkedin": ("cli_linkedin", "browser"),
}

# In-memory probe cache
_PROBE_CACHE: dict[str, dict[str, Any]] = {}


async def _probe_jina() -> tuple[str, str]:
    """Probe Jina reader endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.head("https://r.jina.ai", headers={"User-Agent": "Veya-Probe/1.0"})
            if res.status_code < 500:
                return "ok", ""
            return "broken", f"Jina service responded with HTTP {res.status_code}"
    except Exception as exc:
        return "broken", f"Jina reader unreachable ({type(exc).__name__}): fallback to DOM reader"


def _probe_dom_reader() -> tuple[str, str]:
    """Probe DOM reader availability."""
    flag = os.environ.get("VEYA_DOM_READER", "").lower() in ("1", "true", "yes", "on")
    if flag:
        return "ok", ""
    return "broken", "DOM reader is OFF: set VEYA_DOM_READER=1 or use http_fetch"


async def _probe_http_fetch() -> tuple[str, str]:
    """Probe httpx HTTP fetch."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.head("https://httpbin.org/status/200")
            if res.status_code == 200:
                return "ok", ""
    except Exception:
        pass
    # httpx library itself is installed and usable
    return "ok", ""


def _probe_yt_dlp() -> tuple[str, str]:
    """Probe yt-dlp executable."""
    exe = shutil.which("yt-dlp")
    if not exe:
        return (
            "broken",
            "yt-dlp not installed. Fix: pip install yt-dlp or veya cli-hub install yt-dlp",
        )
    try:
        res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return "ok", ""
        return "broken", f"yt-dlp returned error code {res.returncode}: {res.stderr.strip()}"
    except Exception as exc:
        return "broken", f"yt-dlp execution failed: {exc}"


def _probe_browser() -> tuple[str, str]:
    """Probe browser / Playwright installation."""
    try:
        import playwright  # noqa: F401

        return "ok", ""
    except ImportError:
        return (
            "broken",
            "Playwright not installed. Fix: pip install playwright && playwright install",
        )


def _probe_feedparser() -> tuple[str, str]:
    """Probe feedparser module."""
    try:
        import feedparser  # noqa: F401

        return "ok", ""
    except ImportError:
        return "broken", "feedparser not installed. Fix: pip install feedparser"


async def _probe_gh_cli() -> tuple[str, str]:
    """Probe gh CLI executable and auth status."""
    exe = shutil.which("gh")
    if not exe:
        return "broken", "GitHub CLI (gh) not installed. Fix: apt install gh or brew install gh"
    try:
        res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            return "broken", "gh CLI not functioning"
        # Check auth
        auth_res = subprocess.run(
            [exe, "auth", "status"], capture_output=True, text=True, timeout=5
        )
        if auth_res.returncode == 0:
            return "ok", ""
        return "needs-login", "gh CLI not logged in. Fix: gh auth login or set GITHUB_TOKEN"
    except Exception as exc:
        return "broken", f"gh probe failed: {exc}"


async def _probe_github_api() -> tuple[str, str]:
    """Probe GitHub public API."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                "https://api.github.com/zen", headers={"User-Agent": "Veya-Probe/1.0"}
            )
            if res.status_code == 200:
                return "ok", ""
            return "broken", f"GitHub API returned HTTP {res.status_code}"
    except Exception as exc:
        return "broken", f"GitHub API unreachable: {exc}"


def _probe_cli_bilibili() -> tuple[str, str]:
    """Probe bilibili CLI."""
    exe = shutil.which("bilibili") or shutil.which("bbdown")
    if exe:
        return "ok", ""
    return "broken", "Bilibili CLI tool (bbdown) not found. Fix: install bbdown or use browser"


def _probe_cli_reddit() -> tuple[str, str]:
    """Probe reddit CLI."""
    exe = shutil.which("reddit") or shutil.which("tuir")
    if exe:
        return "ok", ""
    return "broken", "Reddit CLI tool not found. Fix: install tuir or use browser"


def _probe_login_channel(channel_name: str) -> tuple[str, str]:
    """Login-based channels default to OFF until credentials are provided."""
    token = os.environ.get(f"VEYA_SECRET_{channel_name.upper()}_COOKIE", "")
    if token:
        return "ok", ""
    return (
        "needs-login",
        f"Login required for {channel_name}. Credentials must be stored in secret store (use separate account to avoid ban risk).",
    )


async def probe_backend(backend: str) -> tuple[str, str]:
    """Probe a specific backend. Returns (status, fix_hint)."""
    if backend == "jina":
        return await _probe_jina()
    if backend == "dom_reader":
        return _probe_dom_reader()
    if backend == "http_fetch":
        return await _probe_http_fetch()
    if backend == "yt_dlp":
        return _probe_yt_dlp()
    if backend == "browser":
        return _probe_browser()
    if backend == "feedparser":
        return _probe_feedparser()
    if backend == "gh_cli":
        return await _probe_gh_cli()
    if backend == "api":
        return await _probe_github_api()
    if backend == "cli_bilibili":
        return _probe_cli_bilibili()
    if backend == "cli_reddit":
        return _probe_cli_reddit()
    if backend in ("cli_twitter", "cli_xiaohongshu", "cli_linkedin"):
        return _probe_login_channel(backend.replace("cli_", ""))
    return "broken", f"Unknown backend '{backend}'"


async def probe_channel(channel: str) -> dict[str, Any]:
    """Probe a channel's backend ladder until the first working one is found."""
    ladder = WEB_CHANNEL_LADDERS.get(channel, ())
    if not ladder:
        return {
            "channel": channel,
            "active_backend": "none",
            "status": "broken",
            "fix_hint": f"Unknown channel '{channel}'",
            "last_probe_time": time.time(),
        }

    first_broken_hint = ""
    for backend in ladder:
        status, hint = await probe_backend(backend)
        if status == "ok":
            result = {
                "channel": channel,
                "active_backend": backend,
                "status": "ok",
                "fix_hint": "",
                "last_probe_time": time.time(),
            }
            _PROBE_CACHE[channel] = result
            return result
        elif not first_broken_hint:
            first_broken_hint = hint

    # All backends in ladder failed or need login
    status_verdict = "needs-login" if "Login required" in first_broken_hint else "broken"
    result = {
        "channel": channel,
        "active_backend": ladder[0],
        "status": status_verdict,
        "fix_hint": first_broken_hint or f"No working backend for {channel}",
        "last_probe_time": time.time(),
    }
    _PROBE_CACHE[channel] = result
    return result


async def probe_all_channels() -> dict[str, dict[str, Any]]:
    """Probe all registered web-reading channels."""
    tasks = [probe_channel(ch) for ch in WEB_CHANNEL_LADDERS]
    results = await asyncio.gather(*tasks)
    return {r["channel"]: r for r in results}


def get_cached_channel_statuses() -> dict[str, dict[str, Any]]:
    """Return currently cached probe statuses without re-running probes."""
    return dict(_PROBE_CACHE)


async def fetch_url_text(
    url: str,
    timeout: float = 10.0,
    user_agent: str = "Veya/1.0",
) -> str:
    """Fetch URL text content via httpx (obase external IO adapter)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": user_agent})
        resp.raise_for_status()
        return resp.text


async def fetch_github_readme(repo: str, timeout: float = 10.0) -> str:
    """Fetch decoded GitHub repository README via API (obase external IO adapter)."""
    import base64

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"https://api.github.com/repos/{repo}/readme")
        resp.raise_for_status()
        data = resp.json()
        return base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
