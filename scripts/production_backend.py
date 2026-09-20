"""Canonical backend operations entry point (P10D).

``production_backend.py up`` / ``production_backend.py restart`` run the
preflight guard first and only invoke docker compose when the guard allows
it. The wrapper never remediates pin drift itself.

P10E note: the backend is a docker compose service (no systemd unit), so
there is no ExecStartPre slot; this wrapper is the gate. A container
healthcheck is deliberately not used: it fires after the process already
started or crashed, which is too late for a fail-closed restart guard.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

COMPOSE_FILE = Path("deploy/docker-compose.yml")
ENV_FILE = Path(".env")
SERVICE = "backend"


def load_preflight_module(script_dir: Path):
    target = script_dir / "production_backend_preflight.py"
    spec = importlib.util.spec_from_file_location("production_backend_preflight", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load preflight module: {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def default_compose_runner(root: Path, operation: str) -> int:
    """Invoke docker compose for the backend service. Only called on PASS."""
    argv = ["restart", SERVICE] if operation == "restart" else ["up", "-d", SERVICE]
    cmd = ["docker", "compose"]
    env_file = root / ENV_FILE
    if env_file.is_file():
        cmd += ["--env-file", str(env_file)]
    cmd += ["-f", str(root / COMPOSE_FILE), *argv]
    try:
        proc = subprocess.run(cmd, cwd=str(root), timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"compose invocation failed: {exc}")
        return 1
    return proc.returncode


def run_operation(
    root: Path,
    operation: str,
    *,
    preflight_fn=None,
    compose_fn=None,
) -> int:
    """Gate then act. Returns the process exit code."""
    preflight = load_preflight_module(Path(__file__).resolve().parent)
    if preflight_fn is None:

        def _default_preflight() -> int:
            result = preflight.run_preflight(root)
            for line in result.lines:
                print(line)
            return result.exit_code

        preflight_fn = _default_preflight

    code = int(preflight_fn())
    if code != int(preflight.EXIT_OK):
        print(f"compose {operation} NOT invoked (preflight blocked).")
        return code
    runner = compose_fn or (lambda op: default_compose_runner(root, op))
    return int(runner(operation))


def _discover_root() -> Path:
    preflight = load_preflight_module(Path(__file__).resolve().parent)
    found = preflight.find_repo_root(Path(__file__).resolve())
    return found or Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["up", "restart"])
    parser.add_argument("--root", default="", help="Repo root (default: auto-discover)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _discover_root()
    return run_operation(root, args.operation)


if __name__ == "__main__":
    raise SystemExit(main())
