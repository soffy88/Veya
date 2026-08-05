#!/bin/bash
# veya Production Deploy Script
# Usage: ./deploy/deploy.sh [--ssl] [--local]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DOMAIN="${VEYA_DOMAIN:-veya.aiinote.com}"
MODE="${1:-docker}"

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[veya]${NC} $1"; }
warn() { echo -e "${RED}[veya]${NC} $1"; }

# ── Pre-flight checks ────────────────────────────────────
log "Checking prerequisites..."

command -v docker &>/dev/null || { warn "Docker not found. Install: https://docs.docker.com/engine/install/"; exit 1; }
command -v docker compose &>/dev/null && DOCKER_COMPOSE="docker compose" || { command -v docker-compose &>/dev/null && DOCKER_COMPOSE="docker-compose"; } || { warn "Docker Compose not found"; exit 1; }

# ── SSL Certificate Setup ────────────────────────────────
setup_ssl() {
    log "Setting up SSL certificates for $DOMAIN..."
    mkdir -p "$SCRIPT_DIR/certs"

    if command -v certbot &>/dev/null; then
        sudo certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN"
        sudo cp /etc/letsencrypt/live/$DOMAIN/fullchain.pem "$SCRIPT_DIR/certs/"
        sudo cp /etc/letsencrypt/live/$DOMAIN/privkey.pem "$SCRIPT_DIR/certs/"
        log "SSL certificates installed"
    else
        warn "certbot not found. Generating self-signed certificate..."
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout "$SCRIPT_DIR/certs/privkey.pem" \
            -out "$SCRIPT_DIR/certs/fullchain.pem" \
            -subj "/CN=$DOMAIN"
        log "Self-signed certificate generated (browser will show warning)"
    fi
}

# ── Environment Setup ─────────────────────────────────────
setup_env() {
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        log "Creating .env from template..."
        cat > "$PROJECT_DIR/.env" <<EOF
# veya Production Environment
VEYA_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
DASHSCOPE_API_KEY=
VEYA_PSEUDO_SECRET=\$(openssl rand -hex 32)
VEYA_DOMAIN=$DOMAIN
EOF
        warn "Please edit .env and set your API keys!"
    fi
}

# ── Docker Deploy ─────────────────────────────────────────
deploy_docker() {
    log "Building and starting containers..."
    cd "$PROJECT_DIR"

    $DOCKER_COMPOSE -f deploy/docker-compose.yml build --no-cache
    $DOCKER_COMPOSE -f deploy/docker-compose.yml up -d

    log "Waiting for services..."
    sleep 5

    # Health checks
    if curl -sf http://localhost/api/v1/agent/run -X POST -H 'Content-Type: application/json' -d '{"task":"ping","mode":"dry_run"}' >/dev/null 2>&1; then
        log "✅ Backend healthy"
    else
        warn "⚠ Backend health check failed"
    fi

    if curl -sf http://localhost/ >/dev/null 2>&1; then
        log "✅ Frontend healthy"
    else
        warn "⚠ Frontend health check failed"
    fi

    log "🚀 veya deployed at https://$DOMAIN"
    $DOCKER_COMPOSE -f deploy/docker-compose.yml ps
}

# ── Local (non-Docker) Deploy ─────────────────────────────
# Actual production deploy is systemd-managed: see deploy/veya-web.service
# (SvelteKit apps/web) and deploy/veya-gateway.service (L4 gateway).
deploy_local() {
    warn "local deploy target is retired — see deploy/veya-web.service and deploy/veya-gateway.service"
    exit 1
}

# ── Stop ──────────────────────────────────────────────────
stop_services() {
    log "Stopping services..."
    if [ -f /tmp/veya-backend.pid ]; then
        kill $(cat /tmp/veya-backend.pid) 2>/dev/null && rm /tmp/veya-backend.pid
    fi
    $DOCKER_COMPOSE -f deploy/docker-compose.yml down 2>/dev/null
    log "Stopped"
}

# ── Main ──────────────────────────────────────────────────
case "${1:-deploy}" in
    deploy|docker)
        setup_env
        [ "${2:-}" = "--ssl" ] && setup_ssl
        deploy_docker
        ;;
    local)
        deploy_local
        ;;
    ssl)
        setup_ssl
        ;;
    stop)
        stop_services
        ;;
    logs)
        $DOCKER_COMPOSE -f "$SCRIPT_DIR/docker-compose.yml" logs -f
        ;;
    status)
        $DOCKER_COMPOSE -f "$SCRIPT_DIR/docker-compose.yml" ps 2>/dev/null || {
            [ -f /tmp/veya-backend.pid ] && echo "Backend running (PID $(cat /tmp/veya-backend.pid))" || echo "Backend stopped"
        }
        ;;
    *)
        echo "Usage: $0 {deploy|local|ssl|stop|logs|status}"
        echo "  deploy [--ssl]  — Docker-based production deploy"
        echo "  local           — Direct local deploy (no Docker)"
        echo "  ssl             — Setup SSL certificates"
        echo "  stop            — Stop all services"
        echo "  logs            — Tail Docker logs"
        echo "  status          — Show service status"
        exit 1
        ;;
esac
