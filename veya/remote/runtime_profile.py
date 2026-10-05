"""Canonical WorkspaceRuntimeProfile and Discovery Substrate for Local2 Full-Capability L0.

Provides:
- WorkspaceRuntimeProfile dataclass capturing all runtime toolchains and environment.
- ExecutionDomain (L0_WORKSPACE_FULL, L0_ISOLATED, L0_HOST).
- ExecutionTarget (NEW_ISOLATED_WORKTREE, EXISTING_WORKTREE, CANONICAL_WORKTREE, HOST).
- discover_runtime_profile: multi-tier active discovery without guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ExecutionDomain(StrEnum):
    L0_WORKSPACE_FULL = "L0_WORKSPACE_FULL"
    L0_ISOLATED = "L0_ISOLATED"
    L0_HOST = "L0_HOST"


class ExecutionTarget(StrEnum):
    EXECUTION_WORKTREE = "EXECUTION_WORKTREE"
    CANONICAL_WORKTREE = "CANONICAL_WORKTREE"
    HOST = "HOST"

    # Legacy spellings remain accepted by adapters, but serialize to the one
    # canonical isolated target.
    NEW_ISOLATED_WORKTREE = "EXECUTION_WORKTREE"
    EXISTING_WORKTREE = "EXECUTION_WORKTREE"


_KNOWN_PROJECT_CLIS = (
    "antigravity",
    "agy",
    "opencode",
    "pi",
    "grok",
    "dsh",
    "codex",
    "claude",
    "git",
    "docker",
    "docker-compose",
    "ffmpeg",
    "pytest",
    "ruff",
    "mypy",
    "pyright",
    "uv",
    "python",
    "python3",
    "node",
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "cargo",
    "go",
    "rustc",
)

_USER_BIN_DIRS = (
    Path.home() / ".local" / "bin",
    Path.home() / ".local" / "go" / "bin",
    Path.home() / "go" / "bin",
    Path.home() / ".cargo" / "bin",
    Path.home() / ".opencode" / "bin",
    Path.home() / ".gemini" / "antigravity-cli" / "bin",
    Path.home() / ".npm-global" / "bin",
    Path.home() / ".grok" / "bin",
    Path.home() / ".local" / "share" / "pnpm",
)

_SYSTEM_BIN_DIRS = (
    Path("/usr/local/bin"),
    Path("/usr/local/go/bin"),
    Path("/usr/lib/go/bin"),
    Path("/usr/bin"),
    Path("/bin"),
    Path("/usr/local/sbin"),
    Path("/usr/sbin"),
    Path("/sbin"),
)


@dataclass
class WorkspaceRuntimeProfile:
    workspace_root: str
    repo_root: str
    execution_root: str

    python: str | None = None
    python_version: str | None = None
    venv_root: str | None = None

    pytest: str | None = None
    ruff: str | None = None
    mypy: str | None = None

    node: str | None = None
    npm: str | None = None
    npx: str | None = None
    pnpm: str | None = None
    yarn: str | None = None
    bun: str | None = None

    uv: str | None = None
    pip: str | None = None

    git: str | None = None

    docker: str | None = None
    docker_compose: str | None = None

    # Go toolchain
    go: str | None = None
    go_version: str | None = None
    gofmt: str | None = None
    goroot: str | None = None
    gopath: str | None = None
    gobin: str | None = None
    go_path_entries: list[str] = field(default_factory=list)

    # Rust toolchain
    cargo: str | None = None
    rustc: str | None = None

    # Build tools
    make: str | None = None
    cmake: str | None = None

    # Tool status classification (HOST_AVAILABLE, SANDBOX_VISIBLE, PROJECT_AVAILABLE, PROJECTED, UNAVAILABLE)
    tool_status: dict[str, str] = field(default_factory=dict)

    project_bins: list[str] = field(default_factory=list)
    runtime_bins: list[str] = field(default_factory=list)

    path_entries: list[str] = field(default_factory=list)

    env_allowlist: list[str] = field(default_factory=list)
    env_passthrough: list[str] = field(default_factory=list)
    env_redacted: list[str] = field(default_factory=list)

    library_paths: list[str] = field(default_factory=list)

    network_policy: str = "full"
    service_capabilities: list[str] = field(default_factory=list)

    discovered_at: float = field(default_factory=time.time)
    profile_hash: str = ""

    @property
    def python_bin(self) -> str | None:
        return self.python

    @property
    def pytest_bin(self) -> str | None:
        return self.pytest

    @property
    def ruff_bin(self) -> str | None:
        return self.ruff

    @property
    def mypy_bin(self) -> str | None:
        return self.mypy

    @property
    def node_bin(self) -> str | None:
        return self.node

    @property
    def pnpm_bin(self) -> str | None:
        return self.pnpm

    @property
    def uv_bin(self) -> str | None:
        return self.uv

    @property
    def docker_bin(self) -> str | None:
        return self.docker

    @property
    def go_binary(self) -> str | None:
        return self.go

    @property
    def go_bin(self) -> str | None:
        return self.go

    @property
    def gofmt_binary(self) -> str | None:
        return self.gofmt

    @property
    def gofmt_bin(self) -> str | None:
        return self.gofmt

    @property
    def GOROOT(self) -> str | None:
        return self.goroot

    @property
    def GOPATH(self) -> str | None:
        return self.gopath

    @property
    def GOBIN(self) -> str | None:
        return self.gobin

    @property
    def cargo_binary(self) -> str | None:
        return self.cargo

    @property
    def cargo_bin(self) -> str | None:
        return self.cargo

    @property
    def rustc_binary(self) -> str | None:
        return self.rustc

    @property
    def rustc_bin(self) -> str | None:
        return self.rustc

    @property
    def make_binary(self) -> str | None:
        return self.make

    @property
    def make_bin(self) -> str | None:
        return self.make

    @property
    def cmake_binary(self) -> str | None:
        return self.cmake

    @property
    def cmake_bin(self) -> str | None:
        return self.cmake

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "repo_root": self.repo_root,
            "execution_root": self.execution_root,
            "python": self.python,
            "python_version": self.python_version,
            "venv_root": self.venv_root,
            "pytest": self.pytest is not None,
            "ruff": self.ruff is not None,
            "mypy": self.mypy is not None,
            "node": self.node is not None,
            "pnpm": self.pnpm is not None,
            "uv": self.uv is not None,
            "docker": self.docker is not None,
            "git": self.git is not None,
            "go": self.go is not None,
            "go_version": self.go_version,
            "gofmt": self.gofmt is not None,
            "goroot": self.goroot,
            "gopath": self.gopath,
            "cargo": self.cargo is not None,
            "rustc": self.rustc is not None,
            "make": self.make is not None,
            "cmake": self.cmake is not None,
            "tool_status": self.tool_status,
            "available_project_clis": [Path(b).name for b in self.project_bins],
            "profile_hash": self.profile_hash,
        }


def _probe_executable(path: Path | str | None, *version_args: str) -> tuple[str | None, str | None]:
    if not path:
        return None, None
    p = Path(path).expanduser().resolve()
    if not (p.is_file() and os.access(p, os.X_OK)):
        return None, None
    args = list(version_args) if version_args else ["--version"]
    try:
        proc = subprocess.run(
            [str(p), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
        v_str = version[0].strip() if version else "unknown"
        return str(p), v_str
    except (OSError, subprocess.SubprocessError):
        return str(p), "available"


def _find_repo_root_for(candidate: Path) -> Path:
    from veya.remote.workspace_binding import git_main_repo_root

    main = git_main_repo_root(candidate)
    if main is not None:
        return main
    current = candidate if candidate.is_dir() else candidate.parent
    for ancestor in (current, *current.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return current


_PROFILE_CACHE: dict[str, tuple[float, WorkspaceRuntimeProfile]] = {}


def discover_runtime_profile(
    workspace_path: str | Path,
    repo_root: str | Path | None = None,
    *,
    execution_root: str | Path | None = None,
    network_policy: str = "full",
    force_refresh: bool = False,
) -> WorkspaceRuntimeProfile:
    """Discover real runtime tools and construct a WorkspaceRuntimeProfile.

    Priority:
    1. Explicit project config / active venv
    2. <workspace>/.venv or <repo_root>/.venv
    3. <workspace>/venv or <repo_root>/venv
    4. uv environment / virtualenv
    5. Project-local binaries (e.g. node_modules/.bin)
    6. User-local binaries (~/.local/bin, ~/.cargo/bin, etc.)
    7. System binaries (/usr/local/bin, /usr/bin, /bin)
    """
    ws = Path(workspace_path).expanduser().resolve()
    cache_key = str(ws)
    now = time.monotonic()
    if not force_refresh and cache_key in _PROFILE_CACHE:
        cached_time, cached_profile = _PROFILE_CACHE[cache_key]
        if now - cached_time < 30.0:
            return cached_profile

    rp = Path(repo_root).expanduser().resolve() if repo_root else _find_repo_root_for(ws)
    er = Path(execution_root).expanduser().resolve() if execution_root else ws

    # 1. Discover Python & Virtualenv
    python_bin: str | None = None
    python_ver: str | None = None
    venv_dir: str | None = None

    candidate_venvs: list[Path] = [
        ws / ".venv",
        ws / "venv",
        rp / ".venv",
        rp / "venv",
    ]
    # Check if currently active sys.prefix is a venv
    if os.environ.get("VIRTUAL_ENV"):
        candidate_venvs.insert(0, Path(os.environ["VIRTUAL_ENV"]))

    for cand_venv in candidate_venvs:
        py_cand = cand_venv / "bin" / "python"
        if py_cand.is_file() and os.access(py_cand, os.X_OK):
            p_res, v_res = _probe_executable(py_cand)
            if p_res:
                python_bin = p_res
                python_ver = v_res
                venv_dir = str(cand_venv.resolve())
                break

    if not python_bin:
        # Fall back to host python3 or python
        which_py = shutil.which("python3") or shutil.which("python")
        if which_py:
            python_bin, python_ver = _probe_executable(which_py)

    # 2. Discover Python toolchain (pytest, ruff, mypy, pip)
    pytest_bin: str | None = None
    ruff_bin: str | None = None
    mypy_bin: str | None = None
    pip_bin: str | None = None

    if venv_dir:
        v_path = Path(venv_dir) / "bin"
        if (v_path / "pytest").is_file():
            pytest_bin, _ = _probe_executable(v_path / "pytest")
        if (v_path / "ruff").is_file():
            ruff_bin, _ = _probe_executable(v_path / "ruff")
        if (v_path / "mypy").is_file():
            mypy_bin, _ = _probe_executable(v_path / "mypy")
        if (v_path / "pip").is_file():
            pip_bin, _ = _probe_executable(v_path / "pip")

    # If not in venv, check user / system paths
    if not pytest_bin:
        which_pytest = shutil.which("pytest")
        if which_pytest:
            pytest_bin, _ = _probe_executable(which_pytest)
    if not ruff_bin:
        which_ruff = shutil.which("ruff")
        if which_ruff:
            ruff_bin, _ = _probe_executable(which_ruff)
    if not mypy_bin:
        which_mypy = shutil.which("mypy")
        if which_mypy:
            mypy_bin, _ = _probe_executable(which_mypy)
    if not pip_bin:
        which_pip = shutil.which("pip3") or shutil.which("pip")
        if which_pip:
            pip_bin, _ = _probe_executable(which_pip)

    # 3. Discover Node & JS toolchains
    node_bin, _ = _probe_executable(shutil.which("node"))
    npm_bin, _ = _probe_executable(shutil.which("npm"))
    npx_bin, _ = _probe_executable(shutil.which("npx"))
    pnpm_bin, _ = _probe_executable(shutil.which("pnpm"))
    yarn_bin, _ = _probe_executable(shutil.which("yarn"))
    bun_bin, _ = _probe_executable(shutil.which("bun"))

    # 4. Discover Git, Docker, Compose, UV
    git_bin, _ = _probe_executable(shutil.which("git"))
    docker_bin, _ = _probe_executable(shutil.which("docker"))
    docker_compose_bin: str | None = None
    if shutil.which("docker-compose"):
        docker_compose_bin, _ = _probe_executable(shutil.which("docker-compose"))
    elif docker_bin:
        docker_compose_bin = f"{docker_bin} compose"
    uv_bin, _ = _probe_executable(shutil.which("uv"))

    # 4b. Discover Go Toolchain
    go_bin: str | None = None
    go_version: str | None = None
    gofmt_bin: str | None = None
    goroot: str | None = None
    gopath: str | None = None
    gobin: str | None = None
    go_path_entries: list[str] = []

    go_search_candidates = [
        shutil.which("go"),
        str(Path.home() / ".local" / "go" / "bin" / "go"),
        str(Path.home() / "go" / "bin" / "go"),
        "/usr/local/go/bin/go",
        "/usr/lib/go/bin/go",
        "/snap/bin/go",
        "/opt/go/bin/go",
    ]
    for cand in go_search_candidates:
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            go_bin = str(Path(cand).resolve())
            break

    if go_bin:
        try:
            p = subprocess.run(
                [go_bin, "version"], capture_output=True, text=True, timeout=3.0, check=False
            )
            if p.returncode == 0:
                parts = p.stdout.strip().split()
                go_version = parts[2] if len(parts) >= 3 else p.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

        try:
            p = subprocess.run(
                [go_bin, "env", "GOROOT", "GOPATH", "GOBIN"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if p.returncode == 0:
                lines = [line.strip() for line in p.stdout.splitlines()]
                goroot = lines[0] if len(lines) > 0 and lines[0] else None
                gopath = lines[1] if len(lines) > 1 and lines[1] else None
                gobin = lines[2] if len(lines) > 2 and lines[2] else None
        except (OSError, subprocess.SubprocessError):
            pass

        if not goroot and go_bin:
            goroot = str(Path(go_bin).parent.parent)

        gofmt_candidates = [
            shutil.which("gofmt"),
            str(Path(go_bin).parent / "gofmt"),
            str(Path(goroot) / "bin" / "gofmt") if goroot else None,
        ]
        for cand in gofmt_candidates:
            if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
                gofmt_bin = str(Path(cand).resolve())
                break

        go_dir = str(Path(go_bin).parent)
        if go_dir not in go_path_entries:
            go_path_entries.append(go_dir)
        if gopath and (Path(gopath) / "bin").is_dir():
            gp_bin = str(Path(gopath) / "bin")
            if gp_bin not in go_path_entries:
                go_path_entries.append(gp_bin)
        if gobin and Path(gobin).is_dir() and str(gobin) not in go_path_entries:
            go_path_entries.append(str(gobin))

    # 4c. Discover Rust (cargo, rustc), Make, CMake
    cargo_bin, _ = _probe_executable(
        shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")
    )
    rustc_bin, _ = _probe_executable(
        shutil.which("rustc") or str(Path.home() / ".cargo" / "bin" / "rustc")
    )
    make_bin, _ = _probe_executable(shutil.which("make"))
    cmake_bin, _ = _probe_executable(shutil.which("cmake"))

    # 5. Discover Project CLIs
    project_bins: list[str] = []
    for cli_name in _KNOWN_PROJECT_CLIS:
        # Check venv first
        if venv_dir and (Path(venv_dir) / "bin" / cli_name).is_file():
            project_bins.append(str(Path(venv_dir) / "bin" / cli_name))
            continue
        # Check node_modules/.bin
        if (ws / "node_modules" / ".bin" / cli_name).is_file():
            project_bins.append(str(ws / "node_modules" / ".bin" / cli_name))
            continue
        if (rp / "node_modules" / ".bin" / cli_name).is_file():
            project_bins.append(str(rp / "node_modules" / ".bin" / cli_name))
            continue
        # Check user bin dirs
        found = False
        for user_dir in _USER_BIN_DIRS:
            cand = user_dir / cli_name
            if cand.is_file() and os.access(cand, os.X_OK):
                project_bins.append(str(cand))
                found = True
                break
        if found:
            continue
        # Check system PATH
        w = shutil.which(cli_name)
        if w:
            project_bins.append(str(Path(w).resolve()))

    # 6. Construct PATH entries
    path_entries: list[str] = []
    if venv_dir:
        path_entries.append(str(Path(venv_dir) / "bin"))
    if (ws / "node_modules" / ".bin").is_dir():
        path_entries.append(str(ws / "node_modules" / ".bin"))
    if (rp / "node_modules" / ".bin").is_dir() and (rp / "node_modules" / ".bin") != (
        ws / "node_modules" / ".bin"
    ):
        path_entries.append(str(rp / "node_modules" / ".bin"))

    for u_dir in _USER_BIN_DIRS:
        if u_dir.is_dir() and str(u_dir) not in path_entries:
            path_entries.append(str(u_dir))

    for s_dir in _SYSTEM_BIN_DIRS:
        if s_dir.is_dir() and str(s_dir) not in path_entries:
            path_entries.append(str(s_dir))

    for gp_dir in go_path_entries:
        if gp_dir and Path(gp_dir).is_dir() and gp_dir not in path_entries:
            path_entries.append(gp_dir)

    for b in (go_bin, gofmt_bin, cargo_bin, rustc_bin, make_bin, cmake_bin):
        if b and b not in project_bins:
            project_bins.append(b)

    # Inherit existing host PATH entries not yet captured
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and Path(entry).is_dir() and entry not in path_entries:
            path_entries.append(entry)

    # 7. Library paths
    library_paths: list[str] = []
    for lp in (
        "/usr/lib",
        "/usr/local/lib",
        "/lib",
        "/lib64",
        "/usr/lib64",
        "/usr/lib/x86_64-linux-gnu",
    ):
        if Path(lp).is_dir():
            library_paths.append(lp)
    if venv_dir and (Path(venv_dir) / "lib").is_dir():
        library_paths.insert(0, str(Path(venv_dir) / "lib"))

    # 8. Environment categorization
    env_allowlist = [
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONPATH",
        "HOME",
        "TMPDIR",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "PWD",
        "SSH_AUTH_SOCK",
        "GIT_CONFIG_GLOBAL",
        "GOROOT",
        "GOPATH",
        "GOBIN",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "VEYA_SANDBOX_DEPTH",
    ]

    env_passthrough = [
        "NODE_ENV",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "VIRTUAL_ENV",
        "GOROOT",
        "GOPATH",
        "GOBIN",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "VEYA_PROXY",
        "VEYA_RUNTIME_PROXY",
    ]

    env_redacted = [
        "API_KEY",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "AUTH",
        "BEARER",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENCODE_API_KEY",
        "ANTHROPIC_API_KEY",
    ]

    # 8b. Tool status classification
    tool_status: dict[str, str] = {}
    for tool_name, tool_path in [
        ("python", python_bin),
        ("pytest", pytest_bin),
        ("ruff", ruff_bin),
        ("mypy", mypy_bin),
        ("node", node_bin),
        ("npm", npm_bin),
        ("npx", npx_bin),
        ("pnpm", pnpm_bin),
        ("yarn", yarn_bin),
        ("bun", bun_bin),
        ("go", go_bin),
        ("gofmt", gofmt_bin),
        ("cargo", cargo_bin),
        ("rustc", rustc_bin),
        ("make", make_bin),
        ("cmake", cmake_bin),
        ("git", git_bin),
        ("docker", docker_bin),
        ("uv", uv_bin),
    ]:
        if not tool_path:
            tool_status[tool_name] = "UNAVAILABLE"
        elif (venv_dir and venv_dir in tool_path) or "/node_modules/" in tool_path:
            tool_status[tool_name] = "PROJECT_AVAILABLE"
        elif shutil.which(tool_name) and str(Path(shutil.which(tool_name)).resolve()) == str(
            Path(tool_path).resolve()
        ):
            tool_status[tool_name] = "HOST_AVAILABLE"
        else:
            tool_status[tool_name] = "PROJECTED"

    # 9. Service capabilities
    service_capabilities: list[str] = []
    if shutil.which("systemctl"):
        try:
            p = subprocess.run(
                ["systemctl", "--user", "is-system-running"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if p.returncode in (0, 1):  # running or degraded
                service_capabilities.append("systemctl_user")
        except (OSError, subprocess.SubprocessError):
            pass

    if docker_bin:
        try:
            p = subprocess.run(
                [docker_bin, "ps"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if p.returncode == 0:
                service_capabilities.append("docker_daemon")
        except (OSError, subprocess.SubprocessError):
            pass

    # 10. Compute Profile Hash
    components = [
        str(ws),
        str(rp),
        str(python_bin),
        str(python_ver),
        str(pytest_bin),
        str(ruff_bin),
        str(mypy_bin),
        str(node_bin),
        str(go_bin),
        str(docker_bin),
        str(git_bin),
        ",".join(sorted(project_bins)),
        ",".join(path_entries),
    ]
    profile_hash = hashlib.sha256("::".join(components).encode("utf-8")).hexdigest()[:16]

    profile = WorkspaceRuntimeProfile(
        workspace_root=str(ws),
        repo_root=str(rp),
        execution_root=str(er),
        python=python_bin,
        python_version=python_ver,
        venv_root=venv_dir,
        pytest=pytest_bin,
        ruff=ruff_bin,
        mypy=mypy_bin,
        node=node_bin,
        npm=npm_bin,
        npx=npx_bin,
        pnpm=pnpm_bin,
        yarn=yarn_bin,
        bun=bun_bin,
        uv=uv_bin,
        pip=pip_bin,
        git=git_bin,
        docker=docker_bin,
        docker_compose=docker_compose_bin,
        go=go_bin,
        go_version=go_version,
        gofmt=gofmt_bin,
        goroot=goroot,
        gopath=gopath,
        gobin=gobin,
        go_path_entries=go_path_entries,
        cargo=cargo_bin,
        rustc=rustc_bin,
        make=make_bin,
        cmake=cmake_bin,
        tool_status=tool_status,
        project_bins=sorted(set(project_bins)),
        runtime_bins=[
            b
            for b in (
                python_bin,
                pytest_bin,
                ruff_bin,
                mypy_bin,
                node_bin,
                uv_bin,
                go_bin,
                git_bin,
                docker_bin,
            )
            if b
        ],
        path_entries=path_entries,
        env_allowlist=env_allowlist,
        env_passthrough=env_passthrough,
        env_redacted=env_redacted,
        library_paths=library_paths,
        network_policy=network_policy,
        service_capabilities=service_capabilities,
        discovered_at=time.time(),
        profile_hash=profile_hash,
    )
    _PROFILE_CACHE[cache_key] = (now, profile)
    return profile


# Canonical ExecutionDomain -> SandboxProfile mapping (P0-E).
#
# ExecutionDomain is policy/resolution language (what isolation the caller
# wants); SandboxProfile is the mechanism the command runner enforces. The two
# must never be conflated into one string enum. ``profile=`` (old ids such as
# ``local_trusted``/``local_restricted``/``docker_python``/``docker_node`` and
# the newer ``l0_*`` ids) remains accepted for backward compatibility, but an
# explicit ``execution_domain`` always takes precedence.
EXECUTION_DOMAIN_PROFILE: dict[str, str] = {
    "L0_WORKSPACE_FULL": "l0_workspace_full",
    "L0_ISOLATED": "l0_isolated",
    "L0_HOST": "l0_host",
}


def execution_domain_to_profile(execution_domain: str | None) -> str | None:
    """Map an execution domain to its canonical sandbox profile id.

    Returns ``None`` when no (recognised) domain was requested so callers can
    fall back to the legacy ``profile=`` argument or their own default.
    """

    if not execution_domain:
        return None
    return EXECUTION_DOMAIN_PROFILE.get(str(execution_domain).strip().upper())


def declared_runtime(repo_root: str | Path) -> dict[str, str]:
    """What the project itself says it needs, from its own declarations.

    Read from files the project checks in, never inferred from what happens to
    be installed: ``pyproject.toml`` ``requires-python``, ``.python-version``,
    ``package.json`` ``engines``, ``go.mod``. A project that pins nothing
    declares nothing, and that is reported as an absence rather than as a match.
    """
    root = Path(repo_root).expanduser()
    declared: dict[str, str] = {}

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        requires = (data.get("project") or {}).get("requires-python")
        if isinstance(requires, str) and requires.strip():
            declared["python"] = requires.strip()

    version_file = root / ".python-version"
    if version_file.is_file():
        try:
            pinned = version_file.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            pinned = []
        if pinned and pinned[0].strip():
            declared.setdefault("python", pinned[0].strip())

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            engines = (json.loads(package_json.read_text(encoding="utf-8")) or {}).get(
                "engines"
            ) or {}
            node = engines.get("node")
        except Exception:
            node = None
        if isinstance(node, str) and node.strip():
            declared["node"] = node.strip()

    go_mod = root / "go.mod"
    if go_mod.is_file():
        try:
            for line in go_mod.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "go":
                    declared["go"] = parts[1].strip()
                    break
        except OSError:
            pass

    return declared


def _version_tuple(raw: str) -> tuple[int, ...] | None:
    """First dotted-number run in ``raw``, or ``None``.

    Version strings arrive as whatever the tool printed: ``Python 3.14.4``,
    ``go1.23.1``, ``v26.4.0``. Comparing those with a bare ``split(".")`` finds
    no digits at all and every declared constraint then reads as "could not
    interpret" — which is how a real mismatch turns into a silent pass.
    """
    import re

    match = re.search(r"\d+(?:\.\d+)*", str(raw))
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _python_satisfies(actual: str, spec: str) -> bool | None:
    """Whether ``actual`` meets every clause of ``spec``; ``None`` if any is unreadable.

    All clauses must hold. Returning after the first one would make
    ``>=3.12, <3.13`` report satisfied on 3.14, so the comparison is AND across
    clauses and an unparseable clause poisons the whole answer rather than being
    skipped. Reporting ``None`` matters: a constraint this function cannot parse
    must not be reported as satisfied, because a silent pass is exactly the
    failure §7.7 is about.
    """
    have = _version_tuple(actual)
    if have is None:
        return None
    verdict: bool | None = True
    for clause in str(spec).split(","):
        clause = clause.strip()
        if not clause:
            continue
        ok: bool | None
        if clause.startswith("~="):
            # Compatible release: at least the stated version, and no minor/major
            # bump beyond it. ``~=3.12.0`` admits 3.12.5 but not 3.13.0, which a
            # prefix equality check would get backwards.
            want = _version_tuple(clause[2:])
            if want is None:
                return None
            ok = tuple(have[: len(want)]) >= want and tuple(have[: len(want) - 1]) == want[:-1]
        elif clause.startswith(">="):
            want = _version_tuple(clause[2:])
            if want is None:
                return None
            ok = tuple(have[: len(want)]) >= want
        elif clause.startswith("<="):
            want = _version_tuple(clause[2:])
            if want is None:
                return None
            ok = tuple(have[: len(want)]) <= want
        elif clause.startswith("<"):
            want = _version_tuple(clause[1:])
            if want is None:
                return None
            ok = tuple(have[: len(want)]) < want
        elif clause.startswith("=="):
            want = _version_tuple(clause[2:])
            if want is None:
                return None
            ok = tuple(have[: len(want)]) == want
        else:
            # Bare version, or an operator this check does not implement. An
            # exclusion (``!=``, ``<>*``) lands here deliberately: reporting it as
            # uncompared is safer than approximating it.
            want = _version_tuple(clause)
            if want is None or not clause[0].isdigit():
                return None
            ok = tuple(have[: len(want)]) == want
        if not ok:
            verdict = False
    return verdict


def _node_satisfies(actual: str, spec: str) -> bool | None:
    return _python_satisfies(actual, spec)


def compare_runtime(declared: dict[str, str], actual: dict[str, str]) -> dict[str, Any]:
    """Compare what the project declared against what is actually installed.

    Returns ``{"mismatches": [...], "uncompared": [...]}``. A runtime with no
    declaration is not a mismatch and not a pass — it is uncompared, kept
    separate so the caller can tell "checked and fine" from "never checked".
    """
    mismatches: list[dict[str, str]] = []
    uncompared: list[dict[str, str]] = []
    for tool, spec in sorted(declared.items()):
        found = actual.get(tool)
        if not found:
            mismatches.append(
                {
                    "runtime": tool,
                    "declared": spec,
                    "actual": "(not installed)",
                    "detail": f"{tool} is declared as {spec} but no interpreter was found",
                }
            )
            continue
        if tool == "python":
            ok = _python_satisfies(found, spec)
        elif tool == "node":
            ok = _node_satisfies(found, spec)
        elif tool == "go":
            ok = _python_satisfies(found, spec)
        else:
            ok = None
        if ok is True:
            continue
        if ok is None:
            uncompared.append(
                {
                    "runtime": tool,
                    "declared": spec,
                    "actual": found,
                    "detail": f"declared constraint {spec!r} is not a form this check interprets",
                }
            )
            continue
        mismatches.append(
            {
                "runtime": tool,
                "declared": spec,
                "actual": found,
                "detail": f"{tool} {found} does not satisfy declared {spec}",
            }
        )
    return {"mismatches": mismatches, "uncompared": uncompared}


def runtime_environment_report(repo_root: str | Path, profile: Any | None) -> dict[str, Any]:
    """Declared vs actual runtime for a repository, with an explicit verdict.

    ``OK`` means every declared runtime was located and satisfied.
    ``ENVIRONMENT_MISMATCH`` means at least one declared runtime is absent or
    unsatisfied. ``UNDECLARED`` means the project pins no runtime, so there was
    nothing to check — which is not the same as OK and is not reported as OK.
    ``UNCOMPARED`` means a declaration exists in a form this check does not
    interpret; it is surfaced rather than assumed to pass.
    """
    declared = declared_runtime(repo_root)
    actual: dict[str, str] = {}
    if profile is None:
        # Discovery did not produce a profile. That is not evidence that the
        # declared interpreter is missing, so it is reported as uncompared
        # rather than as a mismatch that would send someone hunting a runtime
        # that is installed perfectly well.
        return {
            "verdict": "UNCOMPARED" if declared else "UNDECLARED",
            "declared": declared,
            "actual": {},
            "mismatches": [],
            "uncompared": [
                {
                    "runtime": tool,
                    "declared": spec,
                    "actual": "(not discovered)",
                    "detail": f"runtime discovery produced no profile, so {spec!r} was not checked",
                }
                for tool, spec in sorted(declared.items())
            ],
        }
    if profile is not None:
        if getattr(profile, "python_version", None):
            actual["python"] = str(profile.python_version)
        elif getattr(profile, "python", None):
            actual["python"] = str(profile.python)
        if getattr(profile, "go_version", None):
            actual["go"] = str(profile.go_version)
        elif getattr(profile, "go", None):
            actual["go"] = str(profile.go)
        node_bin = getattr(profile, "node", None)
        if node_bin:
            _, node_version = _probe_executable(node_bin)
            actual["node"] = node_version or str(node_bin)

    comparison = compare_runtime(declared, actual)
    if comparison["mismatches"]:
        verdict = "ENVIRONMENT_MISMATCH"
    elif comparison["uncompared"]:
        verdict = "UNCOMPARED"
    elif not declared:
        verdict = "UNDECLARED"
    else:
        verdict = "OK"
    return {
        "verdict": verdict,
        "declared": declared,
        "actual": actual,
        "mismatches": comparison["mismatches"],
        "uncompared": comparison["uncompared"],
    }


__all__ = [
    "EXECUTION_DOMAIN_PROFILE",
    "ExecutionDomain",
    "ExecutionTarget",
    "WorkspaceRuntimeProfile",
    "compare_runtime",
    "declared_runtime",
    "discover_runtime_profile",
    "execution_domain_to_profile",
    "runtime_environment_report",
]
