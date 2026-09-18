#!/usr/bin/env bash
# Writes insights-frontend/public/config.json from the live PolicyPortal-InsightsApi
# stack output, then pushes just that one file to the InsightsSite bucket and
# invalidates CloudFront. The rest of insights-frontend/public is already synced
# by CDK's BucketDeployment inside InsightsSiteStack on every `cdk deploy` -- this
# script only handles config.json, which needs the InsightsApi stack's output and
# so can't be known until after both stacks have deployed.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="us-east-2"

out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

API_URL=$(out PolicyPortal-InsightsApi InsightsApiUrl)
SITE_BUCKET=$(out PolicyPortal-InsightsSite SiteBucketName)
DIST_ID=$(out PolicyPortal-InsightsSite DistributionId)
SITE_URL=$(out PolicyPortal-InsightsSite InsightsSiteUrl)

echo "ApiUrl=$API_URL Bucket=$SITE_BUCKET Dist=$DIST_ID"

cat > insights-frontend/public/config.json <<EOF
{
  "apiUrl": "$API_URL"
}
EOF

aws s3 cp insights-frontend/public/config.json "s3://$SITE_BUCKET/config.json" --region "$REGION"

if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
  aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null
  echo "CloudFront invalidation submitted for $DIST_ID"
fi

echo "Insights frontend config deployed. Site: $SITE_URL"
