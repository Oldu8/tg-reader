"""
Bot navigation for on-demand chat summaries.

Flow: /start → list of chats (unread, read, all, or search by sending text)
→ chat screen → mode → live status message → summary.
A forum (supergroup with topics) first shows its topics; each topic has its own modes.

Modes:
    next N unread  oldest unread first; marked as read once the summary is delivered,
                   so pressing "next" again continues where the previous batch ended
    last N         newest messages, read or not; the unread counter is not touched

Callback data (Telegram allows 64 bytes):
    l:<f>:<page>   chat list, f = u (unread) | d (read) | a (all) | s (search results)
    r:<f>          reload dialogs from Telegram, then show list f
    c:<chat_id>[:<page>]   chat screen; for a forum, page of its topic list
    t:<chat_id>:<topic_id>   topic screen
    m:<chat_id>:<mode>[:<topic_id>]   summarize, mode = u<N> (next N unread) | <N> (last N)
    x:             no-op (page counter button)
"""

import asyncio
import html
import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.chat_reader import (
    KIND_BOT,
    KIND_CHANNEL,
    KIND_SELF,
    KIND_USER,
    MODE_LAST,
    MODE_UNREAD,
    ChatReader,
    DialogInfo,
    FetchResult,
    TopicInfo,
)
from src.chat_summarizer import STAGE_MAP, ChatSummarizer, ChatSummary, resolve_timezone
from src.config_loader import Config
from src.telegram_session import TelegramSessionError
from src.tg_render import render_html, split_parts, strip_html

CACHE_TTL_SECONDS = 300
STATUS_MIN_INTERVAL = 2.0  # seconds between progress edits
HEARTBEAT_SECONDS = 10.0  # the status shows a fresh elapsed time at least this often
TITLE_MAX = 38
SEARCH_MAX = 64
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

FILTER_UNREAD = "u"
FILTER_READ = "d"
FILTER_ALL = "a"
FILTER_SEARCH = "s"

