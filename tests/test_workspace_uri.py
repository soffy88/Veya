"""Unit and contract tests for VEYA_WORKSPACE_URI (clean-room borrow)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.routes import fs as fs_routes
from server.tool_registry import (
    ToolExecutionError,
    _current_workspace_root,
    _current_write_root,
    _tool_read_hashline,
    _tool_write_file,
)
from server.workspace_uri import (
    FLAG_NAME,
    PathTraversalError,
    is_workspace_uri_enabled,
    sanitize_text,
    to_real_path,
    to_virtual_uri,
)


@pytest.fixture
def ws_dir(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def bound_workspace(ws_dir: Path):
    t1 = _current_workspace_root.set(ws_dir)
    t2 = _current_write_root.set(ws_dir)
    yield ws_dir
    _current_workspace_root.reset(t1)
    _current_write_root.reset(t2)


@pytest.fixture
def client(bound_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("VEYA_WORKSPACE", str(bound_workspace))
    app = FastAPI()
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "alice"}
    app.include_router(fs_routes.router)
    return TestClient(app)


def test_flag_registered_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG_NAME, raising=False)
    assert is_workspace_uri_enabled() is False


def test_flag_turn_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_NAME, "1")
    assert is_workspace_uri_enabled() is True


def test_to_virtual_uri_translation(bound_workspace: Path) -> None:
    file_path = bound_workspace / "src" / "main.py"
    uri = to_virtual_uri(file_path, root=bound_workspace)
    assert uri == "veya://workspace/src/main.py"

    # Already virtual uri
    assert to_virtual_uri("veya://workspace/foo.txt") == "veya://workspace/foo.txt"

    # Relative path
    assert to_virtual_uri("src/utils.py", root=bound_workspace) == "veya://workspace/src/utils.py"


def test_to_real_path_success(bound_workspace: Path) -> None:
    test_file = bound_workspace / "demo.txt"
    test_file.write_text("hello", encoding="utf-8")

    real = to_real_path("veya://workspace/demo.txt", root=bound_workspace, must_exist=True)
    assert real == test_file
    assert real.read_text(encoding="utf-8") == "hello"


def test_to_real_path_anti_traversal(bound_workspace: Path) -> None:
    # Directory traversal attempts must be blocked
    with pytest.raises(PathTraversalError):
        to_real_path("veya://workspace/../escape.txt", root=bound_workspace)

    with pytest.raises(PathTraversalError):
        to_real_path("veya://workspace/sub/../../etc/passwd", root=bound_workspace)


def test_sanitize_text(bound_workspace: Path) -> None:
    text = f"File written to {bound_workspace}/output/result.csv successfully."
    sanitized = sanitize_text(text, root=bound_workspace)
    assert str(bound_workspace) not in sanitized
    assert "veya://workspace/output/result.csv" in sanitized


def test_tool_registry_with_workspace_uri(
    bound_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FLAG_NAME, "1")

    # 1. Write file using veya:// URI
    res = _tool_write_file("veya://workspace/test_uri.txt", "content from virtual uri")
    assert "veya://workspace/test_uri.txt" in res
    assert str(bound_workspace) not in res  # Host path hidden!

    # 2. Read hashline using veya:// URI
    hashline_out = _tool_read_hashline("veya://workspace/test_uri.txt")
    assert "[hashline veya://workspace/test_uri.txt]" in hashline_out
    assert str(bound_workspace) not in hashline_out

    # 3. Traversal in write_file rejected
    with pytest.raises(ToolExecutionError):
        _tool_write_file("veya://workspace/../../escaped.txt", "fail")


def test_fs_read_with_workspace_uri(
    client: TestClient, bound_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FLAG_NAME, "1")
    demo = bound_workspace / "hello.txt"
    demo.write_text("hello world from test", encoding="utf-8")

    # Read using veya:// URI
    resp = client.get("/api/v1/fs/read", params={"path": "veya://workspace/hello.txt"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == "hello world from test"
    assert data["path"] == "veya://workspace/hello.txt"

    # Traversal rejected with 403
    resp_bad = client.get("/api/v1/fs/read", params={"path": "veya://workspace/../etc/passwd"})
    assert resp_bad.status_code == 403
