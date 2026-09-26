"""P11 governance tests: canonical 3O pins block silent repins (P11E).

Fixtures only; the real ``platform/3O`` checkouts are never touched.
Parent-level ``ls-tree`` is faked; obase object checks use real temporary
git repositories wherever the case allows it.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
CHECKER_SRC = SCRIPTS / "check_3o_canonical_pins.py"
PREFLIGHT_SRC = SCRIPTS / "production_backend_preflight.py"

FORBIDDEN_SNIPPETS = (
    "git checkout",
    "git reset",
    "git clean",
    "git submodule update",
    "git update-index",
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


checker = load_module("p11_checker", CHECKER_SRC)

PIN = "678c3a7dd5730544c4b40e2c0039f7050cc7617f"
OTHER = "e381e7edb907a9e4dcff71bd4500b7224b76f7a1"

ACTION_CONTRACT = {"module": "obase.action", "file": "obase/action.py", "symbols": []}


class FakeGit:
    """Canned parent ls-tree; obase object reads go to a real repo."""

    def __init__(
        self,
        *,
        pins: dict[str, str] | None = None,
        missing: set[str] | None = None,
        real_obase: Path | None = None,
    ):
        self.pins = pins or {}
        self.missing = missing or set()
        self.real_obase = real_obase

    def __call__(self, args: list[str], cwd: Path):
        if not args:
            return checker.GitResult(1, "", "empty argv")
        if args[0] == "ls-tree":
            rel = args[-1]
            if rel in self.missing:
                return checker.GitResult(0, "", "")
            sha = self.pins.get(rel, OTHER)
            return checker.GitResult(0, f"160000 commit {sha}\t{rel}\n", "")
        if args[0] == "cat-file" and self.real_obase is not None:
            return checker.default_git_runner(args, self.real_obase)
        if args[0] == "cat-file":
            return checker.GitResult(0, "", "")
        return checker.GitResult(1, "", f"unexpected: {args}")


def write_manifest(root: Path, obase_sha: str = PIN, contracts: list | None = None) -> None:
    manifest: dict = {
        "schema_version": 1,
        "obase": {
            "sha": obase_sha,
            "qualification": "test",
            "required_contracts": (contracts if contracts is not None else [ACTION_CONTRACT]),
        },
    }
    target = root / "platform" / "3O" / "CANONICAL_PINS.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest))


def real_extract(repo: Path):
    def _extract(repo_dir: Path, sha: str, dest: Path):
        assert sha and dest.is_dir()
        return checker.default_extract_runner(repo, sha, dest)

    return _extract


def test_manifest_matches_gitlink_passes(tmp_path: Path):
    write_manifest(tmp_path, contracts=[])
    git = FakeGit(pins={"platform/3O/obase": PIN})
    result = checker.run_governance(tmp_path, run_git=git)
    assert result.allowed and result.exit_code == 0
    assert "CANONICAL_PIN_MANIFEST=PASS" in result.lines
    assert "CANONICAL_PIN_GOVERNANCE=PASS" in result.lines
    print("P11_MATCH_PASS=PASS")


def test_manifest_differs_blocks(tmp_path: Path):
    write_manifest(tmp_path)
    git = FakeGit(pins={"platform/3O/obase": OTHER})
    result = checker.run_governance(tmp_path, run_git=git)
    assert not result.allowed and result.exit_code != 0
    assert "CANONICAL_PIN_GOVERNANCE=BLOCKED" in result.lines
    assert "COMPONENT=obase" in result.lines
    assert f"MANIFEST_SHA={PIN}" in result.lines
    assert f"GITLINK_SHA={OTHER}" in result.lines
    assert "REASON=GITLINK_DIFFERS_FROM_CANONICAL_MANIFEST" in result.lines
    assert "UNCOORDINATED_PIN_CHANGE=BLOCKED" in result.lines
    print("P11_MISMATCH_BLOCK=PASS")


def test_missing_manifest_blocks(tmp_path: Path):
    result = checker.run_governance(tmp_path, run_git=FakeGit())
    assert not result.allowed and result.exit_code != 0
    assert "REASON=MANIFEST_MISSING" in result.lines
    print("P11_MISSING_MANIFEST_BLOCK=PASS")


@pytest.mark.parametrize(
    "body", ["{not json", '{"schema_version": 2}', '{"obase": {"sha": "short"}}', "[]"]
)
def test_malformed_manifest_blocks(tmp_path: Path, body: str):
    target = tmp_path / "platform" / "3O" / "CANONICAL_PINS.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    result = checker.run_governance(tmp_path, run_git=FakeGit())
    assert not result.allowed and result.exit_code != 0
    assert "REASON=MANIFEST_MALFORMED" in result.lines
    print("P11_MALFORMED_MANIFEST_BLOCK=PASS")


def test_missing_component_gitlink_blocks(tmp_path: Path):
    write_manifest(tmp_path)
    git = FakeGit(missing={"platform/3O/obase"})
    result = checker.run_governance(tmp_path, run_git=git)
    assert not result.allowed and result.exit_code != 0
    assert "REASON=COMPONENT_GITLINK_MISSING" in result.lines
    print("P11_MISSING_GITLINK_BLOCK=PASS")


git_available = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not git_available, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_obase_repo(path: Path, *, files: dict[str, str]) -> str:
    for rel, body in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(path, "init", "-b", "main")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "fixture")
    return _git(path, "rev-parse", "HEAD")


@needs_git
def test_canonical_contract_gate_passes_on_sha(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "platform" / "3O" / "obase"
    sha = make_obase_repo(
        repo,
        files={
            "obase/__init__.py": "",
            "obase/action.py": "VALUE = 1\n",
            "obase/provider_routing.py": "THING = 1\n",
        },
    )
    write_manifest(
        tmp_path,
        obase_sha=sha,
        contracts=[
            {"module": "obase.action", "file": "obase/action.py", "symbols": ["VALUE"]},
            {
                "module": "obase.provider_routing",
                "file": "obase/provider_routing.py",
                "symbols": [],
            },
        ],
    )
    git = FakeGit(pins={"platform/3O/obase": sha}, real_obase=repo)
    result = checker.run_governance(tmp_path, run_git=git, extract_runner=real_extract(repo))
    assert result.allowed and result.exit_code == 0
    assert "REQUIRED_CONTRACT_CHECK=PASS_ON_CANONICAL_SHA" in result.lines
    print("P11_CONTRACT_GATE_PASS=PASS")


@needs_git
def test_action_missing_blocks(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "platform" / "3O" / "obase"
    sha = make_obase_repo(repo, files={"obase/__init__.py": ""})
    write_manifest(tmp_path, obase_sha=sha)
    git = FakeGit(pins={"platform/3O/obase": sha}, real_obase=repo)
    result = checker.run_governance(tmp_path, run_git=git, extract_runner=real_extract(repo))
    assert not result.allowed and result.exit_code != 0
    assert "REASON=CANONICAL_CONTRACT_MISSING" in result.lines
    print("P11_ACTION_MISSING_BLOCK=PASS")


@needs_git
def test_action_import_failure_blocks(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "platform" / "3O" / "obase"
    sha = make_obase_repo(
        repo, files={"obase/__init__.py": "", "obase/action.py": "def broken(:\n"}
    )
    write_manifest(tmp_path, obase_sha=sha)
    git = FakeGit(pins={"platform/3O/obase": sha}, real_obase=repo)
    result = checker.run_governance(tmp_path, run_git=git, extract_runner=real_extract(repo))
    assert not result.allowed and result.exit_code != 0
    assert "REASON=CANONICAL_CONTRACT_MISSING" in result.lines
    print("P11_IMPORT_FAILURE_BLOCK=PASS")


def test_import_shadowing_blocks(tmp_path: Path, monkeypatch):
    """A partial extraction must not silently resolve to ambient code."""
    monkeypatch.chdir(tmp_path)
    write_manifest(tmp_path)
    shadow = tmp_path / "shadow" / "obase"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("")
    (shadow / "action.py").write_text("VALUE = 2\n")
    monkeypatch.setenv("PYTHONPATH", str(shadow.parent))
    git = FakeGit(pins={"platform/3O/obase": PIN})

    def _partial_extract(repo_dir: Path, sha: str, dest: Path):
        assert sha == PIN
        (dest / "obase").mkdir(parents=True, exist_ok=True)
        return True, ""

    result = checker.run_governance(tmp_path, run_git=git, extract_runner=_partial_extract)
    assert not result.allowed and result.exit_code != 0
    assert "REASON=CANONICAL_CONTRACT_MISSING" in result.lines
    print("P11_IMPORT_SHADOWING_BLOCK=PASS")


@needs_git
def test_required_module_missing_blocks(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "platform" / "3O" / "obase"
    sha = make_obase_repo(repo, files={"obase/__init__.py": "", "obase/action.py": "VALUE = 1\n"})
    write_manifest(
        tmp_path,
        obase_sha=sha,
        contracts=[
            ACTION_CONTRACT,
            {
                "module": "obase.provider_routing",
                "file": "obase/provider_routing.py",
                "symbols": [],
            },
        ],
    )
    git = FakeGit(pins={"platform/3O/obase": sha}, real_obase=repo)
    result = checker.run_governance(tmp_path, run_git=git, extract_runner=real_extract(repo))
    assert not result.allowed and result.exit_code != 0
    assert "REASON=CANONICAL_CONTRACT_MISSING" in result.lines
    print("P11_REQUIRED_MODULE_MISSING_BLOCK=PASS")


def test_checker_performs_no_mutation():
    text = CHECKER_SRC.read_text(encoding="utf-8")
    for snippet in FORBIDDEN_SNIPPETS:
        assert snippet not in text, f"checker contains {snippet!r}"
    print("P11_NO_AUTOMATIC_PIN_MUTATION=PASS")


def test_p11_and_p10_duties_differ(tmp_path: Path):
    """P11 governs the repo pin; P10 guards the runtime import."""
    write_manifest(tmp_path)
    p11 = checker.run_governance(tmp_path, run_git=FakeGit(pins={"platform/3O/obase": OTHER}))
    assert not p11.allowed  # manifest != gitlink -> P11 BLOCK

    preflight = load_module("p10_preflight_for_p11", PREFLIGHT_SRC)
    obase_dir = tmp_path / "platform" / "3O" / "obase"
    (obase_dir / "obase").mkdir(parents=True)

    class _Git:
        def __call__(self, args: list[str], cwd: Path):
            if args[0] == "ls-tree":
                return preflight.GitResult(0, f"160000 commit {OTHER}\tobase\n", "")
            return preflight.GitResult(0, f"{OTHER}\n", "")

    p10 = preflight.run_preflight(tmp_path, run_git=_Git(), check_omodul=False)
    assert not p10.allowed  # runtime contract missing -> P10 BLOCK
    assert p10.reason in (
        "OBASE_ACTION_MISSING",
        "OBASE_PIN_DRIFT",
        "OBASE_ACTION_IMPORT_FAILURE",
        "OBASE_ACTION_SHADOWED",
        "OBASE_HEAD_INVALID",
        "OBASE_SUBMODULE_MISSING",
        "OBASE_PIN_UNREADABLE",
    )
    print("P11_P10_DUTIES=PASS")
