"""
Reads any chat of the personal Telegram account on demand.

Unlike MessageCollector, which walks the channels listed in config.yaml over a
time window, this module lists every dialog of the account and fetches either the
last N messages (newest ones) or the next N unread ones (oldest unread first) of the
dialog the user picked in the bot. Forum supergroups are read per topic (thread).
Fetching never marks anything as read; the bot calls mark_read explicitly once the
summary of an unread batch is delivered.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.tl import types
from telethon.tl.functions.messages import (
    GetForumTopicsByIDRequest,
    GetForumTopicsRequest,
    GetPeerDialogsRequest,
    ReadDiscussionRequest,
)

from src.collector import Message
from src.config_loader import Config
from src.telegram_session import user_client

KIND_CHANNEL = "channel"
KIND_SUPERGROUP = "supergroup"
KIND_GROUP = "group"
KIND_USER = "user"
KIND_BOT = "bot"
KIND_SELF = "self"

MODE_UNREAD = "unread"
MODE_LAST = "last"

PROGRESS_EVERY = 100  # messages between progress callbacks
TOPICS_LIMIT = 100  # ponytail: one page of forum topics; paginate if a forum ever has more
GENERAL_TOPIC_ID = 1

ProgressCallback = Callable[[int, int], Awaitable[None]]


@dataclass
class DialogInfo:
    """One chat of the account, as shown in the bot's chat list."""

    id: int  # Telethon "marked" peer id: -100… for channels, negative for groups
    title: str
    kind: str
    unread_count: int = 0
    username: Optional[str] = None
    archived: bool = False
    read_inbox_max_id: int = 0
    peer_id: int = 0  # bare id, used in t.me/c/<peer_id>/<msg_id> links
    forum: bool = False  # supergroup split into topics
    entity: Any = field(default=None, repr=False, compare=False)  # Telethon input peer

    @property
    def linkable(self) -> bool:
        """Whether Telegram has t.me links for this chat's messages."""
        return self.kind in (KIND_CHANNEL, KIND_SUPERGROUP)

    def message_link(self, message_id: int) -> str:
        """Link to one message, or "" when this kind of chat has no message links."""
        if not self.linkable or message_id <= 0:
            return ""
        if self.username:
            return f"https://t.me/{self.username}/{message_id}"
        return f"https://t.me/c/{self.peer_id}/{message_id}"


@dataclass
class TopicInfo:
    """One topic (thread) of a forum supergroup."""

    id: int  # id of the message that started the topic; GENERAL_TOPIC_ID is "General"
    title: str
    unread_count: int = 0
    read_inbox_max_id: int = 0
    closed: bool = False


@dataclass
class FetchResult:
    """Messages of one chat plus what the user should know about the selection."""

    messages: list[Message]  # chronological order
    mode: str
    limit: int
    unread_total: int = 0  # unread count reported by Telegram before the fetch (unread mode)
    last_id: int = 0  # newest message id scanned, service and skipped messages included

    @property
    def capped(self) -> bool:
        """More unread messages exist than this batch took."""
        return self.mode == MODE_UNREAD and self.unread_total > self.limit


def dialog_kind(entity: Any) -> str:
    """Classify a Telethon entity into one of the KIND_* values."""
    if isinstance(entity, types.User):
        if entity.is_self:
            return KIND_SELF
        return KIND_BOT if entity.bot else KIND_USER
    if isinstance(entity, types.Channel):
        return KIND_SUPERGROUP if entity.megagroup else KIND_CHANNEL
    return KIND_GROUP


def dialog_from_telethon(dialog: Any) -> DialogInfo:
    """Convert a telethon.tl.custom.Dialog into a DialogInfo."""
    entity = dialog.entity
    kind = dialog_kind(entity)
    title = "Избранное" if kind == KIND_SELF else (dialog.name or "Без названия")
    return DialogInfo(
        id=dialog.id,
        title=title,
        kind=kind,
        unread_count=dialog.unread_count or 0,
        username=getattr(entity, "username", None),
        archived=bool(dialog.archived),
        read_inbox_max_id=getattr(dialog.dialog, "read_inbox_max_id", 0) or 0,
        peer_id=getattr(entity, "id", 0),
        forum=bool(getattr(entity, "forum", False)),
        entity=dialog.input_entity,
    )


def topic_from_telethon(topic: Any) -> TopicInfo:
    """Convert a types.ForumTopic into a TopicInfo."""
    return TopicInfo(
        id=topic.id,
        title=topic.title or "Без названия",
        unread_count=topic.unread_count or 0,
        read_inbox_max_id=topic.read_inbox_max_id or 0,
        closed=bool(topic.closed),
    )


