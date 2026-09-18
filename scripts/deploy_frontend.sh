#!/usr/bin/env bash
# Builds the React frontend, writes runtime config.json from live CFN
# outputs, syncs to the SiteBucket, and invalidates CloudFront. Safe to
# re-run any time the backend stacks change -- no frontend code change
# needed for a backend-only redeploy.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="us-east-2"

out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

USER_POOL_ID=$(out PolicyPortal-Auth UserPoolId)
USER_POOL_CLIENT_ID=$(out PolicyPortal-Auth UserPoolClientId)
API_URL=$(out PolicyPortal-Api ApiFunctionUrl)
SITE_BUCKET=$(out PolicyPortal-Frontend SiteBucketName)
DIST_ID=$(out PolicyPortal-Frontend DistributionId)
# Optional: the Class Insights companion app / Model Tuning feature aren't
# always deployed. Don't fail this script if either stack doesn't exist yet.
INSIGHTS_URL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name PolicyPortal-InsightsSite \
  --query "Stacks[0].Outputs[?OutputKey=='InsightsSiteUrl'].OutputValue" --output text 2>/dev/null || true)
MODEL_SELECTOR_URL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name PolicyPortal-ModelSelector \
  --query "Stacks[0].Outputs[?OutputKey=='ModelSelectorUrl'].OutputValue" --output text 2>/dev/null || true)

echo "UserPoolId=$USER_POOL_ID ClientId=$USER_POOL_CLIENT_ID ApiUrl=$API_URL Bucket=$SITE_BUCKET InsightsUrl=${INSIGHTS_URL:-<none>} ModelSelectorUrl=${MODEL_SELECTOR_URL:-<none>}"

cat > frontend/public/config.json <<EOF
{
  "userPoolId": "$USER_POOL_ID",
  "userPoolClientId": "$USER_POOL_CLIENT_ID",
  "region": "$REGION",
  "apiUrl": "$API_URL",
  "insightsUrl": "${INSIGHTS_URL:-}",
  "modelSelectorUrl": "${MODEL_SELECTOR_URL:-}"
}
EOF

(cd frontend && npm install && npm run build)

aws s3 sync frontend/dist "s3://$SITE_BUCKET" --region "$REGION" --delete

if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
  aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null
  echo "CloudFront invalidation submitted for $DIST_ID"
fi

echo "Frontend deployed. Site: https://www.awsteach.com"
