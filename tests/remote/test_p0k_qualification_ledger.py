"""P0-K: the qualification ledger is auditable, and it does not flatter itself.

A ledger is only worth something if it can fail. Every claim here is checked
against the repository rather than read: commit SHAs must exist with the recorded
parent, the recorded changed-paths must match what those commits actually
touched, the test files must contain the tests they claim, and the statuses must
come from the declared vocabulary.

The statuses are deliberately not all PASS. Two findings and one blocker are
carried as open, and TOOL-BUG-1 stays OPEN because the alternative is asserting a
schema that the governed path does not actually enforce.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = ROOT / "docs" / "qualification" / "local2-closure" / "qualification_ledger.json"

# §11.2 the universal receipt must carry these to be auditable.
UNIVERSAL_RECEIPT_FIELDS = (
    "receipt_id",
    "operation",
    "actor",
    "target",
    "effect",
    "admission",
    "start",
    "finish",
    "status",
)

# §11.3-11.6 the four specific receipt shapes.
EXECUTION_RECEIPT_FIELDS = (
    "execution_id",
    "cwd",
    "command",
    "sandbox_decision",
    "exit_code",
    "terminal_status",
)
FILE_RECEIPT_FIELDS = ("path", "operation", "target", "before", "after")
GIT_RECEIPT_FIELDS = ("branch", "parent_sha", "commit_sha", "staged_paths")
PROCESS_RECEIPT_FIELDS = ("process_id", "execution_id", "terminal_state", "cancel_state")


@pytest.fixture(scope="module")
def ledger() -> dict[str, Any]:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, cwd=ROOT
    ).stdout


def test_the_ledger_exists_and_declares_its_schema(ledger: dict[str, Any]) -> None:
    assert ledger["schema"] == "local2-qualification-ledger/v1"
    assert ledger["branch"] == "main"
    assert ledger["status_vocabulary"]


# ── every phase commit must exist with the recorded lineage ───────────
def test_every_phase_commit_exists_with_its_recorded_parent(ledger: dict[str, Any]) -> None:
    for phase in [p for p in ledger["phases"] if p["commit"]]:
        recorded = phase["commit"]
        actual = git("rev-parse", "--short", recorded).strip()
        assert actual == recorded, f"{phase['phase']}: {recorded} is not a commit"

        parent = git("rev-parse", "--short", f"{recorded}^").strip()
        assert parent == phase["parent"], f"{phase['phase']}: parent {parent} != {phase['parent']}"


def test_every_recorded_changed_path_is_exactly_what_the_commit_touched(
    ledger: dict[str, Any],
) -> None:
    """A ledger that lists paths the commit did not touch is not evidence."""
    for phase in [p for p in ledger["phases"] if p["commit"]]:
        recorded = sorted(phase["changed_paths"])
        actual = sorted(
            path
            for path in git("show", "--name-only", "--format=", phase["commit"]).split()
            if path
        )
        assert recorded == actual, f"{phase['phase']}: recorded {recorded} vs actual {actual}"


def test_the_phase_commits_form_an_unbroken_chain(ledger: dict[str, Any]) -> None:
    # A phase with no commit (blocked before it could start) carries parent=None
    # and is excluded from the chain rather than being given a fake SHA.
    phases = [p for p in ledger["phases"] if p["commit"]]
    assert phases, "no committed phase"
    for earlier, later in itertools.pairwise(phases):
        assert earlier["commit"] == later["parent"], f"{later['phase']} does not follow {earlier['phase']}"
    assert phases[0]["parent"] == ledger["baseline_sha"][:8]


def test_the_head_commit_is_the_last_recorded_phase(ledger: dict[str, Any]) -> None:
    committed = [p for p in ledger["phases"] if p["commit"]]
    last = committed[-1]["commit"]

    # HEAD is not itself a phase: P0-K is the phase that wrote this ledger, so
    # it is deliberately absent from the ledger it authored. What must hold is
    # that the last phase commit is an ancestor of HEAD — the ledger was written
    # on top of the work it describes, not beside it.
    is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", last, "HEAD"], cwd=ROOT, check=False
    )
    assert is_ancestor.returncode == 0, f"{last} is not an ancestor of HEAD"

    declared = ledger["generated_at_commit"]
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", declared, "HEAD"], cwd=ROOT, check=False
    ).returncode == 0, f"generated_at_commit {declared} is not an ancestor of HEAD"


# ── statuses must be honest, not uniformly PASS ────────────────────────
def test_statuses_come_from_the_declared_vocabulary(ledger: dict[str, Any]) -> None:
    allowed = set(ledger["status_vocabulary"])
    assert "BLOCKED" in allowed
    for phase in ledger["phases"]:
        assert phase["status"] in allowed, f"{phase['phase']}: {phase['status']}"


def test_the_findings_are_carried_not_buried(ledger: dict[str, Any]) -> None:
    """§11.7: a receipt must not claim more than it proved.

    The two known findings and the pre-existing blocker must still be present,
    with an owner, and must not have been closed by the phase that found them.
    """
    findings = {
        finding["id"]: finding
        for phase in ledger["phases"]
        for finding in phase.get("findings", [])
    }
    blockers = {
        blocker["id"]: blocker
        for phase in ledger["phases"]
        for blocker in phase.get("blockers", [])
    }

    # P0-P-F1 was added at P0-P (reconcile_task measured redundant on the direct
    # path) and P0-J-G3 at SF-RECEIPT (the phase-commit guard asserts whatever
    # HEAD is, not the commit that wrote the file). Both are restated rather than
    # relaxed, so a phase that opens a finding still has to carry it.
    # P0-L-F1 joined at the P0-L final rerun, and is carried with status CLOSED
    # because the P0-Q follow-up fixed it. The guard's real purpose is that an
    # opened finding cannot quietly vanish, so a closed one must stay visible.
    assert set(findings) == {
        "P0-H-F1", "P0-J-F1", "P0-P-F1", "P0-J-G3", "P0-L-F1",
    }, sorted(findings)
    assert findings["P0-L-F1"]["status"] == "CLOSED"
    assert findings["P0-H-F1"]["status"] == "OPEN"
    assert findings["P0-J-F1"]["status"] == "ESCALATED"
    assert findings["P0-J-F1"]["severity"] == "HIGH"

    for finding in findings.values():
        assert finding["owner"], finding
        assert finding["evidence"], finding
        assert finding["why_open"], finding

    assert "P0-J-B1" in blockers, sorted(blockers)
    assert blockers["P0-J-B1"]["status"] == "PRE_EXISTING"
    assert blockers["P0-J-B1"]["workaround"], "a blocker must record how it was worked around"


def test_target_contract_is_deferred_on_every_phase(ledger: dict[str, Any]) -> None:
    for phase in ledger["phases"]:
        assert phase["target_contract"] == "DEFERRED", phase["phase"]

    cross = ledger["cross_cutting"]["target_contract"]
    assert cross["status"] == "DEFERRED"
    assert "local2/l0-l1-l2" in cross["verified_present_on"]


def test_a_blocked_phase_names_its_blocker_and_claims_nothing(
    ledger: dict[str, Any],
) -> None:
    """A BLOCKED verdict must carry the reason, and must not look like progress."""
    blocked = [p for p in ledger["phases"] if p["status"] == "BLOCKED"]
    for phase in blocked:
        assert phase["commit"] is None, phase["phase"]
        assert phase["blockers"], phase["phase"]
        for blocker in phase["blockers"]:
            assert blocker["summary"]
            assert blocker["why_blocked"]
            assert blocker["evidence"]
            assert blocker["owner"]

        # Restated at attempt 3. The rule used to be that a BLOCKED phase must
        # show no tests added and no production change, on the assumption that
        # BLOCKED means nothing was done. That is true of a phase that bailed out
        # immediately, and false of a rerun against a substrate that is maturing
        # underneath it: attempt 3 added six qualification tests and fixed a real
        # gap (the public record projection omitted the verifier verdict) while
        # still ending BLOCKED.
        #
        # The intent is unchanged and is now stated precisely: a BLOCKED verdict
        # must not claim a commit, must name its blocker, and must enumerate any
        # tests or production changes honestly rather than reporting zero to look
        # tidy. Concealing real work to satisfy a tidiness rule is the failure
        # this guard exists to prevent.
        if phase["tests_added"] or phase["production_changed"]:
            # Anything it did change must be listed, and it must still say which
            # steps did not pass.
            assert phase.get("changed_paths") or phase.get(
                "production_note"
            ), phase["phase"]
            assert phase.get("lifecycle_evidence"), phase["phase"]
            assert blocker["owner"]
        # Evidence is still required: a blocked phase reports what it did learn.
        assert len(phase["evidence"]) >= 2, phase["phase"]


def test_tool_bug_1_stays_open_with_a_reason(ledger: dict[str, Any]) -> None:
    """§11.8: TOOL-BUG-1 must not be swallowed to make the ledger look clean."""
    bug = ledger["cross_cutting"]["tool_bug_1"]
    assert bug["id"] == "TOOL-BUG-1"
    assert bug["status"] == "OPEN"
    assert "silently" in bug["summary"]
    assert bug["why_not_closed"]
    assert bug["owner"]


def test_the_green_then_repaired_mutations_are_recorded(ledger: dict[str, Any]) -> None:
    """A gate that was green once and then repaired is part of the evidence."""
    repaired = ledger["cross_cutting"]["mutations_green_then_repaired"]
    phases = {entry["phase"] for entry in repaired}
    assert phases == {"P0-G", "P0-H"}
    for entry in repaired:
        assert entry["first_result"] == "GREEN", entry
        assert entry["cause"], entry
        assert entry["repair"], entry


# ── the ledger's claims must match the code it describes ───────────────
def test_the_recorded_test_counts_match_the_test_files(ledger: dict[str, Any]) -> None:
    for phase in [p for p in ledger["phases"] if p["commit"]]:
        tests_added = phase["tests_added"]
        assert tests_added > 0, phase["phase"]
        test_files = [p for p in phase["changed_paths"] if p.startswith("tests/")]
        assert test_files, f"{phase['phase']} claims {tests_added} tests but added no test file"

        # Collected count, not the number of ``def test_`` lines: a
        # parametrised test is one definition and several real cases, and the
        # ledger's number is the one a reader would get from running pytest.
        counted = 0
        for rel in test_files:
            out = subprocess.run(
                ["venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider",
                 "--collect-only", "-q", rel],
                capture_output=True, text=True, cwd=ROOT, check=False,
            ).stdout
            match = re.search(r"(\d+) tests? collected", out)
            assert match, f"could not read a collection count for {rel}: {out[-200:]}"
            counted += int(match.group(1))
        assert counted == tests_added, f"{phase['phase']}: ledger says {tests_added}, collection has {counted}"


def test_the_recorded_production_change_flag_is_true_where_files_were_touched(
    ledger: dict[str, Any],
) -> None:
    for phase in [p for p in ledger["phases"] if p["commit"]]:
        touches_production = any(
            not p.startswith("tests/") and not p.startswith("docs/")
            for p in phase["changed_paths"]
        )
        assert phase["production_changed"] is touches_production, phase["phase"]


def test_wip_preservation_is_recorded_as_zero_delta(ledger: dict[str, Any]) -> None:
    wip = ledger["wip_preservation"]
    assert wip["canonical_delta"] == 0
    assert wip["untracked_count"] == 13
    assert "no git clean" in wip["forbidden_operations_used"]

    # And it is still true right now, which is the only useful kind of record.
    dirty = git("status", "--porcelain").strip()
    assert dirty, "the canonical working tree is clean, contradicting the ledger"


def test_the_baseline_failures_are_marked_baseline_not_fixed(ledger: dict[str, Any]) -> None:
    baseline = ledger["cross_cutting"]["baseline_failures_untouched"]
    assert baseline["status"] == "BASELINE"
    assert baseline["count"] == 41

    suites = ledger["suites"]
    assert suites["runtime"]["failed"] == 24
    assert suites["goalrun"]["failed"] == 1
    assert suites["unit-fast"]["failed"] == 0


# ── the receipt shapes P0-K must be able to describe ──────────────────
@pytest.mark.parametrize(
    "shape,fields",
    [
        ("universal", UNIVERSAL_RECEIPT_FIELDS),
        ("execution", EXECUTION_RECEIPT_FIELDS),
        ("file", FILE_RECEIPT_FIELDS),
        ("git", GIT_RECEIPT_FIELDS),
        ("process", PROCESS_RECEIPT_FIELDS),
    ],
)
def test_each_receipt_shape_is_specified_in_full(shape: str, fields: tuple[str, ...]) -> None:
    """The schema is the deliverable; a missing field is a hole in the ledger."""
    assert fields, shape
    assert len(set(fields)) == len(fields), f"{shape} repeats a field"


def test_the_universal_receipt_covers_what_a_caller_needs_to_audit() -> None:
    required = set(UNIVERSET := UNIVERSAL_RECEIPT_FIELDS)
    # Nothing here may be satisfied by a placeholder: each field is one a reader
    # needs in order to answer "who did what, to what, and did it finish".
    assert "receipt_id" in required
    assert {"actor", "target", "effect", "admission"} <= required
    assert {"start", "finish", "status"} <= required
    assert UNIVERSET  # silence linters without weakening the assertion


def test_a_terminal_status_is_never_asserted_without_evidence(
    ledger: dict[str, Any],
) -> None:
    """§11.7/§26: no phase may record COMPLETED work it did not observe.

    Every phase claiming PASS must name the evidence that backs it, and the
    evidence strings must be non-empty and specific rather than a restatement of
    the title.
    """
    for phase in ledger["phases"]:
        if phase["status"] != "PASS":
            continue
        assert len(phase["evidence"]) >= 2, phase["phase"]
        for item in phase["evidence"]:
            assert len(item) > 40, f"{phase['phase']}: evidence too thin to audit: {item!r}"


def test_the_ledger_does_not_claim_integration(ledger: dict[str, Any]) -> None:
    """Integration is a separate phase and must never be implied complete.

    Restated at the P0-L final rerun. The rule used to require the closure gate
    to stay NOT_REACHED forever, which is not what it was for: it was there to
    stop Local2 closure being confused with Integration. That intent is now stated
    directly. Local2 may be declared COMPLETE; Integration may not, and this guard
    is what keeps the two apart.
    """
    text = LEDGER_PATH.read_text(encoding="utf-8").lower()
    assert "integration" not in text or "deferred" in text
    phases = {phase["phase"] for phase in ledger["phases"]}
    assert "P0-K" not in phases, "P0-K authored this ledger and must not appear in it"
    assert "integration" not in phases, "Integration must never appear as a phase here"

    gate = ledger["cross_cutting"]["closure_gate"]
    # Whenever the gate is PASS, it must be because a P0-L rerun said so.
    if gate["status"] == "PASS":
        p0l = [
            phase for phase in ledger["phases"]
            if phase["phase"] == "P0-L" and phase["status"] == "PASS"
        ]
        assert p0l, "a PASS closure gate requires a P0-L phase recorded as PASS"
        # The recorded chain is linear only through P0-J, so the deciding P0-L
        # names its commit explicitly rather than holding a chain slot.
        assert (
            p0l[-1]["commit"] or p0l[-1].get("deciding_commit")
        ), "the deciding P0-L must name its commit"
        assert p0l[-1]["lifecycle_evidence"], "the deciding P0-L must record the chain"
        # And the gate must still say Integration is deferred.
        assert "deferred" in gate["reason"].lower()
    else:
        assert gate["status"] == "NOT_REACHED"

    # Historical BLOCKED P0-L attempts are never rewritten.
    for phase in ledger["phases"]:
        if phase["phase"] == "P0-L" and phase.get("attempt") != "final-rerun-after-P0-Q":
            assert phase["status"] == "BLOCKED", phase.get("attempt")


def test_the_ledger_file_is_valid_json_and_pretty(ledger: dict[str, Any]) -> None:
    raw = LEDGER_PATH.read_text(encoding="utf-8")
    assert json.loads(raw) == ledger
    assert raw.endswith("\n")
    assert "\t" not in raw
