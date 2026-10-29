#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ ! -f .env ]; then
    printf '%s\n' "Missing .env; configure deployment before creating a backup." >&2
    exit 1
fi

mkdir -p backups
chmod 700 backups
umask 077
timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
backup_file="backups/yimi-postgres-${timestamp}.dump"
temporary_file="${backup_file}.tmp"
trap 'rm -f "$temporary_file"' EXIT HUP INT TERM

docker compose exec -T db sh -c \
    'pg_dump --format=custom --no-owner --username="$POSTGRES_USER" "$POSTGRES_DB"' \
    > "$temporary_file"
test -s "$temporary_file"
mv "$temporary_file" "$backup_file"
printf 'Created PostgreSQL backup: %s\n' "$backup_file"
