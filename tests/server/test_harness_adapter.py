"""Phase 0 regressions for the Veya × harness2 adapter and exporter."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from server.harness._harness2 import import_harness2_core
from server.harness.export_base_harness import (
    VeyaSources,
    build_harness_tree,
    export_base_harness,
    read_veya_sources,
)


def _core():
    try:
        return import_harness2_core()
    except ImportError as exc:
        pytest.skip(str(exc))


def _base_harness(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    files = {
        "systemprompt.md": "Veya test system prompt\n",
        "guardrails.md": "Veya test guardrails\n",
        "LongTermMEMORY.md": "Veya test memory\n",
        "tool_descriptions/tool_guidance.md": "Veya test tools\n",
    }
    for name, body in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return base


def _sources() -> VeyaSources:
    return VeyaSources(
        system_prompt="You are Veya.",
        host_sop="# HOST SOP\nAnswer first.",
        tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": "demo_tool",
                    "description": "A demo tool.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }
        ],
        skill_manifests=[{"name": "demo_skill", "description": "A demo skill."}],
        memory={"preferences": [{"rule": "Prefer tests.", "context": "Coding"}]},
        hook_notes="## Hook chain\n- pre_dispatch",
    )


def test_build_harness_tree_maps_veya_sources_to_opencode_surface():
    tree = build_harness_tree(_sources())

    assert "You are Veya." in tree["systemprompt.md"]
    assert "# HOST SOP" in tree["systemprompt.md"]
    assert "pre_dispatch" in tree["guardrails.md"]
    assert "Prefer tests." in tree["LongTermMEMORY.md"]
    assert "demo_tool" in tree["tool_descriptions/tool_guidance.md"]
    assert "demo_skill" in tree["tool_descriptions/tool_guidance.md"]


def test_export_base_harness_writes_a_harness2_tree(tmp_path):
    out = export_base_harness(tmp_path / "exported", sources=_sources())

    assert (out / "systemprompt.md").is_file()
    assert (out / "tool_descriptions/tool_guidance.md").is_file()
    assert not (out / "skills_inventory.md").exists()
    snapshot = out.parent / "snapshot"
    assert (snapshot / "systemprompt.md").is_file()

    core = _core()
    from harness2.substrate.opencode import OPENCODE_SURFACE

    tree = core.tree.read_tree(OPENCODE_SURFACE, out)
    assert "systemprompt.md" in tree
    assert "tool_descriptions/tool_guidance.md" in tree
    core.tree.validate(OPENCODE_SURFACE, tree, allow_subagents=False)


def test_base_harness_stays_under_the_surface_cap_with_large_sources():
    tree = build_harness_tree(
        VeyaSources(
            system_prompt="system " * 20_000,
            host_sop="sop " * 5_000,
            tool_schemas=[
                {
                    "type": "function",
                    "function": {
                        "name": f"tool_{i}",
                        "description": "description " * 50,
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
                for i in range(200)
            ],
            skill_manifests=[
                {"name": f"skill_{i}", "description": "skill description " * 20} for i in range(200)
            ],
            memory={"preferences": [{"rule": "rule " * 100, "context": "General"}] * 100},
        )
    )

    always_on = "\n\n".join(
        tree[name]
        for name in (
            "systemprompt.md",
            "guardrails.md",
            "LongTermMEMORY.md",
            "tool_descriptions/tool_guidance.md",
        )
    )
    assert len(always_on) <= 24_000


def test_adapter_executes_two_fixture_tasks_without_a_model(tmp_path):
    adapter_module = pytest.importorskip("server.harness.adapter")
    adapter = adapter_module.VeyaBenchmarkAdapter(
        tmp_path / "root", base_harness_dir=_base_harness(tmp_path)
    )

    assert adapter.tasks() == ["sum-1", "upper-1"]
    assert adapter.domain_of("sum-1") == "veya-phase0"

    sum_run = adapter.execute("sum-1", None, "phase0/sum-1/base1")
    assert adapter.delivered(sum_run)
    assert adapter.deliverable_text(sum_run) == "sum(1..10)=55\n"
    assert "compute_sum" in adapter.trajectory(sum_run, obs_trunc=100_000)

    upper_run = adapter.execute("upper-1", None, "phase0/upper-1/base1")
    assert adapter.delivered(upper_run)
    assert adapter.deliverable_text(upper_run) == "HELLO FROM VEYA\n"
    assert adapter.deliverable_files(upper_run) == {"deliverable.txt": "HELLO FROM VEYA\n"}


def test_adapter_rejects_tree_missing_always_on_files(tmp_path):
    adapter_module = pytest.importorskip("server.harness.adapter")
    adapter = adapter_module.VeyaBenchmarkAdapter(
        tmp_path / "root", base_harness_dir=_base_harness(tmp_path)
    )

    with pytest.raises(ValueError, match="always-on"):
        adapter.validate_tree({"systemprompt.md": "only one"})


def test_adapter_satisfies_benchmark_adapter_protocol(tmp_path):
    core = _core()
    adapter_module = pytest.importorskip("server.harness.adapter")
    adapter = adapter_module.VeyaBenchmarkAdapter(
        tmp_path / "root", base_harness_dir=_base_harness(tmp_path)
    )

    assert isinstance(adapter, core.spec.BenchmarkAdapter)
    assert adapter.score(tmp_path / "missing", "sum-1") is None


def test_default_adapter_refreshes_the_exported_base(tmp_path):
    _core()
    adapter_module = pytest.importorskip("server.harness.adapter")
    root = tmp_path / "root"
    stale = root / "base" / "systemprompt.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale base\n", encoding="utf-8")

    adapter = adapter_module.VeyaBenchmarkAdapter(root)

    exported = adapter.base_harness() / "systemprompt.md"
    assert exported.read_text(encoding="utf-8") != "stale base\n"
    assert (root / "snapshot" / "systemprompt.md").is_file()


def test_read_veya_sources_uses_the_registry_schema_surface(monkeypatch, tmp_path):
    calls: list[str] = []

    class _Tools:
        def get_all_schemas(self):
            calls.append("get_all_schemas")
            return [{"function": {"name": "runtime_surface"}}]

    coordinator = types.ModuleType("server.coordinator_master")
    coordinator.MASTER_SYSTEM_PROMPT = "master prompt"
    coordinator._HOST_SOP_APPEND = "host sop"
    coordinator._slim_master_prompt = lambda text: f"slim:{text}"
    registry = types.ModuleType("server.tool_registry")
    registry.master_tools = _Tools()
    monkeypatch.setitem(sys.modules, "server.coordinator_master", coordinator)
    monkeypatch.setitem(sys.modules, "server.tool_registry", registry)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    sources = read_veya_sources()

    assert calls == ["get_all_schemas"]
    assert sources.system_prompt == "slim:master prompt"
    assert sources.tool_schemas == [{"function": {"name": "runtime_surface"}}]


def test_k1_full_loop_produces_summary_and_selection(tmp_path):
    core = _core()
    adapter_module = pytest.importorskip("server.harness.adapter")
    root = tmp_path / "root"
    adapter = adapter_module.VeyaBenchmarkAdapter(root, base_harness_dir=_base_harness(tmp_path))
    proposer = adapter_module.VeyaDemoProposer(adapter.spec.channels["proposer"])

    result = core.schedule.run_domain(
        "veya-phase0",
        adapter.tasks(),
        1,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=root / "library" / "k1",
        modes=("agg",),
    )

    assert (result["done"], result["failed"]) == (2, 0)
    assert adapter.execution_count == 8
    for task_id in adapter.tasks():
        summary_path = root / "runs" / "veya-oc-agg1" / task_id / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["status"] == "ok"
        ledger = summary["modes"]["agg"]["ledger"]
        assert (ledger["R"], ledger["P"]) == (4, 3)
        selection = summary["selections"]["agg"]
        assert selection["ok"] is True
        assert selection["chosen_label"] == "agg1"
        assert selection["trivial"] is True
