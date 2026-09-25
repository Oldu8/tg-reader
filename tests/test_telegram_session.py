"""Tests for the shared Telegram user session."""

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from src import telegram_session
from src.telegram_session import TelegramSessionError, check_session_file, new_client


@pytest.fixture
def no_session_file(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_session, "SESSION_NAME", str(tmp_path / "user"))
    monkeypatch.setattr(telegram_session, "SESSION_FILE", str(tmp_path / "user.session"))
    monkeypatch.delenv("TELEGRAM_SESSION", raising=False)


@pytest.mark.unit
def test_missing_session_fails_fast(no_session_file):
    with pytest.raises(TelegramSessionError, match="TELEGRAM_SESSION"):
        check_session_file()


def string_session() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(b"\x01" * 256)
    return session.save()


@pytest.mark.unit
def test_string_session_from_env(no_session_file, monkeypatch, sample_config):
    monkeypatch.setenv("TELEGRAM_SESSION", string_session())
    check_session_file()  # no file needed
    client = new_client(sample_config)
    assert isinstance(client.session, StringSession)
    assert client.session.dc_id == 2


@pytest.mark.unit
def test_file_session_without_env(no_session_file, sample_config):
    client = new_client(sample_config)
    assert isinstance(client.session, SQLiteSession)
    client.session.close()
