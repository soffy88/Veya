"""Tests for Operations V1 Interfaces: CLI, API, and MCP (Phase O7)."""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout

from cli.ops_cli import ops_main
from veya.operations import OperationsController


def test_cli_operations_json_commands() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = OperationsController(base_dir=tmpdir)
        controller.pause_agent("agent_cli_1", reason="Testing CLI")

        # 1. Health
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", tmpdir, "--json", "health"])
        assert code == 0
        data = json.loads(buf.getvalue())
        assert "fleet_health" in data

        # 2. Agents
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", tmpdir, "--json", "agents"])
        assert code == 0
        data = json.loads(buf.getvalue())
        assert any(a["agent_id"] == "agent_cli_1" for a in data)

        # 3. Resume
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", tmpdir, "--json", "resume", "agent_cli_1"])
        assert code == 0
        data = json.loads(buf.getvalue())
        assert data["desired_state"] == "ACTIVE"

        # 4. Drain
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", tmpdir, "--json", "drain", "agent_cli_1"])
        assert code == 0
        data = json.loads(buf.getvalue())
        assert data["desired_state"] == "DRAINING"

        # 5. SLO
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", tmpdir, "--json", "slo"])
        assert code == 0
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)

        # 6. Alerts
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", tmpdir, "--json", "alerts"])
        assert code == 0
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)

        # 7. Incidents
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", tmpdir, "--json", "incidents"])
        assert code == 0
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)

        # 8. Usage
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", tmpdir, "--json", "usage"])
        assert code == 0
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)
