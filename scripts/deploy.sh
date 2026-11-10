#!/usr/bin/env bash
# Запускается на VPS после git pull (вручную или из GitHub Actions).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/direct-analytics-bot}"
cd "$APP_DIR"

if [[ ! -f .env ]]; then
  echo "ERROR: файл .env не найден в $APP_DIR"
  echo "Скопируйте .env.example в .env и заполните секреты (один раз вручную)."
  exit 1
fi

echo "==> git pull"
git fetch origin main
git reset --hard origin/main

append_env() {
  local key="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    return
  fi
  if grep -q "^${key}=" .env; then
    return
  fi
  printf '%s=%s\n' "$key" "$value" >> .env
}

append_env VK_ADS_AGENCY_ID "${VK_ADS_AGENCY_ID:-}"
append_env VK_ADS_CLIENT_ID "${VK_ADS_CLIENT_ID:-}"
append_env VK_ADS_CLIENT_SECRET "${VK_ADS_CLIENT_SECRET:-}"

echo "==> docker compose up"
docker compose up -d --build

echo "==> cleanup old images"
docker image prune -f

echo "==> status"
docker compose ps

echo "Deploy OK: $(date -Iseconds)"
