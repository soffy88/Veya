"""Explicit sandbox policy profiles for local coding execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


class SandboxProfileError(ValueError):
    """An unknown or invalid coding sandbox profile was requested."""


@dataclass(frozen=True)
class SandboxMount:
    source: str
    target: str
    mode: Literal["ro", "rw"]

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SandboxProfile:
    id: str
    executor: Literal["local", "docker"]
    network: Literal["allowed", "denied", "configurable"]
    filesystem: Literal["workspace"]
    approvals: Literal["minimal", "required_for_write"]
    image: str | None = None
    mounts: tuple[SandboxMount, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["mounts"] = [mount.to_dict() for mount in self.mounts]
        return value


_PROFILES: dict[str, SandboxProfile] = {
    "local_trusted": SandboxProfile(
        id="local_trusted",
        executor="local",
        network="allowed",
        filesystem="workspace",
        approvals="minimal",
    ),
    "local_restricted": SandboxProfile(
        id="local_restricted",
        executor="local",
        network="denied",
        filesystem="workspace",
        approvals="required_for_write",
    ),
    "docker_python": SandboxProfile(
        id="docker_python",
        executor="docker",
        network="configurable",
        filesystem="workspace",
        approvals="required_for_write",
        image="veya/python-dev:latest",
        mounts=(SandboxMount("workspace", "/workspace", "rw"),),
    ),
    "docker_node": SandboxProfile(
        id="docker_node",
        executor="docker",
        network="configurable",
        filesystem="workspace",
        approvals="required_for_write",
        image="veya/node-dev:latest",
        mounts=(SandboxMount("workspace", "/workspace", "rw"),),
    ),
}


def get_sandbox_profile(profile: str | SandboxProfile) -> SandboxProfile:
    if isinstance(profile, SandboxProfile):
        return profile
    try:
        return _PROFILES[profile]
    except KeyError as exc:
        raise SandboxProfileError(
            f"unknown sandbox profile {profile!r}; choose from {', '.join(sorted(_PROFILES))}"
        ) from exc


def profile_for(profile: str | SandboxProfile) -> SandboxProfile:
    """Compatibility-friendly name for callers that resolve a profile by id."""
    return get_sandbox_profile(profile)


def list_sandbox_profiles() -> list[SandboxProfile]:
    return [_PROFILES[name] for name in sorted(_PROFILES)]


__all__ = [
    "SandboxMount",
    "SandboxProfile",
    "SandboxProfileError",
    "get_sandbox_profile",
    "list_sandbox_profiles",
    "profile_for",
]
