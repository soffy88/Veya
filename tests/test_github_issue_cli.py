from __future__ import annotations

import json


def test_issue_inspect_and_fix_use_master_agent(monkeypatch, capsys) -> None:
    from cli import github_issue

    prompts: list[str] = []

    async def fake_request(prompt: str, *, tool_name: str):
        prompts.append(f"{tool_name}:{prompt}")
        return {"status": "ok", "data": {"context": {"repo": "acme/demo", "number": 42}}}

    monkeypatch.setattr(github_issue, "_master_request", fake_request)
    assert (
        github_issue.run_github_issue_cli(["inspect", "42", "--repo", "acme/demo", "--json"]) == 0
    )
    json.loads(capsys.readouterr().out)
    assert github_issue.run_github_issue_cli(["fix", "42", "--repo", "acme/demo", "--json"]) == 0
    assert any(item.startswith("github_issue_fetch:") for item in prompts)
    assert any(item.startswith("github_issue_fix_prepare:") for item in prompts)


def test_noninteractive_publish_requires_explicit_approval(monkeypatch, capsys) -> None:
    from cli import github_issue

    called = False

    async def should_not_publish(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("cli.github_issue._publish", should_not_publish)
    assert github_issue.run_github_issue_cli(["publish", "issue-task", "--json"]) == 4
    assert called is False
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "waiting_approval"
    assert value["data"]["remote_side_effect"] is False


def test_issue_tools_are_registered_on_master_registry() -> None:
    from server.tool_registry import master_tools

    assert {
        "github_issue_fetch",
        "github_issue_fix_prepare",
        "github_pr_create_draft",
    } <= set(master_tools.list_tools())
