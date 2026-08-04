# 插件 SDK（G10）

插件 = 目录 + `manifest.json` + Python 入口模块。

```
my-plugin/
├── manifest.json        # name/version/description/entry/hooks
└── plugin.py            # def activate(ctx) -> None
```

## manifest.json

```json
{
  "name": "greet",
  "version": "0.1.0",
  "description": "Registers a greeting tool and a pre_dispatch hook.",
  "author": "veya-team",
  "entry": "plugin:activate",
  "hooks": ["pre_dispatch"]
}
```

`entry` 格式为 `module:function`（本目录下）。

## 入口函数

```python
def activate(ctx):
    ctx.register_tool("greet", lambda name: f"Hello, {name}!")
    ctx.register_hook("pre_dispatch", lambda payload: {"plugin": ctx.plugin_name, "event": payload})
```

`PluginContext` 提供：
- `register_tool(name, fn)` — 注册工具（进入全局工具表，agent 可调用）
- `register_hook(hook_name, fn)` — 注册生命周期钩子
- `get_tools()` / `plugin_name`

## 安装与发现

```python
from registries.plugins import install_plugin, discover_plugins, run_plugin_hooks

install_plugin("examples/plugins/greet-plugin")  # 复制到 ~/.veya/plugins/
discover_plugins()  # 扫描并激活全部插件
run_plugin_hooks("pre_dispatch", {"step": 1})  # 分发事件
```

## 参考实现

`examples/plugins/greet-plugin/` 是可直接安装的示例；测试见
`tests/test_plugins_g10.py`（13 例）。
