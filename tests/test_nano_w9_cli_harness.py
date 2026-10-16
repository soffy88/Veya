"""NANO-W9 CLI-harness tests (addendum D + element 6 + addendum E replay).

Covers the CLI-Anything conventions the agent CLI must obey:

* one stable ``--json`` envelope per command;
* errors as ``{"status":"error","error":{...}}`` with a **non-zero exit**;
* bare invocation enters a REPL;
* ``--help`` is self-describing;
* ``skills/veya-cli/SKILL.md`` is byte-identical to what the generator produces
  from the live argparse tree (drift fails).

The CLI is exercised as a **subprocess** whenever the exit code matters — that
is the only way to prove the contract an agent actually depends on.

Run with:  venv/bin/python -m pytest -q tests/test_nano_w9_cli_harness.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "veya_cli.py"
GENERATOR = SCRIPTS / "gen_veya_cli_skill.py"
SKILL = ROOT / "skills" / "veya-cli" / "SKILL.md"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import veya_cli  # noqa: E402

# ==========================================================================
# helpers
# ==========================================================================


@pytest.fixture
def cli_env(tmp_path):
    """Environment that keeps every store inside ``tmp_path`` and all flags off."""
    env = dict(os.environ)
    env.update(
        {
            "VEYA_EVENT_STORE_PATH": str(tmp_path / "events.jsonl"),
            "VEYA_BOTS_ROOT": str(tmp_path / "bots"),
            "VEYA_BOT_REGISTRY_PATH": str(tmp_path / "bots" / "registry.json"),
            "VEYA_ROUTINE_REGISTRY_PATH": str(tmp_path / "routines.json"),
            "PYTHONPATH": str(ROOT),
        }
    )
    for flag in (
        "VEYA_BRANCH_ROOMS",
        "VEYA_ROUTINE_TRIGGERS",
        "VEYA_ACP_MEMBERS",
        "VEYA_AUDIT_CHAIN",
        "VEYA_CLI_GEN",
        "VEYA_BASE_URL",
        "VEYA_TOKEN",
    ):
        env.pop(flag, None)
    return env


def run_cli(*args: str, env: dict[str, str] | None = None, stdin: str | None = None):
    """Invoke the CLI as a subprocess.

    Args:
        *args: argv after the program name.
        env: environment override.
        stdin: text piped to the process (used for the REPL).

    Returns:
        The completed :class:`subprocess.CompletedProcess`.
    """
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
        cwd=str(ROOT),
        timeout=180,
    )


def envelope(result: subprocess.CompletedProcess) -> dict:
    """Parse the CLI's single JSON envelope from stdout."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"no stdout\nstderr: {result.stderr}"
    return json.loads(lines[-1])


# ==========================================================================
# envelope contract
# ==========================================================================


class TestEnvelope:
    def test_ok_and_error_shapes(self):
        assert veya_cli.ok_envelope(count=1) == {"status": "ok", "count": 1}
        payload = veya_cli.error_envelope("X", "y", flag="F")
        assert payload["status"] == "error"
        assert payload["error"] == {"code": "X", "message": "y", "flag": "F"}

    def test_merge_ok_cannot_be_shadowed_by_a_result_status(self):
        assert veya_cli.merge_ok({"status": "empty", "n": 1}) == {"status": "ok", "n": 1}

    def test_emit_exit_codes(self):
        stream = io.StringIO()
        assert veya_cli.emit(veya_cli.ok_envelope(x=1), stream=stream) == 0
        assert veya_cli.emit(veya_cli.error_envelope("E", "m"), stream=stream) == 1
        assert json.loads(stream.getvalue().splitlines()[0])["status"] == "ok"
        assert json.loads(stream.getvalue().splitlines()[1])["status"] == "error"

    def test_pretty_mode_is_not_the_default(self):
        stream = io.StringIO()
        veya_cli.emit(veya_cli.error_envelope("E", "boom"), as_json=False, stream=stream)
        assert stream.getvalue() == "error E: boom\n"


# ==========================================================================
# --json on every command, non-zero exit on error
# ==========================================================================


