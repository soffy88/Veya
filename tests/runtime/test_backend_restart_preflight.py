"""P10 guard tests: backend restart preflight is fail-closed (P10G).

Uses temporary fixture trees and injectable runners only; the real
``platform/3O/obase`` checkout is never touched.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
PREFLIGHT_SRC = SCRIPTS / "production_backend_preflight.py"
WRAPPER_SRC = SCRIPTS / "production_backend.py"

FORBIDDEN_SNIPPETS = (
    "git checkout",
    "git reset",
    "git clean",
    "git submodule update",
)

PIN = "2283ec4f70216b1658846318c16aa3d6f21a5666"
OTHER = "e381e7edb907a9e4dcff71bd4500b7224b76f7a1"
PARENT = "22f05e293d64854aa5f82655aaa21ca0cca1781c"


def load_module(name: str, path: Path):
    import sys as _sys

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = load_module("p10_preflight", PREFLIGHT_SRC)
wrapper = load_module("p10_wrapper", WRAPPER_SRC)


def make_obase_tree(base: Path, *, with_action: bool = True, broken: bool = False) -> Path:
    obase_dir = base / "platform" / "3O" / "obase"
    pkg = obase_dir / "obase"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    if with_action:
        body = "def broken(:\n" if broken else "VALUE = 1\n"
        (pkg / "action.py").write_text(body)
    omodul = base / "platform" / "3O" / "omodul" / "omodul"
    omodul.mkdir(parents=True)
    (omodul / "__init__.py").write_text("")
    return obase_dir


class FakeGit:
    """Canned git responses keyed by leading argv token."""

    def __init__(
        self, *, lstree: str = "", head_root: str = PARENT, head_obase: str = PIN, rc: int = 0
    ):
        self.lstree = lstree
        self.head_root = head_root
        self.head_obase = head_obase
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], cwd: Path):
        self.calls.append(list(args))
        if not args:
            return preflight.GitResult(1, "", "empty argv")
        if args[0] == "ls-tree":
            if self.rc != 0:
                return preflight.GitResult(self.rc, "", "ls-tree boom")
            return preflight.GitResult(0, self.lstree, "")
        if args[0] == "rev-parse":
            if self.rc != 0:
                return preflight.GitResult(self.rc, "", "rev-parse boom")
            if str(cwd).endswith("obase"):
                return preflight.GitResult(0, self.head_obase + "\n", "")
            return preflight.GitResult(0, self.head_root + "\n", "")
        return preflight.GitResult(1, "", f"unexpected: {args}")


def lstree_line(sha: str) -> str:
    return f"160000 commit {sha}\tplatform/3O/obase\n"


def test_match_pass(tmp_path: Path):
    make_obase_tree(tmp_path)
    git = FakeGit(lstree=lstree_line(PIN), head_obase=PIN)
    result = preflight.run_preflight(tmp_path, run_git=git)
    assert result.allowed and result.exit_code == 0
    assert "PRODUCTION_RESTART_GUARD=PASS" in result.lines
    assert "BACKEND_RESTART_ALLOWED=YES" in result.lines
    assert f"EXPECTED_OBASE_PIN={PIN}" in result.lines
    print("P10_MATCH_PASS=PASS")


def test_mismatch_block(tmp_path: Path):
    make_obase_tree(tmp_path)
    git = FakeGit(lstree=lstree_line(PIN), head_obase=OTHER)
    result = preflight.run_preflight(tmp_path, run_git=git)
    assert not result.allowed and result.exit_code != 0
    assert "BACKEND_RESTART_BLOCKED=YES" in result.lines
    assert "REASON=OBASE_PIN_DRIFT" in result.lines
    print("P10_MISMATCH_BLOCK=PASS")


def test_missing_submodule_block(tmp_path: Path):
    git = FakeGit(lstree=lstree_line(PIN), head_obase=PIN)
    result = preflight.run_preflight(tmp_path, run_git=git)
    assert not result.allowed and result.exit_code != 0
    assert "REASON=OBASE_SUBMODULE_MISSING" in result.lines
    print("P10_MISSING_SUBMODULE_BLOCK=PASS")


def test_invalid_head_block(tmp_path: Path):
    make_obase_tree(tmp_path)
    git = FakeGit(lstree=lstree_line(PIN), rc=1)
    result = preflight.run_preflight(tmp_path, run_git=git)
    assert not result.allowed and result.exit_code != 0
    assert result.reason in ("OBASE_PIN_UNREADABLE", "OBASE_HEAD_INVALID")
    print("P10_INVALID_HEAD_BLOCK=PASS")


def test_action_missing_block(tmp_path: Path):
    make_obase_tree(tmp_path, with_action=False)
    git = FakeGit(lstree=lstree_line(PIN), head_obase=PIN)
    result = preflight.run_preflight(tmp_path, run_git=git)
    assert not result.allowed and result.exit_code != 0
    assert "REASON=OBASE_ACTION_MISSING" in result.lines
    print("P10_ACTION_MISSING_BLOCK=PASS")


def test_import_failure_block(tmp_path: Path):
    make_obase_tree(tmp_path, broken=True)
    git = FakeGit(lstree=lstree_line(PIN), head_obase=PIN)
    result = preflight.run_preflight(tmp_path, run_git=git)
    assert not result.allowed and result.exit_code != 0
    assert "REASON=OBASE_ACTION_IMPORT_FAILURE" in result.lines
    print("P10_IMPORT_FAILURE_BLOCK=PASS")


def test_import_shadowing_block(tmp_path: Path):
    real_dir = tmp_path / "real"
    shadow_dir = tmp_path / "shadow"
    make_obase_tree(real_dir)
    make_obase_tree(shadow_dir)
    check = preflight.verify_module_import(
        "obase.action",
        [str(shadow_dir / "platform" / "3O" / "obase")],
        real_dir / "platform" / "3O" / "obase",
    )
    assert not check.ok
    assert check.origin is not None and "shadow" in check.origin
    print("P10_IMPORT_SHADOWING_BLOCK=PASS")


def test_no_automatic_mutation_in_sources():
    for src in (PREFLIGHT_SRC, WRAPPER_SRC):
        text = src.read_text(encoding="utf-8")
        for snippet in FORBIDDEN_SNIPPETS:
            assert snippet not in text, f"{src.name} contains {snippet!r}"
    print("P10_NO_AUTOMATIC_CHECKOUT=PASS")


def test_wrapper_block_never_invokes_compose(tmp_path: Path):
    calls: list[str] = []
    code = wrapper.run_operation(
        tmp_path,
        "restart",
        preflight_fn=lambda: preflight.EXIT_BLOCKED,
        compose_fn=lambda op: calls.append(op) or 0,
    )
    assert code == preflight.EXIT_BLOCKED
    assert calls == []
    print("P10_WRAPPER_BLOCK_NO_INVOKE=PASS")


def test_wrapper_pass_invokes_compose_once(tmp_path: Path):
    calls: list[str] = []
    code = wrapper.run_operation(
        tmp_path,
        "restart",
        preflight_fn=lambda: preflight.EXIT_OK,
        compose_fn=lambda op: calls.append(op) or 0,
    )
    assert code == 0
    assert calls == ["restart"]
    print("P10_WRAPPER_PASS_SINGLE_INVOKE=PASS")


git_available = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not git_available, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, timeout=60
    )


@needs_git
def test_real_git_pin_read_and_repo_root(tmp_path: Path):
    _git(tmp_path, "init", "-b", "main")
    _git(
        tmp_path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "--allow-empty",
        "-m",
        "init",
    )
    assert preflight.find_repo_root(tmp_path / "scripts") == tmp_path.resolve()
    ok, sha, _ = preflight.read_current_head(tmp_path)
    assert ok == "ok" and sha
    ok_pin, pin, _ = preflight.read_expected_pin(tmp_path)
    assert not ok_pin and pin is None  # no submodule gitlink present
    print("P10_REAL_GIT_READ=PASS")


@needs_git
def test_real_git_invalid_head(tmp_path: Path):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    status, sha, _ = preflight.read_current_head(empty)
    assert status == "invalid" and sha is None
    missing = tmp_path / "absent"
    status, _, _ = preflight.read_current_head(missing)
    assert status == "missing"
    print("P10_REAL_GIT_INVALID=PASS")


@needs_git
def test_real_import_origin_pass_and_failure(tmp_path: Path):
    tree = tmp_path / "tree"
    obase_dir = make_obase_tree(tree)
    check = preflight.verify_module_import(
        "obase.action",
        [str(obase_dir)],
        obase_dir,
        python_exe=sys.executable,
        cwd=tmp_path,
    )
    assert check.ok and check.origin is not None
    assert "tree" in check.origin
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path / "nowhere")
    rc, _, _ = preflight.default_import_runner(
        [sys.executable, "-c", "import obase.action"], env, tmp_path
    )
    assert rc != 0
    print("P10_REAL_IMPORT=PASS")
