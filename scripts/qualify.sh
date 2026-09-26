#!/usr/bin/env bash
set -euo pipefail

# One reproducible qualification entrypoint. The 3O repositories are checked
# out as pinned git submodules and projected into the import surface used by
# CI and the backend container.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/bootstrap_qualification.sh"
PYTHON_BIN="$ROOT/.venv/bin/python"

export PYTHONPATH="$ROOT:$ROOT/platform/3O/oprim:$ROOT/platform/3O/oskill:$ROOT/platform/3O/omodul:$ROOT/platform/3O/obase:$ROOT/platform/3O/oservi${PYTHONPATH:+:$PYTHONPATH}"

echo "== qualification preflight =="
"$PYTHON_BIN" - <<'PY'
import importlib
import platform
import sys

if sys.version_info < (3, 12):
    raise SystemExit(f"Python >= 3.12 required, got {platform.python_version()}")
for name in ("oskill", "oprim", "omodul", "obase", "oservi"):
    importlib.import_module(name)
    print(f"3O import: {name}=PASS")
PY

echo "== pinned source fingerprint =="
git submodule status --recursive
sha256sum pyproject.toml uv.lock platform/3O/CANONICAL_PINS.json 2>/dev/null || true

echo "== architecture gates =="
"$PYTHON_BIN" scripts/check_architecture_manifest.py .
"$PYTHON_BIN" scripts/check_single_master_path.py .

echo "== lint =="
"$ROOT/.venv/bin/ruff" check .
"$ROOT/.venv/bin/ruff" format --check .

echo "== targeted qualification =="
"$PYTHON_BIN" -m pytest -q \
  tests/architecture \
  tests/goal_run \
  tests/runtime/test_execution_runtime.py

echo "== executor qualification =="
"$PYTHON_BIN" scripts/qualify_executors.py --deterministic

echo "== full qualification =="
"$PYTHON_BIN" -m pytest -q
echo "QUALIFICATION=PASS"
