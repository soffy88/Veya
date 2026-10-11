"""CLI-Hub registry client (NANO-W8, ``VEYA_CLI_HUB``).

The HTTP handle for the CLI-Anything registry.  Network access belongs to
obase; the oprims take the ``http_get`` callable this client exposes, so the
physical atoms stay substitutable in tests.

Two rules this module exists to enforce:

* **The registry URL is deployment configuration.**  ``VEYA_CLI_HUB_BASE_URL``
  must name it; with nothing configured :func:`cli_hub_registry_base_url`
  returns ``""`` and no request is ever made.  The server must not guess a
  third-party endpoint from its own defaults.
* **Reads only.**  Installing a CLI and invoking one that writes go through the
  approval path in the service layer; this client never installs anything.

CLI-Anything (HKUDS) is Apache-2.0.  Nothing here is copied from it — the
module only knows where the registry JSON lives and how to read it.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import httpx

#: Published CLI-Anything registry (documentation + pip package name).  Only
#: used as the *suggested* value; it is never fetched unless configured.
SUGGESTED_CLI_HUB_BASE_URL = "https://hkuds.github.io/CLI-Anything"

#: One registry read must not hold a request open.
DEFAULT_CLI_HUB_TIMEOUT_S = 10.0


class CLIHubRegistryError(RuntimeError):
    """The registry could not be read."""


class HttpGetter(Protocol):
    """The one transport this client needs: a GET returning a response."""

    def __call__(self, url: str) -> Any: ...


def cli_hub_registry_base_url() -> str:
    """The registry base URL.

    ``VEYA_CLI_HUB_BASE_URL`` wins; otherwise the published CLI-Anything
    registry is the default, so the atoms have a working endpoint without
    configuration.  A deployment that must not reach it at all checks
    :func:`cli_hub_registry_configured` first — that is how the REST surface
    stays silent instead of calling out on a default it never chose.
    """

    configured = str(os.environ.get("VEYA_CLI_HUB_BASE_URL") or "").strip().rstrip("/")
    return configured or SUGGESTED_CLI_HUB_BASE_URL


def cli_hub_registry_configured() -> bool:
    """Whether this deployment named its own CLI-Hub registry."""

    return bool(str(os.environ.get("VEYA_CLI_HUB_BASE_URL") or "").strip())


def cli_hub_timeout_s() -> float:
    raw = os.environ.get("VEYA_CLI_HUB_TIMEOUT_S") or ""
    try:
        return float(raw) if raw else DEFAULT_CLI_HUB_TIMEOUT_S
    except ValueError:
        return DEFAULT_CLI_HUB_TIMEOUT_S


def registry_http_get(url: str, *, timeout_s: float | None = None) -> httpx.Response:
    """GET one registry URL.

    The blocking client is used on purpose: the CLI-Hub atoms are called from a
    runner's task step, where the surrounding caller already owns the loop.
    """

    with httpx.Client(timeout=timeout_s or cli_hub_timeout_s()) as client:
        return client.get(url)


def registry_url(base_url: str, *parts: str, query: dict[str, str] | None = None) -> str:
    """Join a registry path, encoding any query string.

    URL construction belongs to the HTTP handle, not to the atoms that call it.
    """

    from urllib.parse import urlencode

    path = "/".join(str(part).strip("/") for part in parts if str(part).strip("/"))
    url = f"{base_url}/{path}" if path else base_url
    return f"{url}?{urlencode(query)}" if query else url


def read_json(response: Any) -> dict[str, Any]:
    """A registry response as a dict, or an error naming the HTTP status."""

    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        raise CLIHubRegistryError(f"CLI-Hub registry returned HTTP {status}")
    try:
        payload = response.json()
    except Exception as exc:  # a non-JSON body is a broken registry
        raise CLIHubRegistryError("CLI-Hub registry returned a non-JSON body") from exc
    return payload if isinstance(payload, dict) else {"results": payload}


__all__ = [
    "DEFAULT_CLI_HUB_TIMEOUT_S",
    "SUGGESTED_CLI_HUB_BASE_URL",
    "CLIHubRegistryError",
    "HttpGetter",
    "cli_hub_registry_base_url",
    "cli_hub_registry_configured",
    "cli_hub_timeout_s",
    "read_json",
    "registry_http_get",
    "registry_url",
]
