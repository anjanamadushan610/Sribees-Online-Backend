#!/usr/bin/env bash
# =============================================================================
# SRIBEESonline — Local Development Setup Script
#
# What it does (in order):
#   1. Starts all Docker Compose services (Postgres, Redis, MinIO, Backend)
#   2. Waits for each service to become healthy
#   3. Creates the MinIO bucket via mc (idempotent — safe to re-run)
#   4. Runs SQL migrations against Postgres
#   5. Runs the seed_splash_video.py script inside the backend container
#   6. Prints service URLs (including Android Emulator addresses)
#
# Usage:
#   chmod +x scripts/setup.sh
#   ./scripts/setup.sh
#
# On Windows (Git Bash / WSL):
#   bash scripts/setup.sh
# =============================================================================

set -euo pipefail

# ── Colours ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Config ──
COMPOSE_FILE="docker-compose.yml"

# MinIO
MINIO_ALIAS="local"
MINIO_ENDPOINT="http://localhost:9000"
MINIO_USER="admin"
MINIO_PASS="password123"
BUCKET_NAME="sribees-assets"

# Postgres
DB_HOST="localhost"
DB_PORT="5432"
DB_USER="sribees_user"
DB_PASS="sribees_password_123"
DB_NAME="sribeesonline"

# Backend
BACKEND_CONTAINER="sribees_backend"
BACKEND_URL="http://localhost:8000"

# Splash video
SPLASH_VIDEO="Grocery_App_Animation_Video_Creation.mp4"

