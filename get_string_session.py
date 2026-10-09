#!/usr/bin/env python3
"""
Run this ONCE locally to convert your .session file to a string.
Copy the output and paste it into Railway as the TG_STRING_SESSION env variable.
"""
import configparser, asyncio
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).parent
cfg = configparser.ConfigParser()
cfg.read(BASE_DIR / "config.ini")

API_ID   = int(cfg["telegram"]["api_id"])
API_HASH = cfg["telegram"]["api_hash"]
SESSION  = cfg["telegram"].get("session", "tg_scraper_session")

async def main():
    print("🔑 Connecting to Telegram to extract your session...\n")
    client = TelegramClient(str(BASE_DIR / SESSION), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("❌ Not logged in! Run ./run.sh first and log in, then run this script again.")
        await client.disconnect()
        return

    me = await client.get_me()
    string = StringSession.save(client.session)
    await client.disconnect()

    print(f"✅ Logged in as: {me.first_name} (@{me.username})\n")
    print("=" * 60)
    print("YOUR STRING SESSION (copy everything between the lines):")
    print("=" * 60)
    print(string)
    print("=" * 60)
    print("\n📋 Paste this as TG_STRING_SESSION in Railway environment variables.")

asyncio.run(main())
