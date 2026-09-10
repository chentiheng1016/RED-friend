#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=""
REGION="asia-east1"
REPOSITORY="red"
SERVICE_ACCOUNT_NAME="red-cloud-run"
SCHEDULER_ACCOUNT_NAME="red-scheduler"
BUCKET=""
REQUIRE_OPERATIONAL_DB="0"
CLOUD_SQL_INSTANCE=""

usage() {
  cat <<'USAGE'
Usage: scripts/gcp_preflight.sh [options]

Options:
  --project PROJECT_ID          Google Cloud project id. If omitted, uses gcloud config.
  --region REGION              Default: asia-east1
  --repository NAME            Artifact Registry Docker repo. Default: red
  --service-account NAME       Runtime service account name. Default: red-cloud-run
  --scheduler-account NAME     Scheduler service account name. Default: red-scheduler
  --bucket NAME                Artifact bucket. Default: PROJECT_ID-red-artifacts
  --require-operational-db     Treat missing operational-db-url secret as a failure.
  --cloud-sql-instance NAME    Check Cloud SQL instance connection name for Run resources.

Checks local CLI/login plus expected GCP resources. It does not create anything.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --repository) REPOSITORY="$2"; shift 2 ;;
    --service-account) SERVICE_ACCOUNT_NAME="$2"; shift 2 ;;
    --scheduler-account) SCHEDULER_ACCOUNT_NAME="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --require-operational-db) REQUIRE_OPERATIONAL_DB="1"; shift ;;
    --cloud-sql-instance) CLOUD_SQL_INSTANCE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

ok=1
warn() {
  ok=0
  echo "WARN: $*" >&2
}

pass() {
  echo "OK: $*"
}

if ! command -v gcloud >/dev/null 2>&1; then
  warn "gcloud is missing. Install Google Cloud CLI."
  exit 1
fi
pass "gcloud found: $(gcloud --version | head -1)"

ACCOUNT="$(gcloud auth list --filter='status:ACTIVE' --format='value(account)' 2>/dev/null | head -1 || true)"
if [[ -z "$ACCOUNT" ]]; then
  warn "no active gcloud account. Run: gcloud auth login"
else
  pass "active account: ${ACCOUNT}"
fi

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi
if [[ -z "$PROJECT_ID" ]]; then
  warn "no project configured. Run: gcloud config set project <project-id>"
else
  pass "project: ${PROJECT_ID}"
fi

if gcloud beta run worker-pools deploy --help >/dev/null 2>&1; then
  pass "Cloud Run worker-pools CLI available"
else
  warn "Cloud Run worker-pools CLI unavailable. Run: gcloud components install beta -q"
fi

if gcloud run jobs deploy --help >/dev/null 2>&1; then
  pass "Cloud Run Jobs CLI available"
else
  warn "Cloud Run Jobs CLI unavailable"
fi

if gcloud scheduler jobs create http --help >/dev/null 2>&1; then
  pass "Cloud Scheduler CLI available"
else
  warn "Cloud Scheduler CLI unavailable"
fi

if [[ -n "$PROJECT_ID" && -n "$ACCOUNT" ]]; then
  BUCKET="${BUCKET:-${PROJECT_ID}-red-artifacts}"
  SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  SCHEDULER_ACCOUNT_EMAIL="${SCHEDULER_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

  for api in \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    firestore.googleapis.com \
    sqladmin.googleapis.com \
    cloudscheduler.googleapis.com \
    billingbudgets.googleapis.com \
    storage.googleapis.com; do
    if gcloud services list --project "$PROJECT_ID" --enabled --filter="config.name=${api}" --format='value(config.name)' | grep -qx "$api"; then
      pass "API enabled: ${api}"
    else
      warn "API not enabled: ${api}"
    fi
  done

  if gcloud artifacts repositories describe "$REPOSITORY" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
    pass "Artifact Registry repo exists: ${REGION}/${REPOSITORY}"
  else
    warn "Artifact Registry repo missing: ${REGION}/${REPOSITORY}"
  fi

  if gcloud iam service-accounts describe "$SERVICE_ACCOUNT_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
    pass "runtime service account exists: ${SERVICE_ACCOUNT_EMAIL}"
  else
    warn "runtime service account missing: ${SERVICE_ACCOUNT_EMAIL}"
  fi

  if gcloud iam service-accounts describe "$SCHEDULER_ACCOUNT_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
    pass "scheduler service account exists: ${SCHEDULER_ACCOUNT_EMAIL}"
  else
    warn "scheduler service account missing: ${SCHEDULER_ACCOUNT_EMAIL}"
  fi

  if gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
    pass "artifact bucket exists: gs://${BUCKET}"
  else
    warn "artifact bucket missing: gs://${BUCKET}"
  fi

  if gcloud firestore databases describe --database="(default)" --project "$PROJECT_ID" >/dev/null 2>&1; then
    pass "Firestore database exists: (default)"
  else
    warn "Firestore database missing: create with gcloud firestore databases create --location=${REGION} --database='(default)'"
  fi

  if [[ -n "$CLOUD_SQL_INSTANCE" ]]; then
    SQL_DESCRIBE_PROJECT="$PROJECT_ID"
    SQL_DESCRIBE_INSTANCE="$CLOUD_SQL_INSTANCE"
    if [[ "$CLOUD_SQL_INSTANCE" == *:*:* ]]; then
      SQL_DESCRIBE_PROJECT="${CLOUD_SQL_INSTANCE%%:*}"
      SQL_DESCRIBE_INSTANCE="${CLOUD_SQL_INSTANCE##*:}"
    fi
    if gcloud sql instances describe "$SQL_DESCRIBE_INSTANCE" --project "$SQL_DESCRIBE_PROJECT" >/dev/null 2>&1; then
      pass "Cloud SQL instance exists: ${CLOUD_SQL_INSTANCE}"
    else
      warn "Cloud SQL instance missing or inaccessible: ${CLOUD_SQL_INSTANCE}"
    fi
    if gcloud projects get-iam-policy "$SQL_DESCRIBE_PROJECT" \
      --flatten="bindings[].members" \
      --filter="bindings.role:roles/cloudsql.client AND bindings.members:serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
      --format='value(bindings.role)' | grep -qx 'roles/cloudsql.client'; then
      pass "runtime service account has roles/cloudsql.client"
    else
      warn "runtime service account missing roles/cloudsql.client on ${SQL_DESCRIBE_PROJECT}"
    fi
  fi

  if gcloud secrets describe operational-db-url --project "$PROJECT_ID" >/dev/null 2>&1; then
    pass "Secret exists: operational-db-url"
  elif [[ "$REQUIRE_OPERATIONAL_DB" == "1" ]]; then
    warn "Secret missing: operational-db-url (required for Postgres operational DB deployment)"
  else
    echo "WARN: Secret missing: operational-db-url (Cloud Run deploy will use local fallback operational state)" >&2
  fi
fi

if [[ "$ok" == "1" ]]; then
  echo "Preflight passed."
else
  echo "Preflight found missing setup. Run scripts/gcp_bootstrap.sh after login/project selection." >&2
  exit 1
fi
