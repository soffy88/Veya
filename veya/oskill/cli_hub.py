"""CLI-Hub oskill — the tool ladder and output verification (NANO-W8).

Two pure algorithms, no IO and no LLM:

* :func:`cli_hub_tool_ladder` — ranks the ways a task can be carried out:
  ``api/mcp`` first (an API is the contract the app already promises), then a
  CLI-Hub CLI (structured, scriptable), and desktop *hands* last.  Reaching for
  hands is always recorded with the reason no API and no CLI was usable, so a
  fragile screen-driving step is visible in the run instead of silent.
* :func:`cli_hub_verify_output` — decides whether a produced file is really the
  thing the step claimed to produce.  A zero exit code is a process opinion,
  not evidence: a truncated PDF, an empty PNG, or a renamed text file all exit
  0.

Both take their input as values, so the same answers come out in a test, in a
GoalRun step, and in a report.
"""

from __future__ import annotations

from typing import Any

#: The ladder, best first. Order is the policy: API → MCP → CLI → hands.
LADDER_ORDER: tuple[str, ...] = ("api", "mcp", "cli_hub", "hands")

#: Full 6-step tool ladder order per NANO-W10 主体 7c:
#: context -> connector/MCP/API -> web search -> browser -> desktop -> ask human
FULL_TOOL_LADDER_ORDER: tuple[str, ...] = (
    "context",
    "connector_api",
    "web_search",
    "browser",
    "desktop",
    "ask_human",
)

#: Magic-byte signature per expected file type.
_MAGIC_BYTES: dict[str, bytes] = {
    "pdf": b"%PDF",
    "png": b"\x89PNG\r\n\x1a\n",
    "jpg": b"\xff\xd8\xff",
    "jpeg": b"\xff\xd8\xff",
    "zip": b"PK\x03\x04",
    "docx": b"PK\x03\x04",
    "xlsx": b"PK\x03\x04",
    "pptx": b"PK\x03\x04",
}

#: Canonical MIME type for the types the UI needs to render a link.
_MIME_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "mp4": "video/mp4",
    "wav": "audio/wav",
}

def cli_hub_tool_ladder(
    task_tags: list[str],
    available_tools: dict[str, list[str]],
) -> dict[str, Any]:
    """Rank the ways this task could be carried out.

    Args:
        task_tags: What the task needs ("web-search", "legacy-app", ...).
        available_tools: ``{source: [tool names]}`` for "api", "mcp",
            "cli_hub" and "hands".

    Returns:
        ``{"ranked": [{"source", "tools", "reason"}...], "tags", "chosen"}``
        where ``ranked`` is ordered best-first and every entry says why.
    """
    tags = [str(tag) for tag in (task_tags or [])]
    ranked: list[dict[str, Any]] = []

    api_tools = list(available_tools.get("api") or [])
    mcp_tools = list(available_tools.get("mcp") or [])
    if api_tools:
        ranked.append({
            "source": "api",
            "tools": api_tools,
            "reason": "an API is the contract the app already publishes",
        })
    if mcp_tools:
        ranked.append({
            "source": "mcp",
            "tools": mcp_tools,
            "reason": "an MCP server exposes the same capability as a typed tool",
        })

    cli_tools = list(available_tools.get("cli_hub") or [])
    if cli_tools:
        ranked.append({
            "source": "cli_hub",
            "tools": cli_tools,
            "reason": "no API or MCP tool covers this task; a CLI is structured and scriptable",
        })

    hands_tools = list(available_tools.get("hands") or [])
    if hands_tools:
        # The reason is the point of this entry: screen-driving is the fragile
        # option and a run must be able to show why it was chosen.
        missing = [name for name in ("api", "mcp", "cli_hub") if not available_tools.get(name)]
        ranked.append({
            "source": "hands",
            "tools": hands_tools,
            "reason": (
                "desktop hands used because no usable CLI or API exists for "
                f"tags {tags or ['unspecified']} (unavailable: {', '.join(missing) or 'none'})"
            ),
            "unavailable_sources": missing,
        })

    if not ranked:
        ranked.append({
            "source": "none",
            "tools": [],
            "reason": "no API, MCP, CLI or hands tool is available for this task",
        })

    ranked.sort(key=lambda entry: LADDER_ORDER.index(entry["source"])
                if entry["source"] in LADDER_ORDER else len(LADDER_ORDER))
    return {
        "tags": tags,
        "ranked": ranked,
        "chosen": ranked[0]["source"],
    }


