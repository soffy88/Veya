with open("platform/3O/oskill/oskill/_harness_argv.py", "r") as f:
    content = f.read()
if "extra: list[str] | None = None" not in content:
    content = content.replace("coding_mode: bool = False,", "coding_mode: bool = False,\n    extra: list[str] | None = None,")
if "if extra:" not in content:
    content = content.replace("return {\"ok\": True, \"engine\": name, \"argv\": argv, \"error\": \"\"}", "if extra:\n        argv.extend(extra)\n    return {\"ok\": True, \"engine\": name, \"argv\": argv, \"error\": \"\"}")
with open("platform/3O/oskill/oskill/_harness_argv.py", "w") as f:
    f.write(content)
