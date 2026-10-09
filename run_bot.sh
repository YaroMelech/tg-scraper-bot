#!/usr/bin/env bash
# Run the TG Scraper Telegram Bot
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

if [ ! -f "$VENV/bin/python3" ]; then
    echo "🔧 Setting up virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q telethon rich click python-telegram-bot
    echo "✅ Done."
fi

echo "🤖 Starting TG Scraper Bot..."
"$VENV/bin/python3" "$SCRIPT_DIR/bot.py"
