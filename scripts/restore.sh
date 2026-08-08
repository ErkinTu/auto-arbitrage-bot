#!/usr/bin/env bash
set -euo pipefail

# WARNING: replaces data in the selected database.
# Usage: ./scripts/restore.sh backups/car_analytics_YYYY-MM-DD_HH-MM-SS.sql.gz

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 path/to/backup.sql.gz"
  exit 1
fi

# .env — docker env-файл, а НЕ bash-скрипт: `source .env` ломается на значениях
# с ';' (боевой LALAFO_PROXY_URL: логин DataImpulse содержит '__cr.kg;sessid').
# Под `set -e` это валило restore на строке 127 ещё до восстановления. Читаем
# только нужные переменные, снимая обрамляющие кавычки (как в backup.sh).
read_env() {
  local val
  val="$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2-)"
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}
POSTGRES_USER="$(read_env POSTGRES_USER)"
POSTGRES_DB="$(read_env POSTGRES_DB)"
BACKUP_FILE="$1"

gunzip -c "$BACKUP_FILE" | docker compose exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

echo "Restore finished."
