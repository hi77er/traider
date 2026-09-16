#!/usr/bin/env bash
#
# THE DASHBOARD — one of the two processes. This one serves HTTP: the UI, the
# configuration, the backtests and the reports. It never places an order; the
# other one (./run-bot.sh) does that, in its own process.
#
# See README, "Two processes" for why they are separate.
#
set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "run-dashboard: no interpreter at $PY — create it with: python3.9 -m venv .venv" >&2
  exit 1
fi

# No --reload on purpose: this process gets restarted constantly in development,
# and the loop must not be affected by that. Restart it by hand after editing.
exec "$PY" -m src.web.app "$@"
