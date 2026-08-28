from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.harness.guides import (
    guide_commands,
    guide_conflicts,
    load_guides,
    search_guides,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "harness@example.invalid")
    _git(root, "config", "user.name", "Harness Tests")
    (root / ".gitignore").write_text(".veya/runs/\n.veya/harness/\n", encoding="utf-8")
    (root / "README.md").write_text("harness\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def test_load_agents_preserves_source_path_and_line(tmp_path: Path):
    root = _repo(tmp_path)
    guide_path = root / "AGENTS.md"
    guide_path.write_text(
        "# Project\n\n## Rules\n- Keep changes inside the task worktree.\n",
        encoding="utf-8",
    )

    guides = load_guides(root)

    assert [guide.source_path for guide in guides] == [str(guide_path)]
    rule = guides[0].rules[0]
    assert rule.source_path == str(guide_path)
    assert rule.source_line == 4
    assert rule.text == "Keep changes inside the task worktree."


def test_guide_command_extraction_and_search(tmp_path: Path):
    root = _repo(tmp_path)
    (root / ".veya").mkdir()
    (root / ".veya" / "GUIDES.md").write_text(
        """## Commands
- build: python -m build
- test: pytest -q
- lint: ruff check .
- typecheck: mypy .
- format: ruff format --check .

## Rules
- Run `pytest -q` before finalizing.
""",
        encoding="utf-8",
    )

    guides = load_guides(root)
    commands = guide_commands(guides).to_dict()

    assert commands == {
        "build": ["python -m build"],
        "test": ["pytest -q"],
        "lint": ["ruff check ."],
        "typecheck": ["mypy ."],
        "format": ["ruff format --check ."],
    }
    matches = search_guides(root, "not-present")
    assert matches == []
    assert search_guides(root, "pytest")[0]["source_path"].endswith("GUIDES.md")


def test_contradictory_rules_are_reported_with_both_sources(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text(
        "## Rules\n- Always run pytest before finalizing.\n- Never run pytest before finalizing.\n",
        encoding="utf-8",
    )

    conflicts = guide_conflicts(load_guides(root))

    assert len(conflicts) == 1
    assert conflicts[0].left_rule_id != conflicts[0].right_rule_id
    assert conflicts[0].source_paths == [str(root / "AGENTS.md")] * 2
