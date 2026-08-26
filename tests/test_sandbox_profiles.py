from __future__ import annotations

from veya.obase.sandbox import SandboxProfile, profile_config, profile_for


def test_sandbox_profiles_define_explicit_capabilities():
    read_only = profile_config(SandboxProfile.READ_ONLY)
    test = profile_config("test")
    networked = profile_config(SandboxProfile.NETWORKED)
    assert profile_for(read_only) is SandboxProfile.READ_ONLY
    assert read_only.allow_write is False and read_only.network_blocked is True
    assert test.allow_write is True and test.network_blocked is True
    assert networked.allow_write is False and networked.network_blocked is False
