# 3O-IO-ALLOW — server/remote_control/backend.py
# 理由：W17 规格 §16 指定的第二处物理 IO：ssh_upload / ssh_download 在**本机写根**
# （tool_registry._resolve_write_path 判定的 VEYA_WRITE_ROOT）读/写一个文件。
# 远端动作一律经 ssh.run_remote → process.run_capture 这一个进程 seam。
"""W17 — execution backends: ``local`` and ``ssh``.

``SshBackend`` turns each file / command action into ONE non-interactive ssh
call whose remote side is a fixed script with positional arguments, so model
text is only ever data (``"$1"``), never part of a command line.

Path policy (spec §6.3):
* writes must land inside ``host.write_root`` (empty → read-only host);
  checked locally (normalised string prefix) **and** remotely (``realpath -m``);
* reads are limited to ``write_root`` / ``default_cwd`` / ``workspace_path``
  unless ``host.metadata.read_scope == "host"``.

``LocalBackend`` delegates to the existing canonical tool helpers in
``server.tool_registry`` unchanged (it adds no behaviour of its own).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from server.remote_control import ssh as ssh_mod

MAX_TRANSFER_BYTES = 50 * 1024 * 1024
RUN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RUN_DIR = ".veya/run"
_STATUS_MARK = "__VEYA__"

_WRITE_SCRIPT = (
    'set -e; p="$1"; ow="$2"; d=$(dirname -- "$p"); mkdir -p -- "$d"; '
    'if [ "$ow" != 1 ] && [ -e "$p" ]; then echo "file exists" >&2; exit 17; fi; '
    't="$p.veya-tmp.$$"; cat > "$t"; mv -f -- "$t" "$p"; wc -c < "$p"'
)
_LIST_SCRIPT = 'cd -- "$1" && find . -mindepth 1 -maxdepth 1 -printf "%y\\t%s\\t%f\\n" | sort -k3 | head -n "$2"'
_GREP_SCRIPT = (
    'pat="$1"; root="$2"; n="$3"; g="$4"; '
    "if command -v rg >/dev/null 2>&1; then "
    '  if [ -n "$g" ]; then rg --no-heading -n -m "$n" -g "$g" -e "$pat" -- "$root"; '
    '  else rg --no-heading -n -m "$n" -e "$pat" -- "$root"; fi; '
    "else "
    '  if [ -n "$g" ]; then grep -rnE --include="$g" -m "$n" -e "$pat" -- "$root"; '
    '  else grep -rnE -m "$n" -e "$pat" -- "$root"; fi; '
    'fi | head -n "$n"'
)
_RUN_SCRIPT = 'cd -- "$1" || exit 97; shift; exec "$@"'
_BG_START_SCRIPT = (
    'd="$1"; n="$2"; cwd="$3"; shift 3; mkdir -p -- "$d" || exit 98; '
    'if [ -e "$d/$n.pid" ] && [ ! -e "$d/$n.exit" ] && kill -0 "$(cat "$d/$n.pid")" 2>/dev/null; '
    'then echo "run already active" >&2; exit 16; fi; '
    'rm -f -- "$d/$n.exit" "$d/$n.log"; cd -- "$cwd" || exit 97; '
    'inner=\'"$@" > "$0.log" 2>&1; echo $? > "$0.exit"\'; '
    "if command -v setsid >/dev/null 2>&1; then "
    '  setsid nohup bash -c "$inner" "$d/$n" "$@" < /dev/null > /dev/null 2>&1 & '
    'else nohup bash -c "$inner" "$d/$n" "$@" < /dev/null > /dev/null 2>&1 & fi; '
    'echo $! > "$d/$n.pid"; echo $!'
)
_BG_STATUS_SCRIPT = (
    'd="$1"; n="$2"; m="$3"; '
    'if [ ! -e "$d/$n.pid" ]; then echo "__VEYA_NONE__"; exit 0; fi; '
    'pid=$(cat "$d/$n.pid"); alive=0; kill -0 "$pid" 2>/dev/null && alive=1; '
    'code=""; [ -e "$d/$n.exit" ] && code=$(cat "$d/$n.exit"); '
    'printf "__VEYA__ pid=%s alive=%s exit=%s\\n" "$pid" "$alive" "$code"; '
    'tail -c "$m" -- "$d/$n.log" 2>/dev/null; true'
)
_BG_KILL_SCRIPT = (
    'd="$1"; n="$2"; [ -e "$d/$n.pid" ] || { echo "no such run" >&2; exit 3; }; '
    'pid=$(cat "$d/$n.pid"); '
    'kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null; '
    '[ -e "$d/$n.exit" ] || echo 143 > "$d/$n.exit"; echo killed'
)
_BG_LIST_SCRIPT = (
    'd="$1"; [ -d "$d" ] || exit 0; cd -- "$d" && for f in *.pid; do '
    '[ -e "$f" ] || continue; n="${f%.pid}"; c=""; [ -e "$n.exit" ] && c=$(cat "$n.exit"); '
    'printf "%s\\t%s\\n" "$n" "$c"; done'
)


class BackendError(RuntimeError):
    """A backend action was refused or failed. The message is safe to show."""


@dataclass
class ExecResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
        }


@dataclass
class BgHandle:
    name: str
    host: str
    pid: str
    log_path: str
    exit_path: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class ExecBackend(Protocol):
    name: str

    def read_text(self, path: str, *, max_bytes: int) -> str: ...
    def write_text(self, path: str, content: str, *, overwrite: bool) -> str: ...
    def list_dir(self, path: str) -> list[dict[str, Any]]: ...
    def grep(self, pattern: str, *, root: str, glob: str = "", max_results: int = 100) -> str: ...


# ── local ──────────────────────────────────────────────────────────────────


class LocalBackend:
    """Today's local behaviour, delegated to the canonical helpers unchanged."""

    name = "local"

    def read_text(self, path: str, *, max_bytes: int) -> str:
        from server.tool_registry import _resolve_path

        data = _resolve_path(path).read_bytes()
        return data[:max_bytes].decode("utf-8", "replace")

    def write_text(self, path: str, content: str, *, overwrite: bool) -> str:
        from server.tool_registry import _tool_write_file

        return _tool_write_file(path, content, overwrite=overwrite)

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        from server.tool_registry import _resolve_path

        root = _resolve_path(path)
        return [
            {"name": p.name, "type": "d" if p.is_dir() else "f", "size": p.stat().st_size}
            for p in sorted(root.iterdir(), key=lambda item: item.name)
        ]

    def grep(self, pattern: str, *, root: str, glob: str = "", max_results: int = 100) -> str:
        from server.tool_registry import _tool_grep

        return _tool_grep(pattern, glob=glob or None, root=root or None, max_results=max_results)


