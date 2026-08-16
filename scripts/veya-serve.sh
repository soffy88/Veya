#!/usr/bin/env bash
# 用固定端口启动 veya 本地服务, 并把前端网关同步到该端口。
#
# 背景: `veya start` 默认 8765, 被占时自动 +1 避让, 落点每次可能不同
# (本机 8765 被 helivex api-gateway 占用, 曾避让到 8767)。前端 apps/web/.env
# 的 VEYA_GATEWAY 若指向旧端口就会"加载失败"。本脚本钉死端口消除漂移。
#
# 用法: scripts/veya-serve.sh [PORT]   # PORT 默认 8770
#
# 注意: 必须在装好 obase / 3O 工具链的 veya 运行环境里执行 (容器/正式环境);
# 裸宿主 venv 缺 obase, `veya start` 会 ModuleNotFoundError。
set -euo pipefail

PORT="${1:-8770}"
cd "$(dirname "$0")/.."

# 前端 .env 同步到本次端口 (幂等: 有则替换该行, 无则新增)
ENV_FILE="apps/web/.env"
LINE="VEYA_GATEWAY=http://127.0.0.1:${PORT}"
if [ -f "$ENV_FILE" ] && grep -q '^VEYA_GATEWAY=' "$ENV_FILE"; then
  sed -i "s#^VEYA_GATEWAY=.*#${LINE}#" "$ENV_FILE"
else
  echo "$LINE" >> "$ENV_FILE"
fi
echo "→ 已将 ${ENV_FILE} 的 VEYA_GATEWAY 指向 :${PORT}"

# --port 固定端口 (若仍被占, veya 会避让; 建议先确保该端口空闲)
exec veya start --port "$PORT" --no-browser