def media_label(message: Any) -> str:
    """Short human label for a message's media, or "" when it has none."""
    if not getattr(message, "media", None):
        return ""
    checks = (
        ("sticker", "Стикер"),
        ("gif", "GIF"),
        ("video_note", "Видеокружок"),
        ("voice", "Голосовое"),
        ("photo", "Фото"),
        ("video", "Видео"),
        ("audio", "Аудио"),
        ("poll", "Опрос"),
        ("geo", "Геолокация"),
        ("contact", "Контакт"),
        ("web_preview", ""),  # link previews add nothing beyond the link in the text
        ("document", "Файл"),
    )
    for attr, label in checks:
        if getattr(message, attr, None):
            return label
    return "Медиа"


def _message_text(message: Any) -> str:
    text = (getattr(message, "message", None) or "").strip()
    label = media_label(message)
    poll = getattr(message, "poll", None)
    if poll is not None:
        question = getattr(getattr(poll, "poll", None), "question", None)
        question_text = getattr(question, "text", question)
        if isinstance(question_text, str) and question_text:
            text = f"{text} {question_text}".strip()
    if label and text:
        return f"[{label}] {text}"
    if label:
        return f"[{label}]"
    return text


class ChatReader:
    """Lists the account's dialogs and fetches messages from one of them."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self._sender_names: dict[int, str] = {}

    async def list_dialogs(self) -> list[DialogInfo]:
        """All dialogs of the account, most recent first (archived included)."""
        async with user_client(self.config) as client:
            dialogs = [dialog_from_telethon(d) async for d in client.iter_dialogs()]
        self.logger.info(f"Loaded {len(dialogs)} dialogs")
        return dialogs

    async def list_topics(self, dialog: DialogInfo) -> list[TopicInfo]:
        """Topics of a forum supergroup in Telegram's order (pinned, then latest activity)."""
        async with user_client(self.config) as client:
            result = await client(
                GetForumTopicsRequest(
                    peer=dialog.entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=TOPICS_LIMIT,
                )
            )
        topics = [topic_from_telethon(t) for t in result.topics if isinstance(t, types.ForumTopic)]
        self.logger.info(f"Loaded {len(topics)} topics of {dialog.title!r}")
        return topics

    async def fetch(
        self,
        dialog: DialogInfo,
        mode: str,
        limit: int,
        progress: Optional[ProgressCallback] = None,
        topic: Optional[TopicInfo] = None,
    ) -> FetchResult:
        """Fetch messages of one dialog, or of one topic of a forum.

        Args:
            dialog: Dialog picked by the user
            mode: MODE_UNREAD (the oldest `limit` unread messages) or
                MODE_LAST (the newest `limit` messages, read or not)
            limit: Maximum number of messages to scan
            progress: Awaited as progress(fetched, expected) every PROGRESS_EVERY messages
            topic: Forum topic to read instead of the whole chat

        Raises:
            ValueError: On an unknown mode or a non-positive limit
        """
        if mode not in (MODE_UNREAD, MODE_LAST):
            raise ValueError(f"Unknown fetch mode: {mode!r}")
        if limit < 1:
            raise ValueError(f"limit must be positive, got {limit}")

        async with user_client(self.config) as client:
            me = await client.get_me()
            my_id = getattr(me, "id", 0)
            min_id = 0
            unread_total = 0
            expected = limit
            oldest_first = mode == MODE_UNREAD
            if mode == MODE_UNREAD:
                min_id, unread_total = await self._fresh_unread_state(client, dialog, topic)
                if unread_total == 0:
                    return FetchResult([], mode, limit, 0)
                expected = min(limit, unread_total)

            messages: list[Message] = []
            seen = 0
            last_id = 0
            async for raw in client.iter_messages(
                dialog.entity,
                limit=limit,
                min_id=min_id,
                reverse=oldest_first,
                reply_to=topic.id if topic else None,
            ):
                seen += 1
                last_id = max(last_id, raw.id)
                converted = self._convert(raw, dialog, my_id, skip_own=mode == MODE_UNREAD)
                if converted is not None:
                    messages.append(converted)
                if progress and seen % PROGRESS_EVERY == 0:
                    await progress(seen, expected)

        if not oldest_first:
            messages.reverse()  # Telegram returns newest first
        where = f"{dialog.title!r}" + (f" / {topic.title!r}" if topic else "")
        self.logger.info(
            f"Fetched {len(messages)} messages from {where} "
            f"(mode={mode}, limit={limit}, scanned={seen})"
        )
        return FetchResult(messages, mode, limit, unread_total, last_id)

    async def mark_read(
        self, dialog: DialogInfo, max_id: int, topic: Optional[TopicInfo] = None
    ) -> Optional[int]:
        """Mark messages up to max_id as read, like opening the chat and scrolling to it.

        Updates the cached unread state of the dialog (and of the topic, if given).

        Returns:
            Unread messages left in the dialog or topic, or None when Telegram did not report it

        Raises:
            ValueError: On max_id <= 0 (Telegram treats 0 as "the whole chat")
        """
        if max_id <= 0:
            raise ValueError(f"max_id must be positive, got {max_id}")
        topic_state: Optional[tuple[int, int]] = None
        async with user_client(self.config) as client:
            if topic is None:
                await client.send_read_acknowledge(dialog.entity, max_id=max_id)
            else:
                await client(
                    ReadDiscussionRequest(peer=dialog.entity, msg_id=topic.id, read_max_id=max_id)
                )
                topic_state = await self._topic_state(client, dialog, topic)
            dialog_state = await self._peer_state(client, dialog)  # a forum's total changes too

        if dialog_state is not None:
            dialog.read_inbox_max_id, dialog.unread_count = dialog_state
        target: DialogInfo | TopicInfo = dialog
        state = dialog_state
        if topic is not None:
            target, state = topic, topic_state
        target.read_inbox_max_id = max(target.read_inbox_max_id, max_id)
        where = f"{dialog.title!r}" + (f" / {topic.title!r}" if topic else "")
        if state is None:
            self.logger.info(f"Marked {where} read up to #{max_id}")
            return None
        target.read_inbox_max_id, target.unread_count = state
        self.logger.info(f"Marked {where} read up to #{max_id}, {target.unread_count} unread left")
        return target.unread_count

    async def _fresh_unread_state(
        self, client: TelegramClient, dialog: DialogInfo, topic: Optional[TopicInfo] = None
    ) -> tuple[int, int]:
        """Return (read_inbox_max_id, unread_count), refreshed from Telegram when possible.

        The dialog list in the bot may be minutes old; the user could have read the chat since.
        """
        if topic is not None:
            state = await self._topic_state(client, dialog, topic)
        else:
            state = await self._peer_state(client, dialog)
        if state is None:  # stale numbers are still a usable answer
            source = topic or dialog
            return source.read_inbox_max_id, source.unread_count
        return state

    async def _topic_state(
        self, client: TelegramClient, dialog: DialogInfo, topic: TopicInfo
    ) -> Optional[tuple[int, int]]:
        try:
            result = await client(GetForumTopicsByIDRequest(peer=dialog.entity, topics=[topic.id]))
            fresh = result.topics[0]
            return fresh.read_inbox_max_id or 0, fresh.unread_count or 0
        except Exception as e:
            self.logger.warning(f"Could not get unread state of topic {topic.title!r}: {e}")
            return None

    async def _peer_state(
        self, client: TelegramClient, dialog: DialogInfo
    ) -> Optional[tuple[int, int]]:
        try:
            peer = await client.get_input_entity(dialog.entity)
            result = await client(GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer=peer)]))
            fresh = result.dialogs[0]
            return fresh.read_inbox_max_id or 0, fresh.unread_count or 0
        except Exception as e:
            self.logger.warning(f"Could not get unread state of {dialog.title!r}: {e}")
            return None

    def _convert(
        self, raw: Any, dialog: DialogInfo, my_id: int, skip_own: bool
    ) -> Optional[Message]:
        if isinstance(raw, types.MessageService) or getattr(raw, "action", None):
            return None  # joins, pins, title changes
        is_own = bool(getattr(raw, "out", False))
        if skip_own and is_own:
            return None  # own messages are never "unread"
        text = _message_text(raw)
        if not text:
            return None
        reply_to = getattr(raw, "reply_to", None)
        date: datetime = raw.date
        return Message(
            text=text,
            sender=self._sender_name(raw, dialog, my_id),
            timestamp=date,
            link=dialog.message_link(raw.id),
            channel_name=dialog.title,
            has_media=bool(getattr(raw, "media", None)),
            media_type=media_label(raw),
            message_id=raw.id,
            reply_to_id=getattr(reply_to, "reply_to_msg_id", None) or 0,
            is_own=is_own,
            mentions_me=bool(getattr(raw, "mentioned", False)),
        )

    def _sender_name(self, raw: Any, dialog: DialogInfo, my_id: int) -> str:
        if getattr(raw, "out", False) or (my_id and getattr(raw, "sender_id", None) == my_id):
            return "ME"
        if dialog.kind == KIND_CHANNEL:
            return getattr(raw, "post_author", None) or ""
        sender_id = getattr(raw, "sender_id", None) or 0
        cached = self._sender_names.get(sender_id)
        if cached is not None:
            return cached
        sender = getattr(raw, "sender", None)  # filled from the same API response, no extra call
        name = ""
        if sender is not None:
            first = getattr(sender, "first_name", None)
            if first:
                last = getattr(sender, "last_name", None)
                name = f"{first} {last}" if last else first
            else:
                name = getattr(sender, "title", None) or ""
                if not name and getattr(sender, "username", None):
                    name = f"@{sender.username}"
        if not name:
            return "Unknown"  # not cached: a later message may carry the sender
        if sender_id:
            self._sender_names[sender_id] = name
        return name
