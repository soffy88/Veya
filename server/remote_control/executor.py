"""W13 executor — render and launch a remote agent command.

The command template is operator configuration, never model input. It is split
with :func:`shlex.split` (no shell, no string concatenation) and the two
placeholders are substituted as **one argv element each**, so a task containing
spaces, quotes or semicolons cannot become a second command.

Two transports ship:

* ``local`` — run the rendered argv on this machine (the default; a host may
  be registered for a machine that *is* this box).
* ``ssh``   — ``ssh -o BatchMode=yes <host_id> -- <argv...>``.

A third transport (the existing HTTP runner client in
``server/bot_team/runner_client.py``) is a documented seam, not implemented in
W13 — see ``W13_REPORT.md`` "what is left".

Process spawning itself is injected (:data:`Spawner`) so the suite never forks
anything; the concrete default is :func:`server.remote_control.process.spawn`.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable, Sequence

from server.remote_control.hosts import RemoteHost

#: Placeholders substituted into an operator command template.
TASK_PLACEHOLDER = "{task}"
WORKSPACE_PLACEHOLDER = "{workspace}"

#: Transports a host may declare.
TRANSPORTS: tuple[str, ...] = ("local", "ssh")


class CommandRejected(ValueError):
    """A rendered command was refused before anything was started."""


@dataclass(frozen=True)
class LaunchResult:
    """What happened when a command was launched."""

    host_id: str
    agent_label: str
    argv: tuple[str, ...]
    pid: int | None
    returncode: int | None
    transport: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "agent_label": self.agent_label,
            "argv": list(self.argv),
            "pid": self.pid,
            "returncode": self.returncode,
            "transport": self.transport,
            "ok": self.ok,
        }


#: Injected process starter: argv + cwd in, handle out.
Spawner = Callable[..., object]


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Refuse anything that could smuggle a second command or NUL byte."""

    cleaned: list[str] = []
    for item in argv:
        text = str(item)
        if "\n" in text or "\r" in text or "\0" in text:
            raise CommandRejected("command arguments may not contain newlines or NUL")
        if text == "":
            raise CommandRejected("command arguments may not be empty")
        cleaned.append(text)
    if not cleaned:
        raise CommandRejected("command template rendered to nothing")
    return tuple(cleaned)


def render_command(
    template: str,
    *,
    task: str,
    workspace: str = "",
) -> tuple[str, ...]:
    """Expand ``{task}`` / ``{workspace}`` and split with ``shlex``.

    Placeholders are replaced with an escape-safe placeholder token first, then
    expanded, so a task string can never be re-split by the surrounding
    template's quoting.
    """

    raw = str(template or "").strip()
    if not raw:
        raise CommandRejected("empty command template")
    text = str(task or "")
    if not text.strip():
        raise CommandRejected("task text is required")

    # Shlex sees a unique sentinel, not the task text, so quoting in the task
    # cannot terminate an argv element early.
    sentinel = "\x01TASK\x01"
    sentinel_ws = "\x01WORKSPACE\x01"
    if sentinel in text or sentinel_ws in text:
        raise CommandRejected("task text contains a reserved control sequence")
    expanded = raw.replace(TASK_PLACEHOLDER, sentinel)
    expanded = expanded.replace(WORKSPACE_PLACEHOLDER, sentinel_ws)

    argv = validate_argv(shlex.split(expanded))
    argv = [item.replace(sentinel, text).replace(sentinel_ws, workspace) for item in argv]
    return validate_argv(argv)


def ssh_argv(host: RemoteHost, argv: Sequence[str]) -> tuple[str, ...]:
    """Wrap a rendered argv in a non-interactive ssh invocation."""

    target = str(host.tailnet_ip or host.host_id or "").strip()
    if not target:
        raise CommandRejected("ssh transport needs a tailnet_ip or host_id")
    return validate_argv(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            target,
            "--",
            *[str(item) for item in argv],
        ]
    )


def launch(
    host: RemoteHost,
    *,
    task: str,
    workspace: str = "",
    transport: str = "local",
    spawner: Spawner | None = None,
) -> LaunchResult:
    """Render and start one executor command on ``host``.

    Raises :class:`CommandRejected` before any process is started when the
    template or arguments are unsafe; ``ProcessUnavailable`` when the binary
    is missing.
    """

    if transport not in TRANSPORTS:
        raise CommandRejected(f"unknown transport: {transport}")
    argv = render_command(host.command_template(), task=task, workspace=workspace)
    effective = ssh_argv(host, argv) if transport == "ssh" else argv

    start: Spawner = spawner if spawner is not None else _default_spawner
    handle = start(list(effective), cwd=workspace)
    return LaunchResult(
        host_id=host.host_id,
        agent_label=host.agent_label,
        argv=effective,
        pid=getattr(handle, "pid", None),
        returncode=getattr(handle, "returncode", None),
        transport=transport,
    )


def _default_spawner(argv: list[str], *, cwd: str = ""):
    from server.remote_control import process

    return process.spawn(argv, cwd=cwd)


__all__ = [
    "TASK_PLACEHOLDER",
    "TRANSPORTS",
    "WORKSPACE_PLACEHOLDER",
    "CommandRejected",
    "LaunchResult",
    "Spawner",
    "launch",
    "render_command",
    "ssh_argv",
    "validate_argv",
]
