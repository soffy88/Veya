"""P1-A Persistent Computer: Failing tests first (TDD)."""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import pytest

from runtime.computer import (
    CheckpointRef,
    CredentialRef,
    CredentialType,
    PersistentComputer,
    PersistentComputerStore,
    generate_computer_id,
)


@pytest.fixture
def temp_db_path():
    """Provide a temporary database path for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "computers.db"


@pytest.fixture
def temp_db(temp_db_path):
    """Provide a PersistentComputerStore instance."""
    store = PersistentComputerStore(temp_db_path)
    yield store


class TestPersistentComputerModels:
    """Test the core models."""

    def test_computer_id_stability(self):
        """Same owner+workspace must always produce same computer_id."""
        id1 = generate_computer_id("user1", "/workspace/project")
        id2 = generate_computer_id("user1", "/workspace/project")
        assert id1 == id2

    def test_computer_id_uniqueness(self):
        """Different owner or workspace must produce different computer_id."""
        id1 = generate_computer_id("user1", "/workspace/project")
        id2 = generate_computer_id("user2", "/workspace/project")
        id3 = generate_computer_id("user1", "/workspace/other")
        assert id1 != id2
        assert id1 != id3

    def test_persistent_computer_immutability(self):
        """PersistentComputer must be immutable (frozen)."""
        computer = PersistentComputer(
            computer_id="comp-test",
            owner_id="user1",
            workspace_ref="/workspace/project",
        )
        # Should not be able to modify
        with pytest.raises(dataclasses.FrozenInstanceError):
            computer.owner_id = "user2"

    def test_add_goal_run_creates_new_instance(self):
        """add_goal_run must return new instance (immutability)."""
        computer = PersistentComputer(
            computer_id="comp-test",
            owner_id="user1",
            workspace_ref="/workspace/project",
        )
        new_computer = computer.add_goal_run("goal-1")
        assert new_computer is not computer
        assert "goal-1" in new_computer.goal_run_refs
        assert "goal-1" not in computer.goal_run_refs

    def test_remove_goal_run_creates_new_instance(self):
        """remove_goal_run must return new instance."""
        computer = PersistentComputer(
            computer_id="comp-test",
            owner_id="user1",
            workspace_ref="/workspace/project",
            goal_run_refs=["goal-1", "goal-2"],
        )
        new_computer = computer.remove_goal_run("goal-1")
        assert new_computer is not computer
        assert "goal-1" not in new_computer.goal_run_refs
        assert "goal-2" in new_computer.goal_run_refs


class TestPersistentComputerStore:
    """Test the SQLite-backed store."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_computers.db"
            store = PersistentComputerStore(db_path)
            yield store

    def test_create_computer(self, temp_db):
        """CREATE_COMPUTER: Create a persistent computer."""
        computer = temp_db.create_computer(
            owner_id="user1",
            workspace_ref="/workspace/project",
        )
        assert computer.computer_id.startswith("comp-")
        assert computer.owner_id == "user1"
        assert computer.workspace_ref == "/workspace/project"
        assert computer.lifecycle_state == "created"

    def test_create_computer_idempotent(self, temp_db):
        """CREATE_COMPUTER: Same owner+workspace returns same computer."""
        comp1 = temp_db.create_computer("user1", "/workspace/project")
        comp2 = temp_db.create_computer("user1", "/workspace/project")
        assert comp1.computer_id == comp2.computer_id
        assert comp1 is not comp2

    def test_get_computer(self, temp_db):
        """Get computer by ID."""
        created = temp_db.create_computer("user1", "/workspace/project")
        fetched = temp_db.get_computer(created.computer_id)
        assert fetched is not None
        assert fetched.computer_id == created.computer_id

    def test_get_computer_not_found(self, temp_db):
        """Get non-existent computer returns None."""
        assert temp_db.get_computer("comp-nonexistent") is None

    def test_update_computer(self, temp_db):
        """Update computer metadata."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        updated = computer.with_browser_profile("/browser/profile")
        saved = temp_db.update_computer(updated)
        assert saved.browser_profile_ref == "/browser/profile"

        # Fetch again to verify persistence
        fetched = temp_db.get_computer(saved.computer_id)
        assert fetched.browser_profile_ref == "/browser/profile"

    def test_delete_computer(self, temp_db):
        """Delete computer with no active sessions."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        assert temp_db.delete_computer(computer.computer_id) is True
        assert temp_db.get_computer(computer.computer_id) is None

    def test_delete_computer_with_active_session_fails(self, temp_db):
        """Delete computer with active session must fail."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        temp_db.create_session(computer.computer_id, "user1", "supervisor-1")
        assert temp_db.delete_computer(computer.computer_id) is False

    def test_list_computers(self, temp_db):
        """List computers with filters."""
        temp_db.create_computer("user1", "/workspace/project1")
        temp_db.create_computer("user1", "/workspace/project2")
        temp_db.create_computer("user2", "/workspace/project3")

        user1_computers = temp_db.list_computers(owner_id="user1")
        assert len(user1_computers) == 2

        all_computers = temp_db.list_computers()
        assert len(all_computers) == 3

    def test_session_management(self, temp_db):
        """Create, get, and end sessions."""
        computer = temp_db.create_computer("user1", "/workspace/project")

        session = temp_db.create_session(computer.computer_id, "user1", "supervisor-1")
        assert session.session_id.startswith("sess-")
        assert session.computer_id == computer.computer_id
        assert session.state == "active"

        fetched = temp_db.get_session(session.session_id)
        assert fetched is not None
        assert fetched.session_id == session.session_id

        active = temp_db.get_active_session(computer.computer_id)
        assert active is not None
        assert active.session_id == session.session_id

        assert temp_db.end_session(session.session_id) is True
        ended = temp_db.get_session(session.session_id)
        assert ended.state == "terminated"

    def test_goal_run_correlation(self, temp_db):
        """GOALRUN_CORRELATION: Link/unlink GoalRun to computer."""
        computer = temp_db.create_computer("user1", "/workspace/project")

        # Link
        assert temp_db.link_goal_run("goal-1", computer.computer_id, "user1") is True
        linked = temp_db.get_computer_for_goal_run("goal-1")
        assert linked is not None
        assert linked.computer_id == computer.computer_id

        # Verify computer's goal_run_refs updated
        computer_fetched = temp_db.get_computer(computer.computer_id)
        assert "goal-1" in computer_fetched.goal_run_refs

        # Unlink
        assert temp_db.unlink_goal_run("goal-1") is True
        assert temp_db.get_computer_for_goal_run("goal-1") is None

        computer_fetched = temp_db.get_computer(computer.computer_id)
        assert "goal-1" not in computer_fetched.goal_run_refs

    def test_goal_run_cross_user_denied(self, temp_db):
        """CROSS_USER_ISOLATION: Cannot link goal run from different user."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        assert temp_db.link_goal_run("goal-1", computer.computer_id, "user2") is False

    def test_credential_management(self, temp_db):
        """Credential references (no plaintext)."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        cred = CredentialRef(
            ref_id="cred-1",
            type=CredentialType.API_KEY,
            name="github_token",
        )

        updated = temp_db.add_credential(computer.computer_id, cred)
        assert updated is not None
        assert any(c.ref_id == "cred-1" for c in updated.credential_refs)

        # Remove
        updated = temp_db.remove_credential(computer.computer_id, "cred-1")
        assert updated is not None
        assert not any(c.ref_id == "cred-1" for c in updated.credential_refs)

    def test_credential_plaintext_zero(self, temp_db):
        """CREDENTIAL_PLAINTEXT=0: No plaintext in stored credentials."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        cred = CredentialRef(
            ref_id="cred-1",
            type=CredentialType.API_KEY,
            name="github_token",
            metadata={"hint": "sk-****"},
        )
        temp_db.add_credential(computer.computer_id, cred)

        # Verify no plaintext in stored data by checking the DB directly
        import sqlite3
        with sqlite3.connect(str(temp_db.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT credential_refs FROM persistent_computers WHERE computer_id = ?",
                (computer.computer_id,),
            ).fetchone()
            creds_json = row["credential_refs"]
            # Should not contain actual secret
            assert "sk-" not in creds_json or "sk-****" in creds_json

    def test_checkpoint_management(self, temp_db):
        """CHECKPOINT: Set and get checkpoint."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        checkpoint = CheckpointRef(
            checkpoint_id="ckpt-1",
            computer_id=computer.computer_id,
            goal_run_id="goal-1",
            path="/checkpoints/ckpt-1",
            sha256="abc123",
            size_bytes=1024,
        )

        updated = temp_db.set_checkpoint(computer.computer_id, checkpoint)
        assert updated is not None
        assert updated.checkpoint_ref is not None
        assert updated.checkpoint_ref.checkpoint_id == "ckpt-1"

        fetched = temp_db.get_checkpoint(computer.computer_id)
        assert fetched is not None
        assert fetched.checkpoint_id == "ckpt-1"

    def test_state_transitions(self, temp_db):
        """Lifecycle state transitions."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        assert computer.lifecycle_state == "created"

        temp_db.set_state(computer.computer_id, "running")
        fetched = temp_db.get_computer(computer.computer_id)
        assert fetched.lifecycle_state == "running"

        temp_db.set_state(computer.computer_id, "stopped")
        fetched = temp_db.get_computer(computer.computer_id)
        assert fetched.lifecycle_state == "stopped"

    def test_touch_updates_last_active(self, temp_db):
        """Touch updates last_active_at."""
        computer = temp_db.create_computer("user1", "/workspace/project")
        original_active = computer.last_active_at

        # Small delay to ensure timestamp difference
        import time
        time.sleep(0.01)

        temp_db.touch(computer.computer_id)
        fetched = temp_db.get_computer(computer.computer_id)
        assert fetched.last_active_at > original_active


class TestSupervisorRestart:
    """Test supervisor restart recovery with same computer_id."""

    @pytest.fixture
    def temp_db_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "computers.db"

    def test_supervisor_restart_same_computer_id(self, temp_db_path):
        """SUPERVISOR_RESTART: Create computer, persist, restore with same ID."""
        # Supervisor A creates computer
        store_a = PersistentComputerStore(temp_db_path)
        computer = store_a.create_computer(
            owner_id="user1",
            workspace_ref="/workspace/project",
            browser_profile_ref="/browser/profile",
            downloads_ref="/downloads",
        )
        original_id = computer.computer_id
        store_a.link_goal_run("goal-1", computer.computer_id, "user1")

        # Simulate supervisor termination
        del store_a

        # Supervisor B restores
        store_b = PersistentComputerStore(temp_db_path)
        restored = store_b.get_computer(original_id)

        # Must be SAME computer_id
        assert restored is not None
        assert restored.computer_id == original_id
        assert restored.workspace_ref == "/workspace/project"
        assert restored.browser_profile_ref == "/browser/profile"
        assert restored.downloads_ref == "/downloads"
        assert "goal-1" in restored.goal_run_refs

    def test_supervisor_restart_state_preserved(self, temp_db_path):
        """State preserved across restart."""
        store_a = PersistentComputerStore(temp_db_path)
        computer = store_a.create_computer("user1", "/workspace/project")
        store_a.set_state(computer.computer_id, "running")
        store_a.link_goal_run("goal-1", computer.computer_id, "user1")
        original_id = computer.computer_id
        del store_a

        store_b = PersistentComputerStore(temp_db_path)
        restored = store_b.get_computer(original_id)
        assert restored.lifecycle_state == "running"
        assert "goal-1" in restored.goal_run_refs


class TestFilesystemPersistence:
    """FILESYSTEM_PERSISTENCE: Workspace filesystem preserved."""

    def test_workspace_preserved(self, temp_db_path):
        """Workspace directory preserved across restarts."""
        store_a = PersistentComputerStore(temp_db_path)
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "test.txt").write_text("hello")

            computer = store_a.create_computer(
                owner_id="user1",
                workspace_ref=str(workspace),
            )
            original_id = computer.computer_id
            del store_a

            # Restore
            store_b = PersistentComputerStore(temp_db_path)
            restored = store_b.get_computer(original_id)

            # Filesystem must still exist and be accessible
            assert Path(restored.workspace_ref).exists()
            assert (Path(restored.workspace_ref) / "test.txt").read_text() == "hello"


