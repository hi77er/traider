#!/usr/bin/env bash
#
# THE TRADING LOOP — one of the two processes. This one owns the bar clock and
# places every order. The other one is ./run-dashboard.sh (HTTP only).
#
# They share files and nothing else, so this must run as its own process: see
# README, "Two processes".
#
set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "run-bot: no interpreter at $PY — create it with: python3.9 -m venv .venv" >&2
  exit 1
fi

# Only a single loop may run: two loops mean double orders on the same signal.
exec "$PY" -m src.main "$@"
