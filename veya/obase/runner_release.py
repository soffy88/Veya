"""Runner release manifest and signing helpers (NANO-W8).

The crypto a signed runner release needs, in the layer that owns external
concerns: base64 decoding of a pinned public key, manifest shape, and version
comparison.  The signature check itself lives next to the download atom, in
``veya.oprim.runner_ops``.

A pinned ed25519 public key is configured, not fetched: whoever controls the
manifest endpoint must not also control what verifies it.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

#: Base64 in a config file is routinely wrapped or indented, so decoding
#: ignores whitespace. It does *not* silently drop data characters: a key that
#: decodes to something other than 32 bytes is rejected rather than repaired,
#: because a "repaired" key verifies nothing.
_BASE64_JUNK = re.compile(r"\s+")
_VERSION_PART = re.compile(r"^\d+$")

#: Env vars a deployment pins for the runner's own self-update.
MANIFEST_URL_ENV = "VEYA_RUNNER_RELEASE_MANIFEST_URL"
PUBLIC_KEY_ENV = "VEYA_RUNNER_RELEASE_PUBLIC_KEY_B64"
STAGING_DIR_ENV = "VEYA_RUNNER_UPDATE_DIR"


class RunnerReleaseError(RuntimeError):
    """The release manifest or its signature could not be used."""


#: An ed25519 public key is exactly 32 bytes.
ED25519_PUBLIC_KEY_BYTES = 32


def decode_ed25519_public_key(value: str) -> bytes:
    """Decode a pinned ed25519 public key.

    Accepts wrapping and the urlsafe alphabet.  Raises
    :class:`RunnerReleaseError` unless the result is exactly 32 bytes: a key
    of any other length can verify nothing, and pretending otherwise would turn
    a typo into a silent refusal to update.
    """

    normalised = _BASE64_JUNK.sub("", str(value or "")).replace("-", "+").replace("_", "/")
    if not normalised:
        raise RunnerReleaseError("the pinned public key is empty")
    padded = normalised + "=" * (-len(normalised) % 4)
    try:
        key = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RunnerReleaseError("the pinned public key is not valid base64") from exc
    if len(key) != ED25519_PUBLIC_KEY_BYTES:
        raise RunnerReleaseError(
            f"the pinned public key is {len(key)} bytes, an ed25519 key is "
            f"{ED25519_PUBLIC_KEY_BYTES}"
        )
    return key


def parse_release_manifest(payload: Any) -> dict[str, Any]:
    """The fields a self-update check needs from a release manifest.

    ``url`` and ``signature`` are only required when an update is actually
    taken: "the latest release is what you already run" is a complete answer,
    and a manifest that always carries a download URL would keep publishing one
    for a release nobody should download.
    """

    if not isinstance(payload, dict):
        raise RunnerReleaseError("the release manifest is not a JSON object")
    version = str(payload.get("version") or "").strip()
    if not version:
        raise RunnerReleaseError("the release manifest has no version")
    return {
        "version": version,
        "url": str(payload.get("url") or "").strip(),
        "signature": str(payload.get("signature") or "").strip(),
    }


def is_newer_version(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is strictly newer than ``current``.

    A dotted numeric version is compared part by part.  Anything else (a
    pre-release tag, a build hash) is treated as *not* newer: an unorderable
    version must not become an automatic upgrade.
    """

    def parts(value: str) -> list[int] | None:
        chunks = str(value or "").strip().lstrip("v").split(".")
        if not chunks or not all(_VERSION_PART.match(chunk) for chunk in chunks):
            return None
        return [int(chunk) for chunk in chunks]

    new_parts = parts(candidate)
    old_parts = parts(current)
    if new_parts is None or old_parts is None:
        return False
    width = max(len(new_parts), len(old_parts))
    new_parts += [0] * (width - len(new_parts))
    old_parts += [0] * (width - len(old_parts))
    return new_parts > old_parts


__all__ = [
    "ED25519_PUBLIC_KEY_BYTES",
    "MANIFEST_URL_ENV",
    "PUBLIC_KEY_ENV",
    "STAGING_DIR_ENV",
    "RunnerReleaseError",
    "decode_ed25519_public_key",
    "is_newer_version",
    "parse_release_manifest",
]
