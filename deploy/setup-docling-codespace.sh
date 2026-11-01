#!/usr/bin/env bash
set -euo pipefail
test "${CODESPACES:-false}" = "true" || { echo 'Use the student Codespace; this setup is not for the laptop.'; exit 1; }
cd "$(dirname "$0")/.."
# Match the OpenCV runtime libraries in the backend Dockerfile.
# Scope this install to Debian; unrelated image repositories may have expired keys.
test -r /etc/apt/sources.list.d/debian.sources || { echo 'Expected the Bookworm image Debian sources file; stop and inspect the image.'; exit 1; }
debian_sources=(-o Dir::Etc::sourcelist=/etc/apt/sources.list.d/debian.sources -o Dir::Etc::sourceparts=-)
sudo apt-get "${debian_sources[@]}" -o APT::Update::Error-Mode=any -o APT::Get::List-Cleanup=0 update
sudo env DEBIAN_FRONTEND=noninteractive apt-get "${debian_sources[@]}" install -y --no-install-recommends libglib2.0-0 libgl1
python -m venv backend/.venv-docling
parser_python=backend/.venv-docling/bin/python
"$parser_python" -m pip install --only-binary=:all: --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 torchvision==0.28.0
"$parser_python" -m pip install --only-binary=:all: --index-url https://pypi.org/simple -r backend/requirements-docling.txt
"$parser_python" -m pip check
"$parser_python" -c 'import cv2; from rapidocr import EngineType, RapidOCR; import onnxruntime; print("OpenCV, RapidOCR and ONNX Runtime imports passed; no models loaded.")'
echo 'Parser installed. No model, document or API server has been started. Run one bounded docling_job.py job.'