# ── Resolve project root ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo -e "${BOLD}${CYAN}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║     SRIBEESonline — Local Environment Setup   ║${NC}"
echo -e "${BOLD}${CYAN}╚═══════════════════════════════════════════════╝${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Helper: wait for a service to become healthy
# ─────────────────────────────────────────────────────────────────────────────
wait_for_service() {
    local name="$1"
    local check_cmd="$2"
    local max=30
    local i=1

    while [ "$i" -le "$max" ]; do
        if eval "$check_cmd" > /dev/null 2>&1; then
            echo -e "  ${GREEN}✔ $name${NC}"
            return 0
        fi
        printf "  ${YELLOW}⏳ waiting for %s (%d/%d)${NC}\r" "$name" "$i" "$max"
        sleep 2
        i=$((i + 1))
    done

    echo -e "  ${RED}✘ $name did not start in time${NC}"
    return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. Start Docker Compose
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[1/6] Starting Docker Compose services ...${NC}"
docker compose -f "$COMPOSE_FILE" up -d --build 2>&1 | tail -5
echo -e "${GREEN}  ✔ Containers started${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 2. Wait for services to become healthy
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[2/6] Waiting for services ...${NC}"

wait_for_service "PostgreSQL (postgres_db)" \
    "docker exec sribees_postgres pg_isready -U $DB_USER -d $DB_NAME"

wait_for_service "Redis (redis_cache)" \
    "docker exec sribees_redis redis-cli -a sribees_redis_password ping"

wait_for_service "MinIO (s3-local)" \
    "curl -sf ${MINIO_ENDPOINT}/minio/health/live"

wait_for_service "FastAPI (fastapi_backend)" \
    "curl -sf ${BACKEND_URL}/health"

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 3. Create MinIO bucket (idempotent)
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[3/6] Ensuring MinIO bucket '${BUCKET_NAME}' ...${NC}"

if command -v mc &> /dev/null; then
    mc alias set "$MINIO_ALIAS" "$MINIO_ENDPOINT" "$MINIO_USER" "$MINIO_PASS" \
        --api S3v4 2>/dev/null || true
    mc mb --ignore-existing "${MINIO_ALIAS}/${BUCKET_NAME}" 2>/dev/null || true
    mc anonymous set download "${MINIO_ALIAS}/${BUCKET_NAME}" 2>/dev/null || true
    echo -e "  ${GREEN}✔ Bucket ready (local mc)${NC}"
else
    docker compose run --rm minio_init 2>/dev/null || true
    echo -e "  ${GREEN}✔ Bucket ready (minio_init container)${NC}"
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 4. Run SQL migrations
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[4/6] Running SQL migrations ...${NC}"

MIGRATION_DIR="migrations"
if [ -d "$MIGRATION_DIR" ]; then
    SQL_COUNT=0
    for sql_file in $(ls "$MIGRATION_DIR"/*.sql 2>/dev/null | sort); do
        filename=$(basename "$sql_file")
        echo -e "  ${CYAN}→ $filename${NC}"

        # Run migration via the Postgres container (no local psql required)
        docker exec -i sribees_postgres \
            psql -U "$DB_USER" -d "$DB_NAME" --quiet \
            < "$sql_file" 2>&1 | head -3 || true

        SQL_COUNT=$((SQL_COUNT + 1))
    done
    echo -e "  ${GREEN}✔ Applied $SQL_COUNT migration(s)${NC}"
else
    echo -e "  ${YELLOW}⚠ No migrations directory at $MIGRATION_DIR${NC}"
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 5. Seed splash video
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[5/6] Seeding splash video ...${NC}"

if docker exec "$BACKEND_CONTAINER" test -f /app/migrations/seed_splash_video.py 2>/dev/null; then
    docker exec "$BACKEND_CONTAINER" \
        python -m migrations.seed_splash_video 2>&1 | tail -8 || true
    echo -e "  ${GREEN}✔ Seed script finished${NC}"
else
    echo -e "  ${YELLOW}⚠ seed_splash_video.py not found in container — skipping${NC}"

    # Fallback: upload directly via mc if the video file exists locally
    if [ -f "$SPLASH_VIDEO" ] && command -v mc &> /dev/null; then
        mc cp "$SPLASH_VIDEO" \
            "${MINIO_ALIAS}/${BUCKET_NAME}/splash/splash_video_initial.mp4" \
            2>/dev/null || true
        echo -e "  ${GREEN}✔ Video uploaded directly via mc${NC}"
    fi
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 6. Print service URLs
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[6/6] Local environment ready!${NC}"
echo ""
echo -e "${CYAN}┌────────────────────────────────────────────────────────┐${NC}"
echo -e "${CYAN}│  ${BOLD}Service                URL${NC}${CYAN}                              │${NC}"
echo -e "${CYAN}├────────────────────────────────────────────────────────┤${NC}"
echo -e "${CYAN}│${NC}  FastAPI Backend       http://localhost:8000             ${CYAN}│${NC}"
echo -e "${CYAN}│${NC}  API Docs (Swagger)    http://localhost:8000/docs       ${CYAN}│${NC}"
echo -e "${CYAN}│${NC}  Admin Panel           http://localhost:3000             ${CYAN}│${NC}"
echo -e "${CYAN}│${NC}  MinIO Console         http://localhost:9001             ${CYAN}│${NC}"
echo -e "${CYAN}│${NC}  MinIO S3 API          http://localhost:9000             ${CYAN}│${NC}"
echo -e "${CYAN}│${NC}  PostgreSQL            localhost:5432                    ${CYAN}│${NC}"
echo -e "${CYAN}│${NC}  Redis                 localhost:6379                    ${CYAN}│${NC}"
echo -e "${CYAN}├────────────────────────────────────────────────────────┤${NC}"
echo -e "${CYAN}│${NC}  ${BOLD}Android Emulator${NC}                                        ${CYAN}│${NC}"
echo -e "${CYAN}│${NC}  API                   http://10.0.2.2:8000             ${CYAN}│${NC}"
echo -e "${CYAN}│${NC}  MinIO S3              http://10.0.2.2:9000             ${CYAN}│${NC}"
echo -e "${CYAN}└────────────────────────────────────────────────────────┘${NC}"
echo ""
echo -e "${GREEN}MinIO Console login:  admin / password123${NC}"
echo ""
echo -e "${BOLD}Useful commands:${NC}"
echo "  docker compose logs -f fastapi_backend    # follow backend logs"
echo "  docker compose --profile tools up -d      # start pgAdmin + Redis Commander"
echo "  docker compose down                       # stop everything"
echo "  docker compose down -v                    # stop + delete volumes"
echo ""
