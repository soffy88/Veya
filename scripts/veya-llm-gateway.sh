#!/usr/bin/env bash
# 启动 veya LLM 网关 (scripts/veya_llm_gateway.py) — 固定端口, 供 systemd
# --user 常驻单元 (veya-llm-gateway.service) 或手动前台调试调用。
#
# 用法: scripts/veya-llm-gateway.sh [PORT]   # PORT 默认 8791
#
# 只依赖裸 venv (fastapi/uvicorn/httpx + veya.obase.llm), 不需要 obase/3O
# 工具链齐全的正式运行环境 — 和主服务 (scripts/veya-serve.sh) 不是一回事。
set -euo pipefail

PORT="${1:-8791}"
cd "$(dirname "$0")/.."

exec venv/bin/python scripts/veya_llm_gateway.py --port "$PORT"
