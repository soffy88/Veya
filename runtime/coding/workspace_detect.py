"""Read-only coding workspace discovery and conservative command inference."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .models import CodingWorkspace


class WorkspaceDetectionError(ValueError):
    """The requested path cannot be represented as a coding workspace."""


_COMMAND_FIELDS = {
    "test_commands": "test",
    "lint_commands": "lint",
    "typecheck_commands": "typecheck",
    "build_commands": "build",
}


def _read_text(path: Path, *, limit: int = 1_000_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except (OSError, UnicodeError):
        return ""


def _find_repo_root(path: Path) -> Path:
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return path


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _safe_repo_url(value: str | None) -> str | None:
    """Remove credentials from a Git remote before exposing it in a model."""
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https", "ssh"} and "@" in parsed.netloc:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        value = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    return value


def _provider_for(repo_url: str | None) -> str:
    if not repo_url:
        return "local"
    host = (urlsplit(repo_url).hostname or "").lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host == "gitlab.com" or host.endswith(".gitlab.com"):
        return "gitlab"
    return "unknown"


def _project_metadata(root: Path) -> tuple[str, dict[str, Any]]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return "", {}
    raw = _read_text(path)
    if not raw:
        return "", {}
    try:
        return raw, tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return raw, {}


def _has_text(root: Path, names: tuple[str, ...], needle: str) -> bool:
    return any(needle in _read_text(root / name).lower() for name in names if (root / name).is_file())


def _dedupe(commands: list[str]) -> list[str]:
    result: list[str] = []
    for command in commands:
        normalized = command.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _hint_commands(hints: Mapping[str, Any] | None, field: str) -> list[str]:
    if not hints:
        return []
    value = hints.get(field)
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _make_targets(root: Path) -> set[str]:
    content = _read_text(root / "Makefile")
    return {
        match.group(1)
        for line in content.splitlines()
        if (match := re.match(r"^([A-Za-z0-9_.-]+)\s*:", line))
    }


def _node_script_command(manager: str, script: str) -> str:
    if script == "test":
        return f"{manager} test"
    if manager == "npm":
        return f"npm run {script}"
    return f"{manager} {script}"


def _package_manager(root: Path) -> str | None:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun"
    if (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file():
        return "npm"
    return "npm" if (root / "package.json").is_file() else None


def infer_commands(
    root: str | Path,
    *,
    hints: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Infer reviewable commands from well-known project metadata only.

    This function never runs an inferred command.  Unknown scripts, deploy,
    publish, and release targets are intentionally not synthesized.
    """
    project_root = Path(root).expanduser().resolve()
    pyproject_raw, _ = _project_metadata(project_root)
    project_text = "\n".join(
        [
            pyproject_raw,
            _read_text(project_root / "requirements.txt"),
            _read_text(project_root / "requirements-dev.txt"),
        ]
    ).lower()
    commands = {field: _hint_commands(hints, field) for field in _COMMAND_FIELDS}
    make_targets = _make_targets(project_root)

    package_path = project_root / "package.json"
    package_manager = _package_manager(project_root)
    package_data: dict[str, Any] = {}
    if package_path.is_file():
        try:
            loaded = json.loads(_read_text(package_path))
            if isinstance(loaded, dict):
                package_data = loaded
        except json.JSONDecodeError:
            package_data = {}
    scripts = package_data.get("scripts") if isinstance(package_data.get("scripts"), dict) else {}

    if package_manager and scripts:
        script_aliases = {
            "test_commands": ("test",),
            "lint_commands": ("lint",),
            "typecheck_commands": ("typecheck", "type-check"),
            "build_commands": ("build",),
        }
        for field, aliases in script_aliases.items():
            for script in aliases:
                if script in scripts:
                    commands[field].append(_node_script_command(package_manager, script))
                    break

    is_python = bool(pyproject_raw or (project_root / "requirements.txt").is_file())
    if is_python:
        has_pytest = bool(
            (project_root / "pytest.ini").is_file()
            or (project_root / "tox.ini").is_file()
            or (project_root / "tests").is_dir()
            or "pytest" in project_text
        )
        if has_pytest:
            commands["test_commands"].append("pytest")
        has_ruff = bool(
            (project_root / "ruff.toml").is_file()
            or "[tool.ruff" in pyproject_raw.lower()
            or "ruff" in project_text
        )
        if has_ruff:
            commands["lint_commands"].append("ruff check .")
        has_mypy = bool(
            (project_root / "mypy.ini").is_file()
            or "[tool.mypy" in pyproject_raw.lower()
            or "mypy" in project_text
        )
        if has_mypy:
            commands["typecheck_commands"].append("mypy .")
        if "[build-system" in pyproject_raw.lower():
            commands["build_commands"].append("python -m build")

    if (project_root / "Cargo.toml").is_file():
        commands["test_commands"].append("cargo test")
        commands["lint_commands"].append("cargo clippy")
        commands["typecheck_commands"].append("cargo check")
        commands["build_commands"].append("cargo build")
    if (project_root / "go.mod").is_file():
        commands["test_commands"].append("go test ./...")
        commands["lint_commands"].append("go vet ./...")
        commands["build_commands"].append("go build ./...")

    make_aliases = {
        "test_commands": "test",
        "lint_commands": "lint",
        "typecheck_commands": "typecheck",
        "build_commands": "build",
    }
    for field, target in make_aliases.items():
        if target in make_targets:
            commands[field].append(f"make {target}")

    return {field: _dedupe(values) for field, values in commands.items()}


