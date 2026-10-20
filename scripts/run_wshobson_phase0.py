"""Run Wshobson Phase 0 smoke test and verify index capabilities."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    print("=== Wshobson Phase 0 Smoke Runner ===")

    # Check upstream count
    import glob
    ws_root = os.environ.get("VEYA_WSHOBSON_ROOT", os.path.expanduser("~/projects/wshobson-agents"))
    skills_glob = os.path.join(ws_root, "plugins", "*", "skills", "*", "SKILL.md")
    matched = glob.glob(skills_glob)
    print(f"Upstream wshobson-agents SKILL.md count: {len(matched)}")

    # Run pytest
    print("Running pytest on test_wshobson_index and test_bughunter_index...")
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/server/test_wshobson_index.py", "tests/server/test_bughunter_index.py"])
    if res.returncode != 0:
        print("Pytest failed!")
        return res.returncode

    # Run ruff check
    print("Running ruff check on changed files...")
    ruff_res = subprocess.run(["./venv/bin/ruff", "check", "server/external_skill_index.py", "server/bughunter_index.py", "tests/server/test_wshobson_index.py", "tests/server/test_bughunter_index.py"])
    if ruff_res.returncode != 0:
        print("Ruff check failed!")
        return ruff_res.returncode

    print("All smoke checks passed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
