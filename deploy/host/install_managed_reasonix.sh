#!/usr/bin/env bash
# Install the Veya-managed Reasonix runtime on the host (Veya Local).
#
# Veya Local runs the Remote MCP gateway as a host process, so it does not see
# the official image's /opt/veya/hicode-runtime. This extracts that managed
# runtime from the official image into a host path and verifies the exact
# version required by server/hicode_runtime.py.
#
# Usage:
#   deploy/host/install_managed_reasonix.sh [IMAGE] [DEST]
# Defaults: IMAGE=veya-backend:latest  DEST=$HOME/.veya/hicode-managed-runtime
set -euo pipefail

IMAGE="${1:-veya-backend:latest}"
DEST="${2:-$HOME/.veya/hicode-managed-runtime}"
EXPECTED="1.21.3"
CONTAINER="veya-hicode-extract-$$"

command -v docker >/dev/null || { echo "error: docker not available" >&2; exit 2; }

echo "extracting /opt/veya/hicode-runtime from $IMAGE -> $DEST"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker create --name "$CONTAINER" "$IMAGE" >/dev/null
trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true' EXIT
docker cp "$CONTAINER:/opt/veya/hicode-runtime" "$DEST" >/dev/null

BIN="$DEST/node_modules/.bin/reasonix"
[ -x "$BIN" ] || { echo "error: $BIN is missing or not executable" >&2; exit 1; }

# Reasonix is `#!/usr/bin/env node`; systemd user services have no nvm on PATH,
# so a stable node symlink directory is provided for the unit's PATH.
NODE="$(command -v node || true)"
if [ -n "$NODE" ]; then
  mkdir -p "$HOME/.veya/hicode-runtime/bin"
  for b in node npm npx; do
    src="$(dirname "$NODE")/$b"
    [ -e "$src" ] && ln -sf "$src" "$HOME/.veya/hicode-runtime/bin/$b"
  done
fi

VERSION="$("$BIN" --version 2>&1 | head -1)"
echo "installed: $VERSION"
case "$VERSION" in
  *"$EXPECTED"*) echo "OK: Reasonix $EXPECTED" ;;
  *) echo "error: expected Reasonix $EXPECTED, got: $VERSION" >&2; exit 1 ;;
esac
