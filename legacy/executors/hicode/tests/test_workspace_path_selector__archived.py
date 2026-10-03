"""Archived Hicode tests, split out of `tests/remote/test_workspace_path_selector.py` when Hicode was retired.

Kept for historical reference only; not collected by the product suite.
"""

@pytest.mark.asyncio
async def test_workspace_path_propagates_to_hicode_and_absolute_selector(gateway) -> None:
    server, secret, _projects, repo, executor = gateway
    session = await _session(server, secret)
    accepted = await _rpc(
        server,
        secret,
        session,
        "hicode.execute",
        {
            "workspace_path": str(repo.resolve()),
            "task": "read-only identity check",
            "wait": True,
        },
    )
    assert accepted["ok"] is True, accepted
    assert any(name == "hicode_run" and call["workspace"] for name, call in executor.calls)
    assert str(repo.resolve()) in str(
        [call for name, call in executor.calls if name == "hicode_run"]
    )
