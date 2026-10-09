#!/usr/bin/env bash
# Quick launcher for TG Scraper
# Usage: ./run.sh [options]
# Example: ./run.sh --usernames-only --no-bots

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

# Check if venv exists
if [ ! -f "$VENV/bin/python3" ]; then
    echo "🔧 Setting up virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q telethon rich click
    echo "✅ Dependencies installed."
fi

echo "🚀 Starting TG Scraper..."
"$VENV/bin/python3" "$SCRIPT_DIR/tg_scraper.py" "$@"