# ── ssh ────────────────────────────────────────────────────────────────────


def _clean_path(path: str) -> str:
    raw = str(path or "")
    if not raw or any(ch in raw for ch in ("\x00", "\n", "\r")):
        raise BackendError("path must be a non-empty single line")
    return raw


def _within(path: str, root: str) -> bool:
    root = posixpath.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


class SshBackend:
    """One registered host, driven over non-interactive ssh."""

    name = "ssh"

    def __init__(self, host: Any, *, runner: ssh_mod.Runner | None = None, key_lookup=None) -> None:
        self.host = host
        self._runner = runner
        self._key_lookup = key_lookup

    # ── plumbing ──
    def _remote(
        self,
        argv: list[str],
        *,
        timeout_s: float | None = None,
        input_bytes: bytes | None = None,
        output_limit: int | None = None,
    ) -> ssh_mod.RemoteResult:
        try:
            return ssh_mod.run_remote(
                self.host,
                argv,
                timeout_s=timeout_s,
                input_bytes=input_bytes,
                runner=self._runner,
                key_lookup=self._key_lookup,
                output_limit=output_limit,
            )
        except ssh_mod.SshError as exc:
            raise BackendError(str(exc)) from exc

    def _script(self, script: str, *args: str, **kw: Any) -> ssh_mod.RemoteResult:
        return self._remote(["bash", "-c", script, "veya-w17", *[str(a) for a in args]], **kw)

    @staticmethod
    def _fail(result: ssh_mod.RemoteResult, what: str) -> BackendError:
        if result.timed_out:
            return BackendError(f"{what}: timed out")
        detail = (result.stderr or result.stdout).strip()[:300]
        return BackendError(f"{what} failed (exit {result.returncode}): {detail}")

    # ── path policy ──
    @property
    def write_root(self) -> str:
        return posixpath.normpath(self.host.write_root) if self.host.write_root else ""

    def default_cwd(self) -> str:
        return str(self.host.default_cwd or self.host.write_root or self.host.workspace_path or "")

    def read_roots(self) -> list[str]:
        return [
            r for r in (self.host.write_root, self.host.default_cwd, self.host.workspace_path) if r
        ]

    def _absolute(self, path: str) -> str:
        raw = _clean_path(path)
        if not raw.startswith("/"):
            base = self.default_cwd()
            if not base:
                raise BackendError("relative path needs a host default_cwd or write_root")
            raw = posixpath.join(base, raw)
        return posixpath.normpath(raw)

    def _realpath(self, *paths: str) -> list[str]:
        result = self._remote(["realpath", "-m", "--", *paths], timeout_s=15.0)
        if not result.ok:
            raise self._fail(result, "realpath")
        lines = result.stdout.splitlines()
        if len(lines) != len(paths):
            raise BackendError("realpath returned an unexpected answer")
        return [posixpath.normpath(line) for line in lines]

    def check_write_path(self, path: str) -> str:
        if not self.write_root:
            raise BackendError(f"host {self.host.host_id} has no write_root: it is read-only")
        target = self._absolute(path)
        if not _within(target, self.write_root):
            raise BackendError(f"path {target} is outside the host write_root {self.write_root}")
        real, real_root = self._realpath(target, self.write_root)
        if not _within(real, real_root):
            raise BackendError(f"path {target} resolves outside the host write_root")
        return target

    def check_read_path(self, path: str) -> str:
        target = self._absolute(path)
        if str((self.host.metadata or {}).get("read_scope") or "workspace") == "host":
            return target
        roots = self.read_roots()
        if not roots:
            raise BackendError("host has no write_root/default_cwd to read from")
        if not any(_within(target, posixpath.normpath(r)) for r in roots):
            raise BackendError(f"path {target} is outside the host's readable roots")
        reals = self._realpath(target, *roots)
        if not any(_within(reals[0], r) for r in reals[1:]):
            raise BackendError(f"path {target} resolves outside the host's readable roots")
        return target

    # ── file actions ──
    def read_text(self, path: str, *, max_bytes: int = 200_000) -> str:
        target = self.check_read_path(path)
        result = self._remote(
            ["head", "-c", str(int(max_bytes)), "--", target], output_limit=max_bytes
        )
        if not result.ok:
            raise self._fail(result, "read")
        return result.stdout

    def write_text(self, path: str, content: str, *, overwrite: bool = True) -> str:
        target = self.check_write_path(path)
        data = str(content).encode("utf-8")
        result = self._script(_WRITE_SCRIPT, target, "1" if overwrite else "0", input_bytes=data)
        if not result.ok:
            raise self._fail(result, "write")
        return f"wrote {len(data)} bytes to {self.host.host_id}:{target}"

    def replace_text(self, path: str, old: str, new: str) -> str:
        """Exact, unique string replacement (read → replace → atomic write)."""
        target = self.check_write_path(path)
        result = self._remote(["cat", "--", target], output_limit=MAX_TRANSFER_BYTES)
        if not result.ok:
            raise self._fail(result, "read")
        text = result.raw_stdout.decode("utf-8")
        count = text.count(old) if old else 0
        if count != 1:
            raise BackendError(f"old text must match exactly once (matched {count})")
        return self.write_text(target, text.replace(old, new, 1), overwrite=True)

    def list_dir(self, path: str = "") -> list[dict[str, Any]]:
        target = self.check_read_path(path or self.default_cwd() or ".")
        result = self._script(_LIST_SCRIPT, target, "500")
        if not result.ok:
            raise self._fail(result, "list")
        rows = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                rows.append({"name": parts[2], "type": parts[0], "size": int(parts[1] or 0)})
        return rows

    def grep(self, pattern: str, *, root: str = "", glob: str = "", max_results: int = 100) -> str:
        if not pattern or "\x00" in pattern or "\n" in pattern:
            raise BackendError("pattern must be a non-empty single line")
        target = self.check_read_path(root or self.default_cwd() or ".")
        n = max(1, min(int(max_results), 1000))
        result = self._script(_GREP_SCRIPT, pattern, target, str(n), glob)
        if result.timed_out or result.returncode not in (0, 1):
            raise self._fail(result, "grep")
        return result.stdout

    # ── commands ──
    def run(self, command: str, *, cwd: str = "", timeout_s: float | None = None) -> ExecResult:
        argv = classify_or_refuse(
            command, workspace_root=self.write_root or self.default_cwd() or "/"
        )
        where = self.check_read_path(cwd) if cwd else self.default_cwd()
        if not where:
            raise BackendError("command needs a cwd, a host default_cwd or a write_root")
        result = self._script(_RUN_SCRIPT, where, *argv, timeout_s=timeout_s)
        return ExecResult(result.returncode, result.stdout, result.stderr, result.timed_out)

    # ── background runs ──
    def _run_dir(self) -> str:
        if not self.write_root:
            raise BackendError("background runs need a host write_root")
        return posixpath.join(self.write_root, RUN_DIR)

    @staticmethod
    def _check_name(name: str) -> str:
        if not RUN_NAME_RE.match(str(name or "")):
            raise BackendError("run name must match ^[a-z0-9][a-z0-9_-]{0,39}$")
        return name

    def spawn_bg(self, command: str, *, name: str, cwd: str = "") -> BgHandle:
        self._check_name(name)
        argv = classify_or_refuse(command, workspace_root=self.write_root or "/")
        run_dir = self._run_dir()
        where = self.check_read_path(cwd) if cwd else self.default_cwd()
        result = self._script(_BG_START_SCRIPT, run_dir, name, where, *argv, timeout_s=20.0)
        if not result.ok:
            raise self._fail(result, "background start")
        return BgHandle(
            name=name,
            host=self.host.host_id,
            pid=result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "",
            log_path=f"{run_dir}/{name}.log",
            exit_path=f"{run_dir}/{name}.exit",
        )

    def bg_status(self, name: str, *, tail_bytes: int = 8000) -> dict[str, Any]:
        self._check_name(name)
        result = self._script(
            _BG_STATUS_SCRIPT, self._run_dir(), name, str(int(tail_bytes)), timeout_s=15.0
        )
        if not result.ok:
            raise self._fail(result, "background status")
        lines = result.stdout.splitlines()
        if not lines or lines[0].strip() == "__VEYA_NONE__":
            return {"name": name, "host": self.host.host_id, "found": False, "finished": False}
        head = lines[0]
        if not head.startswith(_STATUS_MARK):
            raise BackendError("background status returned an unexpected answer")
        fields = dict(part.split("=", 1) for part in head.split()[1:] if "=" in part)
        code_raw = fields.get("exit", "")
        exit_code = int(code_raw) if code_raw.lstrip("-").isdigit() else None
        alive = fields.get("alive") == "1"
        return {
            "name": name,
            "host": self.host.host_id,
            "found": True,
            "pid": fields.get("pid", ""),
            "alive": alive,
            "finished": exit_code is not None or not alive,
            "exit_code": exit_code,
            "lost": exit_code is None and not alive,
            "log_tail": "\n".join(lines[1:]),
        }

    def bg_kill(self, name: str) -> str:
        self._check_name(name)
        result = self._script(_BG_KILL_SCRIPT, self._run_dir(), name, timeout_s=15.0)
        if not result.ok:
            raise self._fail(result, "background kill")
        return f"killed {self.host.host_id}:{name}"

    def list_runs(self) -> list[dict[str, Any]]:
        result = self._script(_BG_LIST_SCRIPT, self._run_dir(), timeout_s=15.0)
        if not result.ok:
            raise self._fail(result, "list runs")
        rows = []
        for line in result.stdout.splitlines():
            name, _, code = line.partition("\t")
            if name:
                rows.append(
                    {"name": name, "exit_code": int(code) if code.lstrip("-").isdigit() else None}
                )
        return rows

    # ── transfer ──
    def put_file(self, local_path: str, remote_path: str) -> str:
        from server.tool_registry import _resolve_write_path

        source = _resolve_write_path(local_path, must_exist=True)
        size = source.stat().st_size
        if size > MAX_TRANSFER_BYTES:
            raise BackendError(f"file is larger than {MAX_TRANSFER_BYTES} bytes")
        target = self.check_write_path(remote_path)
        result = self._script(_WRITE_SCRIPT, target, "1", input_bytes=source.read_bytes())
        if not result.ok:
            raise self._fail(result, "upload")
        return f"uploaded {size} bytes to {self.host.host_id}:{target}"

    def get_file(self, remote_path: str, local_path: str) -> str:
        from server.tool_registry import _resolve_write_path

        target = self.check_read_path(remote_path)
        size_res = self._remote(["wc", "-c", "--", target], timeout_s=15.0)
        if not size_res.ok:
            raise self._fail(size_res, "download")
        size = int((size_res.stdout.split() or ["0"])[0])
        if size > MAX_TRANSFER_BYTES:
            raise BackendError(f"remote file is larger than {MAX_TRANSFER_BYTES} bytes")
        result = self._remote(["cat", "--", target], timeout_s=300.0, output_limit=1)
        if not result.ok:
            raise self._fail(result, "download")
        dest: Path = _resolve_write_path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(result.raw_stdout)
        return f"downloaded {len(result.raw_stdout)} bytes to {dest}"


