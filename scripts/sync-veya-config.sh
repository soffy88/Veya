#!/usr/bin/env bash
# 同步主脑 LLM 配置到线上容器 (veya-data named volume)。
#
# 背景: 主脑默认模型链路依赖两份用户配置 —
#   ~/.veya/config.json        (get_provider_config 兜底: llm.provider/model)
#   ~/.veya/llm-router.json    (LLM 路由矩阵, mtime 热重载)
# 两者挂载自 named volume deploy_veya-data → docker cp 进 volume 即持久,
# 容器重建不丢。本脚本幂等: 文件缺失/容器未跑时给出提示并退出码 1。
set -euo pipefail

CONTAINER="${1:-veya-backend}"
SRC_DIR="${VEYA_CONFIG_DIR:-$HOME/.veya}"
for f in config.json llm-router.json; do
  if [ ! -f "$SRC_DIR/$f" ]; then
    echo "⚠ 宿主 $SRC_DIR/$f 不存在, 跳过" >&2
    continue
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "✗ 容器 $CONTAINER 未运行, 无法同步" >&2
    exit 1
  fi
  docker cp "$SRC_DIR/$f" "$CONTAINER:/home/soffy/.veya/$f"
  echo "✓ 已同步 $SRC_DIR/$f → $CONTAINER:/home/soffy/.veya/$f"
done

# 验证容器内可读
if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  docker exec "$CONTAINER" sh -c 'ls -la /home/soffy/.veya/config.json /home/soffy/.veya/llm-router.json' >/dev/null 2>&1 \
    && echo "✓ 容器内配置就位" \
    || echo "⚠ 容器内配置缺失 (重建 volume 后需重新同步)" >&2
fi
