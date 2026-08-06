#!/usr/bin/env bash
# 构建桌面版静态前端 (apps/web → build-desktop/)
#
# adapter-static 不允许 +server.ts 路由, 构建期间临时移走 server 路由,
# 构建完成后恢复 (网页版 adapter-node 构建不受影响)。
set -euo pipefail

cd "$(dirname "$0")/../apps/web"

SRC=src/routes
TMP=$(mktemp -d)
SERVER_ROUTES=(api legacy)

cleanup() {
  for r in "${SERVER_ROUTES[@]}"; do
    if [ -d "$TMP/$r" ]; then
      mv "$TMP/$r" "$SRC/$r" 2>/dev/null || true
    fi
  done
  rmdir "$TMP" 2>/dev/null || true
}
trap cleanup EXIT

# 1. 移走 server 路由
for r in "${SERVER_ROUTES[@]}"; do
  if [ -d "$SRC/$r" ]; then
    mv "$SRC/$r" "$TMP/"
  fi
done

# 2. 静态构建
export VITE_VEYA_ENDPOINT="${VITE_VEYA_ENDPOINT:-http://127.0.0.1:8767}"
pnpm vite build --config vite.config.desktop.ts

echo "✅ 桌面静态前端 → apps/web/build-desktop/ (VITE_VEYA_ENDPOINT=$VITE_VEYA_ENDPOINT)"
