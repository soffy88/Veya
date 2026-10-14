"""Shared offline fakes for the W17 tests (no ssh, no network).

``LocalExecRunner`` stands in for ``process.run_capture``: it checks the ssh
argv shape, strips everything up to ``--`` and runs the remote command string
with a local ``bash -c`` inside the test's tmp dir — so the remote scripts are
exercised for real without any ssh connection.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any

from server.remote_control.process import CaptureResult


@dataclass
class RecordingRunner:
    """Records calls; replies with a canned result."""

    returncode: int | None = 0
    stdout: bytes = b"Linux\n"
    stderr: bytes = b""
    timed_out: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, argv, *, timeout_s, input_bytes=None):
        self.calls.append({"argv": list(argv), "timeout_s": timeout_s, "input": input_bytes})
        return CaptureResult(argv=list(argv), returncode=self.returncode, stdout=self.stdout,
                             stderr=self.stderr, timed_out=self.timed_out)


@dataclass
class LocalExecRunner:
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv, *, timeout_s, input_bytes=None):
        argv = list(argv)
        assert argv[0] == "ssh" and "-o" in argv and "BatchMode=yes" in argv
        idx = argv.index("--")
        assert len(argv) == idx + 2, "remote command must be ONE quoted string after --"
        remote = argv[idx + 1]
        self.calls.append(argv)
        try:
            done = subprocess.run(["bash", "-c", remote], input=input_bytes or b"",
                                  capture_output=True, timeout=min(timeout_s, 30), check=False)
        except subprocess.TimeoutExpired as exc:
            return CaptureResult(argv=argv, returncode=None, stdout=exc.stdout or b"",
                                 stderr=exc.stderr or b"", timed_out=True)
        return CaptureResult(argv=argv, returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)
