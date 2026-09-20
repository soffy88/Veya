"""DSH execution plane — Veya-owned configuration for the DSH executor.

DSH (DeepSeek Harness) is a Veya **executor**, not an independent model router.
Model availability, fallback and audit stay on the Veya LLM gateway
(`127.0.0.1:8791`); DSH only speaks the OpenAI-compatible protocol to it and
never holds a third-party provider credential itself.

Configuration lives in ONE dedicated file rather than being scattered into the
main service environment:

    ~/.config/veya/dsh.env            (0600, Veya-owned)
      DSH_RUNTIME=ENABLED
      DSH_PROVIDER=VEYA_LOCAL_GATEWAY
      DSH_BASE_URL=http://127.0.0.1:8791/v1
      DSH_MODEL=opencode-go/deepseek-v4.1-flash
      DSH_SESSION_DIR=/home/soffy/.local/state/veya/dsh
      # only because the dsh binary itself insists on a non-empty key
      DEEPSEEK_API_KEY=veya-local-gateway

Process environment still wins over the file, so a systemd
`EnvironmentFile=%h/.config/veya/dsh.env` and an ad-hoc CLI run behave the same
way. No real DeepSeek key is ever used on this path.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

CONFIG_PATH_ENV = "VEYA_DSH_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/veya/dsh.env")
DEFAULT_STATE_DIR = Path("~/.local/state/veya/dsh")
DEFAULT_PROFILES_SRC = Path("~/.dsh/profiles")
DEFAULT_BASE_URL = "http://127.0.0.1:8791/v1"
# Placeholder credential: the local gateway is the credential holder, DSH is not.
DEFAULT_API_KEY = "veya-local-gateway"
DEFAULT_MODEL = "opencode-go/deepseek-v4.1-flash"
DEFAULT_PROFILE = "headless"

_DISABLED_VALUES = {"0", "false", "no", "off", "disabled", "none"}


def config_path() -> Path:
    """Path of the dedicated DSH config file (VEYA_DSH_CONFIG overrides)."""
    raw = os.environ.get(CONFIG_PATH_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH.expanduser()


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader (no shell expansion, matches systemd semantics)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def load_config() -> dict[str, str]:
    """Dedicated DSH config: file first, process environment wins."""
    merged = _parse_env_file(config_path())
    merged.update(
        {k: v for k, v in os.environ.items() if k.startswith("DSH_") or k.startswith("DEEPSEEK_")}
    )
    return merged


def is_enabled(cfg: dict[str, str] | None = None) -> bool:
    """DSH_RUNTIME gate. Unset → enabled (back-compat with the pre-config leg)."""
    cfg = load_config() if cfg is None else cfg
    raw = str(cfg.get("DSH_RUNTIME", "")).strip().lower()
    return raw not in _DISABLED_VALUES


def provider(cfg: dict[str, str] | None = None) -> str:
    cfg = load_config() if cfg is None else cfg
    return cfg.get("DSH_PROVIDER", "VEYA_LOCAL_GATEWAY")


def base_url(cfg: dict[str, str] | None = None) -> str:
    cfg = load_config() if cfg is None else cfg
    return cfg.get("DSH_BASE_URL") or cfg.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL


def model(cfg: dict[str, str] | None = None) -> str:
    cfg = load_config() if cfg is None else cfg
    return cfg.get("DSH_MODEL") or cfg.get("DEEPSEEK_DEFAULT_MODEL") or DEFAULT_MODEL


def api_key(cfg: dict[str, str] | None = None) -> str:
    """Placeholder key only — never the (invalid) real DeepSeek secret."""
    cfg = load_config() if cfg is None else cfg
    return cfg.get("DSH_API_KEY") or DEFAULT_API_KEY


def profile(cfg: dict[str, str] | None = None) -> str:
    cfg = load_config() if cfg is None else cfg
    return cfg.get("DSH_PROFILE") or DEFAULT_PROFILE


def state_dir(cfg: dict[str, str] | None = None) -> Path:
    """Persistent user-state root for DSH sessions (never /tmp)."""
    cfg = load_config() if cfg is None else cfg
    raw = cfg.get("DSH_SESSION_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_STATE_DIR.expanduser()


def dsh_home(cfg: dict[str, str] | None = None) -> Path:
    """DSH_HOME actually handed to the binary; sessions live under it."""
    return state_dir(cfg) / "home"


def ensure_state(cfg: dict[str, str] | None = None) -> Path:
    """Create the persistent DSH home.

    `profiles/` is symlinked to the installed profile tree (it carries the
    installed `node_modules`); `sessions/` and `storages/` stay real and
    persistent under the user state dir.
    """
    cfg = load_config() if cfg is None else cfg
    home = dsh_home(cfg)
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "storages").mkdir(parents=True, exist_ok=True)
    src = Path(cfg.get("DSH_PROFILES_SRC") or DEFAULT_PROFILES_SRC).expanduser()
    link = home / "profiles"
    if src.is_dir() and not link.exists():
        with contextlib.suppress(OSError):
            link.symlink_to(src, target_is_directory=True)
    return home


def model_patch(cfg: dict[str, str] | None = None) -> str:
    """dsh profile overlay binding the DSH model id to the Veya gateway model.

    DSH resolves its model id through plugin config (not through
    `DEEPSEEK_DEFAULT_MODEL`), so the executor generates this overlay instead of
    baking a model into a hand-edited user profile.
    """
    cfg = load_config() if cfg is None else cfg
    model_id = model(cfg)
    return (
        "# Generated by server/dsh_plane.py — do not edit by hand.\n"
        "# Binds the DSH default model to a model served by the Veya LLM gateway.\n"
        "- id: llm-deepseek\n"
        "  config:\n"
        "    models:\n"
        f"      - id: {model_id}\n"
        f"        name: {model_id} (veya gateway)\n"
        "        contextWindow: 131072\n"
        "        maxTokens: 8192\n"
        "        inputModalities: [text, image]\n"
        "        systemPromptUpdate: in-history\n"
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: deepseek-official\n"
        f"    model: {model_id}\n"
    )


def model_patch_path(cfg: dict[str, str] | None = None) -> Path:
    """Persist the model overlay under the state dir (rewritten only on change)."""
    cfg = load_config() if cfg is None else cfg
    target = state_dir(cfg) / "model.patch.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    wanted = model_patch(cfg)
    try:
        existing: str | None = target.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing != wanted:
        try:
            target.write_text(wanted, encoding="utf-8")
        except OSError:
            return target
    return target


def subprocess_env(cfg: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the dsh child process.

    Only the DEEPSEEK_* variables the dsh binary itself reads are set, and they
    all point at the Veya gateway.
    """
    cfg = load_config() if cfg is None else cfg
    env = dict(os.environ)
    home = ensure_state(cfg)
    env["DEEPSEEK_BASE_URL"] = base_url(cfg)
    env["DEEPSEEK_API_KEY"] = api_key(cfg)
    env["DEEPSEEK_DEFAULT_MODEL"] = model(cfg)
    env["DSH_HOME"] = str(home)
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    for host in ("127.0.0.1", "localhost"):
        if host not in no_proxy:
            no_proxy = f"{no_proxy},{host}".strip(",")
    env["NO_PROXY"] = env["no_proxy"] = no_proxy
    return env


def dsh_argv(bin_path: str, prompt: str, cfg: dict[str, str] | None = None) -> list[str]:
    """`dsh --profile headless [--patch <overlay>] "<task>"`."""
    cfg = load_config() if cfg is None else cfg
    argv = [bin_path, "--profile", profile(cfg)]
    patch = model_patch_path(cfg)
    if patch.is_file():
        argv += ["--patch", str(patch)]
    argv.append(prompt)
    return argv
