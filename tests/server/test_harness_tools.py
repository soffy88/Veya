from __future__ import annotations

from pathlib import Path

from runtime.harness.tools import harness_guides_load, harness_sensor_list, register_tools
from server.tool_registry import MasterToolRegistry, master_tools


def test_harness_tools_register_additively_and_idempotently():
    registry = MasterToolRegistry()

    assert register_tools(registry) == 10
    assert register_tools(registry) == 0
    names = set(registry.list_tools())
    assert {
        "harness_guides_load",
        "harness_guides_search",
        "harness_guides_show",
        "harness_sensor_list",
        "harness_sensor_run",
        "harness_sensor_report",
        "harness_ratchet_candidates",
        "harness_ratchet_approve",
        "harness_ratchet_reject",
        "harness_ratchet_apply",
    } <= names
    assert {name for name in master_tools.list_tools() if name.startswith("harness_")} >= names


def test_harness_tools_return_source_and_sensor_evidence(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text(
        "## Commands\n- test: pytest\n- lint: ruff check .\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['pytest']\n",
        encoding="utf-8",
    )

    guides = harness_guides_load(str(tmp_path))
    sensors = harness_sensor_list(str(tmp_path))

    assert guides["status"] == "ok"
    assert guides["data"]["guides"][0]["source_path"] == str(tmp_path / "AGENTS.md")
    assert guides["data"]["guides"][0]["rules"][0]["source_line"] == 2
    assert sensors["status"] == "ok"
    assert any(item["command"] == "pytest" for item in sensors["data"]["required"])
