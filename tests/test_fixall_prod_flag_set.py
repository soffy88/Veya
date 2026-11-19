"""fixall 2026-10-14: the production flag set wires the dark surfaces.

The routers for remote control (W13), MCP presets (W14), channels (NANO-W7)
and the provider registry (NANO-W8) are registered in ``server.app`` but are
fail-closed behind flags.  Production reads ``deploy/bot_landing_flags.env``;
this pins that the prod set turns them on and that, with that set loaded, the
gated endpoints answer for real instead of 404.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

FLAGS_FILE = Path(__file__).resolve().parents[1] / "deploy" / "bot_landing_flags.env"
WIRED = ("VEYA_MCP_PRESETS", "VEYA_CHANNELS", "VEYA_PROVIDER_REGISTRY")
# Remote control is deliberately OFF in the committed prod flag set; its route is
# flag-gated and stays dark until an owner explicitly opts in.
DARK = ("VEYA_REMOTE_CONTROL",)


def _prod_flags() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in FLAGS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


@pytest.mark.parametrize("name", WIRED)
def test_prod_flag_set_enables_surface(name: str) -> None:
    assert _prod_flags().get(name) == "1"


@pytest.mark.parametrize("name", DARK)
def test_prod_flag_set_keeps_remote_control_dark(name: str) -> None:
    assert _prod_flags().get(name) == "0"


@pytest.fixture
def prod_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in WIRED:
        monkeypatch.setenv(name, _prod_flags()[name])
    for name in ("HOSTS", "SESSIONS", "WAKE", "FILES", "POLICY"):
        monkeypatch.setenv(f"VEYA_REMOTE_CONTROL_{name}_PATH", str(tmp_path / f"{name}.json"))
    monkeypatch.setenv("VEYA_CHANNELS_ROOT", str(tmp_path / "channels"))


@pytest.fixture
def client(prod_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from server import auth
    from server.routes import providers as providers_routes
    from server.routes.channels import router as channels_router
    from server.routes.mcp_presets import router as presets_router
    from server.routes.providers import router as providers_router
    from server.routes.remote_control import router as remote_router
    from veya.obase.provider_registry import VeyaProviderRegistry

    registry = VeyaProviderRegistry(storage_dir=tmp_path / "providers")
    monkeypatch.setattr(providers_routes, "get_provider_registry", lambda: registry)

    app = FastAPI()
    for router in (remote_router, presets_router, channels_router, providers_router):
        app.include_router(router)
    app.dependency_overrides[auth.require_user] = lambda: {"user_id": "u1"}
    app.dependency_overrides[auth.get_current_user] = lambda: {"user_id": "u1"}
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/channels/bindings",
        "/api/v1/providers",
    ],
)
def test_gated_endpoints_answer_with_prod_flags(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])


def test_mcp_presets_not_flag_disabled_with_prod_flags(client: TestClient) -> None:
    resp = client.get("/api/v1/mcp-presets")
    assert resp.status_code == 200
    reasons = [p["reason"] for p in resp.json()["presets"]]
    assert reasons and not any("flag disabled" in str(r) for r in reasons)


@pytest.mark.parametrize("path", ["/api/v1/remote/hosts", "/api/v1/remote/sessions"])
def test_remote_control_stays_dark_with_prod_flags(client: TestClient, path: str) -> None:
    """b2eeab7b disabled remote control in prod until owner checks land."""
    resp = client.get(path)
    assert resp.status_code == 404, (path, resp.status_code, resp.text[:200])
