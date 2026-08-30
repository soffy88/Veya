from __future__ import annotations

import json


def test_inspect_and_review_cli_use_master_agent(monkeypatch, capsys) -> None:
    from cli import github_pr

    prompts: list[str] = []

    async def fake_request(prompt: str, *, tool_name: str):
        prompts.append(f"{tool_name}:{prompt}")
        return {"status": "ok", "data": {"context": {"repo": "acme/demo", "number": 7}}}

    monkeypatch.setattr(github_pr, "_master_request", fake_request)
    assert github_pr.run_github_pr_cli(["inspect", "7", "--repo", "acme/demo", "--json"]) == 0
    json.loads(capsys.readouterr().out)
    assert github_pr.run_github_pr_cli(["review", "7", "--repo", "acme/demo", "--json"]) == 0
    assert any(item.startswith("github_pr_fetch:") for item in prompts)
    assert any(item.startswith("github_pr_review_prepare:") for item in prompts)


def test_noninteractive_post_does_not_call_master_tool_without_approval(
    monkeypatch, capsys
) -> None:
    from cli import github_pr

    called = False

    async def should_not_post(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("cli.github_pr._post", should_not_post)
    assert github_pr.run_github_pr_cli(["post", "review-1", "--json"]) == 4
    assert called is False
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "waiting_approval"


def test_pr_tools_are_registered_on_master_registry() -> None:
    from server.tool_registry import master_tools

    assert {
        "github_pr_fetch",
        "github_pr_review_prepare",
        "github_pr_post_review",
    } <= set(master_tools.list_tools())
