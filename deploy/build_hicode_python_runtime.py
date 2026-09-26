"""Assemble and fingerprint the non-editable Hicode Python companion."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata as metadata
import json
import os
import platform
import sys
import sysconfig
from pathlib import Path

PACKAGE_VERSIONS = {
    "obase": "0.31.0",
    "oprim": "3.23.0",
    "omodul": "1.49.0",
    "oskill": "4.14.0",
    "oservi": "1.4.0",
    "docker": "7.1.0",
}
SOURCE_SHAS = {
    "obase": "b7d5593da7c67d61e7c0663ad3d406df6b5c5b5d",
    "oprim": "dd132e23aab8de0bebbeba45563351b0bf284faf",
    "omodul": "0f431108959f35a1cc4fa6423e92ba3eab01365e",
    "oskill": "300481c31e9c968518da582d0ed2a886c0152631",
    "oservi": "919fa0ca7eae11c445abf5ee505ab40dab2b89a5",
    "docker": None,
}
REASONIX_COMMIT = "6cc0d73405c3ad12c38753f117bdfa01417ab898"
REASONIX_INTEGRITY = "sha512-F6aZEYvH+0FT9wmuBdC6F83S2xFY2KN6uKDoyb0mKrPtKQ8kid/VDXVQizGm7RGxdHjXI4NXbdhl+ybImhX1XQ=="


def _metadata_hash(distribution: metadata.Distribution) -> str:
    files = [
        path for path in (distribution.files or ()) if str(path).endswith(".dist-info/METADATA")
    ]
    if len(files) != 1:
        raise RuntimeError(f"metadata evidence is ambiguous for {distribution.metadata['Name']}")
    metadata_path = Path(str(distribution.locate_file(files[0])))
    return hashlib.sha256(metadata_path.read_bytes()).hexdigest()


def _load_build_source_manifest(path: Path) -> dict[str, object]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"3O build source manifest is unreadable: {path}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("build_source_dirty") is not False
        or manifest.get("owner_dirty_3o_source_used") is not False
    ):
        raise RuntimeError("3O build source is not a clean committed source set")
    packages = manifest.get("packages")
    if not isinstance(packages, dict):
        raise RuntimeError("3O build source manifest has no package set")
    for name, expected_sha in SOURCE_SHAS.items():
        if name == "docker":
            continue
        package = packages.get(name)
        if not isinstance(package, dict) or package.get("source_sha") != expected_sha:
            raise RuntimeError(f"3O source pin mismatch: {name}")
    return manifest


def _python_evidence(root: Path) -> dict[str, str | bool]:
    python_path = (root / "python/bin/python").resolve()
    executable = Path(sys.executable).resolve()
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    encodings_path = stdlib / "encodings/__init__.py"
    if not python_path.is_file() or not os.access(python_path, os.X_OK):
        raise RuntimeError(f"managed Python is not executable: {python_path}")
    if executable != python_path:
        raise RuntimeError(f"managed Python executable mismatch: {executable} != {python_path}")
    if os.environ.get("PYTHONHOME"):
        raise RuntimeError("PYTHONHOME must not override the managed interpreter")
    if not stdlib.is_dir() or not encodings_path.is_file():
        raise RuntimeError(f"managed Python stdlib/encodings is missing: {encodings_path}")
    import encodings

    if Path(encodings.__file__).resolve() != encodings_path.resolve():
        raise RuntimeError("encodings import did not resolve to the managed stdlib")
    return {
        "executable_valid": True,
        "stdlib_present": True,
        "encodings_import": True,
        "pythonhome_override": False,
        "sys_prefix": str(Path(sys.prefix).resolve()),
        "sys_base_prefix": str(Path(sys.base_prefix).resolve()),
        "stdlib": str(stdlib),
        "encodings": str(Path(encodings.__file__).resolve()),
    }


def build_manifest(root: Path, source_manifest_path: Path) -> None:
    root = root.resolve()
    source_manifest = _load_build_source_manifest(source_manifest_path.resolve())
    python_path = root / "python/bin/python"
    python_evidence = _python_evidence(root)
    site_packages = next(python_path.parent.parent.glob("lib/python*/site-packages"))
    reasonix_path = (root / "node_modules/reasonix/bin/reasonix.js").resolve()
    packages: dict[str, dict[str, str | None]] = {}
    for name, expected_version in PACKAGE_VERSIONS.items():
        module = importlib.import_module(name)
        distribution = metadata.distribution(name)
        actual_version = distribution.version
        if actual_version != expected_version:
            raise RuntimeError(
                f"{name} version drift: expected {expected_version}, got {actual_version}"
            )
        if not module.__file__:
            raise RuntimeError(f"{name} has no import path")
        module_path = Path(module.__file__).resolve().parent
        if root not in module_path.parents:
            raise RuntimeError(f"{name} escaped the managed runtime root: {module_path}")
        packages[name] = {
            "version": actual_version,
            "path": str(module_path.relative_to(root)),
            "source_sha": SOURCE_SHAS[name],
            "metadata_sha256": _metadata_hash(distribution),
        }

    payload = {
        "runtime_root": str(root),
        "python": {
            "executable": str(python_path),
            "version": platform.python_version(),
            "site_packages": str(site_packages),
            **python_evidence,
        },
        "reasonix": {
            "binary": str(reasonix_path),
            "version": "1.21.3",
            "commit": REASONIX_COMMIT,
        },
        "packages": {
            name: {
                "version": package["version"],
                "path": str((root / str(package["path"])).resolve()),
                "source_sha": package["source_sha"],
                "metadata_sha256": package["metadata_sha256"],
            }
            for name, package in sorted(packages.items())
        },
        "build_source": {
            "dirty": False,
            "owner_dirty_3o_source_used": False,
            "packages": {
                name: source_manifest["packages"][name]["source_sha"]
                for name in PACKAGE_VERSIONS
                if name != "docker"
            },
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "runtime_generation": "hicode-3o-clean-2026-09-21",
        "created_from": "clean canonical 3O source wheels; non-editable wheels",
        "python": {
            "version": platform.python_version(),
            "executable": "python/bin/python",
            "site_packages": str(site_packages.relative_to(root)),
            **python_evidence,
        },
        "reasonix": {
            "package": "reasonix",
            "version": "1.21.3",
            "binary": "node_modules/reasonix/bin/reasonix.js",
            "commit": REASONIX_COMMIT,
            "tarball_integrity": REASONIX_INTEGRITY,
        },
        "packages": packages,
        "build_source": {
            "dirty": False,
            "owner_dirty_3o_source_used": False,
            "packages": {
                name: source_manifest["packages"][name]["source_sha"]
                for name in PACKAGE_VERSIONS
                if name != "docker"
            },
        },
        "runtime_fingerprint": fingerprint,
    }
    (root / "runtime-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=None,
        help="clean 3O build-source manifest (defaults to <root>/3o-build-source.json)",
    )
    args = parser.parse_args()
    build_manifest(args.root, args.source_manifest or args.root / "3o-build-source.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
