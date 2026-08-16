"""run_in_sandbox 命令归一: python→python3 / pip→pip3 (沙箱无 python 软链)。"""

from __future__ import annotations

from server.tool_registry import _normalize_sandbox_command as norm


def test_python_to_python3():
    assert norm("python -m unittest discover") == "python3 -m unittest discover"
    assert norm("run python") == "run python3"


def test_pip_to_pip3():
    assert norm("pip install foo") == "pip3 install foo"


def test_already_versioned_untouched():
    assert norm("python3 -m unittest") == "python3 -m unittest"
    assert norm("pip3 install foo") == "pip3 install foo"


def test_substrings_and_paths_untouched():
    assert norm("mypython script") == "mypython script"  # 非整词不动
    assert norm("/usr/bin/python3 x") == "/usr/bin/python3 x"
    assert norm("echo pytest") == "echo pytest"  # pytest 不是 python/pip
