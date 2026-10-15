from veya.remote.verify_command_policy import (
    configured_verify_commands,
    select_server_verify_command,
)


def test_verify_command_allowlist_is_fail_closed_for_untrusted_principals():
    env = {"VEYA_VERIFY_COMMAND_ALLOWLIST": '["python -m pytest -q", "ruff check ."]'}
    assert configured_verify_commands(env) == ("python -m pytest -q", "ruff check .")
    assert (
        select_server_verify_command("python -m pytest -q", owner_authorized=False, environ=env)
        is None
    )
    assert (
        select_server_verify_command("unlisted-command", owner_authorized=True, environ=env) is None
    )


def test_verify_command_requires_single_default_or_explicit_allowlisted_choice():
    assert (
        select_server_verify_command(
            None,
            owner_authorized=True,
            environ={"VEYA_VERIFY_COMMAND_ALLOWLIST": "python -m pytest -q"},
        )
        == "python -m pytest -q"
    )
    env = {"VEYA_VERIFY_COMMAND_ALLOWLIST": '["pytest -q", "ruff check ."]'}
    assert select_server_verify_command(None, owner_authorized=True, environ=env) is None
    assert (
        select_server_verify_command("ruff check .", owner_authorized=True, environ=env)
        == "ruff check ."
    )


def test_verify_command_invalid_configuration_denies():
    assert (
        configured_verify_commands({"VEYA_VERIFY_COMMAND_ALLOWLIST": '{"command":"pytest"}'}) == ()
    )
    assert (
        select_server_verify_command(
            None, owner_authorized=True, environ={"VEYA_VERIFY_COMMAND_ALLOWLIST": ""}
        )
        is None
    )
