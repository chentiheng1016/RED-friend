#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=""
REGION="asia-east1"
SERVICE="red-web"
DOMAIN=""
FORCE_OVERRIDE="0"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp_map_domain.sh --project PROJECT_ID --domain HOSTNAME [options]

Options:
  --project PROJECT_ID          Google Cloud project id. Required.
  --region REGION              Default: asia-east1
  --service SERVICE            Cloud Run service. Default: red-web
  --domain HOSTNAME            Custom domain, for example red.example.com. Required.
  --force-override             Override an existing mapping for this domain.

Creates a Cloud Run domain mapping and prints the DNS records Google expects.
After DNS is active, redeploy with --oauth-redirect-base https://HOSTNAME and
add https://HOSTNAME/auth/callback to the Google OAuth client.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --force-override) FORCE_OVERRIDE="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$PROJECT_ID" || -z "$DOMAIN" ]]; then
  echo "Missing --project PROJECT_ID or --domain HOSTNAME" >&2
  usage
  exit 2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is not installed. Install Google Cloud CLI first." >&2
  exit 127
fi

if gcloud beta run domain-mappings describe \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --domain "$DOMAIN" >/dev/null 2>&1; then
  echo "Domain mapping already exists: ${DOMAIN}"
else
  args=(
    beta run domain-mappings create
    --project "$PROJECT_ID"
    --region "$REGION"
    --service "$SERVICE"
    --domain "$DOMAIN"
  )
  if [[ "$FORCE_OVERRIDE" == "1" ]]; then
    args+=(--force-override)
  fi
  echo "==> Creating domain mapping: ${DOMAIN} -> ${SERVICE}"
  gcloud "${args[@]}"
fi

cat <<EOF

DNS records to add:
EOF
gcloud beta run domain-mappings describe \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --domain "$DOMAIN" \
  --format='table(status.resourceRecords[].type,status.resourceRecords[].rrdata)'

cat <<EOF

After DNS is active:
  scripts/gcp_deploy_cloud_run.sh --project ${PROJECT_ID} --region ${REGION} --oauth-redirect-base https://${DOMAIN}

Google OAuth redirect URI:
  https://${DOMAIN}/auth/callback
EOF
