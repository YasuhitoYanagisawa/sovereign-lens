#!/usr/bin/env bash
# Build the demo video in Google Cloud Shell:  ./demo/run.sh <PROJECT_ID>
set -euo pipefail
PROJECT_ID="${1:?Usage: ./demo/run.sh <PROJECT_ID>}"
cd "$(dirname "$0")/.."
gcloud services enable texttospeech.googleapis.com --project "${PROJECT_ID}" >/dev/null
python3 -m pip install --user -q playwright imageio-ffmpeg
python3 -m playwright install chromium >/dev/null
python3 -m playwright install-deps chromium >/dev/null 2>&1 || sudo -n python3 -m playwright install-deps chromium >/dev/null 2>&1 || true
python3 demo/make_demo.py "${PROJECT_ID}"
