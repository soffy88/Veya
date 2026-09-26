with open("platform/3O/oskill/oskill/_harness_argv.py") as f:
    content = f.read()

content = content.replace(
    '"--auto", "--yolo", "--dangerously-skip-permissions"', '"--auto", "--yolo"'
)

with open("platform/3O/oskill/oskill/_harness_argv.py", "w") as f:
    f.write(content)
