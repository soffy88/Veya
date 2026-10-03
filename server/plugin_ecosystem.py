"""P2-03 Plugin Packaging Ecosystem — package combinations of capabilities.

Packages: Skills, Tools, MCP, Hooks, Workflows, Policies, Agents.
Installation MUST NOT grant undeclared capabilities.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class PluginComponent(StrEnum):
    SKILL = "SKILL"
    TOOL = "TOOL"
    MCP = "MCP"
    HOOK = "HOOK"
    WORKFLOW = "WORKFLOW"
    POLICY = "POLICY"
    AGENT = "AGENT"


class InstallStatus(StrEnum):
    PENDING = "PENDING"
    INSTALLED = "INSTALLED"
    FAILED = "FAILED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class PluginManifest:
    """Manifest for a plugin package."""

    plugin_id: str
    name: str
    version: str
    components: list[PluginComponent]
    declared_capabilities: list[str]
    entry_point: str
    checksum: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["components"] = [c.value for c in self.components]
        return data


@dataclass
class PluginPackage:
    """An installed plugin package."""

    package_id: str
    manifest: PluginManifest
    install_path: str
    status: InstallStatus = InstallStatus.PENDING
    granted_capabilities: list[str] = field(default_factory=list)
    installed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "manifest": self.manifest.to_dict(),
            "install_path": self.install_path,
            "status": self.status.value,
            "granted_capabilities": self.granted_capabilities,
            "installed_at": self.installed_at,
        }


def compute_checksum(path: str | Path) -> str:
    """Compute SHA-256 checksum of a plugin package."""
    p = Path(path)
    if not p.is_file():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()


def new_manifest(
    *,
    name: str,
    version: str,
    components: list[PluginComponent],
    declared_capabilities: list[str],
    entry_point: str,
) -> PluginManifest:
    """Create a new plugin manifest."""
    return PluginManifest(
        plugin_id=str(uuid.uuid4()),
        name=name,
        version=version,
        components=components,
        declared_capabilities=declared_capabilities,
        entry_point=entry_point,
    )


def install_package(
    manifest: PluginManifest,
    install_path: str | Path,
) -> PluginPackage:
    """Install a plugin package. Grants only declared capabilities."""
    pkg = PluginPackage(
        package_id=str(uuid.uuid4()),
        manifest=manifest,
        install_path=str(install_path),
        status=InstallStatus.INSTALLED,
        granted_capabilities=list(manifest.declared_capabilities),
    )
    return pkg


def verify_no_undeclared_capabilities(
    pkg: PluginPackage,
    actual_capabilities: list[str],
) -> tuple[bool, list[str]]:
    """Verify that installed package has no undeclared capabilities."""
    undeclared = [
        cap for cap in actual_capabilities
        if cap not in pkg.manifest.declared_capabilities
    ]
    return len(undeclared) == 0, undeclared
