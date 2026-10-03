"""Archived Hicode tests, split out of `tests/remote/test_workspace_binding.py` when Hicode was retired.

Kept for historical reference only; not collected by the product suite.
"""

async def test_hicode_execute_passes_explicit_workspace(tmp_path: Path, monkeypatch) -> None:
    oprim, veya = make_repos(tmp_path)
    # The canonical Hicode sandbox resolver must accept the bound repo; point its
    # root at the test tree without importing/maintaining a global env change.
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    executor = WorktreeExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, [oprim, veya])
    session = await initialize(gateway, secret, workspace=str(oprim))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "hicode.execute",
            "arguments": {"task": "harmless no-op", "workspace": str(veya), "wait": True},
        },
        secret=secret,
        session=session,
    )
    envelope = response["result"]["structuredContent"]
    assert envelope["ok"] is True, envelope
    assert envelope["execution_id"]
    assert envelope["result"]["phase"] == "COMPLETED"
    # The canonical hicode tool must have been told the explicit workspace.
    assert executor.dispatched("hicode_run")[-1]["workspace"] == str(veya.resolve())
async def test_hicode_nested_repo_uses_bound_root_not_narrow_default(
    tmp_path: Path, monkeypatch
) -> None:
    oprim, veya = make_repos(tmp_path)
    from server import hicode_agent

    # The legacy HICODE_WORKSPACE default is narrower than the one Remote root.
    # Remote containment is authoritative; the worker receives a verified
    # isolated worktree under the explicitly selected nested repository.
    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(oprim))
    executor = WorktreeExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, [oprim, veya])
    session = await initialize(gateway, secret, workspace=str(oprim))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "hicode.execute",
            "arguments": {"task": "harmless no-op", "workspace": str(veya), "wait": True},
        },
        secret=secret,
        session=session,
    )
    envelope = response["result"]["structuredContent"]
    assert envelope["ok"] is True, envelope
    assert executor.dispatched("hicode_run")[-1]["workspace"] == str(veya.resolve())
def test_bound_hicode_workspace_is_scoped_without_global_root_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    from server import hicode_agent

    default_root = tmp_path / "default"
    child_root = tmp_path / "isolated-child"
    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(default_root))

    with hicode_agent.bound_hicode_workspace(child_root):
        assert hicode_agent._resolve_workspace(str(child_root)) == child_root.resolve()

    with pytest.raises(ValueError, match="HICODE_WORKSPACE"):
        hicode_agent._resolve_workspace(str(child_root))
