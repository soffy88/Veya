with open("platform/3O/oskill/oskill/_harness_argv.py", "r") as f:
    content = f.read()

content = content.replace('argv.extend(["--dir", workspace, "--format", "json", "--auto"])', 'argv.extend(["--dir", workspace, "--format", "json", "--auto", "--permission-mode", "bypassPermissions", "--always-approve"])')

with open("platform/3O/oskill/oskill/_harness_argv.py", "w") as f:
    f.write(content)
