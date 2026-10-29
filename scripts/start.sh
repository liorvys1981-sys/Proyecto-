#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ ! -f .env ]; then
    printf '%s\n' "Missing .env; run scripts/setup.sh and configure it first." >&2
    exit 1
fi

if grep -Eq 'reemplazar-con|OPENAI_API_KEY=$|ADMIN_API_KEY=$' .env; then
    printf '%s\n' "Replace example/empty credentials in .env before startup." >&2
    exit 1
fi

admin_key=$(awk -F= '$1 == "ADMIN_API_KEY" {sub(/^[^=]*=/, ""); print; exit}' .env)
if [ -z "$admin_key" ]; then
    printf '%s\n' "ADMIN_API_KEY is missing from .env." >&2
    exit 1
fi

mkdir -p monitoring/secrets
chmod 700 monitoring/secrets
umask 077
printf '%s' "$admin_key" > monitoring/secrets/admin-key
docker compose up --build -d
