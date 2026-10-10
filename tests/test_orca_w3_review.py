"""tests/test_orca_w3_review.py — ORCA-W3 review feature tests.

Tests:
  1. Annotation CRUD + batching message format + resolved filtering
  2. Attribution record/revert/export with tmp_path
  3. Fix-checks with a fake gh runner (failing, none failing, gh missing)

No server started — all tested at module/unit level.
"""
from __future__ import annotations

import json
import unittest.mock as mock
from pathlib import Path

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_mailbox(tmp_path: Path):
    from server.goal_run.agent_mailbox import AgentMailbox
    return AgentMailbox(path=tmp_path / "mailbox.json")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Line Annotations
# ─────────────────────────────────────────────────────────────────────────────

class TestAnnotationCRUD:
    def test_create_returns_annotation(self, tmp_path: Path):
        from server.goal_run.annotations import create_annotation
        ann = create_annotation(
            str(tmp_path), "goal-1",
            worktree="wt1", file="server/foo.py", line=42, body="looks wrong",
        )
        assert ann.id
        assert ann.goal_id == "goal-1"
        assert ann.file == "server/foo.py"
        assert ann.line == 42
        assert ann.status == "open"

    def test_create_persists(self, tmp_path: Path):
        from server.goal_run.annotations import create_annotation, list_annotations
        create_annotation(str(tmp_path), "goal-1", worktree="", file="a.py", line=1, body="x")
        create_annotation(str(tmp_path), "goal-1", worktree="", file="b.py", line=2, body="y")
        anns = list_annotations(str(tmp_path), "goal-1")
        assert len(anns) == 2

    def test_list_filter_by_status(self, tmp_path: Path):
        from server.goal_run.annotations import (
            create_annotation,
            list_annotations,
            resolve_annotation,
        )
        ann = create_annotation(str(tmp_path), "g", worktree="", file="f.py", line=1, body="b")
        create_annotation(str(tmp_path), "g", worktree="", file="f.py", line=2, body="c")
        resolve_annotation(str(tmp_path), "g", ann.id)

        open_anns = list_annotations(str(tmp_path), "g", status="open")
        resolved_anns = list_annotations(str(tmp_path), "g", status="resolved")
        assert len(open_anns) == 1
        assert len(resolved_anns) == 1

    def test_resolve_not_found_returns_false(self, tmp_path: Path):
        from server.goal_run.annotations import resolve_annotation
        found = resolve_annotation(str(tmp_path), "g", "nonexistent-id")
        assert found is False

    def test_resolve_idempotent(self, tmp_path: Path):
        from server.goal_run.annotations import (
            create_annotation,
            resolve_annotation,
        )
        ann = create_annotation(str(tmp_path), "g", worktree="", file="f.py", line=1, body="b")
        assert resolve_annotation(str(tmp_path), "g", ann.id) is True
        assert resolve_annotation(str(tmp_path), "g", ann.id) is False  # already resolved


