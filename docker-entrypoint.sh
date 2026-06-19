#!/bin/sh
set -eu

CONFIG_PATH="${CONFIG_PATH:-/config/config.json}"
REPORT_PATH="${REPORT_PATH:-/config/last-run-report.json}"
DEFAULT_CONFIG_PATH="/app/default-config.json"

mkdir -p "$(dirname "$CONFIG_PATH")"
mkdir -p "$(dirname "$REPORT_PATH")"

if [ ! -f "$CONFIG_PATH" ]; then
  cp "$DEFAULT_CONFIG_PATH" "$CONFIG_PATH"
fi

if [ ! -f "$REPORT_PATH" ]; then
  printf '{\n  "stats": {},\n  "artists": {}\n}\n' > "$REPORT_PATH"
fi

exec python3 /app/webapp.py
