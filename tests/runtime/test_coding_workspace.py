from __future__ import annotations

import json
import subprocess
from pathlib import Path

from runtime.coding.workspace_detect import detect_workspace, infer_commands


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
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Coding Tests")
    (root / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (root / "README.md").write_text("test\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def test_detect_python_workspace_and_infer_known_commands(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
dependencies = ["pytest", "ruff", "mypy"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100

[tool.mypy]
strict = true
""",
        encoding="utf-8",
    )
    workspace = detect_workspace(root)

    assert workspace.root_path == str(root.resolve())
    assert workspace.provider == "local"
    assert workspace.current_branch == "main"
    assert workspace.default_branch == "main"
    assert workspace.language_hints == ["python"]
    assert workspace.test_commands == ["pytest"]
    assert workspace.lint_commands == ["ruff check ."]
    assert workspace.typecheck_commands == ["mypy ."]
    assert workspace.build_commands == ["python -m build"]


def test_detect_node_workspace_uses_lockfile_and_package_scripts(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "vitest",
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                    "build": "vite build",
                    "deploy": "echo must not be inferred",
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    nested = root / "src"
    nested.mkdir()

    workspace = detect_workspace(nested)

    assert workspace.package_manager == "pnpm"
    assert workspace.language_hints == ["typescript"]
    assert workspace.test_commands == ["pnpm test"]
    assert workspace.lint_commands == ["pnpm lint"]
    assert workspace.typecheck_commands == ["pnpm typecheck"]
    assert workspace.build_commands == ["pnpm build"]
    assert all("deploy" not in command for command in workspace.build_commands)


def test_remote_credentials_are_not_exposed_and_hints_are_not_executed(tmp_path: Path):
    root = _repo(tmp_path)
    _git(root, "remote", "add", "origin", "https://user:super-secret@github.com/acme/demo.git")
    before = _git(root, "status", "--porcelain")
    commands = infer_commands(
        root,
        hints={"test_commands": ["rm -rf /"], "sandbox_profile_id": "local_restricted"},
    )
    workspace = detect_workspace(root, hints={"test_commands": ["rm -rf /"]})

    assert workspace.provider == "github"
    assert workspace.repo_url == "https://github.com/acme/demo.git"
    assert commands["test_commands"] == ["rm -rf /"]
    assert _git(root, "status", "--porcelain") == before
