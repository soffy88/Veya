#!/bin/bash
set -e
cd /data/soffy/projects/veya/deploy
export HOME=/home/soffy
exec docker compose -p veya-stage --env-file /home/soffy/.config/veya/stage.env -f docker-compose.stage.yml up -d "$@"
