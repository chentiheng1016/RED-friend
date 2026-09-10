#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=""
REGION="asia-east1"
SERVICE_ACCOUNT_NAME="red-scheduler"
TIME_ZONE="Asia/Taipei"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp_schedule_jobs.sh --project PROJECT_ID [options]

Options:
  --project PROJECT_ID          Google Cloud project id. Required.
  --region REGION              Default: asia-east1
  --service-account NAME       Scheduler OAuth service account. Default: red-scheduler
  --time-zone TZ               Default: Asia/Taipei

Creates or updates Cloud Scheduler HTTP jobs that trigger Cloud Run Jobs.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --service-account) SERVICE_ACCOUNT_NAME="$2"; shift 2 ;;
    --time-zone) TIME_ZONE="$2"; shift 2 ;;
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

SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

upsert_schedule() {
  local scheduler_name="$1"
  local job_name="$2"
  local schedule="$3"
  local description="$4"
  local uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${job_name}:run"

  echo "==> Scheduler: ${scheduler_name} -> ${job_name} (${schedule})"
  if gcloud scheduler jobs describe "$scheduler_name" \
    --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$scheduler_name" \
      --project "$PROJECT_ID" \
      --location "$REGION" \
      --schedule "$schedule" \
      --time-zone "$TIME_ZONE" \
      --uri "$uri" \
      --http-method POST \
      --oauth-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
      --update-headers "Content-Type=application/json" \
      --message-body "{}" \
      --description "$description"
  else
    gcloud scheduler jobs create http "$scheduler_name" \
      --project "$PROJECT_ID" \
      --location "$REGION" \
      --schedule "$schedule" \
      --time-zone "$TIME_ZONE" \
      --uri "$uri" \
      --http-method POST \
      --oauth-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
      --headers "Content-Type=application/json" \
      --message-body "{}" \
      --description "$description"
  fi
}

upsert_schedule "red-dispatcher-5min" "red-task-dispatcher" "*/5 * * * *" "Run RED dispatcher every 5 minutes."
upsert_schedule "red-briefing-5min" "red-task-briefing-15min" "*/5 * * * *" "Scan calendar and push meeting briefings."
upsert_schedule "red-email-ingest-15min" "red-task-email-ingest" "*/15 * * * *" "Incremental Gmail data lake ingest."
upsert_schedule "red-health-30min" "red-task-health-check" "*/30 * * * *" "RED health check."
upsert_schedule "red-mailcheck-hourly" "red-task-mailcheck" "0 * * * *" "Unread mail triage."
upsert_schedule "red-ponder-2h" "red-task-ponder" "0 */2 * * *" "Recent-message insight distillation."
upsert_schedule "red-morning-0830" "red-task-morning" "30 8 * * *" "Morning briefing."
upsert_schedule "red-sample-check-0900" "red-task-sample-check" "0 9 * * *" "Daily sample deadline check."
upsert_schedule "red-rag-sync-0300" "red-task-rag-sync" "0 3 * * *" "Daily Drive/Gmail RAG sync."

echo "Cloud Scheduler jobs are ready."
