#!/usr/bin/env bash
set -euo pipefail

# Immutable production rollout:
#   build SHA image -> record rollback state -> recreate -> verify -> rollback
#
# The backend image contains all /app code. Only data and approved runtime
# directories are bind-mounted. Configuration lives outside the repository so
# this script never rewrites a tracked file during a deploy.

REPO_DIR="${VEYA_REPO_DIR:-/data/soffy/projects/veya}"
BACKUP_ROOT="${VEYA_BACKUP_ROOT:-/home/soffy/veya-fix-1017/p0-3-immutable}"
STATE_FILE="${VEYA_DEPLOY_STATE:-$BACKUP_ROOT/last-deploy.env}"
OVERRIDE="${VEYA_BACKEND_OVERRIDE:-/home/soffy/.config/veya/backend-override.yml}"
COMPOSE_BASE="$REPO_DIR/deploy/docker-compose.yml"
COMPOSE_RESOURCES="$REPO_DIR/deploy/resources.override.yml"
COMPOSE_TAILNET="$REPO_DIR/deploy/tnw-tailnet.override.yml"
ENV_ARGS=(
  --env-file "$REPO_DIR/deploy/.env"
  --env-file /home/soffy/.config/veya/backend.interp.env
)
HEALTH_URL="${VEYA_DEPLOY_HEALTH_URL:-http://127.0.0.1:8767/health}"

cd "$REPO_DIR"
SHORT_SHA="$(git rev-parse --short HEAD)"
IMAGE="veya-backend:$SHORT_SHA"
PREV_IMAGE="$(docker inspect --format='{{.Config.Image}}' veya-backend 2>/dev/null || true)"
PREV_CONTAINER_ID="$(docker inspect --format='{{.Id}}' veya-backend 2>/dev/null || true)"
PREV_OVERRIDE_SHA="$(sha256sum "$OVERRIDE" 2>/dev/null | awk '{print $1}' || true)"
mkdir -p "$BACKUP_ROOT"
STAMP="$(date +%Y%m%dT%H%M%SZ)"

if [ -n "$PREV_IMAGE" ]; then
  PREV_IMAGE_ID="$(docker image inspect --format='{{.Id}}' "$PREV_IMAGE" 2>/dev/null || true)"
else
  PREV_IMAGE_ID=""
fi

compose() {
  docker compose "${ENV_ARGS[@]}" \
    -p veya \
    -f "$COMPOSE_BASE" \
    -f "$OVERRIDE" \
    -f "$COMPOSE_RESOURCES" \
    -f "$COMPOSE_TAILNET" \
    "$@"
}

assert_no_code_mounts() {
  local bad
  bad="$(
    docker inspect veya-backend --format '{{range .Mounts}}{{println .Source "|" .Destination}}{{end}}' \
      | awk '
        BEGIN { FS = "|" }
        $2 == "/repo" ||
        $2 == "/app/server" ||
        $2 == "/app/veya" ||
        $2 == "/app/runtime" ||
        $2 == "/app/platform" ||
        $2 == "/app/tools" ||
        $2 == "/app/config" ||
        $2 == "/app/veya_loop" ||
        $2 == "/app/hicode-entrypoint.sh" ||
        $2 ~ /^\/app\/[^\/]+\/.*\.(py|sh)$/ {print}
      '
  )"
  if [ -n "$bad" ]; then
    echo "[deploy] forbidden code bind mount(s):" >&2
    echo "$bad" >&2
    return 1
  fi
}