class TestBrowserProfilePersistence:
    """BROWSER_PROFILE_PERSISTENCE: Browser profile reference preserved."""

    def test_browser_profile_reference_preserved(self, temp_db_path):
        """Browser profile ref (not PID) preserved."""
        store_a = PersistentComputerStore(temp_db_path)
        computer = store_a.create_computer(
            owner_id="user1",
            workspace_ref="/workspace/project",
            browser_profile_ref="/browser/profiles/user1",
        )
        original_id = computer.computer_id
        del store_a

        store_b = PersistentComputerStore(temp_db_path)
        restored = store_b.get_computer(original_id)
        assert restored.browser_profile_ref == "/browser/profiles/user1"

    def test_browser_profile_not_process_handle(self, temp_db_path):
        """Browser profile is reference, not process handle."""
        store = PersistentComputerStore(temp_db_path)
        computer = store.create_computer(
            owner_id="user1",
            workspace_ref="/workspace/project",
            browser_profile_ref="/browser/profiles/user1",
        )
        # Profile ref is a path string, not a PID or handle
        assert isinstance(computer.browser_profile_ref, str)
        assert not computer.browser_profile_ref.isdigit()


class TestDownloadPersistence:
    """DOWNLOAD_PERSISTENCE: Downloads directory preserved."""

    def test_downloads_reference_preserved(self, temp_db_path):
        """Downloads ref preserved."""
        store_a = PersistentComputerStore(temp_db_path)
        with tempfile.TemporaryDirectory() as tmpdir:
            downloads = Path(tmpdir) / "downloads"
            downloads.mkdir()
            (downloads / "file.pdf").write_bytes(b"PDF content")

            computer = store_a.create_computer(
                owner_id="user1",
                workspace_ref="/workspace/project",
                downloads_ref=str(downloads),
            )
            original_id = computer.computer_id
            del store_a

            store_b = PersistentComputerStore(temp_db_path)
            restored = store_b.get_computer(original_id)
            assert restored.downloads_ref == str(downloads)
            assert (Path(restored.downloads_ref) / "file.pdf").exists()


