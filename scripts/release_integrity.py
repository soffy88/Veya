"""Deterministic release-integrity checks and machine-readable evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED_PARTS = {"node_modules", ".venv", "venv", "dist", "build", ".svelte-kit"}
PINNED = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[^]]+\])?==[^=;]+(?:;\s*.+)?$")
JS_PINNED = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN .*PRIVATE KEY-----|"
    r"(?:^|[\s\"'])sk-[A-Za-z0-9]{20,})"
)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(command: str) -> str:
    result = subprocess.run(command.split(), capture_output=True, text=True, check=False)
    return result.stdout.strip() or result.stderr.strip()


def dependency_pins() -> dict[str, Any]:
    python_deps: list[str] = []
    for path in sorted(ROOT.glob("**/pyproject.toml")):
        if (
            GENERATED_PARTS.intersection(path.parts)
            or "platform" in path.parts
            or "templates" in path.parts
            or "docs" in path.parts
        ):
            continue
        project = tomllib.loads(path.read_text())
        python_deps.extend(project.get("project", {}).get("dependencies", []))
        for group in project.get("project", {}).get("optional-dependencies", {}).values():
            python_deps.extend(group)
    for path in sorted(ROOT.glob("**/requirements*.txt")):
        if GENERATED_PARTS.intersection(path.parts):
            continue
        python_deps.extend(
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    js_deps: list[tuple[str, str]] = []
    for path in sorted(ROOT.glob("**/package.json")):
        if GENERATED_PARTS.intersection(path.parts):
            continue
        data = json.loads(path.read_text())
        for group in ("dependencies", "devDependencies"):
            js_deps.extend(
                (f"{path.relative_to(ROOT)}:{name}", value)
                for name, value in data.get(group, {}).items()
            )
    unpinned_python = [
        item
        for item in python_deps
        if not PINNED.match(item)
        and not item.startswith("-")
        and not re.match(r"^[A-Za-z0-9_.-]+\[[^]]+\]$", item)
    ]
    unpinned_js = [
        name
        for name, value in js_deps
        if value not in {"workspace:*"} and not JS_PINNED.match(value)
    ]
    return {
        "python_dependencies": python_deps,
        "javascript_dependencies": dict(js_deps),
        "unpinned": [*unpinned_python, *unpinned_js],
    }


def submodule_integrity() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in _git("submodule", "status", "--recursive").splitlines():
        match = re.match(r"^[-+ ]?([0-9a-f]{40})\s+([^ ]+)", line)
        if not match:
            raise RuntimeError(f"invalid submodule status: {line}")
        path, sha = match.group(2), match.group(1)
        if line.startswith(("-", "+")):
            raise RuntimeError(f"submodule is not clean/initialized: {path}")
        url = _git("config", "--get", f"submodule.{path}.url")
        subprocess.run(
            ["git", "-C", str(ROOT / path), "cat-file", "-e", f"{sha}^{{commit}}"], check=True
        )
        remote = subprocess.run(
            ["git", "ls-remote", url], capture_output=True, text=True, check=False
        )
        remote_has_sha = any(line.split("\t", 1)[0] == sha for line in remote.stdout.splitlines())
        if remote.returncode != 0 or not remote_has_sha:
            raise RuntimeError(f"submodule commit unavailable remotely: {path}@{sha}")
        rows.append({"path": path, "sha": sha, "url": url})
    return rows


def source_sbom(submodules: list[dict[str, str]]) -> dict[str, Any]:
    components: list[dict[str, str]] = []
    for lock in (ROOT / "uv.lock", ROOT / "veya_loop/uv.lock", ROOT / "services/loop-plane/uv.lock"):
        current: dict[str, str] | None = None
        for line in lock.read_text().splitlines():
            if line == "[[package]]":
                if current:
                    components.append(current)
                current = {}
            elif current is not None:
                name = re.match(r'name = "([^"]+)"', line)
                version = re.match(r'version = "([^"]+)"', line)
                source = re.match(r'source = \{ (?:registry|directory) = "([^"]+)"', line)
                if name:
                    current["name"] = name.group(1)
                elif version:
                    current["version"] = version.group(1)
                elif source:
                    current["source"] = source.group(1)
        if current:
            components.append(current)
    components.extend(
        {"name": row["path"], "version": row["sha"], "source": row["url"]} for row in submodules
    )
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": components}


def secret_scan() -> list[str]:
    findings: list[str] = []
    tracked = _git("ls-files", "-z").split("\0")
    for relative in tracked:
        if (
            not relative
            or relative == "scripts/release_integrity.py"
            or relative.startswith("tests/")
            or relative.startswith("docs/")
        ):
            continue
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        text = path.read_text(errors="ignore")
        if SECRET.search(text):
            findings.append(relative)
    return findings


def generate(output: Path) -> dict[str, Any]:
    pins = dependency_pins()
    submodules = submodule_integrity()
    findings = secret_scan()
    if pins["unpinned"]:
        raise RuntimeError(f"unpinned dependencies: {pins['unpinned']}")
    if findings:
        raise RuntimeError(f"secret findings: {findings}")
    sbom = source_sbom(submodules)
    output.mkdir(parents=True, exist_ok=True)
    (output / "sbom.json").write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n")
    artifact_files = [
        path
        for root in (ROOT / "dist", ROOT / "apps/web/build", ROOT / "apps/web/.svelte-kit/output")
        if root.exists()
        for path in root.rglob("*")
        if path.is_file() and output not in path.parents
    ]
    manifest = {
        "source_sha": _git("rev-parse", "HEAD"),
        "generated_at": datetime.now(UTC).isoformat(),
        "toolchain": {
            "python": platform.python_version(),
            "node": _version("node --version"),
            "pnpm": _version("pnpm --version"),
            "uv": _version("uv --version"),
        },
        "build_commands": [
            "pnpm install --frozen-lockfile --ignore-scripts",
            "pnpm --dir apps/web build",
            "python -m build",
        ],
        "submodules": submodules,
        "dependency_manifests": {
            str(path.relative_to(ROOT)): _sha256(path)
            for path in [
                ROOT / "pyproject.toml",
                ROOT / "uv.lock",
                ROOT / "veya_loop/pyproject.toml",
                ROOT / "veya_loop/uv.lock",
                ROOT / "services/loop-plane/pyproject.toml",
                ROOT / "services/loop-plane/uv.lock",
                ROOT / "pnpm-lock.yaml",
            ]
        },
        "sbom_sha256": _sha256(output / "sbom.json"),
        "artifacts": {str(path.relative_to(ROOT)): _sha256(path) for path in artifact_files},
    }
    (output / "provenance.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest["provenance_sha256"] = _sha256(output / "provenance.json")
    (output / "artifact-hashes.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "release-integrity")
    args = parser.parse_args()
    try:
        print(json.dumps(generate(args.output), indent=2, sort_keys=True))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"release integrity failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
