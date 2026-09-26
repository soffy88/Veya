"""Canonical preflight guard for production backend restarts (P10).

Fail-closed gate: before any ``backend`` container start/restart, the working
tree's ``platform/3O/obase`` checkout must match the parent repo gitlink AND
``obase.action`` must import from that exact checkout. Anything else blocks
the restart with a non-zero exit.

Read-only by design. This script never switches branches, never rewrites
history, never deletes files, and never forces submodule synchronization.
The authority for the expected pin is always the parent gitlink
(``git ls-tree HEAD platform/3O/obase``); nothing is hardcoded.

Exit codes: 0 = restart allowed, 2 = restart blocked, 1 = guard error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
EXIT_GUARD_ERROR = 1
EXIT_BLOCKED = 2

OBASE_SUBMODULE = Path("platform/3O/obase")
OBASE_PACKAGE = Path("obase")
ACTION_MODULE_FILE = OBASE_PACKAGE / "action.py"
ACTION_MODULE_NAME = "obase.action"
OMODUL_DIRNAME = "omodul"

_IMPORT_SNIPPET = "import {module} as _m, sys; print(getattr(_m, '__file__', '') or '')"


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def default_git_runner(args: list[str], cwd: Path) -> GitResult:
    """Run a read-only git command. Only ls-tree / rev-parse are ever issued."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GitResult(1, "", str(exc))
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def find_repo_root(start: Path) -> Path | None:
    """Ascend from *start* until a ``.git`` entry is found."""
    current = start.resolve()
    for _ in range(32):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def read_expected_pin(root: Path, run_git=default_git_runner) -> tuple[bool, str | None, str]:
    """Parent gitlink for the obase submodule (the only authority)."""
    res = run_git(["ls-tree", "HEAD", OBASE_SUBMODULE.as_posix()], root)
    if res.returncode != 0:
        return False, None, f"ls-tree failed: {res.stderr.strip()[:160]}"
    for line in res.stdout.splitlines():
        parts = line.split()
        # Format: "<mode> <type> <sha>\t<path>"
        if len(parts) >= 3 and parts[1] == "commit":
            return True, parts[2], ""
    return False, None, f"no commit gitlink in ls-tree output: {res.stdout[:160]!r}"


def read_current_head(obase_dir: Path, run_git=default_git_runner) -> tuple[str, str | None, str]:
    """Return (status, sha, error) where status is ok/missing/invalid."""
    if not obase_dir.is_dir():
        return "missing", None, f"submodule directory absent: {obase_dir}"
    res = run_git(["rev-parse", "HEAD"], obase_dir)
    if res.returncode != 0:
        return "invalid", None, f"rev-parse failed: {res.stderr.strip()[:160]}"
    sha = res.stdout.strip().split()[0] if res.stdout.strip() else ""
    if not sha:
        return "invalid", None, "rev-parse returned empty output"
    return "ok", sha, ""


def action_file_present(obase_dir: Path) -> bool:
    return (obase_dir / ACTION_MODULE_FILE).is_file()


@dataclass
class ImportCheck:
    ok: bool
    origin: str | None
    error: str