wait_healthy() {
  local expected_version="$1"
  local body
  for _ in $(seq 1 90); do
    body="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null || true)"
    if [ -n "$body" ] && python3 -c '
import json, sys
body = json.loads(sys.argv[1])
expected = sys.argv[2]
if body.get("status") != "ok":
    raise SystemExit(1)
if expected and body.get("version") != expected:
    raise SystemExit(1)
' "$body" "$expected_version" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

rollback() {
  local reason="$1"
  local rc=1
  echo "[deploy] ROLLBACK: $reason" >&2
  {
    echo "time=$STAMP"
    echo "reason=$reason"
    echo "target_image=$IMAGE"
    echo "previous_image=$PREV_IMAGE"
    echo "previous_image_id=$PREV_IMAGE_ID"
    echo "previous_container_id=$PREV_CONTAINER_ID"
    echo "previous_override_sha256=$PREV_OVERRIDE_SHA"
  } > "$BACKUP_ROOT/rollback-$STAMP.txt"

  if [ -n "$PREV_IMAGE" ] && docker image inspect "$PREV_IMAGE" >/dev/null 2>&1; then
    docker rm -f veya-backend >/dev/null 2>&1 || true
    sed -i -E "s#^    image: veya-backend:.*#    image: $PREV_IMAGE#" "$OVERRIDE"
    if compose up -d --no-build --force-recreate backend; then
      if wait_healthy ""; then
        rc=0
        echo "[deploy] rollback healthy: $PREV_IMAGE"
      fi
    fi
  fi
  if [ "$rc" -ne 0 ]; then
    echo "[deploy] rollback to $PREV_IMAGE failed; inspect $BACKUP_ROOT/rollback-$STAMP.txt" >&2
  fi
  return "$rc"
}

trap 'rc=$?; if [ "$rc" -ne 0 ]; then echo "[deploy] failed at line $LINENO (rc=$rc)" >&2; fi' EXIT

echo "[deploy] current=${PREV_IMAGE:-none} target=$IMAGE"

# Build only when the SHA tag is absent or points at an unexpected image. A
# stale tag must never make a new commit appear deployed.
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  EXISTING_SHA="$(docker image inspect --format '{{index .Config.Env}}' "$IMAGE" \
    | tr ' ' '\n' | sed -n 's/^VEYA_BUILD_SHA=//p' | head -1)"
  if [ "$EXISTING_SHA" != "$SHORT_SHA" ]; then
    echo "[deploy] tag $IMAGE exists with VEYA_BUILD_SHA=${EXISTING_SHA:-unset}; rebuilding"
    docker rmi "$IMAGE" >/dev/null 2>&1 || true
  fi
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  VEYA_BUILD_CACHE_FROM="${VEYA_BUILD_CACHE_FROM:-veya-backend:latest}" \
    deploy/build_hicode_image.sh "$IMAGE" "$SHORT_SHA"
fi

docker inspect veya-backend > "$BACKUP_ROOT/veya-backend-inspect-$STAMP.json" 2>/dev/null || true
cp -a "$OVERRIDE" "$BACKUP_ROOT/backend-override.yml.deploy-$STAMP"
compose config > "$BACKUP_ROOT/docker-compose-resolved-$STAMP.yml"

sed -i -E "s#^    image: veya-backend:.*#    image: $IMAGE#" "$OVERRIDE"

if ! compose up -d --no-build --force-recreate backend; then
  rollback "compose recreate failed"
  exit 1
fi

EXPECTED_VERSION="0.6.0-$SHORT_SHA"
if ! wait_healthy "$EXPECTED_VERSION"; then
  rollback "health/version check failed (expected $EXPECTED_VERSION)"
  exit 1
fi
if ! assert_no_code_mounts; then
  rollback "forbidden code bind mount detected"
  exit 1
fi
if ! docker exec veya-backend python -c \
  'import asyncpg, pywebpush; from server.bot_team.push import webpush_available; assert webpush_available()' \
  >/dev/null 2>&1; then
  rollback "image dependency preflight failed (asyncpg/pywebpush)"
  exit 1
fi

{
  echo "deployed_at=$STAMP"
  echo "image=$IMAGE"
  echo "image_id=$(docker image inspect --format '{{.Id}}' "$IMAGE")"
  echo "previous_image=$PREV_IMAGE"
  echo "previous_image_id=$PREV_IMAGE_ID"
  echo "commit=$(git rev-parse HEAD)"
  echo "health_version=$EXPECTED_VERSION"
} | tee "$STATE_FILE"

echo "[deploy] healthy immutable image: $IMAGE"
