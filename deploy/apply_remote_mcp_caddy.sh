#!/usr/bin/env bash
# Publish the Veya Remote MCP gateway at https://veya.aiinote.com/mcp.
#
# The veya site is served by the aegis Caddy (container `aegis-caddy`), whose
# Caddyfile is owned by the operator (no root needed) and bind-mounted into the
# container. This script:
#   1. backs the Caddyfile up,
#   2. idempotently inserts `handle /mcp` and `handle /mcp/health` BEFORE the
#      veya site's existing `handle /mcp/*` (which stays on the Veya backend,
#      unchanged), pointing the new routes at the loopback->bridge relay
#      172.18.0.1:8792,
#   3. runs `caddy validate`,
#   4. reloads only on a successful validate,
#   5. restores the backup (and reloads the old config) on any failure.
#
# Usage:  deploy/apply_remote_mcp_caddy.sh
# Env:    CADDYFILE=/data/soffy/projects/aegis/Caddyfile  CADDY_CONTAINER=aegis-caddy
set -euo pipefail

CADDYFILE="${CADDYFILE:-/data/soffy/projects/aegis/Caddyfile}"
CADDY_CONTAINER="${CADDY_CONTAINER:-aegis-caddy}"
CADDY_IN_CONTAINER="/etc/caddy/Caddyfile"
UPSTREAM="${REMOTE_MCP_UPSTREAM:-172.18.0.1:8792}"
BACKUP=""

log() { printf '%s\n' "$*"; }

[ -f "$CADDYFILE" ] || { log "error: Caddyfile not found: $CADDYFILE"; exit 1; }
[ -w "$CADDYFILE" ] || { log "error: Caddyfile not writable (needs sudo/root): $CADDYFILE"; exit 2; }
command -v docker >/dev/null || { log "error: docker not available"; exit 2; }
docker inspect "$CADDY_CONTAINER" >/dev/null 2>&1 || { log "error: container not found: $CADDY_CONTAINER"; exit 2; }

if grep -q "$UPSTREAM" "$CADDYFILE"; then
  log "already published (upstream $UPSTREAM present) — validating/reloading"
else
  BACKUP="${CADDYFILE}.bak.remote-mcp.$(date +%Y%m%d%H%M%S)"
  cp -p "$CADDYFILE" "$BACKUP"
  log "backup: $BACKUP"

  CADDYFILE="$CADDYFILE" UPSTREAM="$UPSTREAM" python3 - <<'PY'
import os, sys

path = os.environ["CADDYFILE"]
upstream = os.environ["UPSTREAM"]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)

# Target ONLY the veya site's `handle /mcp/*` (upstream 172.18.0.1:8767); an
# identically-named route exists in the mneme block (mneme-api-1:8000).
idx = None
for i, line in enumerate(lines):
    if "handle /mcp/* {" in line:
        for j in range(i + 1, min(i + 5, len(lines))):
            if "172.18.0.1:8767" in lines[j]:
                idx = i
                break
    if idx is not None:
        break
if idx is None:
    print("error: veya `handle /mcp/*` (172.18.0.1:8767) not found; refusing to guess", file=sys.stderr)
    sys.exit(3)

indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip())]
block = [
    f"{indent}handle /mcp {{\n",
    f"{indent}    reverse_proxy {upstream}\n",
    f"{indent}}}\n",
    f"{indent}handle /mcp/health {{\n",
    f"{indent}    reverse_proxy {upstream}\n",
    f"{indent}}}\n",
]
lines[idx:idx] = block
open(path, "w", encoding="utf-8").writelines(lines)
print(f"inserted /mcp + /mcp/health before line {idx + 1}")
PY
fi

# OAuth discovery/registration/authorization/token endpoints must reach the same
# Remote MCP service even when /mcp was published by an earlier run.
CADDYFILE="$CADDYFILE" UPSTREAM="$UPSTREAM" python3 - <<'PY'
import os, sys

path = os.environ["CADDYFILE"]
upstream = os.environ["UPSTREAM"]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)

required = [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/register",
    "/authorize",
    "/authorize/decision",
    "/token",
]
missing = [route for route in required if not any(f"handle {route} {{" in line for line in lines)]
if missing:
    idx = None
    for i, line in enumerate(lines):
        if "handle /mcp/* {" in line:
            for j in range(i + 1, min(i + 5, len(lines))):
                if "172.18.0.1:8767" in lines[j]:
                    idx = i
                    break
        if idx is not None:
            break
    if idx is None:
        print("error: veya /mcp upstream not found; refusing to guess", file=sys.stderr)
        sys.exit(3)

    indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip())]
    block = []
    for route in missing:
        block.extend([
            f"{indent}handle {route} {{\n",
            f"{indent}    reverse_proxy {upstream}\n",
            f"{indent}}}\n",
        ])
    lines[idx:idx] = block
    open(path, "w", encoding="utf-8").writelines(lines)
    print("inserted OAuth routes:", ", ".join(missing))
else:
    print("OAuth routes already published")
PY

restore() {
  if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
    log "restoring $BACKUP"
    cp -p "$BACKUP" "$CADDYFILE"
    docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDY_IN_CONTAINER" --adapter caddyfile >/dev/null 2>&1 || true
  fi
}

log "validating…"
if ! docker exec "$CADDY_CONTAINER" caddy validate --config "$CADDY_IN_CONTAINER" --adapter caddyfile; then
  log "validate FAILED"
  restore
  exit 1
fi

log "reloading…"
if ! docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDY_IN_CONTAINER" --adapter caddyfile; then
  log "reload FAILED"
  restore
  exit 1
fi

log "done: /mcp and /mcp/health -> $UPSTREAM"
