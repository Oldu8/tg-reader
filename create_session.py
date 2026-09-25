#!/usr/bin/env python3
"""
Create Telegram session for Docker deployment.

This script creates a Telegram session file that can be used in Docker
without requiring interactive authentication.

Run this ONCE locally before deploying to Docker:
    python create_session.py

The script will:
1. Prompt for your phone number
2. Send you a verification code
3. Create sessions/user.session file
4. This file can then be used in Docker without interactive prompts

For hosts without a persistent disk (e.g. Railway):
    python create_session.py --string

logs in as a separate device ("tg_brief server") and writes a string session to
sessions/server-session.txt (never printed). Put its content into the TELEGRAM_SESSION
variable of the host, then delete the file. The server session can be terminated
on its own in Telegram → Settings → Devices.
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

STRING_FILE = Path("sessions") / "server-session.txt"


async def create_session(as_string: bool = False):
    """Create Telegram session interactively."""
    print("=" * 70)
    print("Telegram Session Creator for Docker Deployment")
    print("=" * 70)
    print()

    # Load environment variables
    load_dotenv()
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        print("❌ ERROR: Missing Telegram credentials")
        print()
        print("Please ensure your .env file contains:")
        print("  TELEGRAM_API_ID=your_api_id")
        print("  TELEGRAM_API_HASH=your_api_hash")
        print()
        print("Get these from: https://my.telegram.org")
        sys.exit(1)

    # Create sessions directory if it doesn't exist
    sessions_dir = Path("sessions")
    sessions_dir.mkdir(exist_ok=True)

    print(f"API ID: {api_id}")
    print(f"Session file: {STRING_FILE if as_string else 'sessions/user.session'}")
    print()

    # Create client
    if as_string:
        client = TelegramClient(
            StringSession(), int(api_id), api_hash, device_model="tg_brief server"
        )
    else:
        client = TelegramClient("sessions/user", int(api_id), api_hash)

    try:
        print("Connecting to Telegram...")
        print()
        print("You will be prompted for:")
        print("  1. Your phone number (international format: +1234567890)")
        print("  2. Verification code (sent to your Telegram app)")
        print("  3. 2FA password (if enabled)")
        print()
        print("-" * 70)

        # Start client - this will prompt for authentication interactively
        await client.start()

        if as_string:
            STRING_FILE.write_text(client.session.save(), encoding="utf-8")
            STRING_FILE.chmod(0o600)
            print("-" * 70)
            print()
            print(f"✅ SUCCESS! String session saved to {STRING_FILE} (not printed)")
            print("Put it into the TELEGRAM_SESSION variable of the server, then delete the file.")
            print("=" * 70)
            return

        print("-" * 70)
        print()
        print("✅ SUCCESS! Session created successfully")
        print()
        print(f"Session file: {sessions_dir / 'user.session'}")
        print()
        print("Next steps:")
        print("  1. Keep this session file safe (it's your authentication)")
        print("  2. Add 'sessions/' to .gitignore (already done)")
        print("  3. Run docker-compose up to use the session in Docker")
        print()
        print("The session file will be mounted into Docker automatically.")
        print("=" * 70)

        # Test the connection
        me = await client.get_me()
        print()
        print(f"Authenticated as: {me.first_name}")
        if me.username:
            print(f"Username: @{me.username}")
        print(f"Phone: {me.phone}")
        print()

    except KeyboardInterrupt:
        print("\n\n⚠️  Session creation cancelled")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Error creating session: {e}")
        sys.exit(1)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    print()
    asyncio.run(create_session(as_string="--string" in sys.argv[1:]))
