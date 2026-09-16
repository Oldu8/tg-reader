"""
Shared access to the personal Telegram account (Telethon user session).

Every component that talks to Telegram as the user goes through ``TELEGRAM_LOCK``:
two clients on one auth key at the same time can corrupt the SQLite session file
or get the key revoked by Telegram (AUTH_KEY_DUPLICATED).
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from telethon import TelegramClient

from src.config_loader import Config

SESSION_NAME = os.path.join("sessions", "user")
SESSION_FILE = SESSION_NAME + ".session"

# ponytail: one process-wide lock; per-user locks come with multi-account support.
TELEGRAM_LOCK = asyncio.Lock()


class TelegramSessionError(RuntimeError):
    """The user session is missing or no longer authorized."""


def check_session_file() -> None:
    """Fail fast when the one-time login has not been done yet.

    Raises:
        TelegramSessionError: If the session file does not exist
    """
    if not os.path.exists(SESSION_FILE):
        raise TelegramSessionError(
            f"Telegram user session not found at '{SESSION_FILE}'. "
            "Create it once with: python create_session.py"
        )


@asynccontextmanager
async def user_client(config: Config) -> AsyncIterator[TelegramClient]:
    """Connect to Telegram as the user for the duration of the block.

    Holds TELEGRAM_LOCK the whole time, so keep the block to Telegram I/O only.

    Raises:
        TelegramSessionError: If the session is missing or not authorized
    """
    check_session_file()
    async with TELEGRAM_LOCK:
        client = TelegramClient(SESSION_NAME, config.telegram_api_id, config.telegram_api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramSessionError(
                    "Telegram user session is not authorized anymore. "
                    "Log in again with: python create_session.py"
                )
            yield client
        finally:
            await client.disconnect()
