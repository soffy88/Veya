from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from hooks.types import HookInput, HookOutput

# Use the current interpreter so pytest is always found (venv-safe)
_PYTHON = sys.executable


async def test_gate(inp: HookInput) -> HookOutput:
    """H4: run pytest after execute/build turn; block on failure.

    subprocess 在 to_thread 中运行,避免阻塞事件循环(SSE/interrupt 保活)。
    设 ``HICODE_SKIP_TEST_GATE=1``(测试套件 conftest 自动设置)时跳过,
    避免 agent 执行分队 → 测试门 → pytest → 再进执行分队的递归。
    """
    if os.environ.get("HICODE_SKIP_TEST_GATE") == "1":
        return HookOutput(decision="pass", reason="test gate skipped (HICODE_SKIP_TEST_GATE=1)")

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [_PYTHON, "-m", "pytest", "-x", "-q", "--tb=short"],
            cwd=inp.cwd,
            capture_output=True,
            timeout=300,
            text=True,
        )

        code = result.returncode
        stdout = result.stdout + result.stderr

        if code != 0:
            return HookOutput(
                decision="block",
                reason=f"Tests failed:\n{stdout[-2000:]}",
            )
        return HookOutput(decision="pass")
    except subprocess.TimeoutExpired:
        return HookOutput(decision="block", reason="Tests timed out after 300 seconds")
    except Exception as e:
        return HookOutput(decision="block", reason=f"Test execution error: {e!s}")
