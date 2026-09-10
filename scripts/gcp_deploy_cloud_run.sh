#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=""
REGION="asia-east1"
REPOSITORY="red"
IMAGE_TAG=""
SERVICE_ACCOUNT_NAME="red-cloud-run"
BUCKET=""
WEB_SERVICE="red-web"
TELEGRAM_WORKER="red-telegram"
DEPLOY_TELEGRAM="1"
DEPLOY_JOBS="1"
OAUTH_REDIRECT_BASE=""
RED_OBJECT_BASE_URL=""
REQUIRE_OPERATIONAL_DB="0"
CLOUD_SQL_INSTANCE=""

usage() {
  cat <<'USAGE'
Usage: scripts/gcp_deploy_cloud_run.sh --project PROJECT_ID [options]

Options:
  --project PROJECT_ID          Google Cloud project id. Required.
  --region REGION              Default: asia-east1
  --repository NAME            Artifact Registry Docker repo. Default: red
  --tag TAG                    Image tag. Default: current git short SHA or timestamp
  --service-account NAME       Runtime service account name. Default: red-cloud-run
  --bucket NAME                Artifact bucket. Default: PROJECT_ID-red-artifacts
  --oauth-redirect-base URL    Stable public base URL for /auth/callback.
  --object-base-url URL        Public/signed gateway base URL for large artifacts.
  --require-operational-db     Fail if the operational-db-url secret is missing.
  --cloud-sql-instance NAME    Attach Cloud SQL instance connection name to Run resources.
  --skip-telegram              Do not deploy Cloud Run worker pool.
  --skip-jobs                  Do not deploy Cloud Run Jobs.

Required Secret Manager secret ids:
  gemini-api-key
  telegram-bot-token
  telegram-chat-id
  web-secret-key
  google-oauth-client-id
  google-oauth-client-secret
  google-oauth-token-json

Optional Secret Manager secret ids:
  operational-db-url
  line-channel-secret
  line-channel-access-token
  edge-agent-token
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --repository) REPOSITORY="$2"; shift 2 ;;
    --tag) IMAGE_TAG="$2"; shift 2 ;;
    --service-account) SERVICE_ACCOUNT_NAME="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --oauth-redirect-base) OAUTH_REDIRECT_BASE="$2"; shift 2 ;;
    --object-base-url) RED_OBJECT_BASE_URL="$2"; shift 2 ;;
    --require-operational-db) REQUIRE_OPERATIONAL_DB="1"; shift ;;
    --cloud-sql-instance) CLOUD_SQL_INSTANCE="$2"; shift 2 ;;
    --skip-telegram) DEPLOY_TELEGRAM="0"; shift ;;
    --skip-jobs) DEPLOY_JOBS="0"; shift ;;
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

if [[ -z "$IMAGE_TAG" ]]; then
  if git rev-parse --short HEAD >/dev/null 2>&1; then
    IMAGE_TAG="$(git rev-parse --short HEAD)"
  else
    IMAGE_TAG="$(date +%Y%m%d%H%M%S)"
  fi
fi

BUCKET="${BUCKET:-${PROJECT_ID}-red-artifacts}"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/red:${IMAGE_TAG}"
MOUNT_PATH="/mnt/red-artifacts"
VOLUME_NAME="red-artifacts"
RUN_UID="10001"
RUN_GID="10001"

REQUIRED_SECRETS=(
  gemini-api-key
  telegram-bot-token
  telegram-chat-id
  web-secret-key
  google-oauth-client-id
  google-oauth-client-secret
  google-oauth-token-json
)

echo "==> Checking required secrets"
for secret in "${REQUIRED_SECRETS[@]}"; do
  if ! gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "Missing Secret Manager secret: ${secret}" >&2
    echo "Create it before deploying, for example:" >&2
    echo "  printf '%s' 'VALUE' | gcloud secrets create ${secret} --project ${PROJECT_ID} --data-file=-" >&2
    exit 1
  fi
done

COMMON_ENV=(
  "RED_CLOUD_MODE=1"
  "RED_EXCHANGE_MODE=telegram_only"
  "AGENT_DAEMON_MODE=1"
  "RED_RUNTIME_DIR=/var/lib/red"
  "RED_EMPLOYEE_REGISTRY_BACKEND=firestore"
  "RED_FIRESTORE_PROJECT=${PROJECT_ID}"
  "RED_EMPLOYEE_REGISTRY_FILE=${MOUNT_PATH}/data/employee_registry.json"
  "RED_OBJECT_STORAGE_DIR=${MOUNT_PATH}"
  "RED_TELEGRAM_UPLOAD_DIR=${MOUNT_PATH}/incoming"
  "WEB_HTTPS_ONLY=1"
)
if [[ -n "$OAUTH_REDIRECT_BASE" ]]; then
  COMMON_ENV+=("OAUTH_REDIRECT_BASE=${OAUTH_REDIRECT_BASE%/}")