_ICONS = {KIND_CHANNEL: "📢", KIND_USER: "👤", KIND_BOT: "🤖", KIND_SELF: "⭐"}
_KIND_NAMES = {
    KIND_CHANNEL: "канал",
    KIND_USER: "личный чат",
    KIND_BOT: "бот",
    KIND_SELF: "избранное",
}


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 сообщение, 2 сообщения, 5 сообщений."""
    tail = n % 100
    if 11 <= tail <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def messages_word(n: int) -> str:
    """'<n> сообщение/сообщения/сообщений'."""
    return f"{n} {plural(n, 'сообщение', 'сообщения', 'сообщений')}"


def icon(dialog: DialogInfo) -> str:
    """Emoji for the chat kind (groups and supergroups share one, forums have their own)."""
    if dialog.forum:
        return "🗂"
    return _ICONS.get(dialog.kind, "👥")


def topic_label(topic: TopicInfo) -> str:
    """Button text of a forum topic."""
    label = f"{'🔒' if topic.closed else '#'} {short_title(topic.title)}"
    return f"{label} · {topic.unread_count}" if topic.unread_count else label


@dataclass
class Target:
    """What a summary job reads: a whole chat or one topic of a forum."""

    dialog: DialogInfo
    topic: Optional[TopicInfo] = None

    @property
    def unread_count(self) -> int:
        """Unread messages of the chat or topic."""
        return (self.topic or self.dialog).unread_count

    @property
    def name(self) -> str:
        """Plain name for logs and alerts."""
        return self.dialog.title + (f" / {self.topic.title}" if self.topic else "")

    @property
    def header(self) -> str:
        """HTML title line."""
        line = f"{icon(self.dialog)} <b>{html.escape(self.dialog.title)}</b>"
        if self.topic:
            line += f" › # <b>{html.escape(self.topic.title)}</b>"
        return line

    @property
    def screen_data(self) -> str:
        """Callback data that opens this chat's or topic's mode picker."""
        if self.topic:
            return f"t:{self.dialog.id}:{self.topic.id}"
        return f"c:{self.dialog.id}"

    def mode_data(self, mode: str) -> str:
        """Callback data that starts a summary job in the given mode."""
        suffix = f":{self.topic.id}" if self.topic else ""
        return f"m:{self.dialog.id}:{mode}{suffix}"

    def summary_dialog(self) -> DialogInfo:
        """The dialog as the summarizer should see it (topic title included)."""
        return replace(self.dialog, title=self.name) if self.topic else self.dialog


def short_title(title: str, limit: int = TITLE_MAX) -> str:
    """Title cut to fit a button."""
    title = " ".join(title.split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


class StatusMessage:
    """One bot message edited in place to show job progress and elapsed time."""

    def __init__(self, bot, chat_id: int, message_id: int, header: str, logger: logging.Logger):
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._header = header
        self._logger = logger
        self._stage = ""
        self._started = time.monotonic()
        self._last_edit = 0.0
        self._last_text = ""
        self._ticker: Optional[asyncio.Task] = None

    @property
    def elapsed(self) -> int:
        """Whole seconds since the job started."""
        return int(time.monotonic() - self._started)

    def start_heartbeat(self) -> None:
        """Keep refreshing the elapsed time while a long step runs."""
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._edit(force=True)

    async def stop(self) -> None:
        """Stop the heartbeat."""
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except asyncio.CancelledError:
                pass
            self._ticker = None

    async def stage(self, text: str, force: bool = False) -> None:
        """Show a new progress line (throttled unless force)."""
        self._stage = text
        await self._edit(force=force)

    async def finish(self, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
        """Replace the status with a final line and stop the heartbeat."""
        await self.stop()
        await self._send_edit(f"{self._header}\n{text}", keyboard)

    async def _edit(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_edit < STATUS_MIN_INTERVAL:
            return
        text = f"{self._header}\n{self._stage}\n⏱ {self.elapsed} c"
        if text == self._last_text:
            return
        self._last_edit = now
        await self._send_edit(text, None)

    async def _send_edit(self, text: str, keyboard: Optional[InlineKeyboardMarkup]) -> None:
        self._last_text = text
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramError as e:  # a failed status edit must never kill the job
            if "not modified" not in str(e).lower():
                self._logger.debug(f"Status edit failed: {e}")


class ChatMenu:
    """Inline-keyboard navigation over the account's chats and the summary job."""

    def __init__(
        self,
        config: Config,
        logger: logging.Logger,
        reader: Optional[ChatReader] = None,
        summarizer_factory: Optional[Callable[[], ChatSummarizer]] = None,
    ):
        self.config = config
        self.cfg = config.chat_summary
        self.logger = logger
        self.reader = reader or ChatReader(config, logger)
        self._make_summarizer = summarizer_factory or (lambda: ChatSummarizer(config, logger))
        self._tz = resolve_timezone(config.settings.timezone)
        self._dialogs: list[DialogInfo] = []
        self._by_id: dict[int, DialogInfo] = {}
        self._loaded_at: Optional[float] = None
        self._load_lock = asyncio.Lock()
        self._topics: dict[int, tuple[float, list[TopicInfo]]] = {}  # chat id → (loaded, topics)
        self._busy: Optional[str] = None  # title of the chat being summarized

    # ------------------------------------------------------------------ wiring

    def register(self, app: Application) -> None:
        """Add the menu handlers to the bot application."""
        app.add_handler(CommandHandler(["start", "chats"], self.cmd_chats))
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^[lrctmx]:"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_search))

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id == self.config.settings.target_user_id

    # ------------------------------------------------------------------ dialogs

    async def _ensure_dialogs(self, force: bool = False) -> list[DialogInfo]:
        async with self._load_lock:
            fresh = (
                self._loaded_at is not None
                and time.monotonic() - self._loaded_at < CACHE_TTL_SECONDS
            )
            if force or not fresh:
                self._dialogs = await self.reader.list_dialogs()
                self._by_id = {d.id: d for d in self._dialogs}
                self._loaded_at = time.monotonic()
        return self._dialogs

    async def _ensure_topics(self, dialog: DialogInfo) -> list[TopicInfo]:
        cached = self._topics.get(dialog.id)
        if cached is None or time.monotonic() - cached[0] >= CACHE_TTL_SECONDS:
            cached = (time.monotonic(), await self.reader.list_topics(dialog))
            self._topics[dialog.id] = cached
        return cached[1]

    def _filtered(self, flt: str, query: str) -> list[DialogInfo]:
        if flt == FILTER_UNREAD:
            return [d for d in self._dialogs if d.unread_count > 0]
        if flt == FILTER_READ:
            return [d for d in self._dialogs if d.unread_count == 0]
        if flt == FILTER_SEARCH:
            q = query.casefold()
            return [
                d
                for d in self._dialogs
                if q in d.title.casefold() or (d.username and q in d.username.casefold())
            ]
        return list(self._dialogs)

    # ------------------------------------------------------------------ screens

    def list_screen(self, flt: str, page: int, query: str = "") -> tuple[str, InlineKeyboardMarkup]:
        """Text and keyboard of a chat list page."""
        items = self._filtered(flt, query)
        size = self.cfg.page_size
        pages = max(1, math.ceil(len(items) / size))
        page = min(max(page, 0), pages - 1)

        if flt == FILTER_UNREAD:
            title = f"📬 <b>Чаты с непрочитанными</b>: {len(items)}"
            empty = "Непрочитанных нет 🎉"
        elif flt == FILTER_READ:
            title = f"✅ <b>Прочитанные чаты</b>: {len(items)}"
            empty = "Прочитанных чатов нет."
        elif flt == FILTER_SEARCH:
            title = f"🔎 <b>Поиск</b> «{html.escape(query)}»: {len(items)}"
            empty = "Ничего не нашлось."
        else:
            title = f"📋 <b>Все чаты</b>: {len(items)}"
            empty = "Чатов нет."
        lines = [title]
        if not items:
            lines.append(empty)
        lines.append("\nВыбери чат или пришли часть названия для поиска.")

        tabs = (
            (FILTER_UNREAD, "📬 Непрочит."),
            (FILTER_READ, "✅ Прочит."),
            (FILTER_ALL, "📋 Все"),
        )
        rows = [
            [
                InlineKeyboardButton(f"{'• ' if f == flt else ''}{label}", callback_data=f"l:{f}:0")
                for f, label in tabs
            ]
        ]
        for d in items[page * size : (page + 1) * size]:
            label = f"{icon(d)} {short_title(d.title)}"
            if d.unread_count:
                label += f" · {d.unread_count}"
            rows.append([InlineKeyboardButton(label, callback_data=f"c:{d.id}")])
        if pages > 1:
            rows.append(
                [
                    InlineKeyboardButton("◀️", callback_data=f"l:{flt}:{(page - 1) % pages}"),
                    InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="x:"),
                    InlineKeyboardButton("▶️", callback_data=f"l:{flt}:{(page + 1) % pages}"),
                ]
            )
        rows.append([InlineKeyboardButton("🔄 Обновить список", callback_data=f"r:{flt}")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def chat_screen(
        self, d: DialogInfo, back: str, topic: Optional[TopicInfo] = None
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Text and keyboard of the mode picker for one chat or one topic of a forum."""
        target = Target(d, topic)
        lines = [target.header]
        if topic is None:
            lines.append(f"Тип: {_KIND_NAMES.get(d.kind, 'группа')}")
            if d.archived:
                lines[-1] += " · в архиве"
        lines.append(f"Непрочитанных: {target.unread_count}")
        if not d.linkable:
            lines.append(
                "\nℹ️ Ссылок на сообщения не будет: Telegram не даёт их "
                "для личных чатов и обычных групп."
            )
        rows = []
        if target.unread_count:
            lines.append(
                "\n📬 <b>Непрочитанные по порядку</b>: с первого непрочитанного. "
                "После саммари они отмечаются прочитанными."
            )
            rows.append(self._unread_buttons(target))
        lines.append("\n🕘 <b>Последние</b>: самые свежие сообщения, счётчик не меняется.")
        rows.append(self._last_buttons(target))
        if topic is None:
            rows.append([InlineKeyboardButton("⬅️ К списку", callback_data=back)])
        else:
            rows.append([InlineKeyboardButton("⬅️ К веткам", callback_data=f"c:{d.id}")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def forum_screen(
        self, d: DialogInfo, topics: list[TopicInfo], page: int, back: str
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Topic picker of a forum, plus "last N" over the whole chat."""
        size = self.cfg.page_size
        pages = max(1, math.ceil(len(topics) / size))
        page = min(max(page, 0), pages - 1)
        lines = [f"{icon(d)} <b>{html.escape(d.title)}</b>", "Тип: группа с ветками"]
        if d.archived:
            lines[-1] += " · в архиве"
        lines.append(f"Непрочитанных во всех ветках: {d.unread_count}")
        lines.append(f"\nВеток: {len(topics)}. Выбери ветку, чтобы читать её по порядку.")
        lines.append(
            "\n🕘 <b>Последние по всему чату</b>: все ветки вперемешку, счётчик не меняется."
        )
        rows = [
            [InlineKeyboardButton(topic_label(t), callback_data=f"t:{d.id}:{t.id}")]
            for t in topics[page * size : (page + 1) * size]
        ]
        if pages > 1:
            rows.append(
                [
                    InlineKeyboardButton("◀️", callback_data=f"c:{d.id}:{(page - 1) % pages}"),
                    InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="x:"),
                    InlineKeyboardButton("▶️", callback_data=f"c:{d.id}:{(page + 1) % pages}"),
                ]
            )
        rows.append(self._last_buttons(Target(d)))
        rows.append([InlineKeyboardButton("⬅️ К списку", callback_data=back)])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def _unread_buttons(self, target: Target) -> list[InlineKeyboardButton]:
        """Next-N-unread buttons; "all" replaces sizes that would take every unread anyway."""
        unread = target.unread_count
        buttons = [
            InlineKeyboardButton(f"📬 {n}", callback_data=target.mode_data(f"u{n}"))
            for n in self.cfg.message_counts
            if n < unread
        ]
        if unread <= self.cfg.max_messages:
            buttons.append(
                InlineKeyboardButton(
                    f"📬 Все {unread}", callback_data=target.mode_data(f"u{self.cfg.max_messages}")
                )
            )
        return buttons

    def _last_buttons(self, target: Target) -> list[InlineKeyboardButton]:
        return [
            InlineKeyboardButton(f"🕘 {n}", callback_data=target.mode_data(str(n)))
            for n in self.cfg.message_counts
        ]

    # ------------------------------------------------------------------ handlers

    async def cmd_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/start and /chats: show chats with unread messages (all chats if none)."""
        if not self._authorized(update) or update.effective_message is None:
            return
        msg = await update.effective_message.reply_text("⏳ Загружаю список чатов…")
        try:
            await self._ensure_dialogs()
        except Exception as e:
            await msg.edit_text(self._error_text(e), parse_mode=ParseMode.HTML)
            return
        flt = FILTER_UNREAD if self._filtered(FILTER_UNREAD, "") else FILTER_ALL
        self._remember_back(context, f"l:{flt}:0")
        text, kb = self.list_screen(flt, 0)
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def on_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Any plain text: search chats by title."""
        message = update.effective_message
        if not self._authorized(update) or message is None or not message.text:
            return
        query = message.text.strip()[:SEARCH_MAX]
        if not query:
            return
        if context.user_data is not None:
            context.user_data["query"] = query
        try:
            await self._ensure_dialogs()
        except Exception as e:
            await message.reply_text(self._error_text(e), parse_mode=ParseMode.HTML)
            return
        self._remember_back(context, "l:s:0")
        text, kb = self.list_screen(FILTER_SEARCH, 0, query)
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Route inline button presses."""
        query = update.callback_query
        if query is None or not query.data:
            return
        if not self._authorized(update):
            await query.answer()
            return
        kind, _, rest = query.data.partition(":")
        try:
            if kind == "l":
                await self._show_list(update, context, rest, reload=False)
            elif kind == "r":
                await self._show_list(update, context, f"{rest}:0", reload=True)
            elif kind == "c":
                chat_id, _, page = rest.partition(":")
                await self._show_chat(update, context, int(chat_id), int(page or 0))
            elif kind == "t":
                chat_id, _, topic = rest.partition(":")
                await self._show_topic(update, context, int(chat_id), int(topic))
            elif kind == "m":
                await self._start_job(update, context, *self._parse_job(rest))
            else:
                await query.answer()
        except ValueError:
            await query.answer("Кнопка устарела, открой /start", show_alert=True)

    @staticmethod
    def _parse_job(rest: str) -> tuple[int, str, Optional[int]]:
        """'<chat_id>:<mode>[:<topic_id>]' → (chat_id, mode, topic_id)."""
        parts = rest.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(rest)
        return int(parts[0]), parts[1], int(parts[2]) if len(parts) == 3 else None

    async def _show_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, rest: str, reload: bool
    ) -> None:
        query = update.callback_query
        assert query is not None
        flt, _, page_s = rest.partition(":")
        if flt not in (FILTER_UNREAD, FILTER_READ, FILTER_ALL, FILTER_SEARCH):
            raise ValueError(flt)
        page = int(page_s or 0)
        search = (context.user_data or {}).get("query", "")
        if flt == FILTER_SEARCH and not search:
            flt = FILTER_ALL
        await query.answer("Обновляю…" if reload else None)
        try:
            await self._ensure_dialogs(force=reload)
        except Exception as e:
            await self._edit(query, self._error_text(e), self._retry_keyboard(f"r:{flt}"))
            return
        self._remember_back(context, f"l:{flt}:{page}")
        text, kb = self.list_screen(flt, page, search)
        await self._edit(query, text, kb)

    async def _show_chat(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, page: int = 0
    ) -> None:
        query = update.callback_query
        assert query is not None
        dialog = await self._find_dialog(chat_id)
        if dialog is None:
            await query.answer("Чат не найден, обнови список", show_alert=True)
            return
        await query.answer()
        if not dialog.forum:
            text, kb = self.chat_screen(dialog, self._back(context))
            await self._edit(query, text, kb)
            return
        try:
            topics = await self._ensure_topics(dialog)
        except Exception as e:
            self.logger.error(f"Topics of {dialog.title!r} failed to load: {e}", exc_info=True)
            await self._edit(query, self._error_text(e), self._retry_keyboard(f"c:{chat_id}"))
            return
        text, kb = self.forum_screen(dialog, topics, page, self._back(context))
        await self._edit(query, text, kb)

    async def _show_topic(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, topic_id: int
    ) -> None:
        query = update.callback_query
        assert query is not None
        target = await self._find_target(chat_id, topic_id)
        if target is None:
            await query.answer("Ветка не найдена, открой чат заново", show_alert=True)
            return
        await query.answer()
        text, kb = self.chat_screen(target.dialog, self._back(context), target.topic)
        await self._edit(query, text, kb)

    async def _find_target(self, chat_id: int, topic_id: Optional[int]) -> Optional[Target]:
        dialog = await self._find_dialog(chat_id)
        if dialog is None or topic_id is None:
            return Target(dialog) if dialog else None
        if not dialog.forum:
            return None
        try:
            topics = await self._ensure_topics(dialog)
        except Exception as e:
            self.logger.error(f"Topics of {dialog.title!r} failed to load: {e}")
            return None
        topic = next((t for t in topics if t.id == topic_id), None)
        return Target(dialog, topic) if topic else None

    async def _find_dialog(self, chat_id: int) -> Optional[DialogInfo]:
        if chat_id not in self._by_id:  # e.g. the bot restarted since the button was sent
            try:
                await self._ensure_dialogs(force=self._loaded_at is not None)
            except Exception as e:
                self.logger.error(f"Dialog reload failed: {e}")
                return None
        return self._by_id.get(chat_id)

    async def _start_job(  # pylint: disable=too-many-positional-arguments
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        mode_s: str,
        topic_id: Optional[int] = None,
    ) -> None:
        query = update.callback_query
        assert query is not None and query.message is not None
        if mode_s.startswith("u"):
            mode, limit = MODE_UNREAD, int(mode_s[1:])
        else:
            mode, limit = MODE_LAST, int(mode_s)
        if not 1 <= limit <= self.cfg.max_messages:
            raise ValueError(mode_s)
        if self._busy is not None:
            await query.answer(f"Уже делаю саммари «{self._busy}», подожди", show_alert=True)
            return
        self._busy = "…"  # claim the slot before any await: updates are handled concurrently
        try:
            target = await self._find_target(chat_id, topic_id)
            if target is None:
                self._busy = None
                await query.answer("Чат не найден, обнови список", show_alert=True)
                return
            self._busy = target.name
            await query.answer()
            back = self._back(context)
            chat = query.message.chat.id
            status = StatusMessage(
                context.bot, chat, query.message.message_id, target.header, self.logger
            )
            context.application.create_task(
                self._run_job(context.bot, chat, status, target, mode, limit, back),
                update=update,
            )
        except BaseException:
            self._busy = None
            raise

    # ------------------------------------------------------------------ the job

    async def _run_job(  # pylint: disable=too-many-positional-arguments
        self,
        bot,
        chat_id: int,
        status: StatusMessage,
        target: Target,
        mode: str,
        limit: int,
        back: str,
    ) -> None:
        again = "🔁 Эта ветка" if target.topic else "🔁 Этот чат"
        nav_row = [
            InlineKeyboardButton(again, callback_data=target.screen_data),
            InlineKeyboardButton("📋 К списку", callback_data=back),
        ]
        done_kb = InlineKeyboardMarkup([nav_row])
        try:
            what = "непрочитанные по порядку" if mode == MODE_UNREAD else f"последние {limit}"
            await status.stage(f"📥 Загружаю {what}…", force=True)
            status.start_heartbeat()

            async def on_fetch(done: int, expected: int) -> None:
                await status.stage(f"📥 Загружаю сообщения: {done} из ~{expected}")

            fetched = await self.reader.fetch(
                target.dialog, mode, limit, progress=on_fetch, topic=target.topic
            )
            if not fetched.messages:
                await self._finish_empty(bot, chat_id, status, target, fetched, nav_row)
                return

            count = len(fetched.messages)
            await status.stage(f"🧠 Анализирую {messages_word(count)}…", force=True)

            chunked = False

            async def on_summary(stage: str, done: int, total: int) -> None:
                nonlocal chunked
                if stage == STAGE_MAP:
                    chunked = True
                    await status.stage(
                        f"🧠 {messages_word(count)}: готово частей {done} из {total}", force=True
                    )
                elif chunked:
                    await status.stage("🧠 Собираю итоговое саммари…", force=True)

            summarizer = self._make_summarizer()
            summary = await summarizer.summarize(
                target.summary_dialog(), fetched.messages, progress=on_summary
            )

            intro = self._summary_intro(target, mode, count, summary, fetched)
            unread = mode == MODE_UNREAD
            await self._send_summary(
                bot, chat_id, intro, summary.text, summary.links, None if unread else done_kb
            )
            await status.finish(
                f"✅ Готово: {messages_word(count)} за {status.elapsed} c. Саммари ниже ⬇️"
            )
            if unread:
                await self._mark_read(bot, chat_id, target, fetched, nav_row)
        except TelegramSessionError as e:
            self.logger.error(f"Telegram session problem: {e}")
            await status.finish(self._error_text(e), done_kb)
        except Exception as e:
            self.logger.error(f"Chat summary failed for {target.name!r}: {e}", exc_info=True)
            await status.finish(self._error_text(e), done_kb)
        finally:
            await status.stop()
            self._busy = None

    async def _finish_empty(  # pylint: disable=too-many-positional-arguments
        self,
        bot,
        chat_id: int,
        status: StatusMessage,
        target: Target,
        fetched: FetchResult,
        nav_row: list[InlineKeyboardButton],
    ) -> None:
        """Nothing to summarize; an unread batch of joins, pins or own messages is still read."""
        if fetched.mode == MODE_UNREAD and fetched.last_id:
            await status.finish("🤷 Только служебные сообщения, саммари не нужно.")
            await self._mark_read(bot, chat_id, target, fetched, nav_row)
            return
        empty = "Непрочитанных нет." if fetched.mode == MODE_UNREAD else "Сообщений нет."
        await status.finish(f"🤷 {empty}", InlineKeyboardMarkup([nav_row]))

    async def _mark_read(
        self,
        bot,
        chat_id: int,
        target: Target,
        fetched: FetchResult,
        nav_row: list[InlineKeyboardButton],
    ) -> None:
        """Mark the summarized batch as read and offer the next one."""
        rows = [nav_row]
        try:
            left = await self.reader.mark_read(target.dialog, fetched.last_id, topic=target.topic)
        except Exception as e:  # the summary is already delivered; only the counter failed
            self.logger.error(f"Mark read failed for {target.name!r}: {e}", exc_info=True)
            text = (
                "⚠️ Не получилось отметить прочитанным, счётчик не изменился.\n"
                f"{html.escape(str(e))[:300] or type(e).__name__}"
            )
        else:
            if left == 0:
                text = "✔️ Отмечено прочитанным. Непрочитанных больше нет 🎉"
            else:
                text = "✔️ Отмечено прочитанным."
                if left is not None:
                    text += f" Осталось непрочитанных: {left}."
                next_btn = InlineKeyboardButton(
                    f"▶️ Следующие {fetched.limit}",
                    callback_data=target.mode_data(f"u{fetched.limit}"),
                )
                rows.insert(0, [next_btn])
        await self._send_html(bot, chat_id, text, InlineKeyboardMarkup(rows))

    def _summary_intro(
        self,
        target: Target,
        mode: str,
        count: int,
        summary: ChatSummary,
        fetched: FetchResult,
    ) -> str:
        what = "📬 Непрочитанные" if mode == MODE_UNREAD else f"🕘 Последние {fetched.limit}"
        lines = [target.header, f"{what} · {messages_word(count)}"]
        if summary.first_at and summary.last_at:
            start = summary.first_at.astimezone(self._tz).strftime("%d.%m %H:%M")
            end = summary.last_at.astimezone(self._tz).strftime("%d.%m %H:%M")
            lines[-1] += f" · {start} – {end}"
        if fetched.capped:
            lines.append(
                f"Это самые старые из {fetched.unread_total} непрочитанных, "
                "дальше — кнопка «Следующие»."
            )
        return "\n".join(lines)

    async def _send_summary(
        self,
        bot,
        chat_id: int,
        intro: str,
        text: str,
        links: dict[int, str],
        keyboard: Optional[InlineKeyboardMarkup],
    ) -> None:
        parts = split_parts(text) or ["(модель вернула пустой ответ)"]
        for i, part in enumerate(parts):
            body = render_html(part, links)
            if i == 0:
                body = f"{intro}\n\n{body}"
            markup = keyboard if i == len(parts) - 1 else None
            await self._send_html(bot, chat_id, body, markup)

    async def _send_html(
        self, bot, chat_id: int, body: str, markup: Optional[InlineKeyboardMarkup]
    ) -> None:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=body,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
        except BadRequest as e:
            if "parse" not in str(e).lower():
                raise
            self.logger.warning(f"HTML rejected by Telegram ({e}); sending plain text")
            await bot.send_message(
                chat_id=chat_id,
                text=strip_html(body),
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _remember_back(context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
        if context.user_data is not None:
            context.user_data["back"] = data

    @staticmethod
    def _back(context: ContextTypes.DEFAULT_TYPE) -> str:
        return (context.user_data or {}).get("back", "l:u:0")

    @staticmethod
    def _retry_keyboard(data: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить", callback_data=data)]])

    @staticmethod
    def _error_text(error: Exception) -> str:
        if isinstance(error, TelegramSessionError):
            return (
                "❌ Нет доступа к твоему Telegram-аккаунту.\n"
                "Запусти на сервере: <code>python create_session.py</code>"
            )
        detail = html.escape(str(error))[:300] or type(error).__name__
        return f"❌ Не получилось: {html.escape(type(error).__name__)}\n{detail}"

    @staticmethod
    async def _edit(query, text: str, keyboard: Optional[InlineKeyboardMarkup]) -> None:
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                link_preview_options=NO_PREVIEW,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
