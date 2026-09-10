#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=""
REGION="asia-east1"
WEB_SERVICE="red-web"
WEBHOOK_URL=""
CHANNEL_SECRET_FILE=""
ACCESS_TOKEN_FILE=""
APPLY="0"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp_setup_line.sh --project PROJECT_ID [options]

Options:
  --project PROJECT_ID          Google Cloud project id. Required.
  --region REGION              Default: asia-east1
  --web-service NAME           Cloud Run service. Default: red-web
  --webhook-url URL            Default: Cloud Run service URL + /line/webhook
  --channel-secret-file PATH   Read LINE channel secret from file.
  --access-token-file PATH     Read LINE channel access token from file.
  --apply                      Create/update Secret Manager secrets.

Environment variables accepted:
  LINE_CHANNEL_SECRET
  LINE_CHANNEL_ACCESS_TOKEN

This script never prints secret values. Without --apply it only checks inputs
and prints the webhook URL to register in LINE Developers.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --web-service) WEB_SERVICE="$2"; shift 2 ;;
    --webhook-url) WEBHOOK_URL="$2"; shift 2 ;;
    --channel-secret-file) CHANNEL_SECRET_FILE="$2"; shift 2 ;;
    --access-token-file) ACCESS_TOKEN_FILE="$2"; shift 2 ;;
    --apply) APPLY="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$PROJECT_ID" ]]; then
  echo "Missing --project PROJECT_ID" >&2
  usage
  exit 2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is not installed. Install Google Cloud CLI first." >&2
  exit 127
fi

read_secret_value() {
  local env_name="$1"
  local file_path="$2"
  local value="${!env_name:-}"
  if [[ -z "$value" && -n "$file_path" ]]; then
    if [[ ! -f "$file_path" ]]; then
      echo "Secret file not found: ${file_path}" >&2
      return 1
    fi
    value="$(<"$file_path")"
  fi
  printf '%s' "$value"
}

CHANNEL_SECRET="$(read_secret_value LINE_CHANNEL_SECRET "$CHANNEL_SECRET_FILE")"
ACCESS_TOKEN="$(read_secret_value LINE_CHANNEL_ACCESS_TOKEN "$ACCESS_TOKEN_FILE")"

if [[ -z "$WEBHOOK_URL" ]]; then
  SERVICE_URL="$(gcloud run services describe "$WEB_SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --format='value(status.url)' 2>/dev/null || true)"
  if [[ -n "$SERVICE_URL" ]]; then
    WEBHOOK_URL="${SERVICE_URL%/}/line/webhook"
  fi
fi

if [[ -z "$WEBHOOK_URL" ]]; then
  echo "Could not determine webhook URL. Pass --webhook-url explicitly." >&2
  exit 1
fi

upsert_secret() {
  local secret_id="$1"
  local value="$2"
  if gcloud secrets describe "$secret_id" --project "$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$secret_id" \
      --project "$PROJECT_ID" \
      --data-file=- >/dev/null
    echo "Updated Secret Manager secret: ${secret_id}"
  else
    printf '%s' "$value" | gcloud secrets create "$secret_id" \
      --project "$PROJECT_ID" \
      --data-file=- >/dev/null
    echo "Created Secret Manager secret: ${secret_id}"
  fi
}

echo "LINE webhook URL:"
echo "  ${WEBHOOK_URL}"

if [[ "$APPLY" != "1" ]]; then
  cat <<EOF

Dry run only. To store LINE secrets:
  LINE_CHANNEL_SECRET='...' LINE_CHANNEL_ACCESS_TOKEN='...' \\
    scripts/gcp_setup_line.sh --project ${PROJECT_ID} --apply
EOF
  exit 0
fi

if [[ -z "$CHANNEL_SECRET" || -z "$ACCESS_TOKEN" ]]; then
  echo "Missing LINE_CHANNEL_SECRET or LINE_CHANNEL_ACCESS_TOKEN." >&2
  echo "Provide them as env vars or with --channel-secret-file / --access-token-file." >&2
  exit 2
fi

upsert_secret line-channel-secret "$CHANNEL_SECRET"
upsert_secret line-channel-access-token "$ACCESS_TOKEN"

cat <<EOF

Stored LINE secrets.

Next:
  1. Redeploy Cloud Run so the optional LINE secrets are mounted.
  2. In LINE Developers, set webhook URL to:
     ${WEBHOOK_URL}
  3. Enable webhook use and test from LINE Developers.
EOF
