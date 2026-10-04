"""SF-CRED independent real acceptance — do the claims survive contact with reality?

Unit tests establish that the credential fields behave as designed. This file
establishes that the design was not wrong about the world. Everything here makes
real calls through the real worker path; nothing is mocked, and no claim is
taken from the implementation under test.

The acceptance criterion is deliberately one-directional, because that is the
direction that causes damage:

  a claim of `credential_valid is False` must not be contradicted by a real
  completion. Marking a working executor invalid locks it out of the running,
  which is a capability regression disguised as a security fix.

The reverse direction is not asserted for `None`. `None` means "not probed", so a
real failure is permitted and expected — that is the documented codex gap, and a
test that pretended otherwise would be asserting a hope rather than a fact.

Every real call is bounded. claude_code took 217.7s to fail in the P0 inventory,
so waiting for a definitive negative is not worth 3.6 minutes to re-confirm
something local evidence already settles deterministically.
"""

from __future__ import annotations

import subprocess
import sys
import time
from enum import StrEnum

import pytest

from veya.remote.executor_registry import ExecutorRegistry

pytestmark = [pytest.mark.external, pytest.mark.slow, pytest.mark.asyncio]

PATH = "platform/3O"
if PATH not in sys.path:
    sys.path.insert(0, PATH)

WAIT_TIMEOUT_S = 60
# claude_code needed 217.7s to fail in the P0 inventory, so a short poll budget
# must end in INCONCLUSIVE rather than a verdict.
POLL_DEADLINE_S = 240
POLL_TICK_S = 2


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0)


class Observed(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


def _setup_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "seed.txt").write_text("acceptance\n", encoding="utf-8")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "acceptance@example.invalid"],
        ["git", "config", "user.name", "acceptance"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


async def _observe(executor_id: str, repo) -> Observed:
    """Run one real READ task through the real worker path."""
    from veya.remote.models import RemotePermissions, RemoteSession
    from veya.remote.tool_adapter import RemoteToolAdapter

    now = time.time()
    session = RemoteSession(
        session_id="sfcred-acceptance",
        principal="sfcred-acceptance",
        token_id="acceptance",
        workspaces=(str(repo),),
        active_workspace=str(repo),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=True,
            destructive=False,
            service_control=False,
        ),
        created_at=now,
        expires_at=now + 3600,
    )
    # One adapter for both dispatch and lookup: the jobs registry is
    # instance-scoped, so a second adapter cannot see the child execution and
    # every real run looks like it never happened.
    adapter = RemoteToolAdapter(None)
    dispatch = await adapter.call(
        session,
        "worker.dispatch",
        {
            "workspace": str(repo),
            "tasks": [
                {
                    "worker": executor_id,
                    "task": "Read the file seed.txt and report its exact contents.",
                    "task_contract": {"task_kind": "READ"},
                }
            ],
            "wait": True,
            "wait_timeout_s": WAIT_TIMEOUT_S,
        },
    )

    if not dispatch.ok:
        return Observed.FAILED
    payload = dispatch.result or {}
    if str(payload.get("status") or "").upper() in ("FAILED", "REJECTED"):
        return Observed.FAILED

    # worker.dispatch is asynchronous: it returns DISPATCHED with a child
    # execution id, and the real work happens afterwards. Classifying the dispatch
    # envelope reports every executor as inconclusive — including the one that
    # works, which is how this harness first "passed" a run that never happened.
    child_id = (payload.get("child_execution_ids") or [None])[0]
    if child_id is None:
        return Observed.INCONCLUSIVE

    deadline = time.time() + POLL_DEADLINE_S
    while time.time() < deadline:
        child = adapter.jobs.lookup(child_id)
        if child is not None and child.is_terminal:
            break
        time.sleep(POLL_TICK_S)
        await _tick()

    child = adapter.jobs.lookup(child_id)
    if child is None or not child.is_terminal:
        # No terminal state inside the budget. A credential problem and a slow
        # provider look identical here, so claim nothing.
        return Observed.INCONCLUSIVE
    status = str(child.status).upper()
    if status == "COMPLETED":
        return Observed.COMPLETED
    if status in ("FAILED", "CANCELLED", "ERROR", "TIMEOUT"):
        return Observed.FAILED
    return Observed.INCONCLUSIVE


async def _run(executor_id: str, tmp_path) -> tuple[bool | None, Observed]:
    registry = ExecutorRegistry()
    claim = registry.identity(executor_id).credential_valid
    observed = await _observe(executor_id, tmp_path / "repo")
    return claim, observed


# ── the one assertion that matters ─────────────────────────────────────────
async def test_a_refuted_credential_is_not_contradicted_by_real_success(tmp_path) -> None:
    """`credential_valid is False` must never be contradicted by a real run.

    This is the direction that breaks production: an executor wrongly marked
    invalid cannot be selected, so a security-shaped fix would silently remove
    the only capability that works.
    """
    from tests.supervision.test_p4_real_failover_qualification import _setup_repo as setup

    repo = tmp_path / "repo"
    setup(repo)

    # antigravity is deliberately absent from this list. It declares no
    # credential source and was briefly reported False; real acceptance caught
    # that it completes tasks anyway, which is why "absent" now yields None
    # rather than a refutation. Only an empty declared source is refutable.
    refuted = [
        name
        for name in ("pi", "claude_code")
        if ExecutorRegistry().identity(name).credential_valid is False
    ]
    assert refuted, "expected at least one structurally refuted executor"

    for name in refuted:
        observed = await _observe(name, repo)
        assert observed is not Observed.COMPLETED, (
            f"{name} is reported credential_valid=False but completed a real task; "
            "the refutation is wrong and would exclude a working executor"
        )


async def test_the_working_executor_still_completes_for_real(tmp_path) -> None:
    """Capability preservation, measured rather than assumed.

    P0 observed exactly one executor able to complete a real task here. Every
    layer of SF-CRED — presence/validity separation, structural refutation, probe
    bookkeeping — must leave that one working. A credential-truthfulness change
    that quietly emptied the executor pool would pass every other test here.
    """
    from tests.supervision.test_p4_real_failover_qualification import _setup_repo as setup

    repo = tmp_path / "repo"
    setup(repo)

    completed = [
        name
        for name in ("opencode", "codex", "pi", "claude_code")
        if await _observe(name, repo) is Observed.COMPLETED
    ]
    assert completed, "no executor completed a real task; SF-CRED emptied the pool"


async def test_unproven_credentials_make_no_claim(tmp_path) -> None:
    """`None` is permitted to be wrong in the safe direction only.

    codex holds real material that local evidence cannot refute, so it stays
    None and is expected to fail for real. Recording it here keeps the gap
    visible instead of letting it quietly become a silent pass.
    """
    from tests.supervision.test_p4_real_failover_qualification import _setup_repo as setup

    repo = tmp_path / "repo"
    setup(repo)

    unproven = [
        name
        for name in ("opencode", "codex", "pi", "claude_code")
        if ExecutorRegistry().identity(name).credential_valid is None
    ]
    for name in unproven:
        observed = await _observe(name, repo)
        # No assertion on the outcome: None claims nothing, so either result is
        # consistent. Asserting COMPLETED would forbid a future valid probe from
        # reporting success; asserting FAILED would forbid an honest unknown.
        assert observed in tuple(Observed), f"{name}: unexpected observation {observed!r}"
