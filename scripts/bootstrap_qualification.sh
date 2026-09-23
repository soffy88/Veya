#!/usr/bin/env bash
set -euo pipefail

# Rebuild the qualification environment from the declared lock graph.  This
# script must work in a newly checked-out worktree; it must not depend on a
# user's pre-installed docker/ruff or on the repository's historical venv.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$UV_BIN" ]]; then
  echo "QUALIFICATION_BOOTSTRAP=FAIL (uv is required)" >&2
  exit 2
fi

PYTHON_BIN="${QUALIFICATION_PYTHON:-$(command -v python3.12 || command -v python3 || true)}"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "EXPECTED_PYTHON_RUNTIME=FAIL (Python 3.12+ not found)" >&2
  exit 2
fi

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit(f"Python >= 3.12 required, got {sys.version}")
print(f"EXPECTED_PYTHON_RUNTIME=PASS ({sys.version.split()[0]})")
PY

ENV_DIR="${QUALIFICATION_ENV_DIR:-$ROOT/.venv}"
QUAL_PYTHON="$ENV_DIR/bin/python"
export UV_PROJECT_ENVIRONMENT="$ENV_DIR"
"$UV_BIN" sync --locked --extra qualification --python "$PYTHON_BIN"
if [[ ! -x "$QUAL_PYTHON" ]]; then
  echo "QUALIFICATION_DEPS_INSTALL=FAIL ($QUAL_PYTHON missing)" >&2
  exit 2
fi
echo "QUALIFICATION_DEPS_INSTALL=PASS"

export PYTHONPATH="$ROOT:$ROOT/platform/3O/oprim:$ROOT/platform/3O/oskill:$ROOT/platform/3O/omodul:$ROOT/platform/3O/obase:$ROOT/platform/3O/oservi${PYTHONPATH:+:$PYTHONPATH}"

"$QUAL_PYTHON" - <<'PY'
import importlib
import sys
from pathlib import Path

import docker
import veya_loop

print(f"DOCKER_IMPORT=PASS ({docker.__version__})")
print(f"VEYA_LOOP_IMPORT=PASS ({veya_loop.__file__})")
for name in ("oskill", "oprim", "omodul", "obase", "oservi"):
    importlib.import_module(name)
    print(f"3O_IMPORT_{name.upper()}=PASS")
bin_dir = Path(sys.executable).parent
for command in ("ruff", "pytest", "mypy"):
    binary = bin_dir / command
    if not binary.exists():
        raise SystemExit(f"{command} binary unavailable")
    print(f"{command.upper()}_BINARY=PASS")
PY
echo "3O_RUNTIME_PREFLIGHT=PASS"

echo "== full test collection =="
"$QUAL_PYTHON" -m pytest --collect-only -q
echo "PYTEST_COLLECTION=PASS"
echo "FULL_TEST_COLLECTION=PASS"
echo "FRESH_WORKTREE_BOOTSTRAP=PASS"