fi
if [[ -n "$RED_OBJECT_BASE_URL" ]]; then
  COMMON_ENV+=("RED_OBJECT_BASE_URL=${RED_OBJECT_BASE_URL%/}")
fi

join_by_comma() {
  local IFS=,
  echo "$*"
}

SECRET_BINDINGS=(
  "GEMINI_API_KEY=gemini-api-key:latest"
  "TELEGRAM_BOT_TOKEN=telegram-bot-token:latest"
  "TELEGRAM_CHAT_ID=telegram-chat-id:latest"
  "WEB_SECRET_KEY=web-secret-key:latest"
  "GOOGLE_OAUTH_CLIENT_ID=google-oauth-client-id:latest"
  "GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest"
  "GOOGLE_OAUTH_TOKEN_JSON=google-oauth-token-json:latest"
)
if gcloud secrets describe line-channel-secret --project "$PROJECT_ID" >/dev/null 2>&1 && \
   gcloud secrets describe line-channel-access-token --project "$PROJECT_ID" >/dev/null 2>&1; then
  COMMON_ENV+=("RED_LINE_ENABLED=1")
  SECRET_BINDINGS+=(
    "LINE_CHANNEL_SECRET=line-channel-secret:latest"
    "LINE_CHANNEL_ACCESS_TOKEN=line-channel-access-token:latest"
  )
elif gcloud secrets describe line-channel-secret --project "$PROJECT_ID" >/dev/null 2>&1 || \
     gcloud secrets describe line-channel-access-token --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Warning: LINE is partially configured. Add both line-channel-secret and line-channel-access-token to enable /line/webhook." >&2
fi
if gcloud secrets describe edge-agent-token --project "$PROJECT_ID" >/dev/null 2>&1; then
  SECRET_BINDINGS+=("RED_EDGE_AGENT_TOKEN=edge-agent-token:latest")
fi
if gcloud secrets describe operational-db-url --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "==> Enabling Postgres operational DB backends"
  SECRET_BINDINGS+=("RED_OPERATIONAL_DB_URL=operational-db-url:latest")
  COMMON_ENV+=(
    "RED_OPERATIONAL_DB_POOL=1"
    "RED_OPERATIONAL_DB_POOL_MIN_SIZE=0"
    "RED_OPERATIONAL_DB_POOL_MAX_SIZE=4"
    "RED_OPERATIONAL_DB_CONNECT_TIMEOUT_SEC=2"
    "RED_TASK_QUEUE_BACKEND=postgres"
    "RED_TELEGRAM_APPROVALS_BACKEND=postgres"
    "RED_TELEGRAM_AUTH_BACKEND=postgres"
    "RED_TOOL_BUDGETS_BACKEND=postgres"
    "RED_COST_TRACKER_BACKEND=postgres"
    "RED_RUN_HISTORY_BACKEND=postgres"
    "RED_TASK_MEMORY_BACKEND=postgres"
    "RED_EDGE_TASKS_BACKEND=postgres"
    "RED_POLICY_ENGINE_BACKEND=postgres"
    "RED_WORK_MODE_BACKEND=postgres"
    "RED_DRY_RUN_BACKEND=postgres"
    "RED_WEB_DOMAIN_POLICY_BACKEND=postgres"
    "RED_INTENT_ROUTER_BACKEND=postgres"
    "RED_ALERT_PUSH_BACKEND=postgres"
    "RED_GEMINI_CIRCUIT_BACKEND=postgres"
    "RED_GEMINI_CIRCUIT_BREAKER=1"
    "RED_GEMINI_CIRCUIT_FAILURES=3"
    "RED_GEMINI_CIRCUIT_WINDOW_S=300"
    "RED_GEMINI_CIRCUIT_OPEN_S=180"
  )
elif [[ "$REQUIRE_OPERATIONAL_DB" == "1" ]]; then
  echo "Missing Secret Manager secret: operational-db-url" >&2
  echo "Create it with the Postgres connection URL before deploying, then rerun." >&2
  exit 1
else
  echo "Warning: operational-db-url secret missing. Cloud Run will use local fallback operational state." >&2
fi

SECRETS_CSV="$(join_by_comma "${SECRET_BINDINGS[@]}")"

VOLUME_ARGS=(
  --add-volume "name=${VOLUME_NAME},type=cloud-storage,bucket=${BUCKET},mount-options=uid=${RUN_UID};gid=${RUN_GID};implicit-dirs"
  --add-volume-mount "volume=${VOLUME_NAME},mount-path=${MOUNT_PATH}"
)
SERVICE_VOLUME_ARGS=(
  --execution-environment gen2
  --add-volume "name=${VOLUME_NAME},type=cloud-storage,bucket=${BUCKET},mount-options=uid=${RUN_UID};gid=${RUN_GID};implicit-dirs"
  --add-volume-mount "volume=${VOLUME_NAME},mount-path=${MOUNT_PATH}"
)
CLOUD_SQL_RUN_ARGS=()
CLOUD_SQL_JOB_ARGS=()
if [[ -n "$CLOUD_SQL_INSTANCE" ]]; then
  echo "==> Attaching Cloud SQL instance: ${CLOUD_SQL_INSTANCE}"
  CLOUD_SQL_RUN_ARGS=(--add-cloudsql-instances "$CLOUD_SQL_INSTANCE")
  CLOUD_SQL_JOB_ARGS=(--set-cloudsql-instances "$CLOUD_SQL_INSTANCE")