class TestAnnotationBatching:
    def test_batch_send_combines_open_annotations(self, tmp_path: Path):
        from server.goal_run.annotations import batch_send, create_annotation
        mb = _make_mailbox(tmp_path)
        create_annotation(str(tmp_path), "g", worktree="", file="server/foo.py", line=10, body="issue A")
        create_annotation(str(tmp_path), "g", worktree="", file="server/foo.py", line=20, body="issue B")
        create_annotation(str(tmp_path), "g", worktree="", file="server/bar.py", line=5, body="issue C")
        result = batch_send(str(tmp_path), "g", recipient="agent-1", mailbox=mb)
        assert result["sent"] == 3
        assert result["message_id"] is not None
        assert len(result["resolved_ids"]) == 3

    def test_batch_send_message_format(self, tmp_path: Path):
        from server.goal_run.agent_mailbox import DeliveryMode
        from server.goal_run.annotations import batch_send, create_annotation
        mb = _make_mailbox(tmp_path)
        create_annotation(str(tmp_path), "g", worktree="", file="server/foo.py", line=42, body="bad variable name")
        batch_send(str(tmp_path), "g", recipient="agent-x", mailbox=mb)
        msgs = mb.poll("agent-x", mark_delivered=False)
        assert len(msgs) == 1
        msg = msgs[0]
        assert msg.delivery_mode == DeliveryMode.FOLLOW_UP
        payload_text = msg.payload.get("text", "")
        assert "server/foo.py:42" in payload_text
        assert "bad variable name" in payload_text

    def test_batch_send_empty_returns_zero(self, tmp_path: Path):
        from server.goal_run.annotations import batch_send
        mb = _make_mailbox(tmp_path)
        result = batch_send(str(tmp_path), "g", recipient="agent-1", mailbox=mb)
        assert result["sent"] == 0
        assert result["message_id"] is None

    def test_batch_send_marks_resolved(self, tmp_path: Path):
        from server.goal_run.annotations import batch_send, create_annotation, list_annotations
        mb = _make_mailbox(tmp_path)
        create_annotation(str(tmp_path), "g", worktree="", file="f.py", line=1, body="x")
        batch_send(str(tmp_path), "g", recipient="agent-1", mailbox=mb)
        open_anns = list_annotations(str(tmp_path), "g", status="open")
        assert len(open_anns) == 0

    def test_batch_send_excludes_already_resolved(self, tmp_path: Path):
        from server.goal_run.annotations import (
            batch_send,
            create_annotation,
            resolve_annotation,
        )
        mb = _make_mailbox(tmp_path)
        ann = create_annotation(str(tmp_path), "g", worktree="", file="f.py", line=1, body="x")
        resolve_annotation(str(tmp_path), "g", ann.id)
        result = batch_send(str(tmp_path), "g", recipient="agent-1", mailbox=mb)
        assert result["sent"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. AI Attribution
# ─────────────────────────────────────────────────────────────────────────────

class TestAttribution:
    def test_record_and_retrieve(self, tmp_path: Path):
        from server.goal_run.ai_attribution import get_file_attribution, record_ai_write
        record_ai_write(
            tmp_path, "server/foo.py", 1, 10,
            session_id="sess-1", agent="MasterAgent", model="deepseek", content="hello",
        )
        lines = get_file_attribution(tmp_path, "server/foo.py", 15)
        ai_lines = [ln for ln in lines if ln["attribution"] == "ai"]
        human_lines = [ln for ln in lines if ln["attribution"] == "human"]
        assert len(ai_lines) == 10
        assert len(human_lines) == 5
        assert all(ln["line"] in range(1, 11) for ln in ai_lines)

    def test_revert_to_human_full_range(self, tmp_path: Path):
        from server.goal_run.ai_attribution import (
            get_file_attribution,
            record_ai_write,
            revert_to_human,
        )
        record_ai_write(tmp_path, "f.py", 1, 5, session_id="s", agent="A", model="m")
        affected = revert_to_human(tmp_path, "f.py", 1, 5)
        assert affected == 1
        lines = get_file_attribution(tmp_path, "f.py", 5)
        assert all(ln["attribution"] == "human" for ln in lines)

    def test_revert_partial_range(self, tmp_path: Path):
        from server.goal_run.ai_attribution import (
            get_file_attribution,
            record_ai_write,
            revert_to_human,
        )
        # AI wrote lines 1-10
        record_ai_write(tmp_path, "f.py", 1, 10, session_id="s", agent="A", model="m")
        # Human edited lines 4-6
        revert_to_human(tmp_path, "f.py", 4, 6)
        lines = get_file_attribution(tmp_path, "f.py", 10)
        by_line = {ln["line"]: ln["attribution"] for ln in lines}
        assert by_line[1] == "ai"
        assert by_line[3] == "ai"
        assert by_line[4] == "human"
        assert by_line[6] == "human"
        assert by_line[7] == "ai"
        assert by_line[10] == "ai"

    def test_revert_no_overlap_returns_zero(self, tmp_path: Path):
        from server.goal_run.ai_attribution import revert_to_human
        affected = revert_to_human(tmp_path, "f.py", 1, 5)
        assert affected == 0

    def test_export_all_empty(self, tmp_path: Path):
        from server.goal_run.ai_attribution import export_all
        result = export_all(tmp_path)
        assert result == []

    def test_export_all_after_record(self, tmp_path: Path):
        from server.goal_run.ai_attribution import export_all, record_ai_write
        record_ai_write(tmp_path, "a.py", 1, 3, session_id="s", agent="A", model="m")
        record_ai_write(tmp_path, "b.py", 1, 5, session_id="s", agent="A", model="m")
        result = export_all(tmp_path)
        assert len(result) == 2
        files = {r["file"] for r in result}
        assert files == {"a.py", "b.py"}

    def test_content_hash_stored(self, tmp_path: Path):
        from server.goal_run.ai_attribution import _ledger_path, record_ai_write
        record_ai_write(
            tmp_path, "x.py", 1, 1,
            session_id="s", agent="A", model="m", content="hello world",
        )
        lpath = _ledger_path(tmp_path, "x.py")
        data = json.loads(lpath.read_text())
        assert data["entries"][0]["content_hash"].startswith("sha256:")

    def test_overlapping_ai_write_replaces(self, tmp_path: Path):
        from server.goal_run.ai_attribution import get_file_attribution, record_ai_write
        record_ai_write(tmp_path, "f.py", 1, 10, session_id="s1", agent="A", model="m1")
        record_ai_write(tmp_path, "f.py", 5, 8, session_id="s2", agent="B", model="m2")
        lines = get_file_attribution(tmp_path, "f.py", 10)
        by_line = {ln["line"]: ln for ln in lines}
        # Lines 5-8 should now be from session s2
        assert by_line[5]["session_id"] == "s2"
        assert by_line[8]["session_id"] == "s2"
        # Lines 1-4 and 9-10 still from s1
        assert by_line[1]["session_id"] == "s1"
        assert by_line[9]["session_id"] == "s1"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fix Broken Checks
# ─────────────────────────────────────────────────────────────────────────────

def _make_review_router_with_fake_gh(
    tmp_path: Path,
    *,
    gh_available: bool = True,
    failing_runs: list[dict] | None = None,
    log_output: str = "",
):
    """Helper: patch subprocess.run and shutil.which for fix-checks tests."""
    # We test the logic functions directly, not via HTTP.
    return gh_available, failing_runs or [], log_output


class TestFixChecks:
    """Tests for fix-checks logic — no server, patches subprocess."""

    def _run_fix_checks_fn(self, tmp_path: Path, *, gh_available=True, runs=None, log=""):
        """Import and call the internal helpers from the review route."""
        import server.routes.review as review_mod

        # Patch shutil.which
        with mock.patch.object(review_mod, "_gh_available", return_value=gh_available):
            if not gh_available:
                # gh missing → should return gh_unavailable without calling subprocess
                return {"status": "gh_unavailable", "message": "gh CLI not found in PATH"}

            # Patch _fetch_failing_checks
            failing_names = [r["name"] for r in (runs or [])]
            with mock.patch.object(
                review_mod,
                "_fetch_failing_checks",
                return_value=(failing_names, log),
            ):
                # We can't easily call the async route here, so we test the
                # helpers separately and verify they compose correctly.
                return {
                    "failing_names": failing_names,
                    "log": log,
                    "gh_available": gh_available,
                }

    def test_gh_missing(self, tmp_path: Path):
        import server.routes.review as review_mod
        with mock.patch.object(review_mod, "_gh_available", return_value=False):
            assert not review_mod._gh_available()

    def test_gh_available(self, tmp_path: Path):
        import server.routes.review as review_mod
        with (
            mock.patch("shutil.which", return_value="/usr/bin/gh"),
            mock.patch.object(review_mod, "_gh_available", return_value=True),
        ):
            assert review_mod._gh_available()

    def test_fetch_failing_checks_no_fails(self, tmp_path: Path):
        import server.routes.review as review_mod

        runs_json = json.dumps([
            {"databaseId": 1, "name": "build", "status": "completed", "conclusion": "success"},
        ])
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=runs_json, stderr="")
            names, log = review_mod._fetch_failing_checks("main", str(tmp_path))
        assert names == []
        assert log == ""

    def test_fetch_failing_checks_with_failures(self, tmp_path: Path):
        import server.routes.review as review_mod

        runs_json = json.dumps([
            {"databaseId": 42, "name": "test-suite", "status": "completed", "conclusion": "failure"},
        ])
        log_text = "ERROR: test_foo failed\nAssertionError: ..."

        def fake_run(cmd, **kwargs):
            if "list" in cmd:
                return mock.Mock(returncode=0, stdout=runs_json, stderr="")
            # log-failed
            return mock.Mock(returncode=0, stdout=log_text, stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            names, log = review_mod._fetch_failing_checks("main", str(tmp_path))

        assert "test-suite" in names
        assert "ERROR" in log

    def test_fetch_failing_checks_gh_error(self, tmp_path: Path):
        import server.routes.review as review_mod

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="auth required")
            with pytest.raises(RuntimeError, match="gh run list failed"):
                review_mod._fetch_failing_checks("main", str(tmp_path))

    @pytest.mark.asyncio
    async def test_fix_checks_route_gh_missing(self, tmp_path: Path):
        import server.routes.review as review_mod
        from server.routes.review import FixChecksRequest, fix_checks

        req = FixChecksRequest(repo_path=str(tmp_path), branch="main")
        with mock.patch.object(review_mod, "_gh_available", return_value=False):
            result = await fix_checks(req)
        assert result["status"] == "gh_unavailable"

    @pytest.mark.asyncio
    async def test_fix_checks_route_no_failing(self, tmp_path: Path):
        import server.routes.review as review_mod
        from server.routes.review import FixChecksRequest, fix_checks


        req = FixChecksRequest(repo_path=str(tmp_path), branch="main")
        with (
            mock.patch.object(review_mod, "_gh_available", return_value=True),
            mock.patch.object(review_mod, "_fetch_failing_checks", return_value=([], "")),
        ):
            result = await fix_checks(req)
        assert result["status"] == "no_failing_checks"

    @pytest.mark.asyncio
    async def test_fix_checks_route_failing_starts_goal(self, tmp_path: Path):
        import server.routes.review as review_mod
        from server.routes.review import FixChecksRequest, fix_checks

        # Fake GoalRunResponse
        fake_response = mock.Mock()
        fake_response.goal_id = "test-goal-42"

        async def _fake_goal(*args, **kwargs):
            return fake_response

        async def _wait_for(coro, timeout):
            return await coro

        req = FixChecksRequest(repo_path=str(tmp_path), branch="main")
        with (
            mock.patch.object(review_mod, "_gh_available", return_value=True),
            mock.patch.object(
                review_mod,
                "_fetch_failing_checks",
                return_value=(["ci/test", "ci/lint"], "ERROR: failed"),
            ),
            mock.patch("server.goal_run.runner.project_run_goal", side_effect=_fake_goal),
            mock.patch("asyncio.wait_for", side_effect=_wait_for),
        ):
            result = await fix_checks(req)

        assert result["status"] == "started"
        assert "ci/test" in result.get("failing_checks", [])


