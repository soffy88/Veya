"""Example Veya plugin (G10 SDK)."""


def _greet(name: str) -> str:
    return f"Hello, {name}! (from plugin greet v0.1.0)"


def activate(ctx) -> None:
    ctx.register_tool("greet", _greet)
    ctx.register_hook(
        "pre_dispatch",
        lambda payload: {"plugin": ctx.plugin_name, "event": payload},
    )
