#!/usr/bin/env bash
# =============================================================================
# SRIBEESonline - development rollout on the Contabo VPS.
#
# Runs ON the server, invoked over SSH by .github/workflows/deploy-dev.yml:
#     APP_IMAGE=ghcr.io/...@sha256:... ./deploy-dev.sh deploy
#
# Mirrors SribeesExpress's scripts/deploy.sh, with two deliberate differences:
#
#   * Migrations are raw .sql files applied in filename order, not alembic.
#     They are extracted from the IMAGE, never from a running container -- a
#     release that adds a column starts an app whose ORM selects it against a
#     database that has not got it yet, so the app may legitimately be dead at
#     exactly the moment its migrations are needed. Reaching them through it
#     would be circular. Production learned that on 2026-09-12 by going down.
#
#   * One app replica, not two. The reminder worker runs in-process (an asyncio
#     task in app/main.py), so a second replica would fire every reminder
#     twice. Development does not need zero-downtime deploys, it needs correct
#     ones.
# =============================================================================
set -Eeuo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$DIR/docker-compose.dev-contabo.yml"
ENV_FILE="$DIR/.env"
IMAGE_FILE="$DIR/.image"
PREVIOUS_FILE="$DIR/.image.previous"
BACKUP_DIR="$DIR/backups"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

log() { printf '[dev-deploy %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() { printf '[dev-deploy FATAL] %s\n' "$*" >&2; exit 1; }

compose() {
  APP_IMAGE="${APP_IMAGE:-$(cat "$IMAGE_FILE" 2>/dev/null || echo 'validation-placeholder:0')}" \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

require_env() {
  [ -f "$COMPOSE_FILE" ] || die "missing $COMPOSE_FILE"
  [ -f "$ENV_FILE" ]     || die "missing $ENV_FILE - the workflow installs it"
  local cfg_err
  if ! cfg_err="$(compose config --quiet 2>&1)"; then
    log "docker compose rejected the configuration:"
    printf '%s\n' "$cfg_err" >&2
    die "compose configuration is invalid - see above"
  fi
}

wait_healthy() {
  local service="$1" cid state
  local deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  log "waiting for $service (timeout ${HEALTH_TIMEOUT}s)"
  while [ "$SECONDS" -lt "$deadline" ]; do
    cid="$(compose ps -q "$service" || true)"
    if [ -n "$cid" ]; then
      state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
      case "$state" in
        healthy) log "$service is healthy"; return 0 ;;
        unhealthy) log "$service reported unhealthy" ;;
      esac
    fi
    sleep 3
  done
  log "--- last 60 log lines from $service ---"
  compose logs --tail 60 "$service" >&2 || true
  return 1
}

backup_database() {
  install -d -m 0750 "$BACKUP_DIR"
  local file
  file="$BACKUP_DIR/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
  log "dumping database before migrating"
  # Runs inside the container so the host needs no psql client and no exposed
  # port; the bytes land here as the deploy user. PGPASSWORD is required even
  # locally because the container is scram-sha-256, not trust -- without it
  # pg_dump prompts and hangs forever. </dev/null matters for the same reason:
  # `exec -T` inherits the SSH session's stdin, which never closes.
  compose exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
    < /dev/null | gzip -9 > "$file.part" || die "backup failed - refusing to migrate"
  mv "$file.part" "$file"
  chmod 0600 "$file"
  # Retain all previous backups during the Partner test rollout.
  log "backup written to $(basename "$file")"
}

