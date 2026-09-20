"""DSH execution plane: dedicated config, gateway binding, executor hint wiring."""

from __future__ import annotations

import sys
import types

from server import dsh_plane
from veya.supervision import runner as runner_mod
from veya.supervision.models import Mission


def test_config_file_then_process_env_wins(tmp_path, monkeypatch):
    cfg = tmp_path / "dsh.env"
    cfg.write_text(
        "# comment\nDSH_RUNTIME=ENABLED\nDSH_MODEL=model-from-file\nDSH_SESSION_DIR=/tmp/ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VEYA_DSH_CONFIG", str(cfg))
    assert dsh_plane.model() == "model-from-file"
    monkeypatch.setenv("DSH_MODEL", "model-from-env")
    assert dsh_plane.model() == "model-from-env"
    # /tmp must never be the real state dir unless explicitly configured to be.
    monkeypatch.setenv("DSH_SESSION_DIR", str(tmp_path / "state"))
    assert dsh_plane.state_dir() == tmp_path / "state"


def test_runtime_gate(tmp_path, monkeypatch):
    cfg = tmp_path / "dsh.env"
    cfg.write_text("DSH_RUNTIME=DISABLED\n", encoding="utf-8")
    monkeypatch.setenv("VEYA_DSH_CONFIG", str(cfg))
    assert dsh_plane.is_enabled() is False
    cfg.write_text("DSH_RUNTIME=ENABLED\n", encoding="utf-8")
    assert dsh_plane.is_enabled() is True


def test_plane_env_points_at_veya_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_DSH_CONFIG", str(tmp_path / "missing.env"))
    monkeypatch.setenv("DSH_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DSH_MODEL", "some-gateway-model")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = dsh_plane.load_config()
    env = dsh_plane.subprocess_env(cfg)
    assert env["DEEPSEEK_BASE_URL"] == dsh_plane.DEFAULT_BASE_URL
    assert env["DEEPSEEK_DEFAULT_MODEL"] == "some-gateway-model"
    # DSH never holds the real provider credential.
    assert env["DEEPSEEK_API_KEY"] == dsh_plane.DEFAULT_API_KEY
    assert env["DSH_HOME"].startswith(str(tmp_path / "state"))
    assert "127.0.0.1" in env["NO_PROXY"]


def test_model_patch_binds_default_model(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_DSH_CONFIG", str(tmp_path / "missing.env"))
    monkeypatch.setenv("DSH_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DSH_MODEL", "gateway/model-x")
    cfg = dsh_plane.load_config()
    path = dsh_plane.model_patch_path(cfg)
    text = path.read_text(encoding="utf-8")
    assert "id: gateway/model-x" in text
    assert "model: gateway/model-x" in text
    argv = dsh_plane.dsh_argv("/usr/bin/dsh", "TASK", cfg)
    assert argv[:3] == ["/usr/bin/dsh", "--profile", "headless"]
    assert argv[3:5] == ["--patch", str(path)]
    assert argv[-1] == "TASK"


def test_unknown_executor_hint_is_ignored():
    mission = Mission(mission_id="m1", goal="g")
    assert runner_mod._assignee_hint(mission) is None
    mission.policies.execution_policy["assignee_hint"] = "nonsense"
    assert runner_mod._assignee_hint(mission) is None
    mission.policies.execution_policy["assignee_hint"] = "dsh"
    assert runner_mod._assignee_hint(mission) == "dsh"


def test_canonical_runner_pins_assignee_hint(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_project_ask(*, project_root, request, assignee_hint=None):
        seen["project_root"] = project_root
        seen["assignee_hint"] = assignee_hint
        return "done"

    module = types.ModuleType("server.project_ask")
    module.project_ask = fake_project_ask  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "server.project_ask", module)

    mission = Mission(mission_id="m2", goal="g", workspace="/tmp/ws")
    mission.policies.execution_policy["assignee_hint"] = "dsh"
    result = runner_mod.runner_with  # keep import used
    assert result is not None

    import asyncio

    out = asyncio.run(runner_mod.canonical_runner(mission))
    assert seen == {"project_root": "/tmp/ws", "assignee_hint": "dsh"}
    assert out.final_summary == "done"
