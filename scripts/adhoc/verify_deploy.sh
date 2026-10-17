#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/adhoc/verify_deploy.sh — assert that the running build is the commit
# you think you deployed
#
# WHEN THIS WAS USED
#   2026-10-17, after all five `cdk deploy` runs. It is the check that caught
#   INC-0001: `cdk deploy` + `UPDATE_COMPLETE` + a green ALB while the API was
#   still serving the *previous* commit (the instance had been restarted in
#   place, so user data never re-ran). It then confirmed the fix.
#
# WHAT IT PROVES
#   `git_sha` is baked into the instance user data and surfaced by GET /health,
#   so comparing /health.version to `git rev-parse --short HEAD` distinguishes
#   "the stack matches the template" from "the code is actually running".
#   A deploy is not finished until this exits 0.
#
# HOW TO RUN
#   ./verify_deploy.sh                 # BASE_URL auto-detected from the stack
#   BASE_URL=http://... ./verify_deploy.sh
#   EXPECT=5d3733a ./verify_deploy.sh  # assert a specific sha (e.g. in CI/staging)
#
# NOTE ON IT FAILING RIGHT NOW
#   If commits landed after the last deploy, this exits 1 on purpose — that is
#   the drift it exists to detect, not a bug in the script.
# ---------------------------------------------------------------------------
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-IncidentTriage-level1}"
BASE_URL="${BASE_URL:-}"

if [ -z "$BASE_URL" ]; then
  BASE_URL="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" --output text)"
fi
BASE_URL="${BASE_URL%/}"

# `python` is not always on PATH (Git Bash on Windows ships `python3` or neither)
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

EXPECT="${EXPECT:-$(git rev-parse --short HEAD)}"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "note: working tree is dirty - HEAD is not a complete description of the source"
fi

health="$(curl -fsS "$BASE_URL/health")"
actual="$(printf '%s' "$health" | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin).get('version',''))")"

echo "stack    : $STACK ($REGION)"
echo "url      : $BASE_URL"
echo "expected : $EXPECT   (HEAD)"
echo "running  : $actual   (/health version)"

if [ "$actual" != "$EXPECT" ]; then
  echo "FAIL: the deployed build is not this commit." >&2
  echo "      Either the deploy did not ship (see INC-0001) or commits landed after it." >&2
  exit 1
fi
echo "OK: the running build matches $EXPECT"
