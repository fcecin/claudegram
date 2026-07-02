#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
mkdir -p models
BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
echo "Fetching Kokoro model into ./models (~336MB) ..."
[ -f models/kokoro-v1.0.onnx ] || curl -L --fail --retry 3 -o models/kokoro-v1.0.onnx "$BASE/kokoro-v1.0.onnx"
[ -f models/voices-v1.0.bin ]  || curl -L --fail --retry 3 -o models/voices-v1.0.bin  "$BASE/voices-v1.0.bin"
echo "Done: $(du -h models/kokoro-v1.0.onnx models/voices-v1.0.bin | tr '\n' ' ')"
