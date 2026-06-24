#!/usr/bin/env bash
set -euo pipefail

# WARNING: replaces data in the selected database.
# Usage: ./scripts/restore.sh backups/car_analytics_YYYY-MM-DD_HH-MM-SS.sql.gz

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 path/to/backup.sql.gz"
  exit 1
fi

source .env
BACKUP_FILE="$1"

gunzip -c "$BACKUP_FILE" | docker compose exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

echo "Restore finished."
