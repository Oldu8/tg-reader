"""Tests for reading arbitrary chats of the account."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl import types

from src import chat_reader
from src.chat_reader import (
    KIND_BOT,
    KIND_CHANNEL,
    KIND_GROUP,
    KIND_SELF,
    KIND_SUPERGROUP,
    KIND_USER,
    MODE_LAST,
    MODE_UNREAD,
    ChatReader,
    DialogInfo,
    FetchResult,
    dialog_from_telethon,
    dialog_kind,
    media_label,
)

T0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
ME = 777


def user(uid=1, **kw):
    return types.User(id=uid, **kw)


def channel(cid=42, megagroup=False, username=None):
    return types.Channel(
        id=cid,
        title="Chan",
        photo=types.ChatPhotoEmpty(),
        date=T0,
        megagroup=megagroup,
        username=username,
    )


def basic_group():
    return types.Chat(
        id=5,
        title="Old group",
        photo=types.ChatPhotoEmpty(),
        participants_count=3,
        date=T0,
        version=1,
    )


def raw_message(mid, text="hi", **kw):
    base = dict(
        id=mid,
        message=text,
        date=T0 + timedelta(minutes=mid),
        media=None,
        out=False,
        mentioned=False,
        action=None,
        reply_to=None,
        sender_id=1,
        sender=user(1, first_name="Bob", last_name="Lee"),
        post_author=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    def __init__(self, messages=(), dialogs=(), fresh=(0, 0), fresh_error=None):
        self._messages = list(messages)
        self._dialogs = list(dialogs)
        self._fresh = fresh
        self._fresh_error = fresh_error
        self.iter_calls = []
        self.acks = []

    async def get_me(self):
        return SimpleNamespace(id=ME)

    async def get_input_entity(self, entity):
        return types.InputPeerChannel(channel_id=42, access_hash=1)

    async def __call__(self, request):
        if self._fresh_error:
            raise self._fresh_error
        read_max, unread = self._fresh
        return SimpleNamespace(
            dialogs=[SimpleNamespace(read_inbox_max_id=read_max, unread_count=unread)]
        )

    async def iter_messages(self, entity, limit=None, min_id=0, reverse=False):
        self.iter_calls.append({"limit": limit, "min_id": min_id, "reverse": reverse})
        picked = sorted(
            (m for m in self._messages if m.id > min_id), key=lambda m: m.id if reverse else -m.id
        )
        for m in picked[:limit]:
            yield m

    async def send_read_acknowledge(self, entity, max_id=None):
        self.acks.append(max_id)
        return True

    async def iter_dialogs(self):
        for d in self._dialogs:
            yield d


@pytest.fixture
def patch_client(monkeypatch):
    def install(client):
        @asynccontextmanager
        async def fake_user_client(config):
            yield client

        monkeypatch.setattr(chat_reader, "user_client", fake_user_client)
        return client

    return install


@pytest.fixture
def reader(sample_config):
    return ChatReader(sample_config, MagicMock())


@pytest.fixture
def supergroup():
    return DialogInfo(
        id=-10042,
        title="Dev",
        kind=KIND_SUPERGROUP,
        unread_count=3,
        read_inbox_max_id=2,
        peer_id=42,
        entity=object(),
    )


# --------------------------------------------------------------------------- kinds & links


@pytest.mark.unit
@pytest.mark.parametrize(
    "entity, kind",
    [
        (user(is_self=True), KIND_SELF),
        (user(bot=True), KIND_BOT),
        (user(), KIND_USER),
        (channel(), KIND_CHANNEL),
        (channel(megagroup=True), KIND_SUPERGROUP),
        (basic_group(), KIND_GROUP),
    ],
)
def test_dialog_kind(entity, kind):
    assert dialog_kind(entity) == kind


@pytest.mark.unit
def test_message_links_by_kind():
    public = DialogInfo(id=-1, title="p", kind=KIND_CHANNEL, username="news", peer_id=9)
    private = DialogInfo(id=-1, title="p", kind=KIND_SUPERGROUP, peer_id=9)
    person = DialogInfo(id=1, title="p", kind=KIND_USER, username="bob", peer_id=1)
    group = DialogInfo(id=-5, title="g", kind=KIND_GROUP, peer_id=5)
    assert public.message_link(3) == "https://t.me/news/3"
    assert private.message_link(3) == "https://t.me/c/9/3"
    assert person.message_link(3) == ""
    assert group.message_link(3) == ""
    assert private.message_link(0) == ""
    assert not person.linkable and private.linkable


@pytest.mark.unit
def test_dialog_from_telethon():
    entity = channel(megagroup=True, username="devchat")
    raw = SimpleNamespace(
        entity=entity,
        id=-10042,
        name="Dev chat",
        unread_count=None,
        archived=True,
        dialog=SimpleNamespace(read_inbox_max_id=17),
        input_entity="peer",
    )
    info = dialog_from_telethon(raw)
    assert info == DialogInfo(
        id=-10042,
        title="Dev chat",
        kind=KIND_SUPERGROUP,
        unread_count=0,
        username="devchat",
        archived=True,
        read_inbox_max_id=17,
        peer_id=42,
    )
    assert info.entity == "peer"


@pytest.mark.unit
def test_saved_messages_title():
    raw = SimpleNamespace(
        entity=user(is_self=True),
        id=ME,
        name="Me Myself",
        unread_count=0,
        archived=False,
        dialog=SimpleNamespace(read_inbox_max_id=0),
        input_entity=None,
    )
    assert dialog_from_telethon(raw).title == "Избранное"


@pytest.mark.unit
def test_media_labels():
    assert media_label(SimpleNamespace(media=None)) == ""
    assert media_label(SimpleNamespace(media=1, photo=1)) == "Фото"
    assert media_label(SimpleNamespace(media=1, voice=1, document=1)) == "Голосовое"
    assert media_label(SimpleNamespace(media=1, web_preview=1)) == ""
    assert media_label(SimpleNamespace(media=1)) == "Медиа"


@pytest.mark.unit
def test_fetch_result_capped():
    assert FetchResult([], MODE_UNREAD, 1000, 1500).capped
    assert not FetchResult([], MODE_UNREAD, 1000, 900).capped
    assert not FetchResult([], MODE_LAST, 100, 5000).capped


# --------------------------------------------------------------------------- fetching


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_dialogs(reader, patch_client):
    raw = SimpleNamespace(
        entity=user(first_name="Bob"),
        id=1,
        name="Bob",
        unread_count=2,
        archived=False,
        dialog=SimpleNamespace(read_inbox_max_id=0),
        input_entity=None,
    )
    patch_client(FakeClient(dialogs=[raw]))
    dialogs = await reader.list_dialogs()
    assert [(d.title, d.kind, d.unread_count) for d in dialogs] == [("Bob", KIND_USER, 2)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_last_n_chronological_with_conversion(reader, patch_client, supergroup):
    messages = [
        raw_message(1, "first"),
        raw_message(2, "", media=1, photo=1),  # media only
        raw_message(3, "", media=None),  # empty: skipped
        raw_message(4, "joined", action=object()),  # service: skipped
        raw_message(5, "mine", out=True, sender_id=ME),
        raw_message(6, "ping", mentioned=True, reply_to=SimpleNamespace(reply_to_msg_id=5)),
    ]
    client = patch_client(FakeClient(messages))
    progress = AsyncMock()

    result = await reader.fetch(supergroup, MODE_LAST, 10, progress=progress)

    assert [m.message_id for m in result.messages] == [1, 2, 5, 6]
    first, media, mine, ping = result.messages
    assert first.sender == "Bob Lee" and first.link == "https://t.me/c/42/1"
    assert media.text == "[Фото]" and media.has_media
    assert mine.sender == "ME" and mine.is_own
    assert ping.mentions_me and ping.reply_to_id == 5
    assert client.iter_calls == [{"limit": 10, "min_id": 0, "reverse": False}]
    assert result.last_id == 6
    progress.assert_not_awaited()  # fewer than PROGRESS_EVERY messages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_unread_uses_fresh_state_and_skips_own(reader, patch_client, supergroup):
    messages = [raw_message(i) for i in range(1, 8)] + [raw_message(8, "me", out=True)]
    client = patch_client(FakeClient(messages, fresh=(4, 3)))

    result = await reader.fetch(supergroup, MODE_UNREAD, 1000)

    assert [m.message_id for m in result.messages] == [5, 6, 7]
    assert result.unread_total == 3 and not result.capped
    assert result.last_id == 8  # own message is skipped but still covered by "read up to"
    assert client.iter_calls == [{"limit": 1000, "min_id": 4, "reverse": True}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_unread_nothing_unread(reader, patch_client, supergroup):
    client = patch_client(FakeClient([raw_message(1)], fresh=(1, 0)))
    result = await reader.fetch(supergroup, MODE_UNREAD, 100)
    assert result.messages == [] and client.iter_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_unread_falls_back_to_cached_state(reader, patch_client, supergroup):
    messages = [raw_message(i) for i in range(1, 5)]
    client = patch_client(FakeClient(messages, fresh_error=RuntimeError("boom")))
    result = await reader.fetch(supergroup, MODE_UNREAD, 100)
    assert client.iter_calls == [{"limit": 100, "min_id": 2, "reverse": True}]
    assert [m.message_id for m in result.messages] == [3, 4]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_unread_takes_oldest_batch(reader, patch_client, supergroup):
    messages = [raw_message(i) for i in range(1, 251)]
    patch_client(FakeClient(messages, fresh=(0, 250)))
    progress = AsyncMock()
    result = await reader.fetch(supergroup, MODE_UNREAD, 200, progress=progress)
    assert len(result.messages) == 200 and result.capped
    assert [result.messages[0].message_id, result.messages[-1].message_id] == [1, 200]
    assert result.last_id == 200  # the next batch starts at 201
    assert [c.args for c in progress.await_args_list] == [(100, 200), (200, 200)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_read_updates_dialog(reader, patch_client, supergroup):
    client = patch_client(FakeClient(fresh=(200, 50)))
    assert await reader.mark_read(supergroup, 200) == 50
    assert client.acks == [200]
    assert (supergroup.read_inbox_max_id, supergroup.unread_count) == (200, 50)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_read_without_fresh_count(reader, patch_client, supergroup):
    client = patch_client(FakeClient(fresh_error=RuntimeError("boom")))
    assert await reader.mark_read(supergroup, 9) is None
    assert client.acks == [9] and supergroup.read_inbox_max_id == 9


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_read_never_marks_whole_chat(reader, patch_client, supergroup):
    client = patch_client(FakeClient())
    with pytest.raises(ValueError):
        await reader.mark_read(supergroup, 0)  # Telegram reads max_id=0 as "everything"
    assert client.acks == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_channel_posts_use_post_author_and_cache_senders(reader, patch_client):
    news = DialogInfo(id=-1009, title="News", kind=KIND_CHANNEL, username="news", peer_id=9)
    messages = [raw_message(1, post_author="Editor"), raw_message(2)]
    patch_client(FakeClient(messages))
    result = await reader.fetch(news, MODE_LAST, 10)
    assert [m.sender for m in result.messages] == ["Editor", ""]
    assert result.messages[0].link == "https://t.me/news/1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sender_name_fallbacks(reader, patch_client, supergroup):
    # Telegram yields newest first: message 5 is converted before message 1
    messages = [
        raw_message(1, sender_id=2, sender=None),  # name cached from message 4
        raw_message(2, sender_id=3, sender=SimpleNamespace(username="nick")),
        raw_message(3, sender_id=4, sender=None),
        raw_message(4, sender_id=2, sender=SimpleNamespace(title="Some Channel")),
        raw_message(5, sender_id=3, sender=None),  # unknown yet: must not poison the cache
    ]
    patch_client(FakeClient(messages))
    result = await reader.fetch(supergroup, MODE_LAST, 10)
    assert [m.sender for m in result.messages] == [
        "Some Channel",
        "@nick",
        "Unknown",
        "Some Channel",
        "Unknown",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_validates_arguments(reader, supergroup):
    with pytest.raises(ValueError):
        await reader.fetch(supergroup, "everything", 10)
    with pytest.raises(ValueError):
        await reader.fetch(supergroup, MODE_LAST, 0)