def default_import_runner(argv: list[str], env: dict[str, str], cwd: Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def verify_module_import(
    module: str,
    path_entries: list[str],
    expected_origin_dir: Path | None,
    *,
    python_exe: str | None = None,
    cwd: Path | None = None,
    import_runner=default_import_runner,
) -> ImportCheck:
    """Import *module* in a subprocess and report where it resolved from."""
    exe = python_exe or sys.executable
    snippet = _IMPORT_SNIPPET.format(module=module)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    ordered = [p for p in path_entries if p] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(ordered)
    rc, out, err = import_runner([exe, "-c", snippet], env, cwd or Path.cwd())
    if rc != 0:
        detail = (out.strip() + " " + err.strip()).strip()[:300]
        return ImportCheck(False, None, f"import failed (rc={rc}): {detail}")
    origin = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not origin:
        return ImportCheck(False, None, "import succeeded but reported no __file__")
    if expected_origin_dir is not None:
        try:
            expected_pkg = (expected_origin_dir / module.split(".")[0]).resolve()
            actual = Path(origin).resolve()
            inside = actual == expected_pkg or expected_pkg in actual.parents
        except OSError as exc:
            return ImportCheck(False, origin, f"origin resolution failed: {exc}")
        if not inside:
            return ImportCheck(False, origin, f"shadowing: resolved outside {expected_pkg}")
    return ImportCheck(True, origin, "")


@dataclass
class PreflightResult:
    allowed: bool
    exit_code: int
    lines: list[str] = field(default_factory=list)
    reason: str = ""


def run_preflight(
    root: Path,
    *,
    run_git=default_git_runner,
    import_runner=default_import_runner,
    python_exe: str | None = None,
    check_omodul: bool = True,
) -> PreflightResult:
    """Execute all guards in order. Never mutates anything."""
    lines: list[str] = []

    def block(reason: str, extra: list[str] | None = None) -> PreflightResult:
        lines.append("PRODUCTION_RESTART_GUARD=PASS")
        lines.append("OBASE_PIN_GUARD=BLOCKED")
        lines.append("CURRENT_OBASE_AT_PARENT_PIN=NO")
        lines.append("BACKEND_RESTART_ALLOWED=NO")
        lines.append("BACKEND_RESTART_BLOCKED=YES")
        lines.append(f"REASON={reason}")
        if extra:
            lines.extend(extra)
        lines.append("")
        lines.append("Backend restart is blocked.")
        lines.append("Resolve the obase worktree/owner session first.")
        lines.append("Do not force-reset owner changes.")
        lines.append("After reconciliation, rerun:")
        lines.append("  python scripts/production_backend_preflight.py")
        return PreflightResult(False, EXIT_BLOCKED, lines, reason)

    try:
        parent_head_res = run_git(["rev-parse", "HEAD"], root)
        parent_head = (
            parent_head_res.stdout.strip().split()[0]
            if parent_head_res.returncode == 0 and parent_head_res.stdout.strip()
            else "unknown"
        )
        lines.append(f"VEYA_PARENT_HEAD={parent_head}")

        ok, expected, err = read_expected_pin(root, run_git)
        if not ok:
            return block("OBASE_PIN_UNREADABLE", [f"EXPECTED_OBASE_PIN=unknown DETAIL={err}"])
        expected_pin = str(expected)
        lines.append(f"EXPECTED_OBASE_PIN={expected_pin}")

        obase_dir = root / OBASE_SUBMODULE
        status, current, cerr = read_current_head(obase_dir, run_git)
        if status == "missing":
            lines.append("CURRENT_OBASE_HEAD=absent")
            return block("OBASE_SUBMODULE_MISSING", [f"DETAIL={cerr}"])
        if status == "invalid":
            lines.append("CURRENT_OBASE_HEAD=invalid")
            return block("OBASE_HEAD_INVALID", [f"DETAIL={cerr}"])
        current_sha = str(current)
        lines.append(f"CURRENT_OBASE_HEAD={current_sha}")

        if current_sha != expected_pin:
            return block(
                "OBASE_PIN_DRIFT",
                [
                    f"Expected obase pin: {expected_pin}",
                    f"Current obase HEAD: {current_sha}",
                ],
            )
        lines.append("OBASE_PIN_GUARD=PASS")
        lines.append("CURRENT_OBASE_AT_PARENT_PIN=YES")

        if not action_file_present(obase_dir):
            return block(
                "OBASE_ACTION_MISSING",
                [f"DETAIL={(obase_dir / ACTION_MODULE_FILE)} absent"],
            )
        lines.append("OBASE_ACTION_FILE=PASS")

        action_check = verify_module_import(
            ACTION_MODULE_NAME,
            [str(obase_dir)],
            obase_dir,
            python_exe=python_exe,
            cwd=root,
            import_runner=import_runner,
        )
        if not action_check.ok:
            reason = (
                "OBASE_ACTION_SHADOWED" if action_check.origin else "OBASE_ACTION_IMPORT_FAILURE"
            )
            return block(reason, [f"DETAIL={action_check.error}"])
        lines.append("OBASE_ACTION_IMPORT=PASS")
        lines.append("OBASE_IMPORT_ORIGIN=PASS")

        if check_omodul:
            three_o = root / "platform" / "3O"
            # Mirror the backend container import surface: omodul pulls
            # oprim/oskill at import time, so the probe needs them too.
            # Only existing directories are added (fixtures stay minimal).
            probe_dirs = [
                str(candidate)
                for name in (OMODUL_DIRNAME, "obase", "oprim", "oskill")
                if (candidate := three_o / name).is_dir()
            ]
            omodul_dir = three_o / OMODUL_DIRNAME
            omodul_check = verify_module_import(
                OMODUL_DIRNAME,
                probe_dirs or [str(omodul_dir), str(obase_dir)],
                None,
                python_exe=python_exe,
                cwd=root,
                import_runner=import_runner,
            )
            if not omodul_check.ok:
                return block("OMODUL_IMPORT_FAILURE", [f"DETAIL={omodul_check.error}"])
            lines.append("OMODUL_IMPORT=PASS")

        lines.append("PRODUCTION_RESTART_GUARD=PASS")
        lines.append("OBASE_PIN_GUARD=PASS")
        lines.append("BACKEND_RESTART_ALLOWED=YES")
        return PreflightResult(True, EXIT_OK, lines, "")
    except Exception as exc:  # guard itself must fail closed
        return PreflightResult(
            False,
            EXIT_GUARD_ERROR,
            [
                "PRODUCTION_RESTART_GUARD=FAIL",
                "BACKEND_RESTART_ALLOWED=NO",
                f"REASON=GUARD_ERROR: {exc!r}"[:300],
            ],
            f"GUARD_ERROR: {exc!r}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="", help="Repo root (default: auto-discover)")
    parser.add_argument("--python", default="", help="Python for import probes")
    parser.add_argument("--skip-omodul", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else find_repo_root(Path(__file__))
    if root is None:
        print("PRODUCTION_RESTART_GUARD=FAIL")
        print("BACKEND_RESTART_ALLOWED=NO")
        print("REASON=REPO_ROOT_NOT_FOUND")
        return EXIT_GUARD_ERROR
    result = run_preflight(
        root,
        python_exe=args.python or None,
        check_omodul=not args.skip_omodul,
    )
    for line in result.lines:
        print(line)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
