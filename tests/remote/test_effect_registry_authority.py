"""SF-001: the effect authority exists, is deterministic, and refuses to guess.

Phase 1 is read-only, so these tests are about the registry's own properties and
about what it refuses to claim. The decision-path tests belong to Phase 2, once
tools actually declare an effect.

The property that matters most here is the refusal. ``oskill`` answers
``"remote"`` for anything it does not recognise; ``remote`` is a scope word, not
an effect word, and treating it as an effect is how a tool nobody classified ends
up allowed. So an unrecognised tool resolves to UNKNOWN, never to a plausible
guess.
"""

from __future__ import annotations

import pytest

from veya.remote.effect_registry import (
    EFFECT_LATTICE,
    Effect,
    EffectProvenance,
    EffectRecord,
    EffectRegistry,
    get_effect_registry,
    reset_effect_registry,
    resolve_tool_effect,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_effect_registry()
    yield
    reset_effect_registry()


# ── determinism ────────────────────────────────────────────────────────────
def test_resolution_is_deterministic():
    first = resolve_tool_effect("write_file")
    second = resolve_tool_effect("write_file")
    assert first == second
    assert resolve_tool_effect("write_file").to_dict() == second.to_dict()


def test_resolution_does_not_require_the_tool_to_exist():
    """A tool that is not registered still resolves, without raising."""
    record = resolve_tool_effect("definitely_not_a_tool")
    assert record.effect is Effect.UNKNOWN
    assert record.provenance is EffectProvenance.UNKNOWN


# ── unknown fails closed ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "tool_id",
    [
        "github_pr_create_draft",
        "github_pr_post_review",
        "skill_delete",
        "memory_forget",
        "team_shutdown_request",
        "veya_review_apply",
        "",
        "   ",
    ],
)
def test_unknown_effect_fails_closed(tool_id: str):
    """No entry may resolve to an invented effect.

    This is the SF-001 invariant at the registry level: an unclassified tool is
    UNKNOWN, and UNKNOWN is never a permission.
    """
    record = resolve_tool_effect(tool_id)
    assert record.effect is Effect.UNKNOWN
    assert record.provenance is EffectProvenance.UNKNOWN
    assert record.confidence == 0.0
    assert record.is_authoritative is False


def test_the_scope_word_remote_is_not_treated_as_an_effect():
    """`oskill` says "remote" for unknown actions; that is not an effect."""

    # The legacy table maps "remote" to UNKNOWN rather than to a member.
    from veya.remote.effect_registry import _LEGACY_EFFECT_MAP

    assert _LEGACY_EFFECT_MAP["remote"] is Effect.UNKNOWN
    assert "remote" not in {str(e) for e in Effect}


# ── declaration is the only authority ──────────────────────────────────────
def test_effect_registry_is_authoritative():
    registry = EffectRegistry()
    registry.declare("my_tool", Effect.WRITE)
    record = registry.resolve("my_tool")

    assert record.effect is Effect.WRITE
    assert record.provenance is EffectProvenance.DECLARED
    assert record.confidence == 1.0
    assert record.is_authoritative is True
    assert record.deprecated is False


def test_a_declaration_overrides_the_legacy_table():
    registry = EffectRegistry()
    registry.declare("write_file", Effect.DESTRUCTIVE)
    record = registry.resolve("write_file")
    assert record.provenance is EffectProvenance.DECLARED
    assert record.effect is Effect.DESTRUCTIVE


def test_a_declaration_without_an_effect_is_rejected():
    registry = EffectRegistry()
    with pytest.raises(ValueError):
        registry.declare("broken", {"confidence": 0.9})


def test_legacy_lookup_is_labelled_and_never_authoritative():
    record = resolve_tool_effect("write_file")
    assert record.provenance is EffectProvenance.LEGACY_MAPPED
    assert record.is_authoritative is False
    assert record.deprecated is True
    assert record.confidence < 1.0


def test_permission_engine_does_not_infer_effect_from_tool_name():
    """Two names differing only by prefix must not collapse to one effect."""
    shell_like = resolve_tool_effect("execute_report")
    read_like = resolve_tool_effect("read_report")
    assert shell_like.effect is Effect.PROCESS
    assert read_like.effect is Effect.READ
    assert shell_like.effect is not read_like.effect


# ── lattice ────────────────────────────────────────────────────────────────
def test_lattice_order_is_the_security_order():
    assert EFFECT_LATTICE == (Effect.READ, Effect.WRITE, Effect.SHELL, Effect.SYSTEM)


def test_compound_effects_keep_every_member():
    registry = EffectRegistry()
    registry.declare("compound", {"effect": Effect.WRITE, "effects": [Effect.SHELL]})
    record = registry.resolve("compound")
    assert record.effects == frozenset({Effect.WRITE, Effect.SHELL})
    # And the summary is the more privileged member, never the lower one.
    assert record.effect is Effect.SHELL


@pytest.mark.parametrize(
    "members",
    [
        (Effect.WRITE, Effect.SHELL),
        (Effect.WRITE, Effect.NETWORK),
        (Effect.SHELL, Effect.NETWORK),
        (Effect.DESTRUCTIVE, Effect.SHELL),
    ],
)
def test_no_implicit_downgrade_of_compound_effects(members):
    registry = EffectRegistry()
    registry.declare("compound", {"effect": members[0], "effects": list(members[1:])})
    record = registry.resolve("compound")
    assert set(record.effects) == set(members)


def test_parallel_effects_are_reported_not_folded():
    registry = EffectRegistry()
    registry.declare("remote_only", Effect.NETWORK)
    record = registry.resolve("remote_only")
    assert record.effects == frozenset({Effect.NETWORK})
    assert record.effect is Effect.NETWORK


def test_unplaceable_effects_are_reported_not_hidden():
    """NETWORK/DESTRUCTIVE are parallel-axis: no rank, and the record says so.

    Reported rather than silently ranked, so a later consumer cannot treat
    "not in the lattice" as "not present".
    """
    record = EffectRecord(
        tool_id="odd",
        effect=Effect.NETWORK,
        effects=frozenset({Effect.NETWORK, Effect.DESTRUCTIVE}),
        provenance=EffectProvenance.DECLARED,
        confidence=1.0,
    )
    assert record.missing_from_lattice() == frozenset({Effect.NETWORK, Effect.DESTRUCTIVE})
    sequential = EffectRecord(
        tool_id="seq",
        effect=Effect.SHELL,
        effects=frozenset({Effect.SHELL}),
        provenance=EffectProvenance.DECLARED,
        confidence=1.0,
    )
    assert sequential.missing_from_lattice() == frozenset()


def test_revoking_a_declaration_returns_to_legacy():
    registry = EffectRegistry()
    registry.declare("write_file", Effect.WRITE)
    assert registry.revoke("write_file") is True
    assert registry.resolve("write_file").provenance is EffectProvenance.LEGACY_MAPPED
    assert registry.revoke("write_file") is False


def test_report_covers_declared_tools_and_resolves_cleanly():
    registry = EffectRegistry()
    registry.declare("a", Effect.READ)
    registry.declare("b", Effect.SHELL)
    by_id = {r.tool_id: r for r in registry.report()}
    assert by_id["a"].effect is Effect.READ
    assert by_id["b"].effect is Effect.SHELL


def test_default_registry_is_a_singleton():
    assert get_effect_registry() is get_effect_registry()