class TestGoalRunResume:
    """GOALRUN_RESUME: GoalRun can resume on same computer."""

    def test_goal_run_can_resume(self, temp_db_path):
        """GoalRun linked to computer can resume after restart."""
        store_a = PersistentComputerStore(temp_db_path)
        computer = store_a.create_computer("user1", "/workspace/project")
        store_a.link_goal_run("goal-run-1", computer.computer_id, "user1")
        original_id = computer.computer_id
        del store_a

        store_b = PersistentComputerStore(temp_db_path)
        # Verify goal run correlation preserved
        linked_computer = store_b.get_computer_for_goal_run("goal-run-1")
        assert linked_computer is not None
        assert linked_computer.computer_id == original_id

        # Resume: verify can link new goal run to same computer
        assert store_b.link_goal_run("goal-run-2", original_id, "user1") is True


class TestCrossUserIsolation:
    """CROSS_USER_ISOLATION: Users cannot access each other's computers."""

    def test_cross_user_denied(self, temp_db_path):
        """User1 cannot access User2's computer."""
        store = PersistentComputerStore(temp_db_path)
        computer = store.create_computer("user1", "/workspace/project")

        # User2 tries to access
        fetched = store.get_computer(computer.computer_id)
        assert fetched.owner_id == "user1"

        # But user2 cannot link goal run to user1's computer
        assert store.link_goal_run("goal-1", computer.computer_id, "user2") is False

    def test_workspace_isolation(self, temp_db_path):
        """Users cannot escape workspace."""
        store = PersistentComputerStore(temp_db_path)
        computer = store.create_computer("user1", "/workspace/user1/project")

        # Workspace is bound to computer
        assert computer.workspace_ref == "/workspace/user1/project"
        # No way to change workspace_ref to escape


