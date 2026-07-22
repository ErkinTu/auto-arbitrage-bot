#!/usr/bin/env bash
set -euo pipefail

# Run from the project directory: ./scripts/backup.sh
# Uses the PostgreSQL container from docker-compose.yml.

BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

# .env — это docker env-файл, а не bash-скрипт: значения с ';' (например
# LALAFO_PROXY_URL) ломают `source .env`. Читаем только нужные переменные,
# снимая возможные обрамляющие кавычки. Значения не интерпретируются шеллом.
read_env() {
  local val
  val="$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2-)"
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}
POSTGRES_USER="$(read_env POSTGRES_USER)"
POSTGRES_DB="$(read_env POSTGRES_DB)"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
FILE="$BACKUP_DIR/car_analytics_${STAMP}.sql.gz"

docker compose exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip > "$FILE"

find "$BACKUP_DIR" -type f -name '*.sql.gz' -mtime +"$KEEP_DAYS" -delete
printf 'Backup created: %s\n' "$FILE"
