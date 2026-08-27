#!/usr/bin/env python3
"""Veya: 引用 3O 元素执行单一高复杂度业务节点。

将不可信的高危 Pandas 数据处理逻辑包装进 3O O3 沙箱元素中，
利用沙箱池隔离 + 探针观察前向执行，防止宿主机 OOM 或崩溃。

3O 元素依赖:
  - obase.local_sandbox_pool   (O3 infra — 隔离执行池)
  - omodul.sandbox_observe_lookahead  (O3 事务 — 沙箱探针观察)

若主库未安装，自动降级到 veya 内置 sandbox + 模拟探针。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

# ── 环境映射 ──────────────────────────────────────────────────────────
_HICODE = Path(__file__).resolve().parent.parent
_PLATFORM = _HICODE / "platform" / "3O"

for _p in [
    str(_PLATFORM / "obase"),
    str(_PLATFORM / "obase" / "obase"),
    str(_PLATFORM / "omodul"),
    str(_PLATFORM / "omodul" / "omodul"),
    str(_PLATFORM / "oprim"),
    str(_PLATFORM / "oprim" / "oprim"),
    str(_PLATFORM / "oskill"),
    str(_PLATFORM / "oskill" / "oskill"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 3O 元素加载 (容错降级) ──────────────────────────────────────────

_SANDBOX_POOL = None
_SANDBOX_OBSERVE = None
_FALLBACK = False

try:
    from obase.local_sandbox_pool import LocalSandboxPool

    _SANDBOX_POOL = LocalSandboxPool
except ImportError:
    pass

try:
    from omodul.sandbox_observe_lookahead import sandbox_observe_lookahead

    _SANDBOX_OBSERVE = sandbox_observe_lookahead
except ImportError:
    pass

if _SANDBOX_POOL is None or _SANDBOX_OBSERVE is None:
    _FALLBACK = True


def _fallback_sandbox_execute(code: str, timeout: float) -> Dict[str, Any]:
    """降级方案: 使用 veya 内置沙箱执行不可信代码。

    模拟 3O O3 沙箱探针的隔离 + 观察行为。
    """
    import subprocess
    import tempfile
    import time

    code_path = Path(tempfile.mkdtemp(prefix="veya_sandbox_")) / "exec.py"
    code_path.write_text(f"""
import sys, traceback
try:
{chr(10).join("    " + line for line in code.strip().split(chr(10)))}
    print("SANDBOX_OK:" + str(execute()))
except Exception as e:
    print(f"SANDBOX_ERR:{{e}}")
    traceback.print_exc()
""")

    try:
        proc = subprocess.run(
            [sys.executable, str(code_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout
        if "SANDBOX_OK:" in stdout:
            result_text = stdout.split("SANDBOX_OK:", 1)[1].strip()
            return {"status": "success", "result": result_text, "score": 1.0}
        elif "SANDBOX_ERR:" in stdout:
            return {"status": "error", "error": stdout.split("SANDBOX_ERR:", 1)[1].strip()}
        else:
            return {"status": "error", "error": proc.stderr[:500]}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": f"Execution exceeded {timeout}s"}
    finally:
        import shutil

        shutil.rmtree(code_path.parent, ignore_errors=True)


def run_complex_pandas_task(data_path: str = "dummy_data.csv") -> Dict[str, Any]:
    """
    业务逻辑：高复杂度的 Pandas 清洗。
    为防止业务代码引发宿主机 OOM 或崩溃，将其包装进 3O 元素的隔离沙箱中。
    """
    # 模拟由 LLM 生成或外部注入的不可信业务代码
    untrusted_business_code = """
import pandas as pd
import numpy as np
def execute():
    df = pd.DataFrame(np.random.randint(0, 100, size=(100000, 50)))
    if df.isnull().values.any():
        raise ValueError("Data validation failed.")
    return "Data cleaned successfully."
"""

    if _FALLBACK:
        print("[veya_core] 3O O3 沙箱元素未挂载，使用 veya 内置沙箱降级执行")
        # 模拟探针：检查代码是否包含 execute() 函数
        probe = lambda code, timeout: (
            {"status": "success", "score": 1.0} if "execute()" in code else {"status": "error"}
        )
        probe_result = probe(untrusted_business_code, 15.0)
        if probe_result["status"] != "success":
            return {"status": "error", "reason": "probe_validation_failed"}

        result = _fallback_sandbox_execute(untrusted_business_code, timeout=15.0)
        return result

    # ── 3O 主路径: O3 沙箱池 + 探针观察前向执行 ────────────────────
    sandbox_pool = _SANDBOX_POOL(max_workers=2)
    config = {
        "sandbox_pool": sandbox_pool,
        "probe_op": lambda code, timeout: (
            {"status": "success", "score": 1.0} if "execute()" in code else {"status": "error"}
        ),
    }

    result = _SANDBOX_OBSERVE(
        config=config,
        input_data={"code": untrusted_business_code, "timeout": 15.0},
        output_dir="/tmp/veya_runs/",
    )

    return result


if __name__ == "__main__":
    res = run_complex_pandas_task("dummy_data.csv")
    print(f"Single Node Execution Result: {res.get('status', 'unknown')}")
    if res.get("result"):
        print(f"  Output: {res['result']}")
    if res.get("error"):
        print(f"  Error: {res['error']}")
    if res.get("score"):
        print(f"  Score: {res['score']}")