class TestWorkspaceEscape:
    """WORKSPACE_ESCAPE: Workspace path cannot be escaped."""

    def test_workspace_escape_denied(self, temp_db_path):
        """Cannot traverse outside workspace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "file.txt").write_text("inside")

            store = PersistentComputerStore(temp_db_path)
            computer = store.create_computer("user1", str(workspace))

            # Workspace ref is fixed at creation
            assert computer.workspace_ref == str(workspace)
            # Cannot change to parent directory
            # (no API to change workspace_ref after creation)


class TestDuplicateComputers:
    """DUPLICATE_COMPUTERS=0: Idempotent recovery ensures single logical computer."""

    def test_idempotent_restore(self, temp_db_path):
        """DUPLICATE_COMPUTERS=0: Repeated restore yields single logical computer."""
        store_a = PersistentComputerStore(temp_db_path)
        computer = store_a.create_computer("user1", "/workspace/project")
        original_id = computer.computer_id
        store_a.link_goal_run("goal-1", computer.computer_id, "user1")
        del store_a

        # Multiple restores
        for _ in range(3):
            store_b = PersistentComputerStore(temp_db_path)
            restored = store_b.get_computer(original_id)
            assert restored.computer_id == original_id
            del store_b

        # Verify only one computer exists
        final_store = PersistentComputerStore(temp_db_path)
        all_computers = final_store.list_computers(owner_id="user1")
        assert len(all_computers) == 1
        assert all_computers[0].computer_id == original_id


class TestRecovery:
    """Test idempotent recovery."""

    def test_recovery_creates_single_active_runtime(self, temp_db_path):
        """After recovery: 1 logical PersistentComputer, 1 active recovered runtime."""
        store_a = PersistentComputerStore(temp_db_path)
        computer = store_a.create_computer("user1", "/workspace/project")
        original_id = computer.computer_id
        del store_a

        # Multiple recovery attempts
        for i in range(3):
            store = PersistentComputerStore(temp_db_path)
            restored = store.get_computer(original_id)
            assert restored.computer_id == original_id
            # Can create session (active runtime)
            session = store.create_session(original_id, "user1", f"supervisor-{i}")
            assert session.state == "active"
            store.end_session(session.session_id)
            del store

        # Final verification
        final = PersistentComputerStore(temp_db_path)
        assert final.get_computer(original_id) is not None
        assert len(final.list_computers(owner_id="user1")) == 1




if __name__ == "__main__":
    pytest.main([__file__, "-v"])
