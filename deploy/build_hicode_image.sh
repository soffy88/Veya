#!/usr/bin/env bash
set -euo pipefail

# Build the production image with a committed 3O source context.  The owner
# checkout is intentionally not used for the managed Python companion.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:-veya-backend:latest}"
CLEAN_CONTEXT="$(mktemp -d "${TMPDIR:-/tmp}/veya-hicode-3o.XXXXXX")"
trap 'rm -rf "$CLEAN_CONTEXT"' EXIT

python3 "$ROOT/deploy/prepare_hicode_3o_context.py" "$ROOT" "$CLEAN_CONTEXT"
docker build \
  --build-context hicode-3o="$CLEAN_CONTEXT" \
  -f "$ROOT/deploy/Dockerfile.backend" \
  -t "$IMAGE" \
  "$ROOT"
