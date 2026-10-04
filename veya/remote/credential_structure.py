"""Structural inspection of executor credentials — zero network cost.

`executor_registry._credential_present` answers "is a declared credential source
present", where a source is an env var or a file. On 2026-10-04 that question
returned True for all four executors claiming authentication, and three of them
failed on real contact:

    claude_code  .credentials.json  exists, but accessToken="", refreshToken="",
                 expiresAt=0 — no material at all          AUTH_FAILURE
    pi           auth.json           == {}                 PROVIDER_CONFIGURATION_FAILURE
    codex        auth.json           access_token present  PROVIDER_UNAVAILABLE
    opencode     auth.json           67-char api key       worked

Two of the three are not "the credential stopped working". They never had
anything to work with. File existence and credential existence are different
facts, and only the second one is what `authenticated` was being asked about.

This module settles the structural half: given the declared sources for an
executor, does usable material actually exist inside them? It reads nothing but
local files and the environment. It never makes a network call, and it never
returns `True` for validity — only a real probe can do that. So the verdict is
three-valued:

    no usable material anywhere  -> definitively unusable (False)
    material present, unprobed   -> unproven (None)

`present` still distinguishes unconfigured from configured-but-broken.

The distinction between the last two is the whole point. Treating "unproven" as
"unusable" would be wrong (opencode holds a working key and has never been
probed), and treating "unusable" as "unproven" is what produced the three false
positives.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CREDENTIAL_FILES",
    "CredentialInspection",
    "inspect",
    "material_in",
]

CREDENTIAL_FILES: dict[str, list[Path]] = {
    "codex": [Path("~/.codex/auth.json").expanduser()],
    "opencode": [Path("~/.local/share/opencode/auth.json").expanduser()],
    "claude_code": [Path("~/.claude/.credentials.json").expanduser()],
    "pi": [Path("~/.pi/agent/auth.json").expanduser()],
}

# Field names whose non-empty value counts as credential material. Compared
# against the lowercased, underscore-stripped key so that `accessToken`,
# `access_token` and `ACCESS_TOKEN` all match one rule.
_MATERIAL_FIELDS = ("key", "token", "secret", "password", "credential")

# Deliberately not material: `auth_mode` ("chatgpt") and `type` ("api") describe
# how a credential is used, not the credential. Matching them would make an
# empty file with a stray mode string look authenticated.


def _is_material_field(name: str) -> bool:
    normalized = name.lower().replace("_", "").replace("-", "")
    return any(field in normalized for field in _MATERIAL_FIELDS)


def material_in(payload: object) -> bool:
    """Whether parsed JSON holds a non-empty credential value anywhere inside.

    Structural rather than per-provider: every credential file observed so far
    nests its secret under some differently-spelled key (`key`, `accessToken`,
    `tokens.access_token`), and all of them normalize onto one rule. A file of
    `{}` returns False, which is the correct answer and the one the registry was
    previously unable to give.
    """
    if isinstance(payload, dict):
        for name, value in payload.items():
            if _is_material_field(str(name)) and isinstance(value, str) and value.strip():
                return True
            if material_in(value):
                return True
        return False
    if isinstance(payload, list):
        return any(material_in(item) for item in payload)
    return False


@dataclass(frozen=True)
class CredentialInspection:
    """What local state alone can say about an executor's credential.

    `source` names the env var or file that decided the verdict, and `detail`
    is safe to log: both describe shape, never value.
    """

    present: bool
    material_found: bool
    source: str | None = None
    detail: str = ""

    @property
    def unusable(self) -> bool:
        """True when no usable credential material exists locally.

        Covers the absent case as well as the present-but-empty one. An executor
        with no credential has no valid credential either, and returning "unknown"
        for that would understate what we actually know. Which of the two it is
        stays visible through `present`, so nothing is lost.
        """
        return not self.material_found


def _from_env(env_names: list[str]) -> CredentialInspection:
    for name in env_names:
        value = os.environ.get(name)
        if value is None:
            continue
        if value.strip():
            return CredentialInspection(True, True, name, f"env {name} carries material")
        return CredentialInspection(True, False, name, f"env {name} is defined but empty")
    return CredentialInspection(False, False, None, "no credential env var is set")


def _from_files(paths: list[Path]) -> CredentialInspection:
    for path in paths:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return CredentialInspection(
                True, False, str(path), "file exists but does not parse as JSON"
            )
        if material_in(payload):
            return CredentialInspection(True, True, str(path), "file holds credential material")
        return CredentialInspection(True, False, str(path), "file holds no credential material")
    return CredentialInspection(False, False, None, "no credential file exists")


def inspect(executor_id: str, env_names: list[str]) -> CredentialInspection:
    """Structurally inspect one executor's credential.

    Env wins over files: a live env var is the operator's most recent intent, and
    a stale file should not veto it.
    """
    env_verdict = _from_env(env_names)
    if env_verdict.present:
        return env_verdict
    return _from_files(CREDENTIAL_FILES.get(executor_id, []))