def full_6step_tool_ladder(
    task_tags: list[str],
    available_sources: dict[str, list[str]],
) -> dict[str, Any]:
    """Rank the 6 ways a task can be carried out per NANO-W10 主体 7c:

    1. context: in-context memory, previous turn artifacts
    2. connector_api: structured API / MCP / CLI
    3. web_search: search fallback ladder
    4. browser: DOM-to-Markdown reader / headless browser
    5. desktop: hands / GUI automation
    6. ask_human: human clarification / escalation

    Returns:
        {"tags": tags, "ranked": [{"step", "tools", "reason"}...], "chosen": str}
    """
    tags = [str(t) for t in (task_tags or [])]
    ranked: list[dict[str, Any]] = []

    # 1. context
    ctx = list(available_sources.get("context") or [])
    if ctx:
        ranked.append({
            "step": "context",
            "tools": ctx,
            "reason": "in-context memory and recent artifacts have zero overhead and no side effects",
        })

    # 2. connector/MCP/API
    api_tools = list(
        (available_sources.get("connector_api") or [])
        + (available_sources.get("api") or [])
        + (available_sources.get("mcp") or [])
        + (available_sources.get("cli_hub") or [])
    )
    if api_tools:
        ranked.append({
            "step": "connector_api",
            "tools": api_tools,
            "reason": "structured programmatic interfaces (API/MCP/CLI) are typed and scriptable",
        })

    # 3. web search
    search_tools = list(available_sources.get("web_search") or [])
    if search_tools:
        ranked.append({
            "step": "web_search",
            "tools": search_tools,
            "reason": "search fallback ladder resolves public questions without executing code",
        })

    # 4. browser
    browser_tools = list(
        (available_sources.get("browser") or [])
        + (available_sources.get("read_browser") or [])
    )
    if browser_tools:
        ranked.append({
            "step": "browser",
            "tools": browser_tools,
            "reason": "DOM reader or headless browser reads unstructured web pages",
        })

    # 5. desktop
    desktop_tools = list(
        (available_sources.get("desktop") or [])
        + (available_sources.get("hands") or [])
    )
    if desktop_tools:
        ranked.append({
            "step": "desktop",
            "tools": desktop_tools,
            "reason": "desktop GUI automation is fragile and used only when no programmatic interface exists",
        })

    # 6. ask human
    human_tools = list(
        (available_sources.get("ask_human") or [])
        + (available_sources.get("human") or [])
    )
    if human_tools:
        ranked.append({
            "step": "ask_human",
            "tools": human_tools,
            "reason": "human escalation requested when automated tools cannot resolve ambiguous intent",
        })

    if not ranked:
        ranked.append({
            "step": "none",
            "tools": [],
            "reason": "no tools available across the 6-step ladder",
        })

    ranked.sort(
        key=lambda entry: FULL_TOOL_LADDER_ORDER.index(entry["step"])
        if entry["step"] in FULL_TOOL_LADDER_ORDER
        else len(FULL_TOOL_LADDER_ORDER)
    )

    return {
        "tags": tags,
        "ranked": ranked,
        "chosen": ranked[0]["step"],
    }


def cli_hub_verify_output(
    file_path: str,
    expected_type: str,
    content: bytes,
    max_size_mb: float | None = None,
    max_duration_s: float | None = None,
) -> dict[str, Any]:
    """Whether ``content`` really is the ``expected_type`` the step promised.

    Args:
        file_path: The produced path, recorded in the verdict.
        expected_type: "pdf", "png", "jpg", "zip", "docx", "xlsx", "pptx",
            "mp4", "wav", or "txt" (no signature).
        content: The bytes produced. The caller has already read them; this
            skill touches no file.
        max_size_mb: Optional upper bound on the produced size.
        max_duration_s: Optional upper bound on a media duration, in seconds.

    Returns:
        ``{"verified", "reason", "file_path", "expected_type", "mime_type",
        "size_bytes"}``.
    """
    size = len(content or b"")
    verdict: dict[str, Any] = {
        "verified": False,
        "file_path": file_path,
        "expected_type": expected_type,
        "mime_type": _MIME_TYPES.get(expected_type),
        "size_bytes": size,
    }

    if not content:
        return {**verdict, "reason": "empty content: the step produced no bytes"}

    if max_size_mb is not None and size > float(max_size_mb) * 1024 * 1024:
        return {
            **verdict,
            "reason": f"size {size} bytes exceeds the {max_size_mb} MB bound",
        }

    if max_duration_s is not None:
        duration = _media_duration_s(content, expected_type)
        if duration is None:
            return {**verdict, "reason": f"cannot read the {expected_type} duration"}
        if duration > float(max_duration_s):
            return {
                **verdict,
                "reason": f"duration {duration:.3f}s exceeds the {max_duration_s}s bound",
                "duration_s": duration,
            }
        verdict["duration_s"] = duration

    signature = _MAGIC_BYTES.get(expected_type)
    if signature is not None and not content.startswith(signature):
        return {**verdict, "reason": f"missing {expected_type} magic bytes"}

    if expected_type == "mp4" and b"ftyp" not in content[:32]:
        return {**verdict, "reason": "missing mp4 ftyp box"}

    if expected_type == "wav" and not (
        content.startswith(b"RIFF") and b"WAVE" in content[8:16]
    ):
        return {**verdict, "reason": "missing wav RIFF/WAVE header"}

    return {**verdict, "verified": True, "reason": "content matches the declared type"}


def _media_duration_s(content: bytes, expected_type: str) -> float | None:
    """Duration for the container types this skill claims to measure.

    Only WAV carries its length in a header small enough to read here; an MP4
    duration lives in the ``mvhd`` box and is returned as ``None`` (unreadable)
    rather than guessed.
    """

    if expected_type == "wav" and len(content) >= 44:
        byte_rate = int.from_bytes(content[28:32], "little")
        data_size = int.from_bytes(content[40:44], "little")
        if byte_rate > 0:
            return data_size / byte_rate
    return None


__all__ = [
    "LADDER_ORDER",
    "cli_hub_tool_ladder",
    "cli_hub_verify_output",
]
