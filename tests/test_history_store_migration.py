"""Migration proof for the pre-revision SQLite history schema."""

from __future__ import annotations

import sqlite3

from veya.oservi.history_store import SqliteHistoryStore


def test_legacy_history_schema_migrates_without_losing_snapshots(tmp_path):
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE turns (sid TEXT NOT NULL, idx INTEGER NOT NULL, "
            "msg_json TEXT NOT NULL, ts INTEGER NOT NULL, user_id TEXT DEFAULT 'anonymous', "
            "PRIMARY KEY (sid, idx))"
        )
        conn.execute(
            "INSERT INTO turns VALUES (?,?,?,?,?)",
            ("sess_old", 0, '[{"role":"user","content":"old"}]', 1, "alice"),
        )
    store = SqliteHistoryStore(db)
    assert store.load_sync("sess_old", user_id="alice")[0]["content"] == "old"
    assert store.replay_sync("sess_old", user_id="alice")[0]["revision"] == 0
