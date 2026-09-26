"""Canonical 3O pin governance checker (P11).

Merge/commit-time governance: the manifest
``platform/3O/CANONICAL_PINS.json`` is the authority for which submodule
SHAs may be pinned. Any parent gitlink that differs from the manifest
blocks, as does any canonical obase contract missing from the manifest
SHA. P11 complements P10 (restart-time safety); it never remediates.

Read-only by design: this checker only reads git objects, extracts the
manifest SHA to a temporary directory for import probes, and never
modifies worktrees, indexes, or pins.

Exit codes: 0 = all governed, 2 = blocked, 1 = checker error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

EXIT_OK = 0
EXIT_GUARD_ERROR = 1
EXIT_BLOCKED = 2

MANIFEST_REL = Path("platform/3O/CANONICAL_PINS.json")


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def default_git_runner(args: list[str], cwd: Path) -> GitResult:
    """Run read-only git plumbing (ls-tree / cat-file / archive)."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GitResult(1, "", str(exc))
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def find_repo_root(start: Path) -> Path | None:
    current = start.resolve()
    for _ in range(32):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def default_extract_runner(repo_dir: Path, sha: str, dest: Path) -> tuple[bool, str]:
    """Materialize *sha* into *dest* via git archive (no worktree touched)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "archive", sha],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, f"git archive failed (rc={proc.returncode})"
    try:
        with tarfile.open(fileobj=BytesIO(proc.stdout)) as tar:
            tar.extractall(path=str(dest))
    except (tarfile.TarError, OSError) as exc:
        return False, f"archive extraction failed: {exc}"
    return True, ""


def default_import_runner(argv: list[str], env: dict[str, str], cwd: Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=180
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


@dataclass
class GovernanceResult:
    allowed: bool
    exit_code: int
    lines: list[str] = field(default_factory=list)
    reason: str = ""


def _parse_gitlink(output: str) -> str | None:
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "commit":
            return parts[2]
    return None


def run_governance(
    root: Path,
    *,
    run_git=default_git_runner,
    extract_runner=default_extract_runner,
    import_runner=default_import_runner,
    python_exe: str | None = None,
) -> GovernanceResult:
    """Check every manifest component. Never mutates anything."""
    lines: list[str] = []

    def block(reason: str, component: str = "", extra: list[str] | None = None) -> GovernanceResult:
        lines.append("CANONICAL_PIN_GOVERNANCE=BLOCKED")
        if component:
            lines.append(f"COMPONENT={component}")
        lines.append(f"REASON={reason}")
        if extra:
            lines.extend(extra)
        return GovernanceResult(False, EXIT_BLOCKED, lines, reason)

    try:
        manifest_path = root / MANIFEST_REL
        if not manifest_path.is_file():
            return block("MANIFEST_MISSING", extra=[f"DETAIL={MANIFEST_REL} absent"])
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return block("MANIFEST_MALFORMED", extra=[f"DETAIL={exc}"[:200]])
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            return block("MANIFEST_MALFORMED", extra=["DETAIL=schema_version != 1"])
        lines.append("CANONICAL_PIN_MANIFEST=PASS")

        for name, spec in manifest.items():
            if name == "schema_version":
                continue
            if not isinstance(spec, dict):
                return block("MANIFEST_MALFORMED", component=name)
            manifest_sha = spec.get("sha", "")
            if not isinstance(manifest_sha, str) or len(manifest_sha) != 40:
                return block("MANIFEST_MALFORMED", component=name)
            rel = spec.get("path") or f"platform/3O/{name}"
            res = run_git(["ls-tree", "HEAD", Path(rel).as_posix()], root)
            if res.returncode != 0:
                return block(
                    "COMPONENT_GITLINK_MISSING",
                    component=name,
                    extra=[f"DETAIL={res.stderr.strip()[:160]}"],
                )
            gitlink_sha = _parse_gitlink(res.stdout)
            if gitlink_sha is None:
                return block("COMPONENT_GITLINK_MISSING", component=name)
            if gitlink_sha != manifest_sha:
                return block(
                    "GITLINK_DIFFERS_FROM_CANONICAL_MANIFEST",
                    component=name,
                    extra=[
                        f"MANIFEST_SHA={manifest_sha}",
                        f"GITLINK_SHA={gitlink_sha}",
                        "UNCOORDINATED_PIN_CHANGE=BLOCKED",
                    ],
                )
            lines.append(f"PIN_MATCH_{name.upper()}=PASS")

        obase = manifest.get("obase")
        if isinstance(obase, dict) and obase.get("required_contracts"):
            verdict = _check_canonical_contracts(
                root, obase, run_git, extract_runner, import_runner, python_exe
            )
            lines.extend(verdict.lines)
            if not verdict.allowed:
                reason = next(
                    (line.split("=", 1)[1] for line in verdict.lines if line.startswith("REASON=")),
                    "CANONICAL_CONTRACT_MISSING",
                )
                return GovernanceResult(False, EXIT_BLOCKED, lines, reason)

        lines.append("CANONICAL_PIN_GOVERNANCE=PASS")
        return GovernanceResult(True, EXIT_OK, lines, "")
    except Exception as exc:  # fail closed on checker errors
        return GovernanceResult(
            False,
            EXIT_GUARD_ERROR,
            ["CANONICAL_PIN_GOVERNANCE=FAIL", f"REASON=GUARD_ERROR: {exc!r}"[:300]],
            f"GUARD_ERROR: {exc!r}",
        )


@dataclass
class ContractVerdict:
    allowed: bool
    lines: list[str]


def _check_canonical_contracts(
    root: Path,
    obase: dict,
    run_git,
    extract_runner,
    import_runner,
    python_exe: str | None,
) -> ContractVerdict:
    """Verify the manifest SHA carries every required contract (P11D)."""
    lines: list[str] = []
    sha = str(obase["sha"])
    contracts = obase["required_contracts"]
    if not isinstance(contracts, list) or not contracts:
        return ContractVerdict(True, ["REQUIRED_CONTRACT_CHECK=SKIPPED"])
    repo_dir = root / "platform" / "3O" / "obase"
    if not repo_dir.is_dir():
        return ContractVerdict(False, ["REASON=CANONICAL_CONTRACT_MISSING"])
    for contract in contracts:
        if not isinstance(contract, dict):
            return ContractVerdict(False, ["REASON=CANONICAL_CONTRACT_MISSING"])
        rel = str(contract.get("file", ""))
        if not rel:
            return ContractVerdict(False, ["REASON=CANONICAL_CONTRACT_MISSING"])
        res = run_git(["cat-file", "-e", f"{sha}:{rel}"], repo_dir)
        if res.returncode != 0:
            lines.append("CANONICAL_PIN_GOVERNANCE=BLOCKED")
            lines.append("COMPONENT=obase")
            lines.append("REASON=CANONICAL_CONTRACT_MISSING")
            lines.append(f"DETAIL={rel} absent at {sha[:12]}")
            return ContractVerdict(False, lines)
    with tempfile.TemporaryDirectory(prefix="p11-canonical-") as tmp:
        dest = Path(tmp)
        ok, err = extract_runner(repo_dir, sha, dest)
        if not ok:
            lines.append("CANONICAL_PIN_GOVERNANCE=BLOCKED")
            lines.append("COMPONENT=obase")
            lines.append("REASON=CANONICAL_CONTRACT_MISSING")
            lines.append(f"DETAIL={err}"[:200])
            return ContractVerdict(False, lines)
        exe = python_exe or sys.executable
        for contract in contracts:
            module = str(contract.get("module", ""))
            symbols = contract.get("symbols") or []
            snippet = (
                "import importlib as _il; "
                f"_m = _il.import_module({module!r}); "
                f"_s = {symbols!r}; "
                "_missing = [s for s in _s if not hasattr(_m, s)]; "
                "assert not _missing, _missing; "
                "print(getattr(_m, '__file__', '') or '')"
            )
            env = dict(os.environ)
            ambient = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join([str(dest)] + ([ambient] if ambient else []))
            rc, out, err_out = import_runner([exe, "-c", snippet], env, root)
            origin = out.strip().splitlines()[-1].strip() if out.strip() else ""
            try:
                inside = bool(origin) and dest.resolve() in Path(origin).resolve().parents
            except OSError:
                inside = False
            if rc != 0 or not inside:
                lines.append("CANONICAL_PIN_GOVERNANCE=BLOCKED")
                lines.append("COMPONENT=obase")
                lines.append("REASON=CANONICAL_CONTRACT_MISSING")
                detail = (out.strip() + " " + err_out.strip()).strip()[:200]
                lines.append(f"DETAIL={module} {detail}")
                return ContractVerdict(False, lines)
    lines.append("REQUIRED_CONTRACT_CHECK=PASS_ON_CANONICAL_SHA")
    return ContractVerdict(True, lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="")
    parser.add_argument("--python", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else find_repo_root(Path(__file__))
    if root is None:
        print("CANONICAL_PIN_GOVERNANCE=FAIL")
        print("REASON=REPO_ROOT_NOT_FOUND")
        return EXIT_GUARD_ERROR
    result = run_governance(root, python_exe=args.python or None)
    for line in result.lines:
        print(line)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
