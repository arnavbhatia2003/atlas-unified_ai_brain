#!/usr/bin/env bash
set -euo pipefail
test "${CODESPACES:-false}" = "true" || { echo 'Use the student Codespace; this setup is not for the laptop.'; exit 1; }
cd "$(dirname "$0")/.."
python -m venv backend/.venv-docling
parser_python=backend/.venv-docling/bin/python
"$parser_python" -m pip install --only-binary=:all: --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 torchvision==0.28.0
"$parser_python" -m pip install --only-binary=:all: --index-url https://pypi.org/simple -r backend/requirements-docling.txt
"$parser_python" -m pip check
echo 'Parser installed. No model, document or API server has been started. Run one bounded docling_job.py job.'
