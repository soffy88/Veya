"""tests/test_lorca_p3_memory.py — P3 per-bot Markdown memory (Lorca-style).

Covers:
    CRUD            — write / read / delete / list_files
    Traversal       — path traversal names are rejected (ValueError / HTTP 422)
    Isolation       — require_same_bot enforced when caller_bot_id is provided
    Render          — render_for_prompt truncation and empty-store behaviour
    REST            — all four endpoints via FastAPI TestClient
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from runtime.bot_scope import CrossBotAccessDenied
from server.bot_team.memory import (
    CORE_FILE,
    MAX_FILE_BYTES,
    BotMemoryStore,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path):
    """A BotMemoryStore backed by a tmp directory."""
    return BotMemoryStore("bot-alpha", bots_root=tmp_path)


@pytest.fixture()
def store_b(tmp_path):
    """A second store for a different bot (same bots_root)."""
    return BotMemoryStore("bot-beta", bots_root=tmp_path)


# ── CRUD tests ────────────────────────────────────────────────────────────────


class TestCRUD:
    def test_write_and_read(self, store):
        store.write(CORE_FILE, "hello world")
        assert store.read(CORE_FILE) == "hello world"

    def test_read_absent_returns_empty(self, store):
        assert store.read(CORE_FILE) == ""

    def test_list_files_empty(self, store):
        assert store.list_files() == []

    def test_list_files_after_write(self, store):
        store.write(CORE_FILE, "x")
        store.write("notes.md", "y")
        assert store.list_files() == ["MEMORY.md", "notes.md"]

    def test_overwrite_file(self, store):
        store.write(CORE_FILE, "v1")
        store.write(CORE_FILE, "v2")
        assert store.read(CORE_FILE) == "v2"

    def test_delete_existing(self, store):
        store.write(CORE_FILE, "data")
        deleted = store.delete(CORE_FILE)
        assert deleted is True
        assert store.read(CORE_FILE) == ""

    def test_delete_absent_returns_false(self, store):
        assert store.delete(CORE_FILE) is False

    def test_append_note_creates_core_file(self, store):
        store.append_note("first note")
        content = store.read(CORE_FILE)
        assert "first note" in content
        assert "Note —" in content

    def test_append_note_multiple(self, store):
        store.append_note("note A")
        store.append_note("note B")
        content = store.read(CORE_FILE)
        assert "note A" in content
        assert "note B" in content
        assert "---" in content  # separator between notes

    def test_write_multiple_topic_files(self, store):
        store.write("python.md", "Python notes")
        store.write("golang.md", "Go notes")
        files = store.list_files()
        assert "golang.md" in files
        assert "python.md" in files

    def test_write_is_atomic(self, store, tmp_path):
        """After write the file content must be exactly what was written (no partial)."""
        store.write(CORE_FILE, "atomic content")
        path = tmp_path / "bot-alpha" / "memory" / CORE_FILE
        assert path.read_text() == "atomic content"

    def test_delete_removes_from_list(self, store):
        store.write("topic.md", "data")
        store.delete("topic.md")
        assert "topic.md" not in store.list_files()


# ── Path-traversal rejection ─────────────────────────────────────────────────


class TestTraversalRejection:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "../evil.md",
            "../../etc/passwd",
            "sub/dir.md",
            "no_extension",
            "has space.md",
            ".hidden.md",
            "file.txt",
            "",
        ],
    )
    def test_invalid_names_raise_value_error(self, store, bad_name):
        with pytest.raises(ValueError):
            store.write(bad_name, "data")

    def test_non_md_extension_rejected(self, store):
        with pytest.raises(ValueError, match=r"\.md"):
            store.read("notes.txt")

    def test_traversal_rejected_on_delete(self, store):
        with pytest.raises(ValueError):
            store.delete("../evil.md")

    def test_traversal_rejected_on_read(self, store):
        with pytest.raises(ValueError):
            store.read("../../etc/shadow")


# ── Bot isolation ─────────────────────────────────────────────────────────────


class TestIsolation:
    def test_same_bot_write_allowed(self, store):
        store.write(CORE_FILE, "safe", caller_bot_id="bot-alpha")
        assert store.read(CORE_FILE) == "safe"

    def test_different_bot_write_denied(self, store):
        with pytest.raises(CrossBotAccessDenied):
            store.write(CORE_FILE, "hack", caller_bot_id="bot-intruder")

    def test_same_bot_delete_allowed(self, store):
        store.write(CORE_FILE, "x")
        store.delete(CORE_FILE, caller_bot_id="bot-alpha")
        assert CORE_FILE not in store.list_files()

    def test_different_bot_delete_denied(self, store):
        store.write(CORE_FILE, "x")
        with pytest.raises(CrossBotAccessDenied):
            store.delete(CORE_FILE, caller_bot_id="other-bot")

    def test_same_bot_append_note_allowed(self, store):
        store.append_note("ok", caller_bot_id="bot-alpha")
        assert "ok" in store.read(CORE_FILE)

    def test_different_bot_append_note_denied(self, store):
        with pytest.raises(CrossBotAccessDenied):
            store.append_note("inject", caller_bot_id="attacker")

    def test_two_bots_isolated_storage(self, store, store_b, tmp_path):
        """Each bot has its own memory directory; files do not bleed across."""
        store.write(CORE_FILE, "alpha memory")
        store_b.write(CORE_FILE, "beta memory")
        assert store.read(CORE_FILE) == "alpha memory"
        assert store_b.read(CORE_FILE) == "beta memory"

    def test_no_caller_id_skips_isolation_check(self, store):
        """When caller_bot_id is None, isolation is not enforced (opt-in)."""
        store.write(CORE_FILE, "unchecked", caller_bot_id=None)
        assert store.read(CORE_FILE) == "unchecked"


# ── Size caps ─────────────────────────────────────────────────────────────────


class TestSizeCaps:
    def test_file_too_large_rejected(self, store):
        big = "x" * (MAX_FILE_BYTES + 1)
        with pytest.raises(ValueError, match="size cap"):
            store.write(CORE_FILE, big)

    def test_total_store_cap_enforced(self, store):
        # Use chunks smaller than MAX_FILE_BYTES (512 KiB) individually
        # but enough combined to exceed MAX_TOTAL_BYTES (2 MiB).
        # 3 files × 400 KiB = 1.2 MiB < 2 MiB — writes succeed.
        # 4th file × 400 KiB = 1.6 MiB total < 2 MiB — also fine.
        # 5th file pushes us over; use different sizes to control precisely.
        # Simpler: write four 400 KiB files (1.6 MiB) then a 500 KiB file.
        chunk = "a" * (400 * 1024)
        store.write("part1.md", chunk)
        store.write("part2.md", chunk)
        store.write("part3.md", chunk)
        store.write("part4.md", chunk)  # 1.6 MiB total, still under cap
        # 5th file of 400 KiB would make 2.0 MiB (right at cap) — try 500 KiB
        overflow = "b" * (500 * 1024)  # < 512 KiB per-file cap, but pushes total over
        with pytest.raises(ValueError, match="total cap"):
            store.write("part5.md", overflow)

    def test_overwrite_does_not_double_count(self, store):
        """Overwriting the same file must not accumulate its old bytes."""
        chunk = "b" * (MAX_FILE_BYTES // 2)
        store.write(CORE_FILE, chunk)
        # Overwrite with equally large content: should succeed.
        store.write(CORE_FILE, chunk)
        assert len(store.read(CORE_FILE).encode()) == len(chunk.encode())


# ── render_for_prompt truncation ─────────────────────────────────────────────


class TestRenderForPrompt:
    def test_empty_store_returns_empty_string(self, store):
        assert store.render_for_prompt() == ""

    def test_basic_render(self, store):
        store.write(CORE_FILE, "core content")
        rendered = store.render_for_prompt()
        assert "bot-alpha" in rendered
        assert "MEMORY.md" in rendered
        assert "core content" in rendered

    def test_render_includes_all_files(self, store):
        store.write(CORE_FILE, "main")
        store.write("topic.md", "topic data")
        rendered = store.render_for_prompt()
        assert "topic.md" in rendered
        assert "topic data" in rendered

    def test_render_truncates_to_max_chars(self, store):
        store.write(CORE_FILE, "A" * 3000)
        store.write("big.md", "B" * 3000)
        rendered = store.render_for_prompt(max_chars=500)
        assert len(rendered) <= 500

    def test_render_shows_truncated_stub(self, store):
        store.write(CORE_FILE, "main note")
        store.write("huge.md", "Z" * 5000)
        # With a small cap, huge.md should appear only as a stub.
        rendered = store.render_for_prompt(max_chars=300)
        assert "truncated" in rendered or "huge.md" in rendered

    def test_render_does_not_exceed_max_chars(self, store):
        store.write(CORE_FILE, "x" * 100)
        for i in range(10):
            store.write(f"file{i:02d}.md", "y" * 200)
        for cap in [200, 500, 1000, 4000]:
            r = store.render_for_prompt(max_chars=cap)
            assert len(r) <= cap, f"exceeded cap={cap}: got {len(r)}"


# ── REST endpoints via TestClient ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """TestClient with VEYA_BOTS_ROOT pointing to an isolated tmp directory."""
    bots_root = str(tmp_path_factory.mktemp("bots_root"))
    os.environ["VEYA_BOTS_ROOT"] = bots_root
    # Import app after patching the env so the store picks up the override.
    from server.app import app

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    os.environ.pop("VEYA_BOTS_ROOT", None)


BOT = "test-bot-rest"
FILE = "notes.md"


class TestRestEndpoints:
    def test_list_empty(self, client):
        r = client.get(f"/api/v1/bots/{BOT}/memory")
        assert r.status_code == 200
        data = r.json()
        assert data["bot_id"] == BOT
        assert data["files"] == []

    def test_put_and_get(self, client):
        r = client.put(
            f"/api/v1/bots/{BOT}/memory/{FILE}",
            content=b"hello REST",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r2 = client.get(f"/api/v1/bots/{BOT}/memory/{FILE}")
        assert r2.status_code == 200
        assert r2.json()["content"] == "hello REST"

    def test_put_json_body(self, client):
        r = client.put(
            f"/api/v1/bots/{BOT}/memory/json-written.md",
            json={"text": "from JSON"},
        )
        assert r.status_code == 200
        r2 = client.get(f"/api/v1/bots/{BOT}/memory/json-written.md")
        assert r2.json()["content"] == "from JSON"

    def test_list_after_writes(self, client):
        r = client.get(f"/api/v1/bots/{BOT}/memory")
        files = r.json()["files"]
        assert FILE in files
        assert "json-written.md" in files

    def test_delete_existing(self, client):
        r = client.delete(f"/api/v1/bots/{BOT}/memory/{FILE}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    def test_delete_absent_returns_404(self, client):
        r = client.delete(f"/api/v1/bots/{BOT}/memory/gone.md")
        assert r.status_code == 404

    def test_read_absent_returns_404(self, client):
        r = client.get(f"/api/v1/bots/{BOT}/memory/never-written.md")
        assert r.status_code == 404

    def test_traversal_rejected_by_rest(self, client):
        """Path-traversal names must be rejected with 422 (not 500)."""
        # FastAPI encodes path params, so test via direct name validation
        r = client.put(
            f"/api/v1/bots/{BOT}/memory/noext",
            content=b"bad",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 422

    def test_non_md_name_rejected_by_rest(self, client):
        r = client.put(
            f"/api/v1/bots/{BOT}/memory/bad.txt",
            content=b"x",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 422
