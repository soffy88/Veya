"""The required suites must be runnable.

``scripts/ci_test_suites.py`` is the single definition of what CI runs; the
workflow shells out to it per suite. When a test file is deleted but its entry
stays behind, the whole required suite stops: pytest aborts with
``file or directory not found`` before collecting anything, so the gate reports
an infrastructure error instead of a test result and every other suite in the
matrix is reported as failing too.

That is exactly what ``tests/test_hicode_force_cli.py`` and
``tests/test_hicode_workspace_lock.py`` did. Both were removed by the Hicode
retirement (b7b1a7d4) while the suite still named them.

So this file is the guard: deleting a test has to remove its suite entry in the
same change, and a suite that names a suite CI does not run is caught too.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SUITES_PATH = REPO_ROOT / "scripts" / "ci_test_suites.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The suites the checked-in workflow gates on.
REQUIRED_SUITES = ("unit-fast", "runtime", "goalrun", "personal")


def _suite_entries() -> dict[str, tuple[str, ...]]:
    spec = importlib.util.spec_from_file_location("ci_suites", SUITES_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.SUITE_ENTRIES)


def test_suite_entries_exist():
    """Every path a suite names must exist, or the suite cannot start."""

    missing: dict[str, list[str]] = {}
    for suite, paths in _suite_entries().items():
        absent = [path for path in paths if not (REPO_ROOT / path).exists()]
        if absent:
            missing[suite] = absent
    assert not missing, (
        "these suites name test files that do not exist, so pytest aborts before "
        f"collecting anything: {missing}"
    )


def test_every_required_suite_is_defined():
    entries = _suite_entries()
    undefined = [name for name in REQUIRED_SUITES if name not in entries]
    assert not undefined, (
        f"the CI workflow gates on suites this module does not define: {undefined}"
    )


def test_the_workflow_only_gates_on_defined_suites():
    """A workflow pointing at an undefined suite fails for a different reason."""

    assert WORKFLOW.is_file(), WORKFLOW
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"suite:\s*\[(?P<list>[^\]]*)\]", text)
    assert match, "could not find the required suite matrix in the workflow"
    named = tuple(
        part.strip().strip("\"'") for part in match.group("list").split(",") if part.strip()
    )
    entries = _suite_entries()
    unknown = [name for name in named if name not in entries]
    assert not unknown, f"workflow gates on undefined suites: {unknown}; defined: {sorted(entries)}"


def test_no_duplicate_paths_within_a_suite():
    duplicated = {
        suite: sorted({path for path in paths if list(paths).count(path) > 1})
        for suite, paths in _suite_entries().items()
    }
    duplicated = {suite: paths for suite, paths in duplicated.items() if paths}
    assert not duplicated, f"a path listed twice makes the suite run it twice: {duplicated}"


@pytest.mark.parametrize("suite", REQUIRED_SUITES)
def test_required_suites_are_not_empty(suite: str):
    entries = _suite_entries()
    assert entries.get(suite), f"required suite {suite!r} selects no tests"


def test_this_guard_is_itself_covered():
    """The guard has to run in CI, or it only protects whoever remembers it."""

    entries = _suite_entries()
    covered = any(
        str(pathlib.Path(__file__).relative_to(REPO_ROOT)) in paths for paths in entries.values()
    )
    assert covered, (
        f"{pathlib.Path(__file__).relative_to(REPO_ROOT)} is in no suite, so this "
        "guard would not run in CI"
    )
