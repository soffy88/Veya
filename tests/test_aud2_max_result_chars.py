from server.tool_registry import master_tools


def test_skill_teach_has_large_result_limit():
    """Verify skill_teach tool is registered with sufficient result limit (>=32000) for rich eval gate payloads."""
    limit = master_tools._result_limits.get("skill_teach")
    assert limit is not None, "skill_teach must have an explicit max_result_chars limit registered"
    assert limit >= 32000, f"Expected limit >= 32000, got {limit}"
