#!/usr/bin/env bash
# Builds the AgentCore Runtime direct-code-deploy zip (ARM64 manylinux
# wheels) and uploads it to the AgentCodeBucket from PolicyPortal-Data's
# CFN outputs. Must run AFTER PolicyPortal-Data is deployed and BEFORE
# PolicyPortal-AgentRuntime is deployed (app.py checks for the object's
# existence to decide whether to synth that stack at all).
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="us-east-2"
BUCKET=$(aws cloudformation describe-stacks --region "$REGION" \
  --stack-name PolicyPortal-Data \
  --query "Stacks[0].Outputs[?OutputKey=='AgentCodeBucketName'].OutputValue" \
  --output text)

if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
  echo "Could not resolve AgentCodeBucketName from PolicyPortal-Data outputs. Deploy that stack first." >&2
  exit 1
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

echo "Installing ARM64 manylinux wheels into $WORKDIR ..."
pip install \
  --platform manylinux2014_aarch64 \
  --python-version 3.12 \
  --abi cp312 \
  --only-binary=:all: \
  --target "$WORKDIR" \
  -r agents/policy_agent/requirements.txt

cp agents/policy_agent/agent.py "$WORKDIR/agent.py"

ZIP_PATH="$WORKDIR.zip"
python3 - "$WORKDIR" "$ZIP_PATH" <<'PYEOF'
import sys, zipfile, os
src, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, _, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src)
            zf.write(full, rel)
PYEOF

echo "Uploading to s3://$BUCKET/agent-code/policy-agent.zip ..."
aws s3 cp "$ZIP_PATH" "s3://$BUCKET/agent-code/policy-agent.zip" --region "$REGION"
rm -f "$ZIP_PATH"

echo "Done. Now run: cdk deploy --all --require-approval never"
