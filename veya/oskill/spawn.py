"""
veya/oskill/spawn.py — External Agent Spawner (Layer 2).

Composite skill for discovering, launching, and managing external AI coding
agents (Claude Code, Codex CLI, Cursor, etc.) as subprocesses.

Each spawned agent runs in an isolated git worktree with its own session.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import shlex
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------


@dataclass
class AgentSpec:
    """Specification for an external AI coding agent."""

    name: str                    # e.g., "claude-code"
    display_name: str            # e.g., "Claude Code"
    cli_command: str             # e.g., "claude"
    install_command: str | None  # e.g., "npm install -g @anthropic-ai/claude-code"
    env_vars: dict[str, str] = field(default_factory=dict)
    args_template: list[str] = field(default_factory=list)  # ["-p", "{prompt}", "--output-format", "json"]
    default_model: str = ""
    supports_streaming: bool = True
    supports_tools: bool = True
    supports_multimodal: bool = False
    workdir_isolation: bool = True  # requires git worktree
    min_version: str = ""


# Registry of known external agents
_AGENT_REGISTRY: dict[str, AgentSpec] = {
    "claude-code": AgentSpec(
        name="claude-code",
        display_name="Claude Code",
        cli_command="claude",
        install_command="npm install -g @anthropic-ai/claude-code",
        env_vars={"ANTHROPIC_API_KEY": ""},
        args_template=["-p", "{prompt}"],
        default_model="claude-sonnet-4-6",
        supports_tools=True,
        supports_multimodal=True,
    ),
    "codex": AgentSpec(
        name="codex",
        display_name="OpenAI Codex CLI",
        cli_command="codex",
        install_command="pip install codex-cli",
        env_vars={"OPENAI_API_KEY": ""},
        args_template=["exec", "{prompt}"],
        default_model="gpt-4o",
        supports_tools=True,
    ),
    "cursor": AgentSpec(
        name="cursor",
        display_name="Cursor CLI",
        cli_command="cursor",
        install_command=None,  # installed via Cursor app
        env_vars={},
        args_template=["--prompt", "{prompt}"],
        supports_tools=True,
    ),
    "aider": AgentSpec(
        name="aider",
        display_name="Aider",
        cli_command="aider",
        install_command="pip install aider-chat",
        env_vars={"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""},
        args_template=["--message", "{prompt}", "--yes"],
        default_model="claude-sonnet-4-6",
        supports_tools=True,
    ),
    "openclaw": AgentSpec(
        name="openclaw",
        display_name="OpenClaw",
        cli_command="openclaw",
        install_command="npm install -g openclaw",
        env_vars={},
        args_template=["run", "{prompt}"],
        supports_tools=True,
    ),
    "nanobot": AgentSpec(
        name="nanobot",
        display_name="nanobot",
        cli_command="nanobot",
        install_command="npm install -g nanobot",
        env_vars={},
        args_template=["--task", "{prompt}"],
        supports_tools=True,
    ),
}


# ---------------------------------------------------------------------------
# Agent discovery
# ---------------------------------------------------------------------------


def discover_agents() -> dict[str, bool]:
    """Discover which external agents are installed on the system.

    Returns:
        Dict of {agent_name: is_installed}.
    """
    available: dict[str, bool] = {}
    for name, spec in _AGENT_REGISTRY.items():
        available[name] = shutil.which(spec.cli_command) is not None
    return available


def get_agent_spec(name: str) -> AgentSpec | None:
    """Get the spec for a registered agent."""
    return _AGENT_REGISTRY.get(name)


def list_agents() -> list[dict[str, Any]]:
    """List all registered agents with their availability."""
    available = discover_agents()
    result = []
    for name, spec in _AGENT_REGISTRY.items():
        result.append({
            "name": spec.name,
            "display_name": spec.display_name,
            "cli_command": spec.cli_command,
            "install_command": spec.install_command,
            "installed": available.get(name, False),
            "supports_tools": spec.supports_tools,
            "supports_multimodal": spec.supports_multimodal,
        })
    return result


def register_agent(spec: AgentSpec) -> None:
    """Register a new external agent specification."""
    _AGENT_REGISTRY[spec.name] = spec


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------


@dataclass
class SpawnConfig:
    """Configuration for spawning an external agent."""

    agent_name: str
    prompt: str
    workdir: Path = Path(".")
    env: dict[str, str] = field(default_factory=dict)
    timeout_sec: float = 300.0
    max_output_bytes: int = 1_000_000
    stream_output: bool = False
    use_worktree: bool = True
    worktree_base: Path | None = None


@dataclass
class SpawnResult:
    """Result of a spawned agent execution."""

    agent_name: str
    success: bool
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    workdir: str = ""
    duration_sec: float = 0.0
    error: str = ""


class AgentSpawner:
    """Manages spawning and lifecycle of external AI coding agents.

    Each agent runs as a subprocess in an isolated working directory
    (optionally a git worktree). The spawner handles:
    - Agent discovery and installation verification
    - Environment setup (API keys, working directory)
    - Process execution with timeout and output capture
    - Streaming output for real-time feedback
    - Git worktree isolation for sandboxed execution

    Example:
        >>> spawner = AgentSpawner()
        >>> result = await spawner.spawn(
        ...     "claude-code",
        ...     "Fix the bug in src/auth.py",
        ...     workdir=Path("./project"),
        ... )
        >>> print(result.stdout)
    """

    def __init__(self):
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}

    def is_installed(self, agent_name: str) -> bool:
        """Check if an agent is installed."""
        spec = get_agent_spec(agent_name)
        if spec is None:
            return False
        return shutil.which(spec.cli_command) is not None

    async def install(self, agent_name: str) -> tuple[bool, str]:
        """Attempt to install an agent.

        Returns:
            Tuple of (success, message).
        """
        spec = get_agent_spec(agent_name)
        if spec is None:
            return False, f"Unknown agent: {agent_name}"
        if spec.install_command is None:
            return False, f"No install command for {spec.display_name}"
        if self.is_installed(agent_name):
            return True, f"{spec.display_name} already installed"

        try:
            proc = await asyncio.create_subprocess_shell(
                spec.install_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120.0
            )
            if proc.returncode == 0:
                return True, f"{spec.display_name} installed successfully"
            return False, stderr.decode()[:500]
        except Exception as e:
            return False, str(e)

    async def spawn(
        self,
        agent_name: str,
        prompt: str,
        *,
        workdir: Path = Path("."),
        env: dict[str, str] | None = None,
        timeout_sec: float = 300.0,
        stream_output: bool = False,
        use_worktree: bool = True,
        worktree_base: Path | None = None,
    ) -> SpawnResult:
        """Spawn an external agent to execute a prompt.

        Args:
            agent_name: Name of the agent to spawn (e.g., "claude-code").
            prompt: The task prompt for the agent.
            workdir: Working directory.
            env: Extra environment variables.
            timeout_sec: Timeout in seconds.
            stream_output: Stream stdout in real-time.
            use_worktree: Run in an isolated git worktree.
            worktree_base: Base repo for worktree (defaults to workdir).

        Returns:
            SpawnResult with output and metadata.
        """
        spec = get_agent_spec(agent_name)
        if spec is None:
            return SpawnResult(
                agent_name=agent_name,
                success=False,
                error=f"Unknown agent: {agent_name}",
            )

        if not self.is_installed(agent_name):
            return SpawnResult(
                agent_name=agent_name,
                success=False,
                error=f"{spec.display_name} not installed. Run: {spec.install_command}",
            )

        start = time.time()
        actual_workdir = str(workdir.resolve())

        # Setup git worktree isolation
        wt_path = None
        if use_worktree and spec.workdir_isolation:
            base = worktree_base or workdir
            wt_path = await self._create_worktree(agent_name, base)
            if wt_path:
                actual_workdir = str(wt_path)

        # Build command
        args = [spec.cli_command]
        for tmpl in spec.args_template:
            args.append(tmpl.replace("{prompt}", shlex.quote(prompt)))
        cmd_str = " ".join(args)

        # Build environment
        proc_env = os.environ.copy()
        # Merge spec env vars (user should set actual values before spawning)
        for key in spec.env_vars:
            if key in os.environ:
                proc_env[key] = os.environ[key]
        if env:
            proc_env.update(env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=actual_workdir,
                env=proc_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            session_id = f"{agent_name}_{int(time.time())}"
            self._running_processes[session_id] = proc

            if stream_output:
                stdout_chunks: list[str] = []
                async for line in proc.stdout:
                    text = line.decode()
                    stdout_chunks.append(text)
                stdout = "".join(stdout_chunks)
            else:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_sec
                )
                stdout = stdout.decode()
                stderr = stderr.decode()
        except asyncio.TimeoutError:
            proc.kill()
            return SpawnResult(
                agent_name=agent_name,
                success=False,
                error=f"Timeout after {timeout_sec}s",
                duration_sec=time.time() - start,
            )
        except Exception as e:
            return SpawnResult(
                agent_name=agent_name,
                success=False,
                error=str(e),
                duration_sec=time.time() - start,
            )
        finally:
            self._running_processes.pop(session_id, None)
            # Cleanup worktree
            if wt_path:
                await self._remove_worktree(wt_path, worktree_base or workdir)

        # Parse structured output if possible
        output: dict[str, Any] = {}
        try:
            # Try to find JSON in stdout
            import re
            json_match = re.search(r'\{[\s\S]*"result"[\s\S]*\}', stdout)
            if json_match:
                output = json.loads(json_match.group(0))
        except (json.JSONDecodeError, AttributeError):
            pass

        return SpawnResult(
            agent_name=agent_name,
            success=proc.returncode == 0 if proc else False,
            exit_code=proc.returncode if proc else -1,
            stdout=stdout[:100000],
            stderr=stderr[:10000] if stderr else "",
            output=output,
            workdir=actual_workdir,
            duration_sec=time.time() - start,
        )

    async def spawn_multiple(
        self,
        tasks: list[tuple[str, str]],  # [(agent_name, prompt), ...]
        *,
        workdir: Path = Path("."),
        parallel: bool = True,
        max_parallel: int = 4,
        timeout_sec: float = 600.0,
    ) -> list[SpawnResult]:
        """Spawn multiple agents concurrently.

        Args:
            tasks: List of (agent_name, prompt) tuples.
            workdir: Base working directory.
            parallel: Run in parallel (default) or sequentially.
            max_parallel: Max concurrent agents.
            timeout_sec: Per-agent timeout.

        Returns:
            List of SpawnResult in the same order as tasks.
        """
        if parallel:
            sem = asyncio.Semaphore(max_parallel)

            async def _run_one(agent_name: str, prompt: str) -> SpawnResult:
                async with sem:
                    return await self.spawn(
                        agent_name, prompt,
                        workdir=workdir, timeout_sec=timeout_sec,
                    )

            return await asyncio.gather(
                *[_run_one(name, prompt) for name, prompt in tasks]
            )
        else:
            results = []
            for name, prompt in tasks:
                result = await self.spawn(
                    name, prompt,
                    workdir=workdir, timeout_sec=timeout_sec,
                )
                results.append(result)
            return results

    def kill(self, session_id: str):
        """Kill a running agent process."""
        proc = self._running_processes.get(session_id)
        if proc:
            proc.kill()

    def list_running(self) -> list[str]:
        """List currently running agent session IDs."""
        return list(self._running_processes.keys())

    # ── Worktree helpers ──────────────────────────────────────────────

    async def _create_worktree(
        self, agent_name: str, base_repo: Path,
    ) -> Path | None:
        """Create a git worktree for isolated agent execution."""
        base = base_repo.resolve()
        if not (base / ".git").exists():
            return None

        wt_name = f"agent-{agent_name}-{int(time.time())}"
        wt_path = base.parent / f".worktrees" / wt_name
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", str(wt_path),
                cwd=str(base),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode == 0 and wt_path.exists():
                return wt_path
        except Exception:
            pass
        return None

    async def _remove_worktree(self, wt_path: Path, base_repo: Path):
        """Remove a git worktree."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "remove", str(wt_path), "--force",
                cwd=str(base_repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except Exception:
            pass
