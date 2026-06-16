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

echo "==> docker compose up"
docker compose up -d --build

echo "==> cleanup old images"
docker image prune -f

echo "==> status"
docker compose ps

echo "Deploy OK: $(date -Iseconds)"
