#!/usr/bin/env bash
# Sovereign Lens: Cloud Run へのワンコマンドデプロイ（Google Cloud Shell で実行）
#   使い方: ./deploy.sh <PROJECT_ID> [EDINET_API_KEY]
set -euo pipefail

PROJECT_ID="${1:?Usage: ./deploy.sh <PROJECT_ID> [EDINET_API_KEY]}"
EDINET_KEY="${2:-}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-sovereign-lens}"
SA_NAME="sovereign-lens-sa"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> Project: ${PROJECT_ID} / Region: ${REGION}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> Enabling APIs (初回は数分かかります)"
gcloud services enable run.googleapis.com aiplatform.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudtrace.googleapis.com logging.googleapis.com secretmanager.googleapis.com

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "==> Service account (最小権限)"
gcloud iam service-accounts describe "${SA}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${SA_NAME}" --display-name="Sovereign Lens runtime"
for role in roles/aiplatform.user roles/cloudtrace.agent roles/logging.logWriter roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${SA}" --role="${role}" --condition=None >/dev/null
done
# ソースからのビルドに必要な権限（新しいプロジェクトで不足しがち）
for role in roles/run.builder roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${COMPUTE_SA}" --role="${role}" --condition=None >/dev/null || true
done

SECRET_FLAG=()
if [[ -n "${EDINET_KEY}" ]]; then
  echo "==> Storing EDINET API key in Secret Manager"
  if gcloud secrets describe edinet-api-key >/dev/null 2>&1; then
    printf '%s' "${EDINET_KEY}" | gcloud secrets versions add edinet-api-key --data-file=-
  else
    printf '%s' "${EDINET_KEY}" | gcloud secrets create edinet-api-key --data-file=- --replication-policy=automatic
  fi
fi
# 既存のシークレットはそのまま参照（キーを再入力しなくてよい）
SECRETS=()
gcloud secrets describe edinet-api-key >/dev/null 2>&1 && SECRETS+=("EDINET_API_KEY=edinet-api-key:latest")
gcloud secrets describe gmi-api-key >/dev/null 2>&1 && SECRETS+=("GMI_API_KEY=gmi-api-key:latest")
if [[ ${#SECRETS[@]} -gt 0 ]]; then
  SECRET_FLAG=(--update-secrets "$(IFS=,; echo "${SECRETS[*]}")")
fi

echo "==> Deploying to Cloud Run"
gcloud run deploy "${SERVICE}" \
  --source . --quiet \
  --region "${REGION}" \
  --service-account "${SA}" \
  --allow-unauthenticated \
  --memory 1Gi --cpu 1 --timeout 300 \
  --concurrency 20 --min-instances 0 --max-instances 3 \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,ENABLE_CLOUD_TRACE=1,AGENT_RATE_LIMIT=8,MAX_TOOL_CALLS=30,GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.8-flash},GMI_MODEL=${GMI_MODEL:-deepseek-ai/DeepSeek-V4-Pro}" \
  "${SECRET_FLAG[@]}"

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo
echo "✅ Deployed: ${URL}"
echo "   ヘルスチェック: curl ${URL}/api/health"
