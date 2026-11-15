#!/usr/bin/env bash
# Deploys the API to a new Azure App Service (Linux, Python 3.12).
# Usage: az login && ./deploy/azure.sh [app-name] [region] [sku]
set -euo pipefail

APP="${1:-crew-health-api-$RANDOM}"
LOCATION="${2:-brazilsouth}"
SKU="${3:-B1}"
RG="rg-${APP}"
PLAN="plan-${APP}"

az group create -n "$RG" -l "$LOCATION" -o none
az appservice plan create -g "$RG" -n "$PLAN" --is-linux --sku "$SKU" -o none
az webapp create -g "$RG" -p "$PLAN" -n "$APP" --runtime "PYTHON:3.12" -o none

# SQLite under /home survives restarts on App Service. One worker keeps the
# scheduler single and SQLite safe.
az webapp config appsettings set -g "$RG" -n "$APP" -o none --settings \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true \
  DATABASE_URL="sqlite:////home/data/crew_health.db" \
  RAW_CACHE_DIR="/home/data/raw" \
  INGEST_SOURCE=bundled \
  CORS_ORIGINS="*" \
  JWT_SECRET="$(openssl rand -base64 32)" \
  ADMIN_API_KEY="$(openssl rand -hex 24)" \
  SEED_DEMO_USERS=true DEMO_PASSWORD=spaceapps2026 APP_SEED_ON_STARTUP=true \
  WEBSITES_CONTAINER_START_TIME_LIMIT=600

az webapp config set -g "$RG" -n "$APP" -o none \
  --startup-file "mkdir -p /home/data && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"
[ "$SKU" != "F1" ] && az webapp config set -g "$RG" -n "$APP" -o none --always-on true
az webapp config set -g "$RG" -n "$APP" -o none --generic-configurations '{"healthCheckPath": "/health"}'

# Package only what the API needs and deploy (Oryx installs requirements.txt).
ZIP="$(mktemp -d)/app.zip"
zip -qr "$ZIP" app requirements.txt -x '*/__pycache__/*'
az webapp deploy -g "$RG" -n "$APP" --src-path "$ZIP" --type zip -o none

echo "API: https://$(az webapp show -g "$RG" -n "$APP" --query defaultHostName -o tsv)"
