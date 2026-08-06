#!/usr/bin/env bash
# 下载 Playwright chromium 浏览器二进制 (GitHub Release 附件) → deploy/ms-playwright/
#
# 背景: Dockerfile COPY deploy/ms-playwright/ 进镜像 (免 CDN 网络下载, 防构建卡死)。
# 二进制 656MB 无法进 git (单文件 >100MB), 由本脚本从 Release 附件获取。
#
# 用法: bash scripts/fetch-playwright.sh [版本]   (默认 0.6.0)
set -euo pipefail

VERSION="${1:-0.6.0}"
URL="https://github.com/soffy88/Veya/releases/download/v${VERSION}/ms-playwright.tar.gz"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$(dirname "$SCRIPT_DIR")/deploy"

if [ -d "$DEST/ms-playwright/chromium-1234" ]; then
    echo "✔ 已存在 deploy/ms-playwright (无需下载)"
    exit 0
fi

echo "下载 Playwright 浏览器: $URL"
curl -fL --progress-bar "$URL" -o "$DEST/ms-playwright.tar.gz"
tar -xzf "$DEST/ms-playwright.tar.gz" -C "$DEST"
rm -f "$DEST/ms-playwright.tar.gz"
echo "✔ deploy/ms-playwright 就绪 — 现在可以 docker compose build backend"
