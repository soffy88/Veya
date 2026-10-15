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


def test_direct_exec_guard_wiring_is_opt_in(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import veya.remote.direct_exec as direct_exec

    class FakeRunner:
        def __init__(self, workspace_root, profile, runtime_profile=None):
            self.workspace_root = workspace_root
            self.profile = SimpleNamespace(network="allowed", executor="host", id="default")

    monkeypatch.setattr(direct_exec, "CommandRunner", FakeRunner)
    monkeypatch.setattr(direct_exec, "_nested_write_path_violation", lambda *args: None)
    monkeypatch.delenv("VEYA_GSK_CAREFUL", raising=False)
    monkeypatch.delenv("VEYA_GSK_FREEZE_DIR", raising=False)

    planned = direct_exec._plan_execution(
        tmp_path, "echo safe", cwd=tmp_path, profile="default",
        approved=False, network=None,
    )
    assert planned[-1] == "echo safe"

    monkeypatch.setenv("VEYA_GSK_CAREFUL", "1")
    dangerous_command = "r" + "m -rf /"
    with pytest.raises(direct_exec.DirectDenied):
        direct_exec._plan_execution(
            tmp_path, dangerous_command, cwd=tmp_path, profile="default",
            approved=False, network=None,
        )
    with pytest.raises(direct_exec.DirectDenied):
        direct_exec._plan_execution(
            tmp_path, "git push origin main --force", cwd=tmp_path,
            profile="default", approved=False, network=None,
        )

    monkeypatch.setenv("VEYA_GSK_FREEZE_DIR", "veya/gsk")
    with pytest.raises(direct_exec.DirectDenied):
        direct_exec._plan_execution(
            tmp_path, "echo safe", cwd=tmp_path, profile="default",
            approved=False, network=None,
        )
