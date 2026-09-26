"""Tests for Fleet CLI and Observability Percentiles (spec §52, §54, AF-P6)."""

from __future__ import annotations

import tempfile

from cli.fleet_cli import run_fleet_cli
from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    FleetMetrics,
    calculate_percentile,
)


def test_percentile_calculation() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    p50 = calculate_percentile(values, 50.0)
    p95 = calculate_percentile(values, 95.0)
    p99 = calculate_percentile(values, 99.0)

    assert round(p50, 1) == 55.0
    assert p95 > 90.0
    assert p99 >= 99.0

    metrics = FleetMetrics()
    for v in values:
        metrics.record_queue_latency(v)
        metrics.record_placement_latency(v * 2)

    summary = metrics.get_summary()
    assert summary["queue_latency_ms"]["count"] == 10
    assert summary["placement_latency_ms"]["count"] == 10


def test_fleet_cli_execution(capsys) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        # Prepopulate registry
        c = FleetController(fleet_id="default_fleet", base_dir=tmpdir)
        agent = AgentInstance(
            agent_instance_id="agent_cli_1",
            fleet_id="default_fleet",
            capability_profile=["test"],
            status=AgentInstanceStatus.READY,
        )
        c.register_agent(agent)

        ret = run_fleet_cli(["status", "--json"])
        assert ret == 0

        ret_agents = run_fleet_cli(["agents", "list", "--json"])
        assert ret_agents == 0

        ret_queue = run_fleet_cli(["queue", "list", "--json"])
        assert ret_queue == 0

        ret_res = run_fleet_cli(["resources", "--json"])
        assert ret_res == 0
