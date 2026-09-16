"""Tests for the bot's chat navigation and summary job."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from src import chat_menu
from src.chat_menu import ChatMenu, StatusMessage, messages_word, short_title
from src.chat_reader import KIND_CHANNEL, KIND_USER, MODE_LAST, MODE_UNREAD, DialogInfo, FetchResult
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
    assert rows[0] == [("• 📬 Непрочитанные", "l:u:0"), ("📋 Все", "l:a:0")]
    assert rows[1] == [("📢 News · 1500", "c:-1001")]
    assert rows[2] == [("👤 Bob · 2", "c:42")]
    assert rows[-1] == [("🔄 Обновить список", "r:u")]
    assert len(rows) == 4  # one page: no navigation row

    text, kb = menu.list_screen("a", 1)
    rows = button_rows(kb)
    assert rows[0][1][0] == "• 📋 Все"
    assert [r[0][1] for r in rows[1:4]] == ["c:-2001", "c:-2002", "c:-2003"]
    assert rows[4] == [("◀️", "l:a:0"), ("2/3", "x:"), ("▶️", "l:a:2")]


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
    assert rows[0] == [("📬 Непрочитанные (1000+)", "m:-1001:u")]
    assert rows[1] == [
        ("Последние 100", "m:-1001:100"),
        ("Последние 500", "m:-1001:500"),
        ("Последние 1000", "m:-1001:1000"),
    ]
    assert rows[2] == [("⬅️ К списку", "l:a:2")]
    assert "Ссылок на сообщения не будет" not in text

    bob = dialogs()[1]
    text, kb = menu.chat_screen(bob, "l:u:0")
    assert button_rows(kb)[0] == [("📬 Непрочитанные (2)", "m:42:u")]
    assert "Ссылок на сообщения не будет" in text

    quiet = DialogInfo(id=7, title="Quiet", kind=KIND_USER, archived=True)
    text, kb = menu.chat_screen(quiet, "l:u:0")
    assert button_rows(kb)[0][0][1] == "m:7:100"
    assert "в архиве" in text


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
@pytest.mark.parametrize("data", ["c:abc", "m:42:zzz", "m:42:5000", "l:q:0"])
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
async def test_summary_job_success(menu, reader, summarizer, monkeypatch):
    monkeypatch.setattr(chat_menu, "HEARTBEAT_SECONDS", 3600)
    msgs = [message(1), message(2)]

    async def fake_fetch(dialog, mode, limit, progress=None):
        await progress(100, 1000)
        return FetchResult(msgs, mode, limit, unread_total=1500)

    async def fake_summarize(dialog, messages, progress=None):
        await progress(STAGE_MAP, 0, 2)
        await progress(STAGE_MAP, 2, 2)
        await progress(STAGE_REDUCE, 0, 1)
        text = "**Коротко:** x [#1]\n" + "\n".join(f"- line {i}" + "y" * 90 for i in range(60))
        return ChatSummary(text, {1: "https://t.me/c/1/1"}, 2, 2, T0, T0)

    reader.fetch.side_effect = fake_fetch
    summarizer.summarize.side_effect = fake_summarize
    ctx = make_context({"back": "l:u:0"})

    await press(menu, "m:-1001:u", ctx)
    assert menu._busy is None

    reader.fetch.assert_awaited_once()
    assert reader.fetch.call_args.args[1:3] == (MODE_UNREAD, 1000)
    sends = ctx.bot.send_message.await_args_list
    assert len(sends) >= 2
    first = sends[0].kwargs["text"]
    assert first.startswith("📢 <b>News</b>\n📬 Непрочитанные · 2 сообщения")
    assert "⚠️ Непрочитанных 1500, взяты последние 1000." in first
    assert '<a href="https://t.me/c/1/1">🔗</a>' in first
    assert all(s.kwargs["reply_markup"] is None for s in sends[:-1])
    assert button_rows(sends[-1].kwargs["reply_markup"]) == [
        [("🔁 Этот чат", "c:-1001"), ("📋 К списку", "l:u:0")]
    ]
    edits = [c.kwargs["text"] for c in ctx.bot.edit_message_text.await_args_list]
    assert any("Загружаю" in e for e in edits)
    assert any("готово частей 2 из 2" in e for e in edits)
    assert any("Собираю итоговое" in e for e in edits)
    assert "✅ Готово: 2 сообщения" in edits[-1]


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_job_session_error_is_reported(menu, reader):
    reader.fetch.side_effect = TelegramSessionError("gone")
    ctx = make_context()
    await press(menu, "m:42:u", ctx)
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