create_base_tables() {
  local image="$1"
  # The raw SQL migrations only ALTER and seed; they never CREATE TABLE. On a
  # fresh database they therefore have nothing to alter, and the first one to
  # touch a table dies with "relation does not exist". What builds the schema
  # is SQLAlchemy's create_all() at application startup, so the app has to
  # boot once before the migrations can run.
  #
  # THIS BOOT IS ALLOWED TO FAIL, and that is the whole point. A release that
  # adds a column starts an image whose ORM selects that column against a
  # database where the migration adding it has not run yet; startup queries
  # the table, the column is missing, and the app exits. That is not a broken
  # release -- it is the ordering doing what it always does, and the deploy
  # recovers from it below by running the migrations and recreating the app.
  #
  # Mirrors what the EC2 production deploy has done since 2026-09-12, when
  # doing it the other way round took production down.
  log "booting the app once so create_all() can build any missing tables"
  APP_IMAGE="$image" compose up -d --no-build app || true

  local deadline=$(( SECONDS + 60 )) cid state
  while [ "$SECONDS" -lt "$deadline" ]; do
    cid="$(compose ps -q app || true)"
    if [ -n "$cid" ]; then
      state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
      [ "$state" = "healthy" ] && { log "first boot is healthy"; return 0; }
    fi
    sleep 3
  done
  log "first boot did not report healthy - expected when this release adds a"
  log "column. Migrations run next, then the app is recreated."
  return 0
}

start_infrastructure() {
  log "starting datastores"
  compose up -d --no-build postgres redis
  wait_healthy postgres || die "postgres never became healthy"
  wait_healthy redis    || die "redis never became healthy"
}

