"""Veya Control Harness: Project-local control harness for verification.

Implements the 6 required operations:
- doctor: Health check
- launch: Start the product
- drive: Execute user path
- snapshot: Capture state
- trace: Collect traces
- cleanup: Teardown

This harness is designed to be self-testable.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


class VeyaControlHarness:
    """Control harness for Veya Web/CLI verification."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).expanduser().resolve()
        self.processes: list[asyncio.subprocess.Process] = []
        self.temp_files: list[Path] = []

    async def doctor(self) -> dict[str, Any]:
        """Health check: verify environment and dependencies."""
        checks = {}

        # Check Python version
        checks["python_version"] = {
            "ok": sys.version_info >= (3, 11),
            "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }

        # Check key dependencies
        deps = ["pnpm", "uv", "git"]
        for dep in deps:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "which", dep,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                checks[dep] = {"ok": proc.returncode == 0}
            except Exception:
                checks[dep] = {"ok": False}

        # Check project structure
        checks["project_structure"] = {
            "ok": (self.project_root / "pyproject.toml").exists() and (self.project_root / "server").exists(),
            "root": str(self.project_root),
        }

        # Check ports
        checks["ports"] = await self._check_ports()

        all_ok = all(v.get("ok", False) for v in checks.values() if isinstance(v, dict))
        return {"ok": all_ok, "checks": checks, "timestamp": time.time()}

    async def _check_ports(self) -> dict[str, Any]:
        """Check if required ports are available."""
        import socket
        ports = {"3105": "veya_web", "8767": "veya_gateway", "8765": "helivex"}
        results = {}
        for port, name in ports.items():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    result = s.connect_ex(("127.0.0.1", int(port)))
                    results[port] = {"name": name, "available": result != 0}
            except Exception:
                results[port] = {"name": name, "available": False}
        return {"ok": all(r["available"] for r in results.values()), "details": results}

    async def launch(self, *, target: str = "cli") -> dict[str, Any]:
        """Launch the product (CLI, server, or web)."""
        if target == "cli":
            # Test CLI availability
            return await self._run_command("python -m veya.cli.main --help")
        elif target == "server":
            # Start API server in background
            return await self._launch_server()
        elif target == "web":
            # Build and serve web frontend
            return await self._launch_web()
        else:
            return {"ok": False, "error": f"Unknown launch target: {target}"}

    async def _launch_server(self) -> dict[str, Any]:
        """Launch the FastAPI server."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.project_root)
        
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "server.app",
            cwd=self.project_root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(proc)
        
        # Wait for server to start
        await asyncio.sleep(2)
        
        # Check health
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get("http://127.0.0.1:8767/api/v1/mcp/health", timeout=5.0)
                if resp.status_code == 200:
                    return {"ok": True, "pid": proc.pid, "url": "http://127.0.0.1:8767"}
        except Exception as e:
            return {"ok": False, "error": f"Server health check failed: {e}", "pid": proc.pid}
        
        return {"ok": False, "error": "Server did not become healthy", "pid": proc.pid}

    async def _launch_web(self) -> dict[str, Any]:
        """Build and serve the web frontend."""
        # Build first
        build_result = await self._run_command("pnpm --dir apps/web build")
        if not build_result.get("ok"):
            return {"ok": False, "error": "Web build failed", "details": build_result}
        
        # Serve with preview
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            "pnpm", "--dir", "apps/web", "preview", "--port", "3105",
            cwd=self.project_root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(proc)
        await asyncio.sleep(3)
        
        return {"ok": True, "pid": proc.pid, "url": "http://127.0.0.1:3105"}

    async def drive(self, *, scenario: str = "cli_help") -> dict[str, Any]:
        """Execute a user path scenario."""
        if scenario == "cli_help":
            return await self._run_command("python -m veya.cli.main --help")
        elif scenario == "cli_doctor":
            return await self._run_command("python -m veya.cli.main doctor --json")
        elif scenario == "server_health":
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    resp = await client.get("http://127.0.0.1:8767/api/v1/mcp/health", timeout=5.0)
                    return {"ok": resp.status_code == 200, "status_code": resp.status_code, "body": resp.text}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        elif scenario == "web_load":
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    resp = await client.get("http://127.0.0.1:3105", timeout=10.0)
                    return {"ok": resp.status_code == 200, "status_code": resp.status_code}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        else:
            return {"ok": False, "error": f"Unknown drive scenario: {scenario}"}

    async def snapshot(self) -> dict[str, Any]:
        """Capture current state snapshot."""
        snapshot = {
            "timestamp": time.time(),
            "processes": [{"pid": p.pid, "returncode": p.returncode} for p in self.processes],
            "temp_files": [str(f) for f in self.temp_files],
            "git_status": await self._get_git_status(),
            "port_status": await self._check_ports(),
        }
        
        # Save to temp file
        snap_path = self.project_root / ".veya" / "runs" / f"snapshot-{int(time.time())}.json"
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(snapshot, indent=2))
        self.temp_files.append(snap_path)
        
        return {"ok": True, "snapshot_path": str(snap_path), "data": snapshot}

    async def trace(self) -> dict[str, Any]:
        """Collect traces from running processes."""
        traces = {
            "timestamp": time.time(),
            "process_logs": [],
        }
        
        for proc in self.processes:
            if proc.stdout:
                try:
                    # Non-blocking read
                    stdout = await asyncio.wait_for(proc.stdout.read(8192), timeout=0.5)
                    traces["process_logs"].append({
                        "pid": proc.pid,
                        "stdout": stdout.decode() if stdout else "",
                    })
                except asyncio.TimeoutError:
                    pass
            if proc.stderr:
                try:
                    stderr = await asyncio.wait_for(proc.stderr.read(8192), timeout=0.5)
                    traces["process_logs"].append({
                        "pid": proc.pid,
                        "stderr": stderr.decode() if stderr else "",
                    })
                except asyncio.TimeoutError:
                    pass
        
        # Save trace
        trace_path = self.project_root / ".veya" / "runs" / f"trace-{int(time.time())}.json"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps(traces, indent=2))
        self.temp_files.append(trace_path)
        
        return {"ok": True, "trace_path": str(trace_path), "data": traces}

    async def cleanup(self) -> dict[str, Any]:
        """Teardown: stop processes, clean temp files."""
        stopped = []
        for proc in self.processes:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                stopped.append({"pid": proc.pid, "returncode": proc.returncode})
        
        cleaned = []
        for f in self.temp_files:
            try:
                f.unlink()
                cleaned.append(str(f))
            except Exception:
                pass
        
        self.processes.clear()
        self.temp_files.clear()
        
        return {"ok": True, "stopped_processes": stopped, "cleaned_files": cleaned}

    async def _run_command(self, cmd: str) -> dict[str, Any]:
        """Run a command and return structured result."""
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(self.project_root)},
        )
        stdout, stderr = await proc.communicate()
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout.decode() if stdout else "",
            "stderr": stderr.decode() if stderr else "",
            "command": cmd,
        }

    async def _get_git_status(self) -> dict[str, Any]:
        """Get git status."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--short", "--branch", "-uall",
                cwd=self.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return {
                "ok": proc.returncode == 0,
                "stdout": stdout.decode() if stdout else "",
                "stderr": stderr.decode() if stderr else "",
            }
        except Exception:
            return {"ok": False}

    # Self-test methods
    async def self_test(self) -> dict[str, Any]:
        """Run self-tests on the harness itself."""
        results = {}
        
        # Test doctor
        results["doctor"] = await self.doctor()
        
        # Test launch (CLI only for self-test)
        results["launch_cli"] = await self.launch(target="cli")
        
        # Test drive
        results["drive_cli_help"] = await self.drive(scenario="cli_help")
        
        # Test snapshot
        results["snapshot"] = await self.snapshot()
        
        # Test trace
        results["trace"] = await self.trace()
        
        # Test cleanup
        results["cleanup"] = await self.cleanup()
        
        all_ok = all(r.get("ok", False) for r in results.values())
        return {"ok": all_ok, "results": results}


async def main():
    """CLI entry point for the harness."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Veya Control Harness")
    parser.add_argument("operation", choices=["doctor", "launch", "drive", "snapshot", "trace", "cleanup", "self-test"])
    parser.add_argument("--target", default="cli", help="Launch target (cli, server, web)")
    parser.add_argument("--scenario", default="cli_help", help="Drive scenario")
    parser.add_argument("--project-root", default=".", help="Project root directory")
    
    args = parser.parse_args()
    
    harness = VeyaControlHarness(args.project_root)
    
    if args.operation == "doctor":
        result = await harness.doctor()
    elif args.operation == "launch":
        result = await harness.launch(target=args.target)
    elif args.operation == "drive":
        result = await harness.drive(scenario=args.scenario)
    elif args.operation == "snapshot":
        result = await harness.snapshot()
    elif args.operation == "trace":
        result = await harness.trace()
    elif args.operation == "cleanup":
        result = await harness.cleanup()
    elif args.operation == "self-test":
        result = await harness.self_test()
    else:
        result = {"ok": False, "error": f"Unknown operation: {args.operation}"}
    
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    asyncio.run(main())