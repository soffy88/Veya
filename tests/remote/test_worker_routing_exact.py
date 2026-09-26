from veya.remote.executor_health import normalize_executor_name
from veya.remote.tool_adapter import RemoteToolAdapter


def test_worker_aliases_are_canonical_without_cross_executor_fallback() -> None:
    assert normalize_executor_name("DSH") == "dsh"
    assert normalize_executor_name("HICODE") == "hicode"
    assert normalize_executor_name("PI") == "pi"
    assert normalize_executor_name("OpenCode") == "opencode"
    assert normalize_executor_name("AGY") == "antigravity"
    assert normalize_executor_name("unknown-worker") == "unknown-worker"


def test_worker_dispatch_contract_is_exposed() -> None:
    schema = next(t for t in RemoteToolAdapter(None).list_tools() if t["name"] == "worker.dispatch")
    item = schema["inputSchema"]["properties"]["tasks"]["items"]["properties"]
    assert {"task_kind", "effect_requirement", "verification_requirement"} <= set(item)
