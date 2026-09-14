#!/bin/bash
set -e

echo "🚀 TRAIDER Bot Startup"
echo "======================================"

# No sidecar gateway: market data comes from OpenBB and orders go to Alpaca over
# HTTPS with an API key pair. All this can check is that credentials exist — the
# hard gate lives in src/execution/config.py, which refuses to trade without them.
if [ -n "${ALPACA_PAPER_API_KEY:-}" ] || [ -n "${ALPACA_LIVE_API_KEY:-}" ]; then
    echo "🔑 Alpaca credentials present"
else
    echo "ℹ️  No Alpaca credentials set — orders will be refused until they are added"
fi

# Run the bot
echo "🤖 Starting TRAIDER Bot..."
exec python -u /app/main.py
