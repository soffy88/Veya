"""Host-side sandbox execution daemon for veya (srt backend).

Why this exists, in one paragraph: the production veya-backend container
cannot run srt at all (bubblewrap cannot create namespaces inside Docker
under the host kernel's user-namespace restriction, in any non-privileged
configuration). The daemon runs on the HOST, where srt works, and exposes
ProcessSandbox execution over a Unix socket that is bind-mounted into the
container (read-only directory mount, so the container cannot unlink the
socket). The backend's sandbox calls then run srt on the host instead of
failing inside the container.

Trust boundary (read before changing this):
- The daemon runs ONLY srt-sandboxed commands (ProcessSandbox with
  VEYA_SANDBOX_BACKEND=srt). It never executes raw commands. A client with
  socket access gets sandboxed execution, not a host shell.
- Socket file is 0600, directory 0700, both owned by the daemon user
  (soffy). The container runs as the same uid, so it can connect.
- The socket directory lives under ~/.veya, which veya's srt settings
  mask (denyRead) inside every sandbox: a sandboxed child sees an empty
  file at the socket path and cannot connect back to the daemon. That
  masking is what stops a sandboxed child from using the daemon as an
  escape hatch. Do NOT move the socket outside a masked directory without
  adding the new path to _SRT_CREDENTIAL_DENY_READ.
- No token auth: any uid-1000 process can connect, which is exactly the
  backend's own trust level (the container already mounts host paths as
  the same uid). A token would not raise the bar against the threat that
  matters (the sandboxed child, handled by masking).

Protocol: 4-byte big-endian length + JSON object.
Request: {"version": 1, "kind": "execute"|"execute_args"|"run_script",
          "command": str | list[str], "script": str,
          "config": {memory_limit, cpu_limit, time_limit, network_blocked,
                     allowed_domains, allow_write, env_extra}}
Response: {"exit_code","stdout","stderr","command","duration",
           "sandbox_backend": "srt-remote"} or {"error": ...}.
Limits: request <= 8 MiB; stdout/stderr truncated to 4 MiB each.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import socketserver
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from veya.obase.sandbox import ProcessSandbox, SandboxConfig  # noqa: E402

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_CHARS = 4_000_000
MAX_TIME_LIMIT = 900

log = logging.getLogger("sandbox-daemon")


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n...[truncated by sandbox daemon]"
    return text


def _config_from_payload(raw: dict) -> SandboxConfig:
    if raw.get("working_dir"):
        raise ValueError("working_dir is not supported for remote execution")
    allowed = raw.get("allowed_domains")
    if allowed is not None and not isinstance(allowed, list):
        raise ValueError("allowed_domains must be a list or null")
    env_extra = raw.get("env_extra")
    if env_extra is not None and not isinstance(env_extra, dict):
        raise ValueError("env_extra must be an object or null")
    time_limit = raw.get("time_limit")
    if time_limit is not None:
        time_limit = min(float(time_limit), MAX_TIME_LIMIT)
    return SandboxConfig(
        memory_limit=raw.get("memory_limit"),
        cpu_limit=raw.get("cpu_limit"),
        time_limit=time_limit,
        network_blocked=bool(raw.get("network_blocked", True)),
        allowed_domains=allowed,
        allow_write=bool(raw.get("allow_write", False)),
        env_extra=env_extra,
        audit_enabled=False,
    )


def _run_request(payload: dict) -> dict:
    if payload.get("version") != 1:
        raise ValueError("unsupported protocol version")
    kind = payload.get("kind")
    config = _config_from_payload(payload.get("config") or {})
    sandbox = ProcessSandbox(config)

    async def _run():
        if kind == "execute":
            return await sandbox.execute(payload.get("command") or "")
        if kind == "execute_args":
            return await sandbox.execute_args(payload.get("command") or [])
        if kind == "run_script":
            return await sandbox.run_script(payload.get("script") or "")
        raise ValueError(f"unknown kind: {kind!r}")

    result = asyncio.run(_run())
    # Fail closed: the daemon exists to serve srt. If the local run did not
    # actually use srt (e.g. probe fallback engaged), do not relabel it.
    if result.get("sandbox_backend") != "srt":
        raise RuntimeError(
            f"daemon local backend is {result.get('sandbox_backend')!r}, not srt"
        )
    result["sandbox_backend"] = "srt-remote"
    result["stdout"] = _truncate(result.get("stdout") or "")
    result["stderr"] = _truncate(result.get("stderr") or "")
    return result


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        start = time.time()
        try:
            header = self._recvall(4)
            if len(header) < 4:
                return
            (n,) = struct.unpack(">I", header)
            if n > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            data = self._recvall(n)
            payload = json.loads(data.decode("utf-8"))
            result = _run_request(payload)
            log.info(
                "kind=%s exit=%s dur=%.2f",
                payload.get("kind"),
                result.get("exit_code"),
                time.time() - start,
            )
        except Exception as exc:  # noqa: BLE001 - short error only, no traceback leak
            result = {"error": str(exc)[:500]}
            log.warning("request failed: %s", result["error"])
        raw = json.dumps(result).encode("utf-8")
        if len(raw) > MAX_RESPONSE_BYTES:
            raw = json.dumps({"error": "response too large"}).encode("utf-8")
        with contextlib.suppress(OSError):
            self.request.sendall(struct.pack(">I", len(raw)) + raw)

    def _recvall(self, n: int) -> bytes:
        chunks: list[bytes] = []
        remaining = n
        while remaining > 0:
            chunk = self.request.recv(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="veya sandbox execution daemon (srt on host)")
    parser.add_argument("--socket-dir", default=os.path.expanduser("~/.veya/sandbox-sockets"))
    parser.add_argument("--socket-name", default="sandbox-exec.sock")
    args = parser.parse_args()

    os.environ.setdefault("VEYA_SANDBOX_BACKEND", "srt")
    socket_dir = args.socket_dir
    os.makedirs(socket_dir, mode=0o700, exist_ok=True)
    socket_path = os.path.join(socket_dir, args.socket_name)

    # Startup probe: refuse to serve if srt cannot actually sandbox here.
    # (A daemon that silently served legacy execution under an srt label
    # would be worse than no daemon.)
    from veya.obase.sandbox import _srt_binary, _srt_probe

    binary = _srt_binary()
    if binary is None or not _srt_probe(binary):
        print("FATAL: srt probe failed; refusing to start (check VEYA_SRT_BIN and srt deps)",
              file=sys.stderr)
        return 1

    with contextlib.suppress(OSError):
        os.unlink(socket_path)
    server = _Server(socket_path, _Handler)
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass
    log.info("listening on %s (srt binary: %s)", socket_path, binary)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(OSError):
            server.server_close()
        with contextlib.suppress(OSError):
            os.unlink(socket_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
