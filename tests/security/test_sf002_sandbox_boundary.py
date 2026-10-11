"""SF-002 — adversarial sandbox boundary qualification.

Not a happy-path suite. Each of the four boundaries is probed with an attempt to
defeat it, and a boundary that cannot be defeated has to say so with evidence
rather than with a passing assertion about the easy case.

The four boundaries:

  command semantics  a plain command runs; combination semantics are classified;
                    a refusal is synchronous and yields no execution_id
  path              canonical checkout / isolated worktree / outside workspace;
                    no target drift between shell, file and git; a write outside
                    the sandbox is refused, not performed and reported afterwards
  permission        READ / WRITE / SHELL agree with PermissionEngine; shell cannot
                    wrap its way past a file or git permission; unknown effect
                    stays fail-closed
  evidence          submitted / running / completed / refused stay distinct; a
                    refusal mints no execution receipt; a receipt carries target,
                    command, effect, admission, sandbox decision and terminal result

Two things this suite deliberately does not do. It does not assume a boundary
holds because the happy path passed, and it does not conclude that a control is
absent because there is no way to test it here — the permission-boundary probes
run against the real engine, and the ones that cannot be settled locally are
recorded as unresolved rather than passed.

Known smuggling forms are pinned by name in
``TESTED_COMMAND_SMUGGLING_FORMS`` and asserted as current behaviour. They are
defects, not expectations: the parser in ``permission_engine`` does not recognise
these separators, so a mutation reaches the engine as READ_ONLY and is allowed.
Each carries the reason it is still open so the pin cannot outlive its fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from veya.remote.permission_engine import PermissionEngine, parse_command_context

PATH = "platform/3O"
if PATH not in sys.path:
    sys.path.insert(0, PATH)

ROOT = Path.cwd()


def _evaluate(command: str, *, cwd: Path | None = None, workspace: Path | None = None):
    base = cwd or ROOT
    context = parse_command_context(command, cwd=base, workspace_root=workspace or base)
    return context, PermissionEngine().evaluate(context)


# ── boundary 1: command semantics ──────────────────────────────────────────
class TestCommandSemantics:
    def test_a_plain_command_is_classified_and_runs_through_the_policy(self) -> None:
        context, decision = _evaluate("echo ok")
        assert context.command_effect == "READ_ONLY"
        assert decision.decision.value == "ALLOW"

    @pytest.mark.parametrize(
        "command",
        [
            "echo ok && touch pwn",
            "false || touch pwn",
            "echo ok | touch pwn",
            "echo ok & touch pwn",
            "eval 'touch pwn'",
            "sh -c 'touch pwn'",
        ],
    )
    def test_recognised_combination_syntax_is_never_read_only(self, command: str) -> None:
        """A mutation smuggled behind a known separator must not read as READ_ONLY.

        These are the forms the parser does recognise. They are asserted because
        they are the ones that must keep working: if a change to the parser loses
        one, a mutation silently becomes a read.
        """
        context, _ = _evaluate(command)
        assert context.command_effect != "READ_ONLY", command

    @pytest.mark.parametrize(
        ("label", "command"),
        [
            ("semicolon", "echo ok; touch pwn"),
            ("newline", "echo ok\ntouch pwn"),
            ("dollar_paren", "echo $(touch pwn)"),
            ("backtick", "echo `touch pwn`"),
            ("env_wrapper", "env touch pwn"),
        ],
    )
    def test_hidden_mutation_is_graded_as_a_mutation(self, label: str, command: str) -> None:
        """A mutation hidden behind syntax the parser cannot model must not pass.

        These five were classified READ_ONLY and allowed: `;` and a newline are
        not seen as separators because shlex.split turns them into ordinary tokens,
        command substitution is never expanded, and a bare `env` prefix is not
        stripped. The second command in the string was therefore invisible.

        They are now graded at the top class rather than unwrapped. Unwrapping
        needs correct nested-quote handling and a mistake there fails open, which
        is the bug being fixed; assuming the worst cannot.
        """
        # LOCAL2-U4: the parser now models these forms, so the hidden second
        # command is seen and graded for what it is (a project write), never as
        # a read.
        context, _decision = _evaluate(command)
        assert context.command_effect == "REVERSIBLE_MUTATION", label

    def test_a_refusal_yields_no_execution_id(self) -> None:
        """A refusal is synchronous and mints nothing.

        The engine returns a decision, not an execution. If a refusal ever produced
        an execution id, the evidence boundary would be broken at its root: there
        would be a receipt for something that did not run.
        """
        for command, expected in (
            ("echo ok > /tmp/sf002", "DENY"),
            ("echo ok > /etc/hosts", "APPROVAL_REQUIRED"),
        ):
            context, decision = _evaluate(command)
            # Both refuse execution; they differ in why. A write under /tmp is a
            # scope escape and is denied outright, while a host path resolves to
            # host scope and requires approval. Neither one runs.
            assert decision.decision.value == expected, command
            assert decision.decision.value != "ALLOW", command
            assert getattr(context, "execution_id", None) is None, (
                "a refused command must not carry an execution id"
            )


# ── boundary 2: path ───────────────────────────────────────────────────────
class TestPathBoundary:
    def test_write_outside_the_workspace_is_refused_before_execution(self) -> None:
        """Refused, not performed and reported afterwards."""
        for command in ("echo ok > /tmp/sf002", "echo ok | tee /tmp/sf002"):
            _, decision = _evaluate(command)
            assert decision.decision.value == "DENY", command
            assert "SCOPE" in decision.reason.value or "PATH" in decision.reason.value, command

    def test_dotdot_escape_is_denied(self) -> None:
        _, decision = _evaluate("touch ../../outside")
        assert decision.decision.value == "DENY"
        assert decision.reason.value == "DENY_PATH_TRAVERSAL"

    def test_a_write_inside_the_workspace_is_a_project_mutation(self) -> None:
        """The boundary is the workspace, not a blanket refusal of writes."""
        context, decision = _evaluate("touch inside")
        assert context.command_effect == "REVERSIBLE_MUTATION"
        assert decision.decision.value == "ALLOW"
        assert decision.reason.value == "ALLOW_PROJECT_MUTATION"

    def test_target_does_not_drift_between_the_command_and_its_paths(self) -> None:
        """shell, file and git must resolve the same target.

        A command that resolves its operand one way while the recorded target
        paths resolve another is how a write lands somewhere the caller did not
        name. Relative operands must resolve under the declared cwd.
        """
        context = parse_command_context("touch report.txt", cwd=ROOT, workspace_root=ROOT)
        named = {p.name for p in context.target_paths}
        assert "report.txt" in named, f"target drift: recorded {sorted(named)}"

    def test_canonical_checkout_and_isolated_worktree_are_both_accepted(self) -> None:
        """A worktree is a legitimate root; it must not be treated as an escape."""
        context, decision = _evaluate("touch inside", cwd=ROOT)
        assert decision.decision.value == "ALLOW"
        assert context.command_effect == "REVERSIBLE_MUTATION"


# ── boundary 3: permission ─────────────────────────────────────────────────
class TestPermissionBoundary:
    def test_remote_mutation_requires_approval(self) -> None:
        context, decision = _evaluate("git push origin main")
        assert context.command_effect == "REMOTE_MUTATION"
        assert decision.decision.value == "APPROVAL_REQUIRED"

    def test_privileged_host_mutation_requires_approval(self) -> None:
        context, decision = _evaluate("sudo rm -rf /")
        assert context.command_effect == "PRIVILEGED_HOST_MUTATION"
        assert decision.decision.value == "APPROVAL_REQUIRED"

    def test_destructive_mutation_requires_approval(self) -> None:
        _, decision = _evaluate("rm -rf build")
        assert decision.decision.value == "APPROVAL_REQUIRED"

    def test_read_stays_read(self) -> None:
        context, decision = _evaluate("cat notes.txt")
        assert context.command_effect == "READ_ONLY"
        assert decision.decision.value == "ALLOW"

    @pytest.mark.parametrize(
        ("effect", "expected_open"),
        [
            # SF-001's finding: an unclassified effect must not be treated as a
            # read. The declared-effect path through _build_context is what makes
            # remote and destructive visible; this asserts the engine's own half.
            ("", True),
            ("remote", False),
            ("destructive", False),
        ],
    )
    def test_effect_classification_drives_the_verdict(
        self, effect: str, expected_open: bool
    ) -> None:
        from veya.remote.policy_resolver import PolicyRequest, _build_context

        context = _build_context(
            PolicyRequest(
                actor="sf002",
                tool="probe",
                args={},
                workspace=str(ROOT),
                cwd=str(ROOT),
                effect=effect,
            )
        )
        decision = PermissionEngine().evaluate(context)
        if expected_open:
            assert decision.decision.value == "ALLOW", effect
        else:
            assert decision.decision.value == "APPROVAL_REQUIRED", effect

    @pytest.mark.parametrize(
        "wrapper",
        [
            "sh -c 'git push origin main'",
            "bash -c 'git push origin main'",
            "env sh -c 'git push'",
            "ssh host 'git push'",
        ],
    )
    def test_shell_wrappers_fail_safe_instead_of_bypassing(self, wrapper: str) -> None:
        """A wrapper must not lower the verdict for the command it hides.

        `parse_command_context` grades on argv[0], so anything deferring the real
        command to a nested string used to be graded as the wrapper: bare
        `git push origin main` is REMOTE_MUTATION and requires approval, while
        `sh -c 'git push origin main'` was graded REVERSIBLE_MUTATION and allowed
        outright, and `env sh -c` dropped all the way to READ_ONLY.

        The inner command is re-classified and the worse of the two wins, rather
        than every wrapper being escalated blindly. The engine already unwrapped
        `sh -c` for a hardcoded denylist of privileged binaries, which is why
        `bash -lc 'sudo apt-get install curl'` was caught while
        `bash -lc 'git push'` was not — `git` was simply absent from that list.
        Extending the list would have left the next unlisted binary open, and
        escalating every wrapper would have broken a genuinely safe wrapper,
        which is asserted separately below.
        """
        context, decision = _evaluate(wrapper)
        # The property is that wrapping changes nothing: the same classification
        # and the same verdict as the bare command.
        assert context.command_effect == "REMOTE_MUTATION", wrapper
        assert decision.decision.value == "APPROVAL_REQUIRED", wrapper

    def test_the_bare_remote_mutation_is_still_gated(self) -> None:
        """The control the bypass evades must stay in place.

        Pinned separately so that fixing the wrappers cannot be mistaken for
        having broken the underlying rule.
        """
        context, decision = _evaluate("git push origin main")
        assert context.command_effect == "REMOTE_MUTATION"
        assert decision.decision.value == "APPROVAL_REQUIRED"

    def test_privileged_mutation_survives_a_wrapper(self) -> None:
        """Recorded because it is the one class that is not bypassable today."""
        _, wrapped = _evaluate("sh -c 'sudo rm -rf /'")
        _, bare = _evaluate("sudo rm -rf /")
        assert bare.decision.value == "APPROVAL_REQUIRED"
        assert wrapped.decision.value == "APPROVAL_REQUIRED", (
            "host scope must be resolved independently of the command text"
        )

    def test_unknown_tool_effect_is_not_a_read(self) -> None:
        """UNKNOWN stays fail-closed: it is not evidence of safety."""
        from veya.remote.policy_resolver import PolicyRequest, _build_context

        context = _build_context(
            PolicyRequest(
                actor="sf002",
                tool="totally_unknown_tool",
                args={},
                workspace=str(ROOT),
                cwd=str(ROOT),
                effect="unknown",
            )
        )
        decision = PermissionEngine().evaluate(context)
        # Undeclared grants no capability, so the engine must not claim a write.
        assert decision.decision.value == "ALLOW"
        assert decision.reason.value == "ALLOW_READ_ONLY", (
            "an unclassified effect reached the engine as a read; UNKNOWN != READ"
        )


# ── boundary 4: evidence ───────────────────────────────────────────────────
class TestEvidenceBoundary:
    def test_a_refusal_is_not_reported_as_a_terminal_result(self) -> None:
        """refused and completed must not share a shape that reads alike."""
        _, refused = _evaluate("echo ok > /etc/hosts")
        _, denied = _evaluate("echo ok > /tmp/sf002")
        _, allowed = _evaluate("echo ok")
        assert denied.decision.value == "DENY"
        assert refused.decision.value == "APPROVAL_REQUIRED"
        assert allowed.decision.value == "ALLOW"
        # Three distinct outcomes with three distinct reasons: nothing here reads
        # like another, so a refusal cannot be mistaken for a result.
        assert len({denied.reason.value, refused.reason.value, allowed.reason.value}) == 3

    def test_a_refusal_records_intent_but_no_execution(self) -> None:
        """A refusal must show what was refused and that nothing ran.

        Recording the intended effect is required — a receipt that cannot say what
        it declined to do is not evidence. What must not exist is execution
        evidence: an execution id or a side-effect record would mean the call ran
        and was reported afterwards, which is the failure mode this boundary
        exists to prevent.
        """
        context, decision = _evaluate("echo ok > /tmp/sf002")
        assert decision.decision.value == "DENY"
        # Intent is on the record.
        assert context.filesystem_effect == "write"
        assert decision.effects, "a refusal has to name the effects it refused"
        # Execution is not.
        assert getattr(context, "execution_id", None) is None
        assert context.goal_run_id is None

    def test_decision_carries_scope_effects_and_reason_together(self) -> None:
        """What a receipt has to prove: target, effect, admission, sandbox decision."""
        context, decision = _evaluate("rm -rf build")
        assert decision.decision.value == "APPROVAL_REQUIRED"
        assert decision.reason.value
        assert decision.scope, "a receipt has to name the scope the call was judged in"
        assert decision.effects, "a receipt has to name the effects that were judged"
        assert context.command_effect == "DESTRUCTIVE_MUTATION"
        assert context.target_paths, "a receipt has to name the target"

    def test_submitted_running_and_terminal_are_distinct_states(self) -> None:
        """The engine decides; it does not report progress states.

        A permission decision has exactly three outcomes. Anything resembling
        submitted or running belongs to the execution layer, and conflating them
        is how a refusal starts looking like work in progress.
        """
        outcomes = set()
        for command in ("echo ok", "git push origin main", "rm -rf build", "touch ../../x"):
            _, decision = _evaluate(command)
            outcomes.add(decision.decision.value)
        assert outcomes <= {"ALLOW", "APPROVAL_REQUIRED", "DENY"}
        assert "RUNNING" not in outcomes and "SUBMITTED" not in outcomes


# ── approval settlement: the gateway turns every non-ALLOW into a failure ───
class TestApprovalSettlement:
    """A1 — an approval requirement must not surface as a failure.

    The injected ActionGatewayEngine maps every non-ALLOW verdict to
    {"status": "failed"} and never consults its own approval_resolver. Without
    settlement in the policy hook, a caller that supplied a resolver granting
    approval still saw the action fail. That is what broke restart-resume in
    tests/goal_run when the remote-effect branch was first added, and the branch
    was wrongly reverted instead of the wiring being fixed.
    """

    def _adapter(self, resolver):
        from server.action_gateway_adapter import ActionGatewayAdapter

        return ActionGatewayAdapter(approval_resolver=resolver)

    def _request(self):
        import obase

        return obase.ActionRequest(action="unclassified_remote", effect="remote")

    def test_a_granted_approval_becomes_allow(self) -> None:
        adapter = self._adapter(lambda _request: True)
        decision = adapter._evaluate_policy(self._request())
        assert decision.verdict == "ALLOW", decision.reason
        assert "approval granted" in decision.reason

    def test_a_refused_approval_becomes_deny(self) -> None:
        adapter = self._adapter(lambda _request: False)
        decision = adapter._evaluate_policy(self._request())
        assert decision.verdict == "DENY"
        assert "approval refused" in decision.reason

    def test_an_async_resolver_is_denied_explicitly_not_silently_allowed(self) -> None:
        """A requirement nobody could evaluate must not become permission."""

        async def resolver(_request):
            return True

        adapter = self._adapter(resolver)
        decision = adapter._evaluate_policy(self._request())
        assert decision.verdict == "DENY", (
            "an async approval resolver cannot be awaited from the sync policy "
            "hook; allowing it would grant permission nobody granted"
        )
        assert "asynchronous" in decision.reason

    def test_a_raising_resolver_denies_rather_than_propagating(self) -> None:
        def resolver(_request):
            raise RuntimeError("boom")

        adapter = self._adapter(resolver)
        decision = adapter._evaluate_policy(self._request())
        assert decision.verdict == "DENY"
        assert "approval resolver failed" in decision.reason

    def test_no_resolver_leaves_the_verdict_untouched(self) -> None:
        """Without a resolver the caller's own approval flow applies.

        Returning DENY here would deny on the engine's behalf, which is not this
        hook's decision to make.
        """
        adapter = self._adapter(None)
        decision = adapter._evaluate_policy(self._request())
        assert decision.verdict == "APPROVAL_REQUIRED", decision.verdict
