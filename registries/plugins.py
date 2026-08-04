"""
Plugin SDK (G10) — manifest, install, discover, activate.

A plugin is a directory with a ``manifest.json`` and a Python module exposing an
``activate(ctx)`` entry point. Example::

    my-plugin/
    ├── manifest.json        # name/version/description/entry/hooks
    └── plugin.py            # def activate(ctx: PluginContext) -> None

``activate`` receives a :class:`PluginContext` with ``register_tool`` /
``register_hook`` / ``plugin_name``. The legacy in-process registry
(``register_plugin`` / ``get_plugin`` / ``list_plugins``) is preserved.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from registries.tools import get_registered_tools, list_tools, register_tool  # noqa: F401

# ---------------------------------------------------------------------------
# In-process registry (legacy API preserved)
# ---------------------------------------------------------------------------

_PLUGIN_REGISTRY: dict[str, Callable] = {}
_PLUGIN_HOOKS: dict[str, list[tuple[str, Callable]]] = {}


def register_plugin(name: str, fn: Callable) -> None:
    _PLUGIN_REGISTRY[name] = fn


def get_plugin(name: str) -> Callable | None:
    return _PLUGIN_REGISTRY.get(name)


def list_plugins() -> list[str]:
    return list(_PLUGIN_REGISTRY)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_DEFAULT_PLUGIN_DIR = pathlib.Path.home() / ".veya" / "plugins"


class PluginError(RuntimeError):
    """Raised for malformed manifests or failed plugin activation."""


@dataclass
class PluginManifest:
    """Declared metadata of a plugin (validated against manifest.json)."""

    name: str
    version: str = "0.1.0"
    description: str = ""
    entry: str = "plugin:activate"
    hooks: list[str] = field(default_factory=list)
    author: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifest:
        name = str(data.get("name") or "").strip()
        if not name:
            raise PluginError("manifest.json must declare a non-empty 'name'")
        entry = str(data.get("entry") or "plugin:activate")
        if ":" not in entry:
            raise PluginError(f"entry must be 'module:function', got {entry!r}")
        hooks = data.get("hooks", [])
        if not isinstance(hooks, list) or not all(isinstance(h, str) for h in hooks):
            raise PluginError("'hooks' must be a list of hook names")
        return cls(
            name=name,
            version=str(data.get("version") or "0.1.0"),
            description=str(data.get("description") or ""),
            entry=entry,
            hooks=hooks,
            author=str(data.get("author") or ""),
        )

    @classmethod
    def from_file(cls, manifest_path: str | pathlib.Path) -> PluginManifest:
        path = pathlib.Path(manifest_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginError(f"cannot read manifest {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PluginError(f"manifest {path} must be a JSON object")
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# Plugin context (the public SDK surface handed to activate())
# ---------------------------------------------------------------------------


class PluginContext:
    """SDK surface exposed to plugins inside ``activate(ctx)``."""

    def __init__(self, manifest: PluginManifest):
        self.manifest = manifest

    @property
    def plugin_name(self) -> str:
        return self.manifest.name

    def register_tool(self, name: str, fn: Callable) -> None:
        """Register a tool callable into the global tool registry."""
        if not callable(fn):
            raise PluginError(f"tool {name!r} must be callable")
        try:
            register_tool(name, fn)
        except Exception as exc:
            raise PluginError(f"cannot register tool {name!r}: {exc}") from exc

    def register_hook(self, hook_name: str, fn: Callable) -> None:
        """Register a lifecycle hook (pre_dispatch/post_result/...)."""
        if not callable(fn):
            raise PluginError(f"hook {hook_name!r} must be callable")
        _PLUGIN_HOOKS.setdefault(hook_name, []).append((self.manifest.name, fn))

    def get_tools(self) -> list[str]:
        """List currently registered tool names."""
        return list_tools()


# ---------------------------------------------------------------------------
# Install / discover / activate
# ---------------------------------------------------------------------------


def install_plugin(
    source_dir: str | pathlib.Path,
    plugins_dir: str | pathlib.Path | None = None,
) -> PluginManifest:
    """Copy a plugin directory into the plugins dir and validate its manifest.

    Returns the validated manifest. Raises PluginError on malformed manifests.
    """
    src = pathlib.Path(source_dir)
    if not src.is_dir():
        raise PluginError(f"plugin source {src} is not a directory")
    manifest = PluginManifest.from_file(src / "manifest.json")

    dest_root = pathlib.Path(plugins_dir) if plugins_dir else _DEFAULT_PLUGIN_DIR
    dest = dest_root / manifest.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return manifest


def _import_entry(plugin_dir: pathlib.Path, manifest: PluginManifest) -> Callable:
    module_path = plugin_dir / f"{manifest.entry.split(':')[0]}.py"
    if not module_path.exists():
        raise PluginError(f"entry module missing: {module_path}")
    module_name = f"veya_plugin_{manifest.name}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot load plugin module {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn_name = manifest.entry.split(":", 1)[1]
    fn = getattr(module, fn_name, None)
    if not callable(fn):
        raise PluginError(f"entry function {fn_name!r} not found in {module_path}")
    return fn


def activate_plugin(
    plugin_dir: str | pathlib.Path,
    ctx: PluginContext | None = None,
) -> PluginManifest:
    """Load + activate a plugin directory (reads manifest.json, calls entry)."""
    path = pathlib.Path(plugin_dir)
    manifest = PluginManifest.from_file(path / "manifest.json")
    entry = _import_entry(path, manifest)
    entry(ctx or PluginContext(manifest))
    register_plugin(manifest.name, entry)
    return manifest


def discover_plugins(
    plugins_dir: str | pathlib.Path | None = None,
) -> list[PluginManifest]:
    """Scan the plugins dir, activate every directory with a manifest.json.

    Returns the list of activated manifests. Malformed plugins are skipped
    (collected via a logger-free warning print) rather than aborting discovery.
    """
    root = pathlib.Path(plugins_dir) if plugins_dir else _DEFAULT_PLUGIN_DIR
    activated: list[PluginManifest] = []
    if not root.is_dir():
        return activated
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / "manifest.json").exists():
            continue
        try:
            activated.append(activate_plugin(child))
        except PluginError as exc:
            print(f"[plugins] skip {child.name}: {exc}")
    return activated


def run_plugin_hooks(hook_name: str, payload: Any = None) -> list[Any]:
    """Dispatch ``payload`` to every registered hook under ``hook_name``.

    Returns collected return values (truthy values can veto/annotate a step;
    the caller decides the semantics per hook).
    """
    results: list[Any] = []
    for _plugin, fn in _PLUGIN_HOOKS.get(hook_name, []):
        results.append(fn(payload))
    return results
