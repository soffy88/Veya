#!/bin/bash
set -e

echo "[1/3] 初始化工作区与快照目录..."
mkdir -p /tmp/veya_workspace
chmod 777 /tmp/veya_workspace

echo "[2/3] 检查并更新 3O 元素主库..."
git submodule update --init --recursive

echo "[3/3] 启动 Docker Compose 编排集群..."
docker compose up --build -d

echo ""
echo "✅ Veya 3O 集群已启动。"
echo "可以使用 'docker logs -f veya_core_engine' 查看执行日志。"
