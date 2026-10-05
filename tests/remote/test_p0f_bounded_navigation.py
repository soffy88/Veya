"""P0-F: bounded read/search navigation.

The contract under test is that ``file.read`` and ``file.search`` return a
*window* the caller can reason about: a known first line, a known last line, a
count, and an explicit statement of what was left out. Before this, ``file.read``
had no ``start_line`` at all and its ``max_lines`` was a hashline rendering limit
that the fast path did not clamp, so ``max_lines=10**7`` returned the whole file
while the ``read_hashline`` path returned 8000 lines — both under ``ok=true``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from server.hashline import DEFAULT_MAX_LINES, HARD_MAX_LINES, line_hash, render
from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway
from veya.remote.tool_adapter import (
    _FILE_SEARCH_HARD_MAX_CONTEXT,
    _FILE_SEARCH_HARD_MAX_RESULTS,
    BINDINGS,
    _read_window,
    _rg_hit_records,
    build_ripgrep_args,
)

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)

#: Larger than HARD_MAX_LINES so that asking for "everything" is observably
#: different from asking for "the maximum the contract allows".
OVER_CEILING_LINES = HARD_MAX_LINES + 1000
MIDDLE_START = 400
SECOND_HALF_START = 700


class PrimitiveExecutor:
    """Fails on any LLM/agent tool; the read primitives never reach it."""

    _LLM_MARKERS = ("hicode", "veya_", "agent", "llm", "planner")

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append(name)
        if any(marker in name for marker in self._LLM_MARKERS):
            raise AssertionError(f"LLM/agent tool invoked on the direct path: {name}")
        return "{}"


def make_workspace(tmp_path: Path, *, git: bool = True) -> Path:
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def make_gateway(tmp_path: Path, executor: Any, store: ExecutionStore):
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(executor, redact=audit.redact, execution_store=store)
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret, adapter


async def _read(tmp_path: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = (
        await gateway.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            authorization=f"Bearer {secret}",
        )
    )["result"]["sessionId"]
    envelope = (
        await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "file.read", "arguments": arguments},
            },
            authorization=f"Bearer {secret}",
            session_header=session,
        )
    )["result"]["structuredContent"]
    assert envelope["ok"] is True, envelope
    return envelope["result"]


async def _search(tmp_path: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = (
        await gateway.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            authorization=f"Bearer {secret}",
        )
    )["result"]["sessionId"]
    envelope = (
        await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "file.search", "arguments": arguments},
            },
            authorization=f"Bearer {secret}",
            session_header=session,
        )
    )["result"]["structuredContent"]
    assert envelope["ok"] is True, envelope
    return envelope["result"]


def _write_lines(path: Path, count: int) -> None:
    path.write_text("".join(f"line{i}\n" for i in range(1, count + 1)), encoding="utf-8")


def _rendered_line(number: int) -> str:
    """The exact line ``render`` emits for ``line<number>``."""
    return f"{number:>6}|LINE#{line_hash(f'line{number}')}|line{number}"


async def _session(gateway, secret: str) -> str:
    return (
        await gateway.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            authorization=f"Bearer {secret}",
        )
    )["result"]["sessionId"]


async def _call(tmp_path: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a read tool and return the envelope without asserting on ok."""
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await _session(gateway, secret)
    return (
        await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            authorization=f"Bearer {secret}",
            session_header=session,
        )
    )["result"]["structuredContent"]


