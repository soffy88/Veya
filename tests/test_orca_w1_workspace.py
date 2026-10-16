"""Tests for ORCA-W1: worktree shared paths, CLI agent registry, quota meter."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.coding.cli_agents import (
    _validate_command_template,
    get_cli_agent_registry,
)
from runtime.coding.quota import get_quota_meter
from runtime.coding.worktree_manifest import (
    WorktreeManifest,
    _validate_relative_path,
    apply_manifest,
    load_manifest,
    write_manifest,
)


class TestWorktreeManifest:
    """Tests for worktree manifest parsing and validation."""

    def test_validate_relative_path_valid(self):
        assert _validate_relative_path(".env") == ".env"
        assert _validate_relative_path("config/local.yaml") == "config/local.yaml"
        assert _validate_relative_path("apps/web/node_modules") == "apps/web/node_modules"

    def test_validate_relative_path_invalid(self):
        with pytest.raises(ValueError, match="absolute"):
            _validate_relative_path("/absolute/path")
        with pytest.raises(ValueError, match="traverse upwards"):
            _validate_relative_path("../outside")
        with pytest.raises(ValueError, match="traverse upwards"):
            _validate_relative_path("path/../outside")
        with pytest.raises(ValueError, match="NUL"):
            _validate_relative_path("path\x00with_null")
        with pytest.raises(ValueError, match="non-empty"):
            _validate_relative_path("")
        with pytest.raises(ValueError, match="non-empty"):
            _validate_relative_path("   ")

    def test_load_manifest_empty(self, tmp_path: Path):
        manifest = load_manifest(tmp_path)
        assert manifest.copy == ()
        assert manifest.share == ()

    def test_load_manifest_valid(self, tmp_path: Path):
        manifest_dir = tmp_path / ".veya"
        manifest_dir.mkdir()
        manifest_file = manifest_dir / "worktree.toml"
        manifest_file.write_text(
            'copy = [".env", ".env.local"]\nshare = ["node_modules", ".venv"]\n'
        )

        manifest = load_manifest(tmp_path)
        assert manifest.copy == (".env", ".env.local")
        assert manifest.share == ("node_modules", ".venv")

    def test_load_manifest_invalid_toml(self, tmp_path: Path):
        manifest_dir = tmp_path / ".veya"
        manifest_dir.mkdir()
        manifest_file = manifest_dir / "worktree.toml"
        manifest_file.write_text('copy = [".env", \n')  # Invalid TOML

        manifest = load_manifest(tmp_path)
        assert manifest.copy == ()
        assert manifest.share == ()

    def test_write_manifest(self, tmp_path: Path):
        manifest = WorktreeManifest(copy=(".env",), share=("node_modules",))
        write_manifest(tmp_path, manifest)

        manifest_file = tmp_path / ".veya" / "worktree.toml"
        assert manifest_file.is_file()
        content = manifest_file.read_text()
        assert 'copy = [".env"]' in content
        assert 'share = ["node_modules"]' in content

    def test_apply_manifest_copy(self, tmp_path: Path):
        # Create source files
        (tmp_path / ".env").write_text("FOO=bar\n")
        (tmp_path / ".env.local").write_text("LOCAL=1\n")

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        manifest = WorktreeManifest(copy=(".env", ".env.local", ".missing"), share=())
        result = apply_manifest(tmp_path, worktree, manifest)

        assert ".env" in result["copied"]
        assert ".env.local" in result["copied"]
        assert ".missing" in result["skipped_copy"]
        assert (worktree / ".env").read_text() == "FOO=bar\n"
        assert (worktree / ".env.local").read_text() == "LOCAL=1\n"

    def test_apply_manifest_share(self, tmp_path: Path):
        # Create source directories
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg").write_text("content")
        (tmp_path / ".venv").mkdir()

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        manifest = WorktreeManifest(copy=(), share=("node_modules", ".venv", ".missing"))
        result = apply_manifest(tmp_path, worktree, manifest)

        assert "node_modules" in result["shared"]
        assert ".venv" in result["shared"]
        assert ".missing" in result["skipped_share"]
        assert (worktree / "node_modules").is_symlink()
        assert (worktree / "node_modules").resolve() == (tmp_path / "node_modules").resolve()
        assert (worktree / ".venv").is_symlink()

    def test_apply_manifest_idempotent(self, tmp_path: Path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / ".venv").mkdir()

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        manifest = WorktreeManifest(copy=(), share=("node_modules", ".venv"))

        # Apply twice
        result1 = apply_manifest(tmp_path, worktree, manifest)
        result2 = apply_manifest(tmp_path, worktree, manifest)

        assert "node_modules" in result1["shared"]
        assert "node_modules" in result2["shared"]


class TestCLIAgentRegistry:
    """Tests for CLI agent registry and detection."""

    def test_validate_command_template_valid(self):
        template = "codex {prompt} --cwd {cwd}"
        assert _validate_command_template(template) == template
        with pytest.raises(ValueError, match="allowlisted"):
            _validate_command_template("cmd {prompt} {cwd}")

    def test_unsafe_persisted_profile_is_ignored(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("VEYA_CLI_EXECUTABLE_ALLOWLIST", "safe-cli")
        config = tmp_path / ".veya" / "cli_agents.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            '{"profiles": ['
            '{"name": "codex", "command_template": "safe-cli {prompt} {cwd}"},'
            '{"name": "unsafe", "command_template": "other-cli {prompt} {cwd}"}'
            "]}",
            encoding="utf-8",
        )
        registry = get_cli_agent_registry(tmp_path)
        assert registry.get("codex").is_custom is False
        assert registry.get("unsafe") is None

    def test_validate_command_template_invalid(self):
        with pytest.raises(ValueError, match="prompt"):
            _validate_command_template("cmd {cwd}")
        with pytest.raises(ValueError, match="cwd"):
            _validate_command_template("cmd {prompt}")
        with pytest.raises(ValueError, match="non-empty"):
            _validate_command_template("")

    def test_registry_discovery(self, tmp_path: Path):
        registry = get_cli_agent_registry(tmp_path)
        profiles = registry.list_all()

        # Should have all built-in profiles (available or not)
        names = {p.name for p in profiles}
        assert "claude-code" in names
        assert "codex" in names
        assert "opencode" in names
        assert "cursor-agent" in names
        assert "gemini" in names
        assert "qwen" in names

    def test_custom_profile_registration(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("VEYA_CLI_EXECUTABLE_ALLOWLIST", "my-agent")
        registry = get_cli_agent_registry(tmp_path)

        profile = registry.register_custom("my-agent", "my-agent {prompt} --dir {cwd}")
        assert profile.name == "my-agent"
        assert profile.is_custom is True
        assert profile.available is True

        # Should appear in list
        profiles = registry.list_all()
        names = {p.name for p in profiles}
        assert "my-agent" in names

    def test_custom_profile_override_builtin_fails(self, tmp_path: Path):
        registry = get_cli_agent_registry(tmp_path)
        with pytest.raises(ValueError, match="override built-in"):
            registry.register_custom("claude-code", "codex {prompt} {cwd}")

    def test_render_command(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("VEYA_CLI_EXECUTABLE_ALLOWLIST", "my-cmd")
        registry = get_cli_agent_registry(tmp_path)
        profile = registry.register_custom("test-agent", "my-cmd '{prompt}' --cwd {cwd}")

        argv = registry.render_command(profile, "fix the bug", "/tmp/work")
        assert "fix the bug" in argv
        assert "/tmp/work" in argv
        assert argv[0] == "my-cmd"

    def test_render_command_keeps_prompt_and_cwd_as_single_arguments(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("VEYA_CLI_EXECUTABLE_ALLOWLIST", "my-cmd")
        registry = get_cli_agent_registry(tmp_path)
        profile = registry.register_custom("safe", "my-cmd '{prompt}' --cwd '{cwd}'")
        argv = registry.render_command(profile, "repair'; touch /tmp/NOPE; #", "/tmp/a path")
        assert argv == ["my-cmd", "repair'; touch /tmp/NOPE; #", "--cwd", "/tmp/a path"]

    def test_persist_custom_profiles(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("VEYA_CLI_EXECUTABLE_ALLOWLIST", "cmd1,cmd2")
        registry = get_cli_agent_registry(tmp_path)
        registry.register_custom("agent1", "cmd1 {prompt} {cwd}")
        registry.register_custom("agent2", "cmd2 {prompt} {cwd}")

        # Create new registry instance to test persistence
        registry2 = get_cli_agent_registry(tmp_path)
        profiles = registry2.list_all()
        names = {p.name for p in profiles}
        assert "agent1" in names
        assert "agent2" in names


class TestQuotaMeter:
    """Tests for quota meter."""

    def test_quota_unknown_by_default(self, tmp_path: Path):
        meter = get_quota_meter(tmp_path)
        quota = meter.get_quota("nonexistent-agent")
        assert quota.backend == "nonexistent-agent"
        assert quota.used == "unknown"
        assert quota.limit == "unknown"
        assert quota.reset_at == "unknown"
        assert quota.unit == "unknown"

    def test_parse_claude_status(self, tmp_path: Path):
        meter = get_quota_meter(tmp_path)
        output = """account: user@example.com
