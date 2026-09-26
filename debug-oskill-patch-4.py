with open("platform/3O/oskill/oskill/_harness_argv.py") as f:
    content = f.read()

content = content.replace('"--yes"', '"--permission-mode", "bypassPermissions", "--always-approve"')

with open("platform/3O/oskill/oskill/_harness_argv.py", "w") as f:
    f.write(content)