def detect_workspace(
    path: str | Path = ".",
    *,
    repo_url: str | None = None,
    owner_user_id: str = "local",
    hints: Mapping[str, Any] | None = None,
) -> CodingWorkspace:
    """Detect a workspace without initializing, checking out, or executing it."""
    requested = Path(path).expanduser()
    if not requested.exists():
        raise WorkspaceDetectionError(f"workspace path does not exist: {requested}")
    if not requested.is_dir():
        raise WorkspaceDetectionError(f"workspace path is not a directory: {requested}")

    root = _find_repo_root(requested.resolve())
    root = root.resolve()
    discovered_url = _safe_repo_url(repo_url) or _safe_repo_url(_git_value(root, "config", "--get", "remote.origin.url"))
    current_branch = _git_value(root, "branch", "--show-current")
    default_branch = _git_value(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if default_branch and default_branch.startswith("origin/"):
        default_branch = default_branch.removeprefix("origin/")
    if default_branch is None and current_branch in {"main", "master"}:
        default_branch = current_branch

    manifests = {
        ".git": (root / ".git").exists(),
        "pyproject.toml": (root / "pyproject.toml").is_file(),
        "package.json": (root / "package.json").is_file(),
        "pnpm-workspace.yaml": (root / "pnpm-workspace.yaml").is_file(),
        "requirements.txt": (root / "requirements.txt").is_file(),
        "Cargo.toml": (root / "Cargo.toml").is_file(),
        "go.mod": (root / "go.mod").is_file(),
        "Makefile": (root / "Makefile").is_file(),
        ".github/workflows": (root / ".github" / "workflows").is_dir(),
        "pytest.ini": (root / "pytest.ini").is_file(),
        "tox.ini": (root / "tox.ini").is_file(),
        "ruff.toml": (root / "ruff.toml").is_file(),
        "mypy.ini": (root / "mypy.ini").is_file(),
        "tsconfig.json": (root / "tsconfig.json").is_file(),
    }
    languages: list[str] = []
    if manifests["pyproject.toml"] or manifests["requirements.txt"]:
        languages.append("python")
    if manifests["package.json"]:
        languages.append("typescript" if manifests["tsconfig.json"] else "javascript")
    elif manifests["tsconfig.json"]:
        languages.append("typescript")
    if manifests["Cargo.toml"]:
        languages.append("rust")
    if manifests["go.mod"]:
        languages.append("go")

    inferred = infer_commands(root, hints=hints)
    workspace_id = "workspace-" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    return CodingWorkspace(
        id=workspace_id,
        owner_user_id=owner_user_id or "local",
        name=root.name or "workspace",
        root_path=str(root),
        repo_url=discovered_url,
        provider=_provider_for(discovered_url),
        default_branch=default_branch,
        current_branch=current_branch,
        language_hints=languages,
        package_manager=_package_manager(root),
        test_commands=inferred["test_commands"],
        lint_commands=inferred["lint_commands"],
        typecheck_commands=inferred["typecheck_commands"],
        build_commands=inferred["build_commands"],
        sandbox_profile_id=str((hints or {}).get("sandbox_profile_id") or "local_restricted"),
    )


__all__ = ["WorkspaceDetectionError", "detect_workspace", "infer_commands"]
