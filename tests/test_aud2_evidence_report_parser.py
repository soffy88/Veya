from pathlib import Path


def test_evidence_parser_exists():
    p = Path("apps/web/src/lib/evidenceReport.ts")
    assert p.exists(), "apps/web/src/lib/evidenceReport.ts must exist"
    content = p.read_text(encoding="utf-8")
    assert "parseEvidenceReport" in content, "parseEvidenceReport function must be exported"
    assert "sources" in content
    assert "guesses" in content
    assert "skips" in content
