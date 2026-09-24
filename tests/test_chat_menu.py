"""Tests for the bot's chat navigation and summary job."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from src import chat_menu
from src.chat_menu import ChatMenu, StatusMessage, messages_word, short_title
from src.chat_reader import (
    KIND_CHANNEL,
    KIND_SUPERGROUP,
    KIND_USER,
    MODE_LAST,
    MODE_UNREAD,
    DialogInfo,
    FetchResult,
    TopicInfo,
)
from src.chat_summarizer import STAGE_MAP, STAGE_REDUCE, ChatSummary
from src.collector import Message
from src.config_loader import ChatSummaryConfig
from src.telegram_session import TelegramSessionError

OWNER = 123456789
T0 = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)


def dialogs(n_read=0):
    unread = [
        DialogInfo(id=-1001, title="News", kind=KIND_CHANNEL, unread_count=1500, peer_id=1),
        DialogInfo(id=42, title="Bob", kind=KIND_USER, unread_count=2, peer_id=42),
    ]
    read = [
        DialogInfo(id=-(2000 + i), title=f"Group {i}", kind="group", peer_id=2000 + i)
        for i in range(n_read)
    ]
    return unread + read


def forum():
    return DialogInfo(
        id=-3000, title="Forum", kind=KIND_SUPERGROUP, unread_count=60, peer_id=3000, forum=True
    )


def topics(n_extra=0):
    main = [
        TopicInfo(id=1, title="General", unread_count=51, read_inbox_max_id=10),
        TopicInfo(id=5, title="Жильё", unread_count=9),
        TopicInfo(id=8, title="Архив", closed=True),
    ]
    return main + [TopicInfo(id=100 + i, title=f"T{i}") for i in range(n_extra)]


def message(i):
    return Message(
        text=f"m{i}",
        sender="A",
        timestamp=T0,
        link="",
        channel_name="x",
        has_media=False,
        media_type="",
        message_id=i,
    )


@pytest.fixture
def config(sample_config):
    sample_config.chat_summary = ChatSummaryConfig(page_size=3)
    return sample_config


@pytest.fixture
def reader():
    r = MagicMock()
    r.list_dialogs = AsyncMock(return_value=dialogs(n_read=5))
    r.fetch = AsyncMock()
    r.mark_read = AsyncMock(return_value=0)
    r.list_topics = AsyncMock(return_value=topics())
    return r


@pytest.fixture
def summarizer():
    s = MagicMock()
    s.summarize = AsyncMock()
    return s


@pytest.fixture
def menu(config, reader, summarizer):
    return ChatMenu(config, MagicMock(), reader=reader, summarizer_factory=lambda: summarizer)


def make_context(user_data=None):
    ctx = MagicMock()
    ctx.user_data = {} if user_data is None else user_data
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.edit_message_text = AsyncMock()
    ctx.jobs = []
    ctx.application.create_task = MagicMock(
        side_effect=lambda coro, update=None: ctx.jobs.append(coro)
    )
    return ctx


async def press(menu, data, ctx):
    """Press a button and wait for any background job it started."""
    update = callback_update(data)
    await menu.on_callback(update, ctx)
    while ctx.jobs:
        await ctx.jobs.pop(0)
    return update


def callback_update(data, user_id=OWNER):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.chat.id = 555
    query.message.message_id = 99
    return SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=user_id), effective_message=None
    )


def text_update(text, user_id=OWNER):
    reply = MagicMock()
    reply.edit_text = AsyncMock()
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock(return_value=reply)
    return (
        SimpleNamespace(
            callback_query=None, effective_user=SimpleNamespace(id=user_id), effective_message=msg
        ),
        reply,
    )


def button_rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


# --------------------------------------------------------------------------- helpers


@pytest.mark.unit
@pytest.mark.parametrize(
    "n, word",
    [
        (1, "1 сообщение"),
        (3, "3 сообщения"),
        (5, "5 сообщений"),
        (11, "11 сообщений"),
        (21, "21 сообщение"),
        (112, "112 сообщений"),
        (1000, "1000 сообщений"),
    ],
)
def test_messages_word(n, word):
    assert messages_word(n) == word


@pytest.mark.unit
def test_short_title():
    assert short_title("  a\n b ") == "a b"
    assert short_title("x" * 50, limit=10) == "x" * 9 + "…"


# --------------------------------------------------------------------------- screens


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_screen_unread_and_pagination(menu):
    await menu._ensure_dialogs()
    text, kb = menu.list_screen("u", 0)
    rows = button_rows(kb)
    assert "Чаты с непрочитанными</b>: 2" in text
    assert rows[0] == [("• 📬 Непрочит.", "l:u:0"), ("✅ Прочит.", "l:d:0"), ("📋 Все", "l:a:0")]
    assert rows[1] == [("📢 News · 1500", "c:-1001")]
    assert rows[2] == [("👤 Bob · 2", "c:42")]
    assert rows[-1] == [("🔄 Обновить список", "r:u")]
    assert len(rows) == 4  # one page: no navigation row

    text, kb = menu.list_screen("a", 1)
    rows = button_rows(kb)
    assert rows[0][2][0] == "• 📋 Все"
    assert [r[0][1] for r in rows[1:4]] == ["c:-2001", "c:-2002", "c:-2003"]
    assert rows[4] == [("◀️", "l:a:0"), ("2/3", "x:"), ("▶️", "l:a:2")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_screen_read_chats(menu):
    await menu._ensure_dialogs()
    text, kb = menu.list_screen("d", 0)
    rows = button_rows(kb)
    assert "Прочитанные чаты</b>: 5" in text
    assert rows[0][1] == ("• ✅ Прочит.", "l:d:0")
    assert [r[0][1] for r in rows[1:4]] == ["c:-2000", "c:-2001", "c:-2002"]
    assert rows[-1] == [("🔄 Обновить список", "r:d")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_screen_page_is_clamped_and_wraps(menu):
    await menu._ensure_dialogs()
    _, kb = menu.list_screen("a", 99)
    nav = button_rows(kb)[-2]
    assert nav == [("◀️", "l:a:1"), ("3/3", "x:"), ("▶️", "l:a:0")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_screen_search_and_empty(menu):
    await menu._ensure_dialogs()
    text, kb = menu.list_screen("s", 0, "GROUP 1")
    assert "«GROUP 1»: 1" in text
    assert button_rows(kb)[1] == [("👥 Group 1", "c:-2001")]
    text, _ = menu.list_screen("s", 0, "<nope>")
    assert "&lt;nope&gt;" in text and "Ничего не нашлось." in text


@pytest.mark.unit
def test_callback_data_fits_telegram_limit(menu):
    menu._dialogs = [DialogInfo(id=-1009999999999999, title="t", kind="supergroup", unread_count=1)]
    _, kb = menu.list_screen("u", 0)
    _, kb2 = menu.chat_screen(menu._dialogs[0], "l:u:0")
    for markup in (kb, kb2):
        for row in markup.inline_keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64


@pytest.mark.unit
def test_chat_screen_buttons(menu):
    news = dialogs()[0]
    text, kb = menu.chat_screen(news, "l:a:2")
    rows = button_rows(kb)
    assert rows[0] == [  # 1500 unread, more than the 1000 cap: no "all" button
        ("📬 100", "m:-1001:u100"),
        ("📬 500", "m:-1001:u500"),
        ("📬 1000", "m:-1001:u1000"),
    ]
    assert rows[1] == [
        ("🕘 100", "m:-1001:100"),
        ("🕘 500", "m:-1001:500"),
        ("🕘 1000", "m:-1001:1000"),
    ]
    assert rows[2] == [("⬅️ К списку", "l:a:2")]
    assert "Непрочитанные по порядку" in text
    assert "Ссылок на сообщения не будет" not in text

    bob = dialogs()[1]
    text, kb = menu.chat_screen(bob, "l:u:0")
    assert button_rows(kb)[0] == [("📬 Все 2", "m:42:u1000")]
    assert "Ссылок на сообщения не будет" in text

    some = DialogInfo(id=8, title="Some", kind=KIND_USER, unread_count=300)
    _, kb = menu.chat_screen(some, "l:u:0")
    assert button_rows(kb)[0] == [("📬 100", "m:8:u100"), ("📬 Все 300", "m:8:u1000")]

    quiet = DialogInfo(id=7, title="Quiet", kind=KIND_USER, archived=True)
    text, kb = menu.chat_screen(quiet, "l:u:0")
    assert button_rows(kb)[0][0][1] == "m:7:100"
    assert "в архиве" in text and "Непрочитанные по порядку" not in text


# --------------------------------------------------------------------------- handlers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_shows_unread_list(menu):
    update, reply = text_update("/start")
    ctx = make_context()
    await menu.cmd_chats(update, ctx)
    text = reply.edit_text.call_args.args[0]
    assert "Чаты с непрочитанными" in text
    assert ctx.user_data["back"] == "l:u:0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_shows_all_when_nothing_unread(menu, reader):
    reader.list_dialogs.return_value = [DialogInfo(id=1, title="A", kind=KIND_USER)]
    update, reply = text_update("/start")
    await menu.cmd_chats(update, make_context())
    assert "Все чаты" in reply.edit_text.call_args.args[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_reports_session_problem(menu, reader):
    reader.list_dialogs.side_effect = TelegramSessionError("no session")
    update, reply = text_update("/start")
    await menu.cmd_chats(update, make_context())
    assert "create_session.py" in reply.edit_text.call_args.args[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strangers_are_ignored(menu, reader):
    update, _ = text_update("/start", user_id=1)
    await menu.cmd_chats(update, make_context())
    await menu.on_search(update, make_context())
    cb = callback_update("l:a:0", user_id=1)
    await menu.on_callback(cb, make_context())
    reader.list_dialogs.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()
    cb.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_by_text(menu):
    update, _ = text_update("  bob ")
    ctx = make_context()
    await menu.on_search(update, ctx)
    assert ctx.user_data == {"query": "bob", "back": "l:s:0"}
    text = update.effective_message.reply_text.call_args.args[0]
    assert "«bob»: 1" in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_callbacks_use_cache_and_refresh_reloads(menu, reader):
    ctx = make_context()
    await menu.on_callback(callback_update("l:a:1"), ctx)
    await menu.on_callback(callback_update("l:u:0"), ctx)
    assert reader.list_dialogs.await_count == 1
    assert ctx.user_data["back"] == "l:u:0"
    await menu.on_callback(callback_update("r:a"), ctx)
    assert reader.list_dialogs.await_count == 2
    assert ctx.user_data["back"] == "l:a:0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_page_without_query_falls_back_to_all(menu):
    update = callback_update("l:s:0")
    await menu.on_callback(update, make_context())
    assert "Все чаты" in update.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_load_error_offers_retry(menu, reader):
    reader.list_dialogs.side_effect = RuntimeError("network down")
    update = callback_update("r:u")
    await menu.on_callback(update, make_context())
    call = update.callback_query.edit_message_text.call_args
    assert "network down" in call.args[0]
    assert button_rows(call.kwargs["reply_markup"]) == [[("🔄 Повторить", "r:u")]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_chat_and_unknown_chat(menu, reader):
    ctx = make_context({"back": "l:a:1"})
    update = callback_update("c:42")
    await menu.on_callback(update, ctx)
    call = update.callback_query.edit_message_text.call_args
    assert "<b>Bob</b>" in call.args[0]
    assert button_rows(call.kwargs["reply_markup"])[-1] == [("⬅️ К списку", "l:a:1")]

    update = callback_update("c:31337")
    await menu.on_callback(update, ctx)
    update.callback_query.answer.assert_awaited_with(
        "Чат не найден, обнови список", show_alert=True
    )
    assert reader.list_dialogs.await_count == 2  # retried with a fresh list


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "c:abc",
        "m:42:zzz",
        "m:42:5000",
        "m:42:u",
        "m:42:u5000",
        "m:42:u0",
        "m:42:u100:x",
        "m:42:u100:1:2",
        "t:42:x",
        "l:q:0",
    ],
)
async def test_stale_or_bad_buttons(menu, data):
    update = callback_update(data)
    await menu.on_callback(update, make_context())
    update.callback_query.answer.assert_awaited_with(
        "Кнопка устарела, открой /start", show_alert=True
    )
    assert menu._busy is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_noop_button(menu):
    update = callback_update("x:")
    await menu.on_callback(update, make_context())
    update.callback_query.answer.assert_awaited_once_with()


# --------------------------------------------------------------------------- the job


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unread_job_summarizes_then_marks_read(menu, reader, summarizer, monkeypatch):
    monkeypatch.setattr(chat_menu, "HEARTBEAT_SECONDS", 3600)
    msgs = [message(1), message(2)]

    async def fake_fetch(dialog, mode, limit, progress=None, topic=None):
        await progress(100, 100)
        return FetchResult(msgs, mode, limit, unread_total=1500, last_id=3)

    async def fake_summarize(dialog, messages, progress=None):
        await progress(STAGE_MAP, 0, 2)
        await progress(STAGE_MAP, 2, 2)
        await progress(STAGE_REDUCE, 0, 1)
        text = "**Коротко:** x [#1]\n" + "\n".join(f"- line {i}" + "y" * 90 for i in range(60))
        return ChatSummary(text, {1: "https://t.me/c/1/1"}, 2, 2, T0, T0)

    reader.fetch.side_effect = fake_fetch
    summarizer.summarize.side_effect = fake_summarize
    reader.mark_read.return_value = 1400
    ctx = make_context({"back": "l:u:0"})

    await press(menu, "m:-1001:u100", ctx)
    assert menu._busy is None

    reader.fetch.assert_awaited_once()
    assert reader.fetch.call_args.args[1:3] == (MODE_UNREAD, 100)
    news = reader.fetch.call_args.args[0]
    reader.mark_read.assert_awaited_once_with(news, 3, topic=None)
    sends = ctx.bot.send_message.await_args_list
    assert len(sends) >= 3
    first = sends[0].kwargs["text"]
    assert first.startswith("📢 <b>News</b>\n📬 Непрочитанные · 2 сообщения")
    assert "самые старые из 1500 непрочитанных" in first
    assert '<a href="https://t.me/c/1/1">🔗</a>' in first
    assert all(s.kwargs["reply_markup"] is None for s in sends[:-1])
    footer = sends[-1].kwargs
    assert footer["text"] == "✔️ Отмечено прочитанным. Осталось непрочитанных: 1400."
    assert button_rows(footer["reply_markup"]) == [
        [("▶️ Следующие 100", "m:-1001:u100")],
        [("🔁 Этот чат", "c:-1001"), ("📋 К списку", "l:u:0")],
    ]
    edits = [c.kwargs["text"] for c in ctx.bot.edit_message_text.await_args_list]
    assert any("Загружаю" in e for e in edits)
    assert any("готово частей 2 из 2" in e for e in edits)
    assert any("Собираю итоговое" in e for e in edits)
    assert "✅ Готово: 2 сообщения" in edits[-1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_last_n_job_does_not_mark_read(menu, reader, summarizer):
    reader.fetch.return_value = FetchResult([message(1)], MODE_LAST, 100, last_id=1)
    summarizer.summarize.return_value = ChatSummary("**Коротко:** x", {}, 1, 1, T0, T0)
    ctx = make_context({"back": "l:d:0"})
    await press(menu, "m:42:100", ctx)
    reader.mark_read.assert_not_awaited()
    sends = ctx.bot.send_message.await_args_list
    assert len(sends) == 1
    assert "🕘 Последние 100 · 1 сообщение" in sends[0].kwargs["text"]
    assert button_rows(sends[0].kwargs["reply_markup"]) == [
        [("🔁 Этот чат", "c:42"), ("📋 К списку", "l:d:0")]
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "left, error, expected",
    [
        (0, None, "Непрочитанных больше нет"),
        (None, RuntimeError("FLOOD <wait>"), "Не получилось отметить прочитанным"),
    ],
)
async def test_unread_job_without_next_batch(menu, reader, summarizer, left, error, expected):
    reader.fetch.return_value = FetchResult([message(1)], MODE_UNREAD, 100, 1, last_id=1)
    summarizer.summarize.return_value = ChatSummary("x", {}, 1, 1, T0, T0)
    reader.mark_read.return_value = left
    reader.mark_read.side_effect = error
    ctx = make_context()
    await press(menu, "m:42:u100", ctx)
    footer = ctx.bot.send_message.await_args_list[-1].kwargs
    assert expected in footer["text"] and "<wait>" not in footer["text"]
    assert button_rows(footer["reply_markup"]) == [
        [("🔁 Этот чат", "c:42"), ("📋 К списку", "l:u:0")]
    ]
    assert menu._busy is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unread_batch_of_service_messages_is_marked_read(menu, reader, summarizer):
    reader.fetch.return_value = FetchResult([], MODE_UNREAD, 100, unread_total=3, last_id=7)
    reader.mark_read.return_value = 0
    ctx = make_context()
    await press(menu, "m:42:u100", ctx)
    summarizer.summarize.assert_not_awaited()
    reader.mark_read.assert_awaited_once()
    assert reader.mark_read.call_args.args[1] == 7
    assert "Непрочитанных больше нет" in ctx.bot.send_message.call_args.kwargs["text"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_one_job_at_a_time(menu, reader, summarizer):
    ctx = make_context()
    created = ctx.jobs
    await menu.on_callback(callback_update("m:42:100"), ctx)
    assert menu._busy == "Bob"

    second = callback_update("m:-1001:100")
    await menu.on_callback(second, ctx)
    second.callback_query.answer.assert_awaited_with(
        "Уже делаю саммари «Bob», подожди", show_alert=True
    )
    assert len(created) == 1

    reader.fetch.return_value = FetchResult([], MODE_LAST, 100)
    await created[0]
    assert menu._busy is None
    assert "Сообщений нет." in ctx.bot.edit_message_text.await_args_list[-1].kwargs["text"]
    summarizer.summarize.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_job_failure_is_reported(menu, reader, summarizer):
    reader.fetch.return_value = FetchResult([message(1)], MODE_LAST, 100)
    summarizer.summarize.side_effect = RuntimeError("OpenAI 500 <oops>")
    ctx = make_context()
    await press(menu, "m:42:100", ctx)
    last = ctx.bot.edit_message_text.await_args_list[-1].kwargs
    assert "❌ Не получилось: RuntimeError" in last["text"]
    assert "&lt;oops&gt;" in last["text"]
    assert last["reply_markup"] is not None
    assert menu._busy is None
    reader.mark_read.assert_not_awaited()  # nothing delivered, nothing marked


@pytest.mark.unit
@pytest.mark.asyncio
async def test_job_session_error_is_reported(menu, reader):
    reader.fetch.side_effect = TelegramSessionError("gone")
    ctx = make_context()
    await press(menu, "m:42:u100", ctx)
    assert "create_session.py" in ctx.bot.edit_message_text.await_args_list[-1].kwargs["text"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_html_rejected_falls_back_to_plain(menu):
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[BadRequest("Can't parse entities"), None])
    await menu._send_html(bot, 1, "<b>A</b> &amp; B", None)
    assert bot.send_message.await_args_list[1].kwargs["text"] == "A & B"
    assert "parse_mode" not in bot.send_message.await_args_list[1].kwargs

    bot.send_message = AsyncMock(side_effect=BadRequest("Chat not found"))
    with pytest.raises(BadRequest):
        await menu._send_html(bot, 1, "x", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_model_answer_still_sends_something(menu):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await menu._send_summary(bot, 1, "intro", "   ", {}, None)
    assert "пустой ответ" in bot.send_message.call_args.kwargs["text"]


# --------------------------------------------------------------------------- status message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_throttles_and_ignores_edit_errors(monkeypatch):
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    status = StatusMessage(bot, 1, 2, "H", MagicMock())
    await status.stage("one", force=True)
    await status.stage("two")  # within STATUS_MIN_INTERVAL: skipped
    assert bot.edit_message_text.await_count == 1
    await status.stage("three", force=True)
    assert bot.edit_message_text.await_count == 2
    assert bot.edit_message_text.call_args.kwargs["text"].startswith("H\nthree\n⏱ ")

    bot.edit_message_text.side_effect = BadRequest("Message is not modified")
    await status.stage("four", force=True)  # must not raise
    bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    await status.finish("done")  # must not raise


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_heartbeat_refreshes(monkeypatch):
    monkeypatch.setattr(chat_menu, "HEARTBEAT_SECONDS", 0.01)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    status = StatusMessage(bot, 1, 2, "H", MagicMock())
    status._started -= 5  # make every heartbeat text differ from the first edit
    await status.stage("working", force=True)
    status.start_heartbeat()
    await asyncio.sleep(0.05)
    await status.stop()
    assert status._ticker is None
    assert bot.edit_message_text.await_count >= 1


# --------------------------------------------------------------------------- forum topics


@pytest.fixture
def forum_menu(menu, reader):
    reader.list_dialogs.return_value = [forum()] + dialogs()
    return menu


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forum_screen_lists_topics(forum_menu, reader):
    reader.list_topics.return_value = topics(n_extra=2)  # 5 topics, page size 3
    ctx = make_context({"back": "l:u:0"})
    update = await press(forum_menu, "c:-3000", ctx)
    call = update.callback_query.edit_message_text.call_args
    rows = button_rows(call.kwargs["reply_markup"])
    assert "🗂 <b>Forum</b>" in call.args[0]
    assert "Непрочитанных во всех ветках: 60" in call.args[0]
    assert rows[:3] == [
        [("# General · 51", "t:-3000:1")],
        [("# Жильё · 9", "t:-3000:5")],
        [("🔒 Архив", "t:-3000:8")],
    ]
    assert rows[3] == [("◀️", "c:-3000:1"), ("1/2", "x:"), ("▶️", "c:-3000:1")]
    assert rows[4] == [
        ("🕘 100", "m:-3000:100"),
        ("🕘 500", "m:-3000:500"),
        ("🕘 1000", "m:-3000:1000"),
    ]
    assert rows[5] == [("⬅️ К списку", "l:u:0")]

    update = await press(forum_menu, "c:-3000:1", ctx)
    rows = button_rows(update.callback_query.edit_message_text.call_args.kwargs["reply_markup"])
    assert rows[0] == [("# T0", "t:-3000:100")]
    assert reader.list_topics.await_count == 1  # cached


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forum_topics_load_error_offers_retry(forum_menu, reader):
    reader.list_topics.side_effect = RuntimeError("TOPICS <down>")
    update = await press(forum_menu, "c:-3000", make_context())
    call = update.callback_query.edit_message_text.call_args
    assert "&lt;down&gt;" in call.args[0]
    assert button_rows(call.kwargs["reply_markup"]) == [[("🔄 Повторить", "c:-3000")]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_topic_screen(forum_menu):
    update = await press(forum_menu, "t:-3000:1", make_context())
    call = update.callback_query.edit_message_text.call_args
    rows = button_rows(call.kwargs["reply_markup"])
    assert call.args[0].startswith("🗂 <b>Forum</b> › # <b>General</b>\nНепрочитанных: 51")
    assert rows[0] == [("📬 Все 51", "m:-3000:u1000:1")]
    assert rows[1] == [
        ("🕘 100", "m:-3000:100:1"),
        ("🕘 500", "m:-3000:500:1"),
        ("🕘 1000", "m:-3000:1000:1"),
    ]
    assert rows[2] == [("⬅️ К веткам", "c:-3000")]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["t:-3000:999", "t:42:1", "m:-3000:u100:999"])
async def test_unknown_topic(forum_menu, data):
    update = await press(forum_menu, data, make_context())
    alert = update.callback_query.answer.await_args.args[0]
    assert "не найден" in alert
    assert forum_menu._busy is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_topic_job_reads_and_marks_the_topic(forum_menu, reader, summarizer):
    reader.fetch.return_value = FetchResult(
        [message(11)], MODE_UNREAD, 100, unread_total=150, last_id=12
    )
    summarizer.summarize.return_value = ChatSummary("x", {}, 1, 1, T0, T0)
    reader.mark_read.return_value = 50
    ctx = make_context({"back": "l:u:0"})
    await press(forum_menu, "m:-3000:u100:1", ctx)

    dialog = reader.fetch.call_args.args[0]
    topic = reader.fetch.call_args.kwargs["topic"]
    assert (dialog.id, topic.id) == (-3000, 1)
    assert summarizer.summarize.call_args.args[0].title == "Forum / General"
    reader.mark_read.assert_awaited_once_with(dialog, 12, topic=topic)
    sends = ctx.bot.send_message.await_args_list
    assert sends[0].kwargs["text"].startswith("🗂 <b>Forum</b> › # <b>General</b>\n📬")
    assert button_rows(sends[-1].kwargs["reply_markup"]) == [
        [("▶️ Следующие 100", "m:-3000:u100:1")],
        [("🔁 Эта ветка", "t:-3000:1"), ("📋 К списку", "l:u:0")],
    ]
