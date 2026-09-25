"""
Shared access to the personal Telegram account (Telethon user session).

Every component that talks to Telegram as the user goes through ``TELEGRAM_LOCK``:
two clients on one auth key at the same time can corrupt the SQLite session file
or get the key revoked by Telegram (AUTH_KEY_DUPLICATED).

The session is the file sessions/user.session, or the TELEGRAM_SESSION environment
variable (a Telethon string session) on hosts without a persistent disk, e.g. Railway.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.sessions import StringSession

from src.config_loader import Config

SESSION_NAME = os.path.join("sessions", "user")
SESSION_FILE = SESSION_NAME + ".session"
SESSION_ENV = "TELEGRAM_SESSION"

# ponytail: one process-wide lock; per-user locks come with multi-account support.
TELEGRAM_LOCK = asyncio.Lock()


class TelegramSessionError(RuntimeError):
    """The user session is missing or no longer authorized."""


def _session_string() -> str:
    return os.getenv(SESSION_ENV, "").strip()


def check_session_file() -> None:
    """Fail fast when the one-time login has not been done yet.

    Raises:
        TelegramSessionError: If neither TELEGRAM_SESSION nor the session file exists
    """
    if not _session_string() and not os.path.exists(SESSION_FILE):
        raise TelegramSessionError(
            f"Telegram user session not found at '{SESSION_FILE}' and {SESSION_ENV} is not set. "
            "Create it once with: python create_session.py"
        )


def new_client(config: Config) -> TelegramClient:
    """A not yet connected client on the account's session (string session wins over the file)."""
    value = _session_string()
    session = StringSession(value) if value else SESSION_NAME
    return TelegramClient(session, config.telegram_api_id, config.telegram_api_hash)


@asynccontextmanager
async def user_client(config: Config) -> AsyncIterator[TelegramClient]:
    """Connect to Telegram as the user for the duration of the block.

    Holds TELEGRAM_LOCK the whole time, so keep the block to Telegram I/O only.

    Raises:
        TelegramSessionError: If the session is missing or not authorized
    """
    check_session_file()
    async with TELEGRAM_LOCK:
        client = new_client(config)
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