used: 15000
limit: 100000
reset: 2026-10-11T00:00:00Z
unit: tokens"""
        quota = meter.parse_cli_status("claude-code", output)
        assert quota is not None
        assert quota.backend == "claude-code"
        assert quota.account == "user@example.com"
        assert quota.used == "15000"
        assert quota.limit == "100000"
        assert quota.unit == "tokens"

    def test_parse_codex_status(self, tmp_path: Path):
        meter = get_quota_meter(tmp_path)
        output = """account: user
used: 50
limit: 100
reset_at: 2026-10-11T00:00:00Z
unit: requests"""
        quota = meter.parse_cli_status("codex", output)
        assert quota is not None
        assert quota.backend == "codex"
        assert quota.used == "50"
        assert quota.limit == "100"
        assert quota.unit == "requests"

    def test_parse_opencode_status(self, tmp_path: Path):
        meter = get_quota_meter(tmp_path)
        output = """account: user
used: 5000
limit: 50000
reset: 2026-10-11T00:00:00Z
unit: tokens"""
        quota = meter.parse_cli_status("opencode", output)
        assert quota is not None
        assert quota.backend == "opencode"
        assert quota.used == "5000"
        assert quota.limit == "50000"
        assert quota.unit == "tokens"

    def test_parse_response_headers(self, tmp_path: Path):
        meter = get_quota_meter(tmp_path)
        headers = {
            "x-ratelimit-used": "100",
            "x-ratelimit-limit": "1000",
            "x-ratelimit-reset": "1791628800",  # 2026-10-10T00:00:00Z
        }
        quota = meter.parse_response_headers("test-backend", headers)
        assert quota is not None
        assert quota.backend == "test-backend"
        assert quota.used == "100"
        assert quota.limit == "1000"
        assert quota.unit == "requests"
        assert "2026-10-10" in quota.reset_at


class TestIntegration:
    """Integration tests using tmp git repos."""

    def test_worktree_manifest_with_git_repo(self, tmp_path: Path):
        # Initialize git repo
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

        # Create initial commit
        (repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True
        )

        # Create manifest
        (repo / ".veya").mkdir()
        (repo / ".veya" / "worktree.toml").write_text('copy = [".env"]\nshare = ["node_modules"]\n')

        # Create source files
        (repo / ".env").write_text("TEST=1\n")
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "pkg").write_text("content")

        # Create worktree
        worktree = repo / ".veya" / "worktrees" / "task-test"
        worktree.parent.mkdir(parents=True)

        # Simulate git worktree add
        subprocess.run(
            ["git", "worktree", "add", "-b", "test-branch", str(worktree), "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Apply manifest
        from runtime.coding.worktree_manifest import apply_manifest, load_manifest

        manifest = load_manifest(repo)
        result = apply_manifest(repo, worktree, manifest)

        assert ".env" in result["copied"]
        assert "node_modules" in result["shared"]
        assert (worktree / ".env").read_text() == "TEST=1\n"
        assert (worktree / "node_modules").is_symlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