class TestJsonContract:
    def test_flags_command(self, cli_env):
        result = run_cli("flags", "--json", env=cli_env)
        assert result.returncode == 0, result.stderr
        payload = envelope(result)
        assert payload["status"] == "ok"
        assert set(payload["flags"]) == {
            "VEYA_ACP_MEMBERS",
            "VEYA_AUDIT_CHAIN",
            "VEYA_BRANCH_ROOMS",
            "VEYA_CLI_GEN",
            "VEYA_ROUTINE_TRIGGERS",
            "VEYA_EVENT_TRIGGERS",
        }
        assert not any(payload["flags"].values())

    @pytest.mark.parametrize(
        ("args", "expected_code"),
        [
            (("room", "open", "feat/x"), "FEATURE_DISABLED"),
            (
                (
                    "room",
                    "list",
                ),
                "FEATURE_DISABLED",
            ),
            (("room", "publish", "--branch", "b", "--group", "g"), "FEATURE_DISABLED"),
            (
                (
                    "acp",
                    "members",
                ),
                "FEATURE_DISABLED",
            ),
            (("acp", "add", "goose", "--owner", "u"), "FEATURE_DISABLED"),
            (("acp", "turn", "acp:g", "hi"), "FEATURE_DISABLED"),
            (("routine", "signal", "message", "--text", "x"), "FEATURE_DISABLED"),
            (("cli-gen", "--repo", "/tmp"), "FEATURE_DISABLED"),
        ],
    )
    def test_disabled_features_error_with_nonzero_exit(self, cli_env, args, expected_code):
        result = run_cli(*args, "--json", env=cli_env)
        assert result.returncode != 0, args
        payload = envelope(result)
        assert payload["status"] == "error"
        assert payload["error"]["code"] == expected_code
        assert "VEYA_" in payload["error"].get("flag", "") or expected_code == "FEATURE_DISABLED"

    def test_json_flag_position_is_flexible(self, cli_env):
        before = run_cli("--json", "flags", env=cli_env)
        after = run_cli("flags", "--json", env=cli_env)
        assert before.returncode == 0 and after.returncode == 0
        assert envelope(before) == envelope(after)

    def test_global_transport_flags_are_stripped(self, cli_env):
        result = run_cli(
            "--base-url", "http://127.0.0.1:1", "--token", "abc", "flags", "--json", env=cli_env
        )
        # Unreachable base URL ⇒ a transport envelope, not a crash.
        payload = envelope(result)
        assert payload["status"] == "error"
        assert payload["error"]["code"] in {"HTTP_UNREACHABLE", "HTTP_ERROR"}
        assert payload["error"]["base_url"] == "http://127.0.0.1:1"

    def test_search_is_always_available_and_cites(self, cli_env):
        result = run_cli("search", "no such thing", "--json", env=cli_env)
        assert result.returncode == 0
        payload = envelope(result)
        assert payload["status"] == "ok"
        assert payload["cited"] is False
        assert payload["receipts"] == []

    def test_audit_verify_on_an_empty_chain(self, cli_env):
        result = run_cli("audit", "verify", "--json", env=cli_env)
        assert result.returncode == 0
        payload = envelope(result)
        assert payload["status"] == "ok"
        assert payload["length"] == 0

    def test_audit_verify_reports_a_broken_chain(self, cli_env, monkeypatch):
        from server import events as events_module
        from server.nano_w9.audit_chain import stamp_event, verify_audit_chain

        monkeypatch.setenv("VEYA_EVENT_STORE_PATH", cli_env["VEYA_EVENT_STORE_PATH"])
        monkeypatch.setenv("VEYA_AUDIT_CHAIN", "1")
        # ``event_store`` is a module-level singleton bound at import time; the
        # env var alone would still read whatever store this process opened first.
        monkeypatch.setattr(
            events_module,
            "event_store",
            events_module.EventStore(Path(cli_env["VEYA_EVENT_STORE_PATH"])),
        )
        if True:
            stamp_event("git.event", {"n": 0})
            stamp_event("git.event", {"n": 1})
            path = Path(cli_env["VEYA_EVENT_STORE_PATH"])
            lines = path.read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[0])
            record["payload"]["audit"]["payload"]["n"] = 99
            lines[0] = json.dumps(record, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            assert verify_audit_chain()["status"] == "broken"

        result = run_cli("audit", "verify", "--json", env={**cli_env, "VEYA_AUDIT_CHAIN": "1"})
        assert result.returncode == 1
        payload = envelope(result)
        assert payload["error"]["code"] == "CHAIN_BROKEN"
        assert payload["broken_index"] == 0

    def test_trajectory_record_then_replay(self, cli_env):
        record = run_cli(
            "trajectory",
            "record",
            "--step",
            "1",
            "--command",
            "pnpm build",
            "--artifact",
            "build/app.js",
            "--goal",
            "g-cli",
            "--json",
            env=cli_env,
        )
        assert record.returncode == 0, record.stderr
        assert envelope(record)["status"] == "ok"

        replay = run_cli("trajectory", "replay", "g-cli", "--json", env=cli_env)
        assert replay.returncode == 0
        payload = envelope(replay)
        assert payload["artifacts"] == ["build/app.js"]
        assert payload["states"][0]["command"] == "pnpm build"

    def test_branch_rooms_end_to_end_when_enabled(self, cli_env):
        env = {**cli_env, "VEYA_BRANCH_ROOMS": "1"}
        opened = run_cli("room", "open", "feat/w9", "--json", env=env)
        assert opened.returncode == 0, opened.stderr
        payload = envelope(opened)
        assert payload["room_name"] == "branch:feat/w9"
        assert payload["created"] is True

        again = run_cli("room", "open", "feat/w9", "--json", env=env)
        assert envelope(again)["created"] is False, "opening twice must reuse the room"

        published = run_cli(
            "room",
            "publish",
            "--branch",
            "feat/w9",
            "--group",
            payload["group_id"],
            "--decision",
            "squash",
            "--json",
            env=env,
        )
        assert published.returncode == 0, published.stderr
        assert "merge_decision" in envelope(published)["posted"]

        listed = run_cli("room", "list", "--json", env=env)
        assert [r["name"] for r in envelope(listed)["rooms"]] == ["branch:feat/w9"]

    def test_cli_gen_when_enabled(self, cli_env):
        result = run_cli(
            "cli-gen", "--repo", "/tmp/x", "--json", env={**cli_env, "VEYA_CLI_GEN": "1"}
        )
        # No executor is injected from the CLI, so the flow still completes with
        # placeholder artifacts per phase — and is always marked experimental.
        assert result.returncode == 0, result.stderr
        payload = envelope(result)
        assert payload["experimental"] is True
        assert len(payload["phases"]) == 7


# ==========================================================================
# REPL
# ==========================================================================


class TestRepl:
    def test_bare_invocation_enters_a_repl(self, cli_env):
        result = run_cli(env=cli_env, stdin="flags\nquit\n")
        assert result.returncode == 0, result.stderr
        assert "REPL" in result.stdout
        payloads = [
            json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")
        ]
        assert payloads and payloads[0]["status"] == "ok"

    def test_repl_reports_a_disabled_feature_without_crashing(self, cli_env):
        result = run_cli(env=cli_env, stdin="room open feat/x\nquit\n")
        assert result.returncode == 1
        payload = envelope(result)
        assert payload["error"]["code"] == "FEATURE_DISABLED"

    def test_repl_help_subcommand(self, cli_env):
        result = run_cli(env=cli_env, stdin="help room\nquit\n")
        assert result.returncode == 0
        assert "branch rooms" in result.stdout
        assert "open" in result.stdout and "publish" in result.stdout

    def test_repl_eof_exits_cleanly(self, cli_env):
        result = run_cli(env=cli_env, stdin="")
        assert result.returncode == 0
        assert "REPL" in result.stdout


# ==========================================================================
# --help self-describing
# ==========================================================================


class TestHelp:
    def test_root_help_lists_every_group(self, cli_env):
        result = run_cli("--help", env=cli_env)
        assert result.returncode == 0
        for group in (
            "flags",
            "chat",
            "room",
            "goal",
            "routine",
            "search",
            "audit",
            "trajectory",
            "acp",
            "cli-gen",
        ):
            assert group in result.stdout, group

    def test_help_documents_the_json_contract(self, cli_env):
        result = run_cli("--help", env=cli_env)
        assert "--json" in result.stdout
        assert "status" in result.stdout
        assert "REPL" in result.stdout

    def test_version(self, cli_env):
        result = run_cli("--version", env=cli_env)
        assert result.returncode == 0
        assert veya_cli.__version__ in result.stdout

    def test_unknown_command_is_a_usage_error(self, cli_env):
        result = run_cli("definitely-not-a-group", env=cli_env)
        assert result.returncode == veya_cli.EXIT_USAGE
        assert "invalid choice" in result.stderr

    def test_every_leaf_command_declares_its_json_schema(self):
        for row in veya_cli.command_index():
            assert row["path"], row
            assert row["schema"], f"{row['path']} does not document its JSON schema"
            assert row["help"], f"{row['path']} does not document what it does"


# ==========================================================================
# SKILL.md generation + sync (addendum D)
# ==========================================================================


class TestSkillSync:
    def test_skill_file_exists_with_frontmatter(self):
        assert SKILL.is_file(), "skills/veya-cli/SKILL.md must be checked in"
        text = SKILL.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        frontmatter = text.split("---", 2)[1]
        assert "name: veya-cli" in frontmatter
        assert "description:" in frontmatter

    def test_skill_md_is_in_sync_with_the_command_tree(self, tmp_path):
        """The generated SKILL.md must match the live argparse tree byte for byte."""
        out = tmp_path / "SKILL.md"
        generated = subprocess.run(
            [sys.executable, str(GENERATOR), "--out", str(out)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=180,
        )
        assert generated.returncode == 0, generated.stderr
        assert out.read_text(encoding="utf-8") == SKILL.read_text(encoding="utf-8"), (
            "skills/veya-cli/SKILL.md is stale; run scripts/gen_veya_cli_skill.py"
        )

    def test_generator_check_mode_passes(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        assert "in sync" in result.stdout

    def test_generator_detects_drift(self, tmp_path):
        stale = tmp_path / "SKILL.md"
        stale.write_text("# stale\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check", "--out", str(stale)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=180,
        )
        assert result.returncode == 1
        assert "out of sync" in result.stderr

    def test_skill_documents_every_command(self):
        text = SKILL.read_text(encoding="utf-8")
        for row in veya_cli.command_index():
            assert f"veya {row['path']}" in text, row["path"]

    def test_skill_carries_agent_guidance(self):
        text = SKILL.read_text(encoding="utf-8")
        assert "## How to use this skill" in text
        assert "## Examples" in text
        assert "FEATURE_DISABLED" in text
        assert "VEYA_TOKEN" in text
