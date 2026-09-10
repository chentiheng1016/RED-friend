#!/usr/bin/env bash
set -euo pipefail

REGION="asia-east1"
REPOSITORY="red"
SERVICE_ACCOUNT_NAME="red-cloud-run"
SCHEDULER_ACCOUNT_NAME="red-scheduler"
BUCKET=""
PROJECT_ID=""

usage() {
  cat <<'USAGE'
Usage: scripts/gcp_bootstrap.sh --project PROJECT_ID [options]

Options:
  --project PROJECT_ID          Google Cloud project id. Required.
  --region REGION              Region for Cloud Run/Artifact Registry. Default: asia-east1
  --repository NAME            Artifact Registry Docker repo. Default: red
  --service-account NAME       Runtime service account name. Default: red-cloud-run
  --scheduler-account NAME     Scheduler service account name. Default: red-scheduler
  --bucket NAME                Artifact bucket name. Default: PROJECT_ID-red-artifacts

This script enables APIs and creates:
  - Artifact Registry Docker repository
  - Cloud Run runtime service account
  - Cloud Scheduler invoker service account
  - Cloud Storage bucket for RED artifacts
  - IAM bindings needed by Cloud Run, Cloud Scheduler, and Cloud Build
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

BUCKET="${BUCKET:-${PROJECT_ID}-red-artifacts}"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_ACCOUNT_EMAIL="${SCHEDULER_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> Project: ${PROJECT_ID}"
gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  sqladmin.googleapis.com \
  cloudscheduler.googleapis.com \
  billingbudgets.googleapis.com \
  iam.googleapis.com \
  storage.googleapis.com

echo "==> Ensuring Artifact Registry repository: ${REPOSITORY} (${REGION})"
if ! gcloud artifacts repositories describe "$REPOSITORY" \
  --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPOSITORY" \
    --project "$PROJECT_ID" \
    --location "$REGION" \
    --repository-format docker \
    --description "RED container images"
fi

echo "==> Ensuring runtime service account: ${SERVICE_ACCOUNT_EMAIL}"
if ! gcloud iam service-accounts describe "$SERVICE_ACCOUNT_EMAIL" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
    --project "$PROJECT_ID" \
    --display-name "RED Cloud Run runtime"
fi

echo "==> Ensuring scheduler service account: ${SCHEDULER_ACCOUNT_EMAIL}"
if ! gcloud iam service-accounts describe "$SCHEDULER_ACCOUNT_EMAIL" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SCHEDULER_ACCOUNT_NAME" \
    --project "$PROJECT_ID" \
    --display-name "RED Cloud Scheduler invoker"
fi

echo "==> Ensuring artifact bucket: gs://${BUCKET}"
if ! gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --project "$PROJECT_ID" \
    --location "$REGION" \
    --uniform-bucket-level-access
fi

grant_project_role() {
  local member="$1"
  local role="$2"
  echo "==> Granting ${role} to ${member}"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "$member" \
    --role "$role" \
    --condition=None >/dev/null
}

RUNTIME_MEMBER="serviceAccount:${SERVICE_ACCOUNT_EMAIL}"
SCHEDULER_MEMBER="serviceAccount:${SCHEDULER_ACCOUNT_EMAIL}"
grant_project_role "$RUNTIME_MEMBER" "roles/secretmanager.secretAccessor"
grant_project_role "$RUNTIME_MEMBER" "roles/datastore.user"
grant_project_role "$RUNTIME_MEMBER" "roles/cloudsql.client"
grant_project_role "$RUNTIME_MEMBER" "roles/logging.logWriter"
grant_project_role "$SCHEDULER_MEMBER" "roles/run.invoker"

echo "==> Granting bucket objectAdmin to runtime service account"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "$RUNTIME_MEMBER" \
  --role "roles/storage.objectAdmin" >/dev/null

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
CLOUDBUILD_MEMBERS=(
  "serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
  "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
)
for member in "${CLOUDBUILD_MEMBERS[@]}"; do
  grant_project_role "$member" "roles/artifactregistry.writer"
  grant_project_role "$member" "roles/logging.logWriter"
done

cat <<EOF

Bootstrap complete.

Runtime service account:
  ${SERVICE_ACCOUNT_EMAIL}

Scheduler service account:
  ${SCHEDULER_ACCOUNT_EMAIL}

Artifact Registry:
  ${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}

Artifact bucket:
  gs://${BUCKET}

Next:
  1. Create required secrets in Secret Manager.
  2. Run scripts/gcp_deploy_cloud_run.sh --project ${PROJECT_ID}
EOF
