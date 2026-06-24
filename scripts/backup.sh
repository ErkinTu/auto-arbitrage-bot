#!/usr/bin/env bash
set -euo pipefail

# Run from the project directory: ./scripts/backup.sh
# Uses the PostgreSQL container from docker-compose.yml.

BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

source .env
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
FILE="$BACKUP_DIR/car_analytics_${STAMP}.sql.gz"

docker compose exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip > "$FILE"

find "$BACKUP_DIR" -type f -name '*.sql.gz' -mtime +"$KEEP_DAYS" -delete
printf 'Backup created: %s\n' "$FILE"