# ── §6.5 read scenarios ──────────────────────────────────────────────
async def test_read_from_the_beginning_of_a_large_file(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    _write_lines(tmp_path / "big.txt", 1200)

    result = await _read(tmp_path, {"path": "big.txt", "max_lines": 10})

    assert result["start_line"] == 1
    assert result["end_line"] == 10
    assert result["lines"] == 10
    assert result["total_lines"] == 1200
    assert result["truncated"] is True
    assert result["next_start_line"] == 11
    assert _rendered_line(1) in result["text"]
    assert _rendered_line(10) in result["text"]
    assert _rendered_line(11) not in result["text"]


async def test_read_from_the_middle_of_a_large_file(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    _write_lines(tmp_path / "big.txt", 1200)

    result = await _read(tmp_path, {"path": "big.txt", "start_line": MIDDLE_START, "max_lines": 5})

    assert result["start_line"] == MIDDLE_START
    assert result["end_line"] == MIDDLE_START + 4
    assert result["lines"] == 5
    assert _rendered_line(MIDDLE_START) in result["text"]
    assert _rendered_line(MIDDLE_START + 4) in result["text"]
    # Nothing before the window leaked in.
    assert _rendered_line(MIDDLE_START - 1) not in result["text"]


async def test_read_from_the_second_half_of_a_large_file(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    _write_lines(tmp_path / "big.txt", 1200)

    result = await _read(
        tmp_path, {"path": "big.txt", "start_line": SECOND_HALF_START, "max_lines": 4}
    )

    assert result["start_line"] == SECOND_HALF_START
    assert result["end_line"] == SECOND_HALF_START + 3
    assert _rendered_line(SECOND_HALF_START) in result["text"]
    assert _rendered_line(SECOND_HALF_START + 3) in result["text"]


async def test_read_past_the_end_reports_an_empty_window(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    _write_lines(tmp_path / "big.txt", 1200)

    result = await _read(tmp_path, {"path": "big.txt", "start_line": 5000, "max_lines": 5})

    assert result["lines"] == 0
    assert result["end_line"] < result["start_line"]
    assert result["truncated"] is False
    assert "next_start_line" not in result


async def test_max_lines_is_enforced_at_the_hard_ceiling(tmp_path: Path) -> None:
    """Asking for more than the ceiling returns the ceiling, and says so.

    This is the regression the fast path carried: it passed the caller's number
    to the renderer without clamping, so an unbounded request was honoured.
    """
    make_workspace(tmp_path)
    _write_lines(tmp_path / "big.txt", OVER_CEILING_LINES)

    result = await _read(tmp_path, {"path": "big.txt", "max_lines": 10**7})

    assert result["lines"] == HARD_MAX_LINES
    assert result["end_line"] == HARD_MAX_LINES
    assert result["total_lines"] == OVER_CEILING_LINES
    assert result["line_truncated"] is True
    assert result["next_start_line"] == HARD_MAX_LINES + 1


async def test_a_window_that_fits_is_not_reported_as_truncated(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    _write_lines(tmp_path / "small.txt", 5)

    result = await _read(tmp_path, {"path": "small.txt", "start_line": 2, "max_lines": 10})

    assert result["truncated"] is False
    assert result["line_truncated"] is False
    assert result["byte_truncated"] is False
    assert "next_start_line" not in result
    assert result["lines"] == 4


async def test_read_refuses_a_window_that_is_not_a_positive_integer(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")

    for bad in (0, -5, "not-a-number"):
        envelope = await _call(tmp_path, "file.read", {"path": "a.py", "max_lines": bad})
        assert envelope["ok"] is False, (bad, envelope)
        assert "max_lines" in envelope["message"]


# ── §6.5 search scenarios ────────────────────────────────────────────
async def test_search_max_results_is_enforced(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    for n in range(10):
        (tmp_path / f"m{n}.txt").write_text("".join(f"NEEDLE{i}\n" for i in range(10)), "utf-8")

    result = await _search(tmp_path, {"pattern": "NEEDLE", "max_results": 7})

    assert len(result["results"]) == 7
    assert result["hit_truncated"] is True
    assert result["truncated"] is True
    # The cap is on matches, not bytes: seven records came back whole.
    for record in result["results"]:
        assert record["path"] and record["line"] >= 1 and "NEEDLE" in record["match"]


async def test_search_returns_everything_when_under_the_cap(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    (tmp_path / "m.txt").write_text("NEEDLE\n" + "plain\n" + "NEEDLE too\n", encoding="utf-8")

    result = await _search(tmp_path, {"pattern": "NEEDLE", "max_results": 50})

    assert len(result["results"]) == 2
    assert result["hit_truncated"] is False
    assert [r["line"] for r in result["results"]] == [1, 3]


async def test_search_max_results_is_enforced_at_the_hard_ceiling(tmp_path: Path) -> None:
    """Asking for more results than the contract allows returns the ceiling.

    Mirrors the read ceiling: a caller cannot lift the bound by asking for a
    larger number, so the response size stays a function of the contract rather
    than of the request.
    """
    make_workspace(tmp_path)
    (tmp_path / "many.txt").write_text(
        "".join(f"NEEDLE{i}\n" for i in range(_FILE_SEARCH_HARD_MAX_RESULTS + 200)),
        encoding="utf-8",
    )

    result = await _search(tmp_path, {"pattern": "NEEDLE", "max_results": 10**6})

    assert len(result["results"]) == _FILE_SEARCH_HARD_MAX_RESULTS
    assert result["hit_truncated"] is True


async def test_search_context_is_returned_and_bounded(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    body = [f"ctx{i}" for i in range(100)]
    body[49] = "NEEDLE"
    (tmp_path / "m.txt").write_text("\n".join(body) + "\n", encoding="utf-8")

    result = await _search(tmp_path, {"pattern": "NEEDLE", "context_before": 2, "context_after": 2})

    record = result["results"][0]
    assert record["line"] == 50
    assert record["context"] == [
        "48: ctx47",
        "49: ctx48",
        "50: NEEDLE",
        "51: ctx50",
        "52: ctx51",
    ]

    # A context request beyond the ceiling is clamped, not honoured. The file is
    # long enough that the clamp, not the end of the file, is what stops it.
    wide = await _search(
        tmp_path, {"pattern": "NEEDLE", "context_before": 5000, "context_after": 5000}
    )
    wide_record = wide["results"][0]
    assert len(wide_record["context"]) == 1 + 2 * _FILE_SEARCH_HARD_MAX_CONTEXT
    assert wide_record["context"][0] == f"{50 - _FILE_SEARCH_HARD_MAX_CONTEXT}: " + (
        f"ctx{49 - _FILE_SEARCH_HARD_MAX_CONTEXT}"
    )
    assert wide_record["context"][-1] == f"{50 + _FILE_SEARCH_HARD_MAX_CONTEXT}: " + (
        f"ctx{49 + _FILE_SEARCH_HARD_MAX_CONTEXT}"
    )


async def test_search_order_is_deterministic(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    for name in ("zeta", "alpha", "mu", "beta"):
        (tmp_path / f"{name}.txt").write_text("NEEDLE\n", encoding="utf-8")

    first = await _search(tmp_path, {"pattern": "NEEDLE"})
    second = await _search(tmp_path, {"pattern": "NEEDLE"})

    paths = [r["path"] for r in first["results"]]
    assert paths == [r["path"] for r in second["results"]]
    assert paths == sorted(paths)


async def test_search_glob_still_narrows(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    (tmp_path / "keep.py").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("NEEDLE\n", encoding="utf-8")

    result = await _search(tmp_path, {"pattern": "NEEDLE", "glob": "*.py"})

    assert [Path(r["path"]).name for r in result["results"]] == ["keep.py"]


# ── §6.5 containment ─────────────────────────────────────────────────
async def test_path_outside_the_allowed_workspace_is_rejected(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")

    read_escape = await _call(tmp_path, "file.read", {"path": "../../etc/passwd"})
    assert read_escape["ok"] is False
    assert read_escape["error_code"] == "POLICY_BLOCKED"

    search_escape = await _call(tmp_path, "file.search", {"pattern": "root", "path": "../.."})
    assert search_escape["ok"] is False


# ── invariants that protect file.patch ────────────────────────────────
async def test_window_line_numbers_stay_true_file_line_numbers(tmp_path: Path) -> None:
    """A tag read from a window must still resolve against the whole file.

    ``edit_hashline`` looks tags up by hash over every line of the live file, so
    the tag itself is always sound. The printed number is the part a caller
    trusts, and numbering a deep window from 1 would tell it the wrong place.
    """
    make_workspace(tmp_path)
    _write_lines(tmp_path / "big.txt", 1200)

    result = await _read(tmp_path, {"path": "big.txt", "start_line": MIDDLE_START, "max_lines": 3})

    assert f"{MIDDLE_START:>6}|LINE#{line_hash(f'line{MIDDLE_START}')}|" in result["text"]


def test_render_with_the_default_window_is_unchanged() -> None:
    source = "".join(f"row{i}\n" for i in range(1, 50))

    assert render(source) == render(source, start_line=1)
    assert render(source, max_lines=10) == render(source, max_lines=10, start_line=1)


def test_render_window_numbers_from_the_requested_line() -> None:
    source = "".join(f"row{i}\n" for i in range(1, 10))

    out = render(source, max_lines=2, start_line=5)

    assert f"{5:>6}|LINE#{line_hash('row5')}|row5" in out
    assert f"{6:>6}|LINE#{line_hash('row6')}|row6" in out
    assert "row4" not in out
    assert "start_line=7" in out


# ── the bound is one bound, not two ───────────────────────────────────
def test_both_paths_clamp_max_lines_identically(tmp_path: Path, monkeypatch) -> None:
    """The fast path and the ``read_hashline`` path must not disagree again.

    Both now clamp to ``HARD_MAX_LINES``. They are separate implementations, so
    the agreement is pinned rather than assumed.
    """
    from server.tool_registry import _tool_read_hashline

    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    _write_lines(tmp_path / "big.txt", OVER_CEILING_LINES)

    adapter_value, error = _read_window(
        10**7, name="max_lines", default=DEFAULT_MAX_LINES, hard_max=HARD_MAX_LINES
    )
    assert error is None

    rendered = _tool_read_hashline("big.txt", max_lines=10**7)
    emitted = [ln for ln in rendered.splitlines() if "LINE#" in ln]

    assert adapter_value == HARD_MAX_LINES
    # The canonical tool honoured the ceiling rather than the caller's number.
    assert len(emitted) == HARD_MAX_LINES
    assert f"{HARD_MAX_LINES:>6}|LINE#" in rendered
    assert f"{HARD_MAX_LINES + 1:>6}|LINE#" not in rendered


def test_window_builder_clamps_and_refuses() -> None:
    assert _read_window(5, name="max_results", default=40, hard_max=1000) == (5, None)
    assert _read_window(10**6, name="max_results", default=40, hard_max=1000) == (1000, None)
    assert _read_window(0, name="max_results", default=40, hard_max=1000)[1]
    assert _read_window(1.5, name="max_results", default=40, hard_max=1000)[1]
    assert _read_window("x", name="max_results", default=40, hard_max=1000)[1]
    assert _read_window(None, name="context_before", default=0, minimum=0) == (0, None)
    assert _read_window(3, name="context_before", default=0, minimum=0) == (3, None)


def test_search_defaults_are_the_documented_ones() -> None:
    properties = next(b for b in BINDINGS if b.name == "file.search").schema["properties"]
    assert {"max_results", "context_before", "context_after"} <= set(properties)
    assert next(b for b in BINDINGS if b.name == "file.read").schema["properties"]["start_line"]


def test_tool_binding_count_is_unchanged() -> None:
    """Guards the recorded supervision baseline that counts bindings.

    P0-F widens two existing contracts. It must not add or remove a tool, or the
    baseline count recorded elsewhere stops describing reality.
    """
    assert len(BINDINGS) == 42


def test_search_arguments_carry_the_window_and_stay_deterministic() -> None:
    plain = build_ripgrep_args("pat", root="/r")
    assert plain[:5] == ["rg", "--json", "-n", "--sort", "path"]
    assert "-B" not in plain and "-A" not in plain

    windowed = build_ripgrep_args("pat", root="/r", context_before=2, context_after=3)
    assert windowed[windowed.index("-B") + 1] == "2"
    assert windowed[windowed.index("-A") + 1] == "3"


def test_hit_records_cap_and_report_truncation() -> None:
    import json as _json

    stdout = "\n".join(
        _json.dumps(
            {
                "type": "context" if n in (1, 3) else "match",
                "data": {
                    "path": {"text": "f.py"},
                    "line_number": n,
                    "lines": {"text": f"body{n}\n"},
                },
            }
        )
        for n in (1, 2, 3, 4)
    )

    records, truncated = _rg_hit_records(stdout, max_results=5, context_before=1, context_after=1)
    assert truncated is False
    assert [r["line"] for r in records] == [2, 4]
    assert records[0]["context"] == ["1: body1", "2: body2", "3: body3"]
    assert records[1]["context"] == ["3: body3", "4: body4"]

    capped, truncated = _rg_hit_records(stdout, max_results=1)
    assert len(capped) == 1
    assert truncated is True


def test_hard_bounds_are_the_stated_ones() -> None:
    assert HARD_MAX_LINES == 8000
    assert DEFAULT_MAX_LINES == 2000
    assert _FILE_SEARCH_HARD_MAX_RESULTS == 1000
    assert _FILE_SEARCH_HARD_MAX_CONTEXT == 20


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
