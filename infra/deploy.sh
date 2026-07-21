#!/usr/bin/env bash
# Deploy the mock MES to Azure Container Apps (Consumption, scale-to-zero).
#
# Prereqs: az CLI logged in. Images must already be published (public) to GHCR
# by the GitHub Actions workflow (.github/workflows/images.yml).
#
# Usage:
#   ./infra/deploy.sh                      # defaults: rg-mock-mes-kr / koreacentral
#   RG=my-rg LOCATION=eastus ./infra/deploy.sh
set -euo pipefail

RG="${RG:-rg-mock-mes-kr}"
LOCATION="${LOCATION:-koreacentral}"
APP_NAME="${APP_NAME:-mock-mes}"
APP_IMAGE="${APP_IMAGE:-ghcr.io/changju-ahn/mock-mes-app:latest}"
PROXY_IMAGE="${PROXY_IMAGE:-ghcr.io/changju-ahn/mock-mes-proxy:latest}"

echo "==> Ensuring containerapp extension + providers"
az extension add --name containerapp --upgrade --only-show-errors -y >/dev/null 2>&1 || true
az provider register --namespace Microsoft.App --wait --only-show-errors || true
az provider register --namespace Microsoft.OperationalInsights --wait --only-show-errors || true

echo "==> Creating resource group '$RG' in '$LOCATION'"
az group create -n "$RG" -l "$LOCATION" -o none

echo "==> Deploying Bicep"
az deployment group create \
  -g "$RG" \
  -n "mock-mes-$(date +%s)" \
  -f infra/main.bicep \
  -p location="$LOCATION" appName="$APP_NAME" appImage="$APP_IMAGE" proxyImage="$PROXY_IMAGE" \
  -o none

FQDN="$(az containerapp show -g "$RG" -n "$APP_NAME" --query properties.configuration.ingress.fqdn -o tsv)"

echo ""
echo "==> Deployed. Endpoints:"
echo "    Web console : https://$FQDN/"
echo "    REST API    : https://$FQDN/api      (docs: https://$FQDN/api/docs)"
echo "    MCP server  : https://$FQDN/mcp"
