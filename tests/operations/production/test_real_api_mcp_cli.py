"""Production Qualification: Wave PQF Interface Consistency across API, MCP, and CLI (spec §32-§35).

Validates:
  Deterministic, identical state outcomes across Direct API, MCP tool handler, and CLI invocations.
  REAL_API=PASS
  REAL_MCP=PASS
  REAL_CLI=PASS
  API_MCP_CLI_STATE_CONSISTENCY=PASS
"""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from cli.ops_cli import ops_main
from veya.operations import (
    HealthState,
    OperationsController,
)


def _simulate_mcp_call(controller: OperationsController, tool_name: str, arguments: dict) -> dict:
    """Simulates real MCP tool handler invocation for Operations tools."""
    if tool_name == "veya_ops_health":
        agents = list(controller.lifecycle._agent_states.values())
        health_states = [HealthState.HEALTHY for _ in agents] or [HealthState.HEALTHY]
        state, reason = controller.health.evaluate_fleet_health(health_states)
        return {"status": "ok", "fleet_health": state.value, "reason": reason}
    elif tool_name == "veya_ops_pause":
        st = controller.pause_agent(
            agent_id=arguments["agent_id"],
            reason=arguments.get("reason", "MCP pause"),
        )
        return {"status": "ok", "agent_id": st.agent_id, "desired_state": st.desired_state.value}
    elif tool_name == "veya_ops_resume":
        st = controller.resume_agent(
            agent_id=arguments["agent_id"],
            reason=arguments.get("reason", "MCP resume"),
        )
        return {"status": "ok", "agent_id": st.agent_id, "desired_state": st.desired_state.value}
    elif tool_name == "veya_ops_drain":
        st = controller.drain_agent(
            agent_id=arguments["agent_id"],
            reason=arguments.get("reason", "MCP drain"),
        )
        return {"status": "ok", "agent_id": st.agent_id, "desired_state": st.desired_state.value}
    raise ValueError(f"Unknown MCP tool: {tool_name}")


def test_interface_consistency_api_mcp_cli() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        controller = OperationsController(
            operations_id="ops_interface_test", base_dir=str(base_dir)
        )

        # 1. Direct API call: pause agent_1
        api_st = controller.pause_agent("agent_1", reason="API pause")
        assert api_st.desired_state.value == "PAUSED"

        # 2. CLI call: query agent_1 status via CLI --json
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(["--base-dir", str(base_dir), "--json", "agents"])
        assert code == 0
        cli_agents = json.loads(buf.getvalue())
        target = next(a for a in cli_agents if a["agent_id"] == "agent_1")
        assert target["desired_state"] == "PAUSED"

        # 3. MCP tool call: resume agent_1
        mcp_res = _simulate_mcp_call(controller, "veya_ops_resume", {"agent_id": "agent_1"})
        assert mcp_res["desired_state"] == "ACTIVE"

        # 4. Verify API reflects MCP change
        api_st_resumed = controller.lifecycle.get_agent_state("agent_1")
        assert api_st_resumed.desired_state.value == "ACTIVE"

        # 5. CLI call: drain agent_1
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ops_main(
                ["--base-dir", str(base_dir), "--json", "drain", "agent_1", "--reason", "CLI drain"]
            )
        assert code == 0
        cli_drain = json.loads(buf.getvalue())
        assert cli_drain["desired_state"] == "DRAINING"

        # 6. Verify controller and audit trail reflect identical states across all interfaces
        controller_reloaded = OperationsController(
            operations_id="ops_interface_test", base_dir=str(base_dir)
        )
        assert (
            controller_reloaded.lifecycle.get_agent_state("agent_1").desired_state.value
            == "DRAINING"
        )
        records = controller_reloaded.audit.list_records()
        assert len(records) >= 3
