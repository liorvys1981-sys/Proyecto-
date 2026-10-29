#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    printf '%s\n' "Created .env from .env.example."
    printf '%s\n' "Edit .env and replace every example value before running scripts/start.sh."
else
    printf '%s\n' ".env already exists; leaving it unchanged."
fi

mkdir -p monitoring/secrets backups
chmod 700 monitoring/secrets backups
printf '%s\n' "Created private monitoring/secrets and backups directories."
