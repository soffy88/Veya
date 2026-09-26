with open("server/engine_runner.py", "r") as f:
    content = f.read()
if "extra: list[str] | None = None" not in content:
    content = content.replace("coding_mode: bool = False,", "coding_mode: bool = False,\n    extra: list[str] | None = None,")
    content = content.replace("extra=extra or None", "extra=extra or None") # Do not replace, just need to make sure we pass extra to build_argv in tool_adapter
with open("server/engine_runner.py", "w") as f:
    f.write(content)
