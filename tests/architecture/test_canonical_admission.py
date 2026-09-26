"""Admission contract tests for the canonical production entry classes."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _manifest() -> dict:
    return yaml.safe_load((ROOT / "architecture/admission.yaml").read_text(encoding="utf-8"))


def test_admission_manifest_has_three_explicit_classes():
    manifest = _manifest()
    assert set(manifest) >= {
        "semantic_ingress",
        "typed_command",
        "read_only_query",
        "invariants",
    }
    for category in ("semantic_ingress", "typed_command", "read_only_query"):
        entries = manifest[category]
        assert entries
        assert all(item.get("path") and item.get("authority") for item in entries)


def test_semantic_ingress_points_at_canonical_master():
    manifest = _manifest()
    for item in manifest["semantic_ingress"]:
        assert item["authority"] == "server.coordinator_master" or item["delegates_to"] == (
            "server.coordinator_master"
        )


def test_admission_invariants_are_fail_closed():
    invariants = _manifest()["invariants"]
    assert invariants["semantic_ingress_must_delegate_to_master"] is True
    assert invariants["typed_commands_must_not_infer_user_intent"] is True
    assert invariants["read_only_queries_must_not_start_work"] is True
    assert invariants["unknown_surface_is_rejected"] is True