fi

echo "==> Building image via Cloud Build: ${IMAGE}"
gcloud builds submit \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --tag "$IMAGE" \
  .

echo "==> Deploying Cloud Run service: ${WEB_SERVICE}"
gcloud run deploy "$WEB_SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$SERVICE_ACCOUNT_EMAIL" \
  --allow-unauthenticated \
  --cpu 1 \
  --memory 2Gi \
  --timeout 900 \
  --set-env-vars "$(join_by_comma "${COMMON_ENV[@]}" "RED_CLOUD_ROLE=web")" \
  --set-secrets "$SECRETS_CSV" \
  "${CLOUD_SQL_RUN_ARGS[@]+"${CLOUD_SQL_RUN_ARGS[@]}"}" \
  "${SERVICE_VOLUME_ARGS[@]}"

WEB_URL="$(gcloud run services describe "$WEB_SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)' 2>/dev/null || true)"
if [[ -z "$OAUTH_REDIRECT_BASE" && -n "$WEB_URL" ]]; then
  echo "==> Setting OAUTH_REDIRECT_BASE to Cloud Run service URL: ${WEB_URL}"
  gcloud run services update "$WEB_SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --update-env-vars "OAUTH_REDIRECT_BASE=${WEB_URL}"
  OAUTH_REDIRECT_BASE="$WEB_URL"
fi

if [[ "$DEPLOY_TELEGRAM" == "1" ]]; then
  echo "==> Deploying Cloud Run worker pool: ${TELEGRAM_WORKER}"
  # --no-cpu-throttling: telegram_bot runs a long-poll loop. With the
  # default CPU-only-during-requests allocation, Cloud Run reclaims CPU
  # between getUpdates calls and the worker stalls. Always-on CPU costs
  # more but keeps the bot responsive.
  gcloud beta run worker-pools deploy "$TELEGRAM_WORKER" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE" \
    --service-account "$SERVICE_ACCOUNT_EMAIL" \
    --cpu 1 \
    --memory 2Gi \
    --no-cpu-throttling \
    --set-env-vars "$(join_by_comma "${COMMON_ENV[@]}" "RED_CLOUD_ROLE=telegram")" \
    --set-secrets "$SECRETS_CSV" \
    "${CLOUD_SQL_RUN_ARGS[@]+"${CLOUD_SQL_RUN_ARGS[@]}"}" \
    --add-volume "name=${VOLUME_NAME},type=cloud-storage,bucket=${BUCKET},mount-options=uid=${RUN_UID};gid=${RUN_GID};implicit-dirs" \
    --add-volume-mount "volume=${VOLUME_NAME},mount-path=${MOUNT_PATH}"
fi

deploy_job() {
  local task="$1"
  local timeout="$2"
  local job="red-task-${task//_/-}"
  echo "==> Deploying Cloud Run job: ${job} (${task})"
  gcloud run jobs deploy "$job" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE" \
    --service-account "$SERVICE_ACCOUNT_EMAIL" \
    --cpu 1 \
    --memory 2Gi \
    --tasks 1 \
    --max-retries 1 \
    --task-timeout "$timeout" \
    --set-env-vars "$(join_by_comma "${COMMON_ENV[@]}" "RED_CLOUD_ROLE=task" "RED_DAEMON_TASK=${task}")" \
    --set-secrets "$SECRETS_CSV" \
    "${CLOUD_SQL_JOB_ARGS[@]+"${CLOUD_SQL_JOB_ARGS[@]}"}" \
    "${VOLUME_ARGS[@]}"
}

if [[ "$DEPLOY_JOBS" == "1" ]]; then
  deploy_job "dispatcher" "900s"
  deploy_job "briefing_15min" "900s"
  deploy_job "email_ingest" "1800s"
  deploy_job "health_check" "900s"
  deploy_job "mailcheck" "1800s"
  deploy_job "morning" "1800s"
  deploy_job "ponder" "1800s"
  deploy_job "rag_sync" "7200s"
  deploy_job "sample_check" "1800s"
fi

cat <<EOF

Deploy complete.

Image:
  ${IMAGE}

Web service URL:
  ${WEB_URL}

OAuth redirect URI to add in Google Cloud Console:
  ${OAUTH_REDIRECT_BASE%/}/auth/callback

Next:
  scripts/gcp_schedule_jobs.sh --project ${PROJECT_ID}
EOF
