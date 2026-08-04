"""G10 — plugin SDK: manifest, install, discover, activate, hooks.

Uses the checked-in example plugin (examples/plugins/greet-plugin) and temp
copies thereof, so tests never touch the real ~/.veya/plugins dir.
"""

from __future__ import annotations

import pytest

from registries import plugins as plug
from registries.tools import get_registered_tools, list_tools

_EXAMPLE = "examples/plugins/greet-plugin"


@pytest.fixture(autouse=True)
def _isolate_registries():
    """Reset in-process registries so tests are order-independent."""
    before_tools = get_registered_tools()
    plug._PLUGIN_HOOKS.clear()
    plug._PLUGIN_REGISTRY.clear()
    yield
    plug._PLUGIN_HOOKS.clear()
    plug._PLUGIN_REGISTRY.clear()
    from server.assembly import _ALL_TOOLS

    for name in list(_ALL_TOOLS):
        if name not in before_tools:
            del _ALL_TOOLS[name]


@pytest.fixture()
def installed_dir(tmp_path):
    """Install the example plugin into a temp plugins dir; return its path."""
    dest = tmp_path / "plugins"
    dest.mkdir()
    plug.install_plugin(_EXAMPLE, dest)
    return dest / "greet"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_from_file(installed_dir):
    m = plug.PluginManifest.from_file(installed_dir / "manifest.json")
    assert m.name == "greet"
    assert m.version == "0.1.0"
    assert m.entry == "plugin:activate"
    assert m.hooks == ["pre_dispatch"]
    assert m.author == "veya-team"


def test_manifest_requires_name():
    with pytest.raises(plug.PluginError):
        plug.PluginManifest.from_dict({"version": "1.0.0"})


def test_manifest_requires_module_colon_function_entry():
    with pytest.raises(plug.PluginError):
        plug.PluginManifest.from_dict({"name": "x", "entry": "no-colon"})


def test_manifest_bad_hooks_type():
    with pytest.raises(plug.PluginError):
        plug.PluginManifest.from_dict({"name": "x", "hooks": "pre_dispatch"})


def test_manifest_missing_file(tmp_path):
    with pytest.raises(plug.PluginError):
        plug.PluginManifest.from_file(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# Install / discover / activate
# ---------------------------------------------------------------------------


def test_install_copies_and_validates(tmp_path, installed_dir):
    assert installed_dir.exists()
    assert (installed_dir / "manifest.json").exists()
    assert (installed_dir / "plugin.py").exists()


def test_install_rejects_missing_dir(tmp_path):
    with pytest.raises(plug.PluginError):
        plug.install_plugin(tmp_path / "does-not-exist", tmp_path)


def test_activate_registers_tool_and_hook(installed_dir):
    plug.activate_plugin(installed_dir)
    assert plug.get_plugin("greet") is not None
    assert "greet" in list_tools()

    # 工具真实可用(闭路:插件工具进入全局工具表)
    assert get_registered_tools()["greet"]("veya") == "Hello, veya! (from plugin greet v0.1.0)"

    # hook 已注册并可分发
    results = plug.run_plugin_hooks("pre_dispatch", {"step": 1})
    assert results and results[0]["plugin"] == "greet"
    assert results[0]["event"] == {"step": 1}


def test_discover_activates_all(tmp_path, installed_dir):
    # 再装一个伪插件
    extra = tmp_path / "plugins" / "noop"
    extra.mkdir()
    (extra / "manifest.json").write_text('{"name": "noop", "entry": "plugin:activate"}')
    (extra / "plugin.py").write_text(
        "def activate(ctx):\n    ctx.register_hook('post_result', lambda p: p)\n"
    )

    manifests = plug.discover_plugins(tmp_path / "plugins")
    names = {m.name for m in manifests}
    assert {"greet", "noop"} <= names
    assert plug.run_plugin_hooks("post_result", "x") == ["x"]


def test_discover_skips_malformed(tmp_path):
    bad = tmp_path / "plugins" / "bad"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text('{"version": "1.0"}')  # no name → error
    manifests = plug.discover_plugins(tmp_path / "plugins")
    assert manifests == []


def test_activate_missing_entry_module(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "manifest.json").write_text('{"name": "p", "entry": "plugin:activate"}')
    with pytest.raises(plug.PluginError):
        plug.activate_plugin(d)


def test_plugin_context_registers_invalid_tool(installed_dir):
    ctx = plug.PluginContext(plug.PluginManifest.from_file(installed_dir / "manifest.json"))
    with pytest.raises(plug.PluginError):
        ctx.register_tool("not-callable", 42)


def test_legacy_api_preserved():
    fn = lambda x: x  # noqa: E731
    plug.register_plugin("legacy", fn)
    assert plug.get_plugin("legacy") is fn
    assert "legacy" in plug.list_plugins()
