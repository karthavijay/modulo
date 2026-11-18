#!/bin/sh
# Builds the sample packages, serves them, and plays them in a browser with a mock LMS.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-$(mktemp -d)}
python3 "$HERE/make_packages.py" "$OUT"
cp "$HERE/harness.html" "$OUT/"
python3 -m http.server 8765 --directory "$OUT" >/dev/null 2>&1 &
PID=$!
trap 'kill $PID' EXIT
sleep 1
node "$HERE/scorm_runtime_test.js" http://localhost:8765 "$OUT"
