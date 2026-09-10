#!/bin/bash
set -e

echo "🚀 TRAIDER Bot Startup"
echo "======================================"

# Check if IBKR Client Portal Gateway should be started
if [ "${START_GATEWAY:-false}" == "true" ]; then
    echo "📡 Starting IBKR Client Portal Gateway..."
    # Gateway startup would go here
    # /opt/ibkr/gateway.sh &
    sleep 5
else
    echo "⏭️  Skipping IBKR Gateway (set START_GATEWAY=true to enable)"
fi

# Verify required environment variables
if [ -z "$IBKR_API_URL" ]; then
    echo "⚠️  Warning: IBKR_API_URL not set. Will use default."
fi

# Run the bot
echo "🤖 Starting TRAIDER Bot..."
exec python -u /app/main.py