def classify_or_refuse(command: str, *, workspace_root: str) -> list[str]:
    """``parse_command`` + the ONE PermissionEngine; DENY is refused outright.

    Everything else still needs the caller's approval (host exec is
    ``external_mutation``); this only removes catastrophic commands early.
    """

    from runtime.coding.command_runner import CommandPolicyError, parse_command
    from veya.remote.permission_engine import Decision, PermissionEngine, parse_command_context

    try:
        argv = parse_command(command)
    except CommandPolicyError as exc:
        raise BackendError(str(exc)) from exc
    root = Path(workspace_root or "/")
    decision = PermissionEngine().evaluate(
        parse_command_context(tuple(argv), cwd=root, workspace_root=root)
    )
    if decision.decision == Decision.DENY:
        raise BackendError(f"command refused by policy: {decision.reason}")
    return argv


def backend_for(host_id: str = "", *, owner_id: str = "", runner=None, key_lookup=None) -> Any:
    """``""`` → LocalBackend; else the caller's own enabled host over ssh."""

    if not host_id:
        return LocalBackend()
    from server.remote_control import hosts as hosts_mod

    host = hosts_mod.get_host(host_id)
    if host is None or not owner_id or host.owner_id != owner_id or not host.enabled:
        raise BackendError(f"host {host_id!r} not found")
    return SshBackend(host, runner=runner, key_lookup=key_lookup)


__all__ = [
    "BackendError",
    "BgHandle",
    "ExecBackend",
    "ExecResult",
    "LocalBackend",
    "SshBackend",
    "backend_for",
    "classify_or_refuse",
]