run_migrations() {
  local image="$1"
  local work="$DIR/.migration-runs/$(date -u +%Y%m%dT%H%M%SZ)-$$/content"
  mkdir -p "$(dirname "$work")"
  log "extracting migrations from the image"
  local cid
  cid="$(docker create "$image")"
  docker cp "${cid}:/app/migrations" "$work" >/dev/null
  docker rm -f "$cid" >/dev/null 2>&1 || true

  shopt -s nullglob
  local files=("$work"/*.sql)
  shopt -u nullglob
  if [ ${#files[@]} -eq 0 ]; then
    log "no .sql migrations in the image - nothing to apply"
    return 0
  fi

  local f
  # `sort -V`, not a plain sort: 010_x.sql must follow 9_x.sql, and a lexical
  # sort puts it first.
  while IFS= read -r f; do
    log ">>> $(basename "$f")"
    # PGPASSWORD is required even over the local socket. The container is
    # initialised with scram-sha-256 for local connections as well as host
    # ones, not the usual trust-for-local, so an unauthenticated psql is
    # refused with "password authentication failed" — which reads like wrong
    # credentials and is really the auth method. Same reason backup_database
    # sets it.
    #
    # Credentials are read from the container's OWN environment inside the
    # container, so they never appear in this host's argv or process list,
    # and the script does not need to parse them out of .env at all.
    #
    # ON_ERROR_STOP=1 makes a genuine SQL error exit non-zero, while the
    # NOTICEs from IF NOT EXISTS clauses pass through harmlessly.
    if ! compose exec -T postgres sh -c          'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'          < "$f"; then
      die "migration $(basename "$f") failed - the database is at the dump taken above"
    fi
  done < <(printf '%s\n' "${files[@]}" | sort -V)
  log "all migrations applied"
}

seed_development() {
  # Applied ONLY here. Nothing in the production pipeline reads this path, and
  # that is deliberate: these scripts invent data -- NICs, phone numbers -- and
  # inventing identity data is fine for a demo dataset and never fine for real
  # customers. A backfill exactly like this one used to sit in migrations/055,
  # where it ran against production and stamped 89 real people with NICs they
  # could not correct.
  #
  # The seeds are extracted from the IMAGE, like the migrations, so a rollout
  # always applies the pair that shipped together.
  local image="$1"
  local work="$DIR/.seed-runs/$(date -u +%Y%m%dT%H%M%SZ)-$$/content"
  mkdir -p "$(dirname "$work")"
  local cid
  cid="$(docker create "$image")"
  # The directory is optional: a release that ships none is not a failure.
  if ! docker cp "${cid}:/app/seeds/development" "$work" >/dev/null 2>&1; then
    docker rm -f "$cid" >/dev/null 2>&1 || true
    log "no seeds/development in the image - skipping"
    return 0
  fi
  docker rm -f "$cid" >/dev/null 2>&1 || true

  shopt -s nullglob
  local files=("$work"/*.sql)
  shopt -u nullglob
  [ ${#files[@]} -eq 0 ] && { log "no .sql seeds to apply"; return 0; }

  local f
  while IFS= read -r f; do
    log "seed >>> $(basename "$f")"
    # Same authentication as the migrations, for the same reason: the
    # container is scram-sha-256 even over its own socket.
    if ! compose exec -T postgres sh -c          'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'          < "$f"; then
      die "seed $(basename "$f") failed"
    fi
  done < <(printf '%s
' "${files[@]}" | sort -V)
  log "development seeds applied"
}

cmd_deploy() {
  require_env
  local image="${APP_IMAGE:-}"
  [ -n "$image" ] || image="$(cat "$IMAGE_FILE" 2>/dev/null || true)"
  [ -n "$image" ] || die "no APP_IMAGE given and no .image recorded"
  log "deploying $image"
  local free_kb
  free_kb=$(df -Pk "$DIR" | awk 'NR==2 {print $4}')
  [ "$free_kb" -ge 5242880 ] || die "less than 5 GiB free on development server"
  [ -n "$(compose ps -q postgres)" ] || die "expected existing development database; refusing replacement"

  log "pulling"
  APP_IMAGE="$image" compose pull --quiet app || die "could not pull $image"

  start_infrastructure
  backup_database
  create_base_tables "$image"
  run_migrations "$image"
  seed_development "$image"

  log "rolling out the app"
  APP_IMAGE="$image" compose up -d --no-build --force-recreate app
  wait_healthy app || die "app did not become healthy - run './deploy-dev.sh rollback'"

  # No edge to reconcile: this stack has no Caddy of its own. The public edge
  # is sribees-express-dev's Caddy, which proxies devapionline.sribees.com to
  # the loopback port published above, and it is reconciled by its own deploy.

  # Recorded only once the rollout is proven, so `.image` always names a
  # release that actually served traffic and rollback has somewhere to go.
  [ -f "$IMAGE_FILE" ] && cp "$IMAGE_FILE" "$PREVIOUS_FILE"
  printf '%s\n' "$image" > "$IMAGE_FILE"
  log "deployed $image"
}

cmd_rollback() {
  require_env
  [ -f "$PREVIOUS_FILE" ] || die "no previous image recorded - nothing to roll back to"
  local previous
  previous="$(cat "$PREVIOUS_FILE")"
  log "rolling back to $previous"
  # The schema is NOT rolled back. These migrations are additive (ADD COLUMN
  # IF NOT EXISTS, CREATE INDEX IF NOT EXISTS), so the older image runs happily
  # against the newer schema, while restoring the dump would throw away
  # everything written since it was taken. If a migration was genuinely
  # destructive, the dump under backups/ is the deliberate, manual way back.
  APP_IMAGE="$previous" compose up -d --no-build --force-recreate app
  wait_healthy app || die "the rollback target is also unhealthy - read the logs"
  printf '%s\n' "$previous" > "$IMAGE_FILE"
  log "rolled back to $previous"
}

cmd_status() {
  require_env
  compose ps
  printf 'current:  %s\n' "$(cat "$IMAGE_FILE" 2>/dev/null || echo none)"
  printf 'previous: %s\n' "$(cat "$PREVIOUS_FILE" 2>/dev/null || echo none)"
}

cmd_logs() {
  require_env
  compose logs --tail "${2:-100}" "${1:-app}"
}

main() {
  case "${1:-}" in
    deploy)   cmd_deploy ;;
    rollback) cmd_rollback ;;
    status)   cmd_status ;;
    logs)     shift; cmd_logs "$@" ;;
    *) die "usage: $0 {deploy|rollback|status|logs [service] [lines]}" ;;
  esac
}

main "$@"
