"""Server boot / import gate.

Regression gate for the incident where canonical main `5796cbaf` was published
and declared qualified while `server.app` could not be imported at all:
commit 9876111f removed the in-memory SSE queue API but left four modules
importing it, and no mandatory suite imported `server.app`, so 19 green suites
did not catch a backend that could not start.

This gate is the cheapest possible detector for "the server does not boot":
each module is imported in a FRESH interpreter, because an in-process import
can be satisfied by a module another test already imported. On top of that the
FastAPI app is instantiated and its routes enumerated, which catches
route-registration and wiring errors that a bare import would not.

Keep this file in the mandatory CI suite. If it fails, the server does not boot.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Every module that must import cleanly for the backend to serve traffic.
BOOT_MODULES = (
    "server.sse",
    "server.session_events",
    "server.events",
    "server.chat_stream",
    "server.coordinator_master",
    "server.routes.prompt",
    "server.routes.master",
    "server.routes.vscode",
    "server.app",
)


def _import_in_fresh_interpreter(module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize("module", BOOT_MODULES)
def test_module_imports_in_fresh_interpreter(module: str) -> None:
    """A cold `import <module>` must succeed — this is the production path."""
    result = _import_in_fresh_interpreter(module)
    assert result.returncode == 0, (
        f"import {module} failed in a fresh interpreter (rc={result.returncode}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_server_app_importable_as_uvicorn_target() -> None:
    """`uvicorn server.app:app` is the container's real entrypoint.

    Import the exact target string rather than a module, so an attribute rename
    on `app` is caught too.
    """
    result = subprocess.run(
        [sys.executable, "-c", "from server.app import app; assert app is not None"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"uvicorn target 'server.app:app' is not importable (rc={result.returncode}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_app_bootstraps_and_registers_routes() -> None:
    """Instantiate far enough to catch route-registration / wiring errors.

    Uses the OpenAPI schema rather than ``app.routes``: FastAPI >= 0.12 keeps
    ``include_router`` results as lazy ``_IncludedRouter`` wrappers, so route
    objects are not flattened onto ``app.routes`` until the app is built.
    """
    from server.app import app

    paths = set(app.openapi()["paths"])
    assert paths, "app exposes no routes"
    # The SSE and master surfaces are the ones the durable migration touched.
    assert any(p.startswith("/stream") for p in paths), f"no /stream route in {sorted(paths)}"
    assert any(p.startswith("/master") for p in paths), f"no /master route in {sorted(paths)}"
    assert "/prompt" in paths, "prompt ingress route missing"
    assert len(paths) > 100, f"route table looks truncated: only {len(paths)} paths"


def test_no_stale_in_memory_sse_api() -> None:
    """The removed in-memory event authority must not come back.

    Guards against reintroducing SSEQueue / _queues / get_or_create_queue as a
    compatibility shim: durable_session_store is the only event authority.
    """
    import server.session_events as session_events
    import server.sse as sse

    for mod in (sse, session_events):
        for banned in ("SSEQueue", "_queues", "get_or_create_queue"):
            assert not hasattr(mod, banned), (
                f"{mod.__name__} still exposes {banned!r}; the durable session store "
                "must be the only event authority"
            )
    # The canonical authority is present and is the singleton producers use.
    from server.session_events import durable_session_store

    assert durable_session_store is session_events.durable_session_store
    assert hasattr(durable_session_store, "append_event")
    assert hasattr(durable_session_store, "publish_terminal")
    assert hasattr(durable_session_store, "begin_stream")
