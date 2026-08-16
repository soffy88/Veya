#!/bin/sh
# veya-backend 容器入口: 拉起 hicode serve (reasonix CLI) 独立 oservi + 前台跑主服务。
# - .env 写入 opencode-go key (容器层, 不入 git; ~/.reasonix 配置目录单文件 ro 挂载)
# - hicode serve 监听 0.0.0.0:8768, 模型 opencode-go (云端, 独立于 veya 网关)
# - 主服务 uvicorn 前台 (容器主进程, 依赖其退出/重启语义)
set -e

mkdir -p /home/soffy/.reasonix
echo "OPENCODE_API_KEY=${OPENCODE_API_KEY}" > /home/soffy/.reasonix/.env

# hicode serve 守护循环: 被杀/崩溃自动重启 (veya 硬停止依赖此机制),
# 日志可 docker logs 查看 (/tmp 在容器层)。cd 限定在子 shell 内 — 主脑跑
# hicode 任务时会在 hicode-workspace 里探索/创建同名目录 (server/platform/
# omodul 等), 若主进程 CWD 也停在这里, `import server.app` 会被这些目录
# 影子遮蔽, 拿到假包而不是 /app/server (2026-08-16 踩过: 容器起不来)。
(
  cd /home/soffy/.veya/hicode-workspace
  while true; do
    reasonix serve --addr 0.0.0.0:8768 --auth none --model opencode-go \
      >> /tmp/hicode-serve.log 2>&1
    echo "[entrypoint] hicode serve 退出 (rc=$?), 1s 后重启" >> /tmp/hicode-serve.log
    sleep 1
  done
) &

cd /app
exec uvicorn server.app:app --host 0.0.0.0 --port 8765
