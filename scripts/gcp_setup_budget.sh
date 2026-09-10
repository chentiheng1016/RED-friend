#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=""
BILLING_ACCOUNT=""
DISPLAY_NAME="RED Monthly Budget"
BUDGET_AMOUNT=""
NOTIFICATION_CHANNEL=""

usage() {
  cat <<'USAGE'
Usage: scripts/gcp_setup_budget.sh --project PROJECT_ID --amount AMOUNT [options]

Options:
  --project PROJECT_ID          Google Cloud project id. Used for budget filter and billing lookup.
  --billing-account ID          Billing account id. Default: lookup from --project.
  --amount AMOUNT               Monthly budget amount, for example 1000TWD or 50USD. Required.
  --display-name NAME           Default: RED Monthly Budget
  --notification-channel NAME   Monitoring notification channel resource name.

Creates a monthly budget for the RED project with 50%, 80%, 100%, and
80%-forecasted thresholds. It does not cap spend; it only sends alerts.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --billing-account) BILLING_ACCOUNT="$2"; shift 2 ;;
    --amount) BUDGET_AMOUNT="$2"; shift 2 ;;
    --display-name) DISPLAY_NAME="$2"; shift 2 ;;
    --notification-channel) NOTIFICATION_CHANNEL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$PROJECT_ID" && -z "$BILLING_ACCOUNT" ]]; then
  echo "Missing --project PROJECT_ID or --billing-account ID" >&2
  usage
  exit 2
fi
if [[ -z "$BUDGET_AMOUNT" ]]; then
  echo "Missing --amount AMOUNT, for example 1000TWD or 50USD" >&2
  usage
  exit 2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is not installed. Install Google Cloud CLI first." >&2
  exit 127
fi

if [[ -n "$PROJECT_ID" ]]; then
  echo "==> Ensuring Budget API is enabled for ${PROJECT_ID}"
  gcloud services enable billingbudgets.googleapis.com --project "$PROJECT_ID" >/dev/null
fi

if [[ -z "$BILLING_ACCOUNT" ]]; then
  BILLING_ACCOUNT="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)' | sed 's#billingAccounts/##')"
fi
if [[ -z "$BILLING_ACCOUNT" ]]; then
  echo "Could not determine billing account. Pass --billing-account explicitly." >&2
  exit 1
fi

existing_budget="$(gcloud beta billing budgets list \
  --billing-account "$BILLING_ACCOUNT" \
  --format='csv[no-heading](name,displayName)' \
  | awk -F, -v target="$DISPLAY_NAME" '$2 == target {print $1; exit}')"
if [[ -n "$existing_budget" ]]; then
  echo "Budget already exists:"
  echo "  ${existing_budget}"
  exit 0
fi

args=(
  beta billing budgets create
  --billing-account "$BILLING_ACCOUNT"
  --display-name "$DISPLAY_NAME"
  --budget-amount "$BUDGET_AMOUNT"
  --calendar-period month
  --threshold-rule percent=0.50
  --threshold-rule percent=0.80
  --threshold-rule percent=1.00
  --threshold-rule percent=0.80,basis=forecasted-spend
)
if [[ -n "$PROJECT_ID" ]]; then
  args+=(--filter-projects "projects/${PROJECT_ID}")
fi
if [[ -n "$NOTIFICATION_CHANNEL" ]]; then
  args+=(--all-updates-rule-monitoring-notification-channels "$NOTIFICATION_CHANNEL")
fi

echo "==> Creating budget: ${DISPLAY_NAME} (${BUDGET_AMOUNT})"
gcloud "${args[@]}"
