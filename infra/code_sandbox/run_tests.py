"""Code sandbox test runner (scheme C).

协议: stdin JSON → stdout JSON。

输入:
    {"files": {"main.py": "...", "tests/test_main.py": "..."},
     "test_args": ["-q", "--tb=short"], "timeout_sec": 60}

输出 (对齐 TestResult):
    {"passed": bool, "n_passed": int, "n_failed": int, "duration_s": float,
     "stdout": str, "stderr": str, "failed_nodeids": [str], "metadata": {}}

安全: 仅允许相对路径 (拒绝 ..), 运行在无网/只读容器内 (由 docker run 参数保证)。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _safe_relpath(rel: str) -> bool:
    """仅允许相对路径且不含 .."""
    return not (rel.startswith("/") or ".." in Path(rel).parts)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        print(
            json.dumps(
                {
                    "passed": False,
                    "n_failed": 1,
                    "stderr": "bad stdin json",
                    "metadata": {"env_error": True},
                }
            )
        )
        return 1

    files: dict = payload.get("files") or {}
    test_args: list = payload.get("test_args") or ["-q", "--tb=short"]
    timeout_sec: float = float(payload.get("timeout_sec") or 60.0)

    # 写入临时工作目录 (仅允许相对路径)
    work = Path(tempfile.mkdtemp(prefix="ws-", dir="/work"))
    try:
        for rel, content in files.items():
            if not _safe_relpath(rel):
                print(
                    json.dumps(
                        {
                            "passed": False,
                            "n_failed": 1,
                            "stderr": f"illegal path: {rel}",
                            "metadata": {"env_error": True},
                        }
                    )
                )
                return 1
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        # 跑 pytest: junitxml 内建输出 (版本无关, 结构稳定) + 简短 stdout
        junit_path = "/tmp/junit.xml"
        cmd = [sys.executable, "-m", "pytest", *test_args, f"--junitxml={junit_path}"]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=work, capture_output=True, text=True, timeout=timeout_sec
            )
            timed_out = False
        except subprocess.TimeoutExpired:
            proc = None
            timed_out = True

        duration = time.time() - t0

        if timed_out:
            print(
                json.dumps(
                    {
                        "passed": False,
                        "n_passed": 0,
                        "n_failed": 1,
                        "duration_s": round(duration, 3),
                        "stdout": "",
                        "stderr": f"timeout after {timeout_sec}s",
                        "failed_nodeids": [],
                        "metadata": {"timeout": True},
                    }
                )
            )
            return 1

        out_text = proc.stdout or ""
        err_text = proc.stderr or ""
        n_passed = n_failed = 0
        failed_nodeids: list[str] = []

        # 解析 junitxml (pytest 内建, 版本无关)
        try:
            import xml.etree.ElementTree as ET

            root = ET.parse(junit_path).getroot()
            # pytest 9: 根是 <testsuites>, 计数在子 <testsuite> 上
            suite = root if root.tag == "testsuite" else root.find("testsuite")
            n_passed = (
                int(suite.get("tests") or 0)
                - int(suite.get("failures") or 0)
                - int(suite.get("errors") or 0)
            )
            n_failed = int(suite.get("failures") or 0) + int(suite.get("errors") or 0)
            for tc in suite.iter("testcase"):
                if tc.find("failure") is not None or tc.find("error") is not None:
                    cls = tc.get("classname") or ""
                    name = tc.get("name") or ""
                    failed_nodeids.append(f"{cls}::{name}" if cls else name)
        except Exception:
            # 兜底: 解析简短输出
            import re

            m = re.search(r"(\d+) passed", out_text)
            n_passed = int(m.group(1)) if m else 0
            m = re.search(r"(\d+) failed", out_text)
            n_failed = int(m.group(1)) if m else (0 if n_passed else 1)
            failed_nodeids = re.findall(r"^FAILED (\S+) -", out_text, re.MULTILINE)
        if "no tests ran" in out_text and n_passed == 0 and n_failed == 0:
            n_failed = 1
            failed_nodeids = failed_nodeids or ["<no-tests-collected>"]

        passed = n_failed == 0 and n_passed > 0
        print(
            json.dumps(
                {
                    "passed": passed,
                    "n_passed": n_passed,
                    "n_failed": n_failed,
                    "duration_s": round(duration, 3),
                    "stdout": out_text[-4000:],
                    "stderr": err_text[-2000:],
                    "failed_nodeids": failed_nodeids,
                    "metadata": {"timeout": False, "python": sys.version.split()[0]},
                },
                ensure_ascii=False,
            )
        )
        return 0 if passed else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "n_failed": 1,
                    "stderr": f"sandbox internal error: {exc}\n{traceback.format_exc()[-1000:]}",
                    "metadata": {"env_error": True},
                }
            )
        )
        return 1
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
