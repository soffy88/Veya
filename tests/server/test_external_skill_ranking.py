"""P2-1: external skill ranking must prefer the skill the query names.

Acceptance found `search("ssrf")` returning ten equal-score hits ordered by
name, with the actually-matching `hunt-ssrf` at rank 10 and outside top-5.
The scorer must weight a name hit above a description-only hit.
"""

from __future__ import annotations

import pytest

from server import external_skill_index
from server.external_skill_index import ExternalSkill, ExternalSkillIndex


def _entry(name: str, description: str, dir_name: str | None = None) -> ExternalSkill:
    return ExternalSkill(
        name=name,
        description=description,
        dir_name=dir_name or name,
        path=f"/tmp/{name}/SKILL.md",
        source="bughunter",
    )


@pytest.fixture
def synthetic_index(monkeypatch: pytest.MonkeyPatch) -> ExternalSkillIndex:
    monkeypatch.setenv("VEYA_BUGHUNTER_INDEX", "1")
    index = ExternalSkillIndex()
    entries = [
        # Exactly the acceptance shape: a long description that mentions ssrf
        # repeatedly outranks nothing, and the same-named skill must win.
        _entry("hunt-ssrf", "Hunting skill for ssrf vulnerabilities."),
        _entry(
            "bb-local-toolkit",
            "Local toolkit. Mentions ssrf, xss, sqli, rce, lfi, open redirect, "
            "csrf, idor and many other bug classes in passing.",
        ),
        _entry(
            "cloud-iam-deep",
            "Cloud IAM review. Covers ssrf against metadata endpoints, plus "
            "assume-role chains, permission boundaries and policy simulation.",
        ),
        _entry(
            "enterprise-vpn-attack",
            "VPN attack surface. SSRF pivots, tunnel abuse, split tunneling, "
            "certificate pinning bypass and device posture checks.",
        ),
        _entry("hunt-cache-poison", "Cache poisoning. Sometimes reaches ssrf."),
    ]
    monkeypatch.setattr(index, "entries", lambda: list(entries))
    return index


def test_named_skill_ranks_first(synthetic_index: ExternalSkillIndex) -> None:
    hits = synthetic_index.search("ssrf", top_k=5)
    assert hits[0]["name"] == "hunt-ssrf"


def test_named_skill_is_in_top_five(synthetic_index: ExternalSkillIndex) -> None:
    hits = synthetic_index.search("ssrf", top_k=5)
    names = [hit["name"] for hit in hits]
    assert "hunt-ssrf" in names


def test_name_hit_outranks_description_only_hit(synthetic_index: ExternalSkillIndex) -> None:
    hits = {hit["name"]: hit["score"] for hit in synthetic_index.search("ssrf", top_k=10)}
    assert hits["hunt-ssrf"] > hits["bb-local-toolkit"]
    assert hits["hunt-ssrf"] > hits["cloud-iam-deep"]


def test_prefix_match_is_weighted(synthetic_index: ExternalSkillIndex) -> None:
    hits = synthetic_index.search("hunt-ssrf", top_k=3)
    assert hits[0]["name"] == "hunt-ssrf"


def test_empty_query_returns_nothing(synthetic_index: ExternalSkillIndex) -> None:
    assert synthetic_index.search("   ") == []


def test_long_description_does_not_outscore_exact_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token overlap is normalized: padding a description cannot buy rank."""
    monkeypatch.setenv("VEYA_BUGHUNTER_INDEX", "1")
    index = ExternalSkillIndex()
    padding = " ".join(f"word{i}" for i in range(200))
    entries = [
        _entry("hunt-ssrf", "Hunting skill for ssrf vulnerabilities."),
        _entry("padded", f"ssrf {padding}"),
    ]
    monkeypatch.setattr(index, "entries", lambda: list(entries))
    hits = index.search("ssrf", top_k=2)
    assert hits[0]["name"] == "hunt-ssrf"


def test_disabled_index_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in ("VEYA_BUGHUNTER_INDEX", "VEYA_WSHOBSON_INDEX"):
        monkeypatch.delenv(env, raising=False)
    assert external_skill_index.search_external_skills("ssrf") == []
