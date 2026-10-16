#!/usr/bin/env bash
# Deploy Counter to Cloud Run: build from source with Cloud Build.
#
#   bash deploy/deploy.sh
#
# Run deploy/push_secrets.py first so every counter-<key> secret exists.
# The runtime service account needs roles/secretmanager.secretAccessor on each
# counter-<key> secret. The compute service account cannot grant that itself
# (403 on secrets.setIamPolicy); a project owner ran the grant once, so the
# default path below uses --set-secrets. If you cannot get the grant, a
# fallback passes plain env vars from a temp file outside the repo (delete it
# right after; never commit or print it):
#
#   COUNTER_ENV_VARS_FILE=/tmp/counter-env-vars.yaml bash deploy/deploy.sh
#
# Flags and why:
#   --min/max-instances 1     one process: sessions and pending actions live in memory
#   --no-cpu-throttling       Slack Socket Mode needs CPU while no request is in flight
#   --timeout 3600            the console holds a long-lived SSE stream open
#   --session-affinity        keep one browser on one instance
#   --allow-unauthenticated   judges open the URL without a Google login
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SECRETS_FLAG=(
  --set-secrets "ARMORIQ_API_KEY=counter-armoriq-api-key:latest,ARMORIQ_LOGIN=counter-armoriq-login:latest,GEMINI_API_KEY=counter-gemini-api-key:latest,OWNER_PASSWORD=counter-owner-password:latest,MCP_TOKEN=counter-mcp-token:latest,SLACK_BOT_TOKEN=counter-slack-bot-token:latest,SLACK_APP_TOKEN=counter-slack-app-token:latest,SLACK_CHANNEL=counter-slack-channel:latest"
)
ENV_FLAG=(--set-env-vars "COUNTER_WEB_DIR=/app/web,COUNTER_DB=/tmp/counter.db,MCP_URL=http://127.0.0.1:8001,MCP_HOST=127.0.0.1,MCP_PORT=8001")
if [ -n "${COUNTER_ENV_VARS_FILE:-}" ]; then
  # --env-vars-file replaces --set-env-vars, so that file carries all 13 vars.
  echo "secrets: --env-vars-file ${COUNTER_ENV_VARS_FILE} (secret IAM grant failed on this project)"
  SECRETS_FLAG=(--env-vars-file "$COUNTER_ENV_VARS_FILE")
  ENV_FLAG=()
fi

gcloud run deploy counter \
  --project gen-lang-client-0261050164 \
  --region us-central1 \
  --source . \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 1 \
  --no-cpu-throttling \
  --timeout 3600 \
  --session-affinity \
  --memory 1Gi \
  "${SECRETS_FLAG[@]}" \
  ${ENV_FLAG[@]+"${ENV_FLAG[@]}"}
