#!/bin/sh
# entrypoint: 容器入口由 Dockerfile ENTRYPOINT 固定为 run_tests.py; 本脚本仅作
# 显式入口文档/调试用 (等价调用)。
exec python /usr/local/bin/run_tests.py "$@"
