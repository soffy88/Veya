from __future__ import annotations

from server.feature_flags import enabled, snapshot


def test_stable_flags_have_owner_removal_date_and_default_on(monkeypatch):
    monkeypatch.delenv("VEYA_EVENT_STORE_V1", raising=False)
    rows = snapshot()
    assert rows
    assert all(row["owner"] and row["removal_date"] for row in rows)
    assert enabled("VEYA_EVENT_STORE_V1") is True


def test_feature_flag_can_be_disabled_for_rollout(monkeypatch):
    monkeypatch.setenv("VEYA_EVENT_STORE_V1", "0")
    assert enabled("VEYA_EVENT_STORE_V1") is False
