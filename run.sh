#!/usr/bin/env bash
# Start / restart the WeatherGPT dev server.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="${ROOT}/.server.pid"
PORT="${PORT:-8000}"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")" && sleep 1
fi

cd "${ROOT}/backend"
setsid nohup python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  > /tmp/weathergpt.log 2>&1 < /dev/null &
echo $! > "$PIDFILE"
sleep 5
curl -s -o /dev/null -w "server on :%{http_code}\n" "http://localhost:${PORT}/api/health"
