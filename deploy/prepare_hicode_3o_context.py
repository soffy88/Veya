"""Prepare a clean, pinned 3O source context for the Hicode image build.

The checkout under ``platform/3O`` is intentionally allowed to be dirty for
owner development.  Production must never install from that working tree, so
this helper archives the committed submodule pins into a separate build
context.  The resulting manifest is copied into the image as provenance.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
from pathlib import Path

PACKAGES = ("obase", "oprim", "omodul", "oskill", "oservi")


def _run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}: {detail}")
    return result.stdout.strip()


def _archive_commit(repo: Path, commit: str, destination: Path) -> None:
    _run("git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}")
    archive = destination.with_suffix(".tar")
    _run("git", "-C", str(repo), "archive", "--format=tar", "--output", str(archive), commit)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r") as handle:
        handle.extractall(destination)
    archive.unlink()


def prepare(repo_root: Path, destination: Path) -> dict[str, object]:
    repo_root = repo_root.resolve()
    pins_path = repo_root / "platform/3O/CANONICAL_PINS.json"
    pins = json.loads(pins_path.read_text(encoding="utf-8"))
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    packages: dict[str, dict[str, object]] = {}
    for name in PACKAGES:
        commit = str(pins[name]["sha"])
        source_repo = repo_root / "platform/3O" / name
        if not source_repo.is_dir():
            raise RuntimeError(f"missing 3O checkout: {source_repo}")
        dirty = bool(
            _run(
                "git",
                "-C",
                str(source_repo),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )
        )
        clean_source = destination / name
        _archive_commit(source_repo, commit, clean_source)
        packages[name] = {
            "source_sha": commit,
            "owner_checkout_dirty": dirty,
            "build_source": "committed-git-archive",
        }

    manifest = {
        "schema_version": 1,
        "build_source_dirty": False,
        "owner_dirty_3o_source_used": False,
        "packages": packages,
    }
    (destination / "build-source-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    manifest = prepare(args.repo_root, args.destination)
    print("BUILD_SOURCE_DIRTY=NO")
    print("OWNER_DIRTY_3O_SOURCE_USED=NO")
    for name in PACKAGES:
        print(f"{name.upper()}_SOURCE_SHA={manifest['packages'][name]['source_sha']}")
    print(f"HICODE_CLEAN_3O_CONTEXT={args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
