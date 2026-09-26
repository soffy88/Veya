with open("platform/3O/oskill/oskill/_harness_argv.py", "r") as f:
    content = f.read()

content = content.replace('"--yes"', '"--auto", "--yolo", "--dangerously-skip-permissions"')

with open("platform/3O/oskill/oskill/_harness_argv.py", "w") as f:
    f.write(content)
