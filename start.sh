#!/bin/sh
# Tennis IQ: sets itself up on first run, then starts the site at http://localhost:8000
cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8000}"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Tennis IQ needs Python 3.11 or newer. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

if [ ! -x .venv/bin/uvicorn ]; then
  echo "First run: installing (takes a minute or two)..."
  "$PY" -m venv .venv && .venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -r backend/requirements.txt || {
    echo "Install failed. Check your internet connection and try again."; rm -rf .venv; exit 1; }
fi

echo "Starting Tennis IQ at http://localhost:$PORT"
echo "The first start downloads ATP data (about 2-3 minutes); the site works while that finishes."
echo "Leave this window open. Press Ctrl+C to stop."
( sleep 4; if command -v open >/dev/null 2>&1; then open "http://localhost:$PORT"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "http://localhost:$PORT"; fi ) >/dev/null 2>&1 &
exec .venv/bin/uvicorn app:app --app-dir backend --host 127.0.0.1 --port "$PORT"
