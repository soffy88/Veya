import pytest
from veya.gsk.careful import check_careful, check_freeze

def test_careful_guard_rules(monkeypatch):
    monkeypatch.setenv("VEYA_GSK_CAREFUL", "1")
    ok, _ = check_careful("git status")
    assert ok is True

    ok, msg = check_careful("rm -rf /")
    assert ok is False
    assert "rm -rf" in msg

    ok, msg = check_careful("DROP TABLE users;")
    assert ok is False

    ok, msg = check_careful("git push origin main --force")
    assert ok is False

def test_freeze_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("VEYA_GSK_CAREFUL", "1")
    monkeypatch.setenv("VEYA_GSK_FREEZE_DIR", "veya/gsk")

    ok, _ = check_freeze("veya/gsk/evidence.py")
    assert ok is True

    ok, msg = check_freeze("veya/server/app.py")
    assert ok is False
    assert "outside the frozen directory" in msg
