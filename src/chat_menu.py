"""
Bot navigation for on-demand chat summaries.

Flow: /start → list of chats (unread first, all, or search by sending text)
→ chat screen → mode (unread / last N) → live status message → summary.

Callback data (Telegram allows 64 bytes):
    l:<f>:<page>   chat list, f = u (unread) | a (all) | s (search results)
    r:<f>          reload dialogs from Telegram, then show list f
    c:<chat_id>    chat screen
    m:<chat_id>:<mode>   summarize, mode = u (unread) | <N> (last N)
    x:             no-op (page counter button)
"""

import asyncio
import html
import logging
import math
import time
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
    """Emoji for the chat kind (groups and supergroups share one)."""
    return _ICONS.get(dialog.kind, "👥")


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
        self._busy: Optional[str] = None  # title of the chat being summarized

    # ------------------------------------------------------------------ wiring

    def register(self, app: Application) -> None:
        """Add the menu handlers to the bot application."""
        app.add_handler(CommandHandler(["start", "chats"], self.cmd_chats))
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^[lrcmx]:"))
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

    def _filtered(self, flt: str, query: str) -> list[DialogInfo]:
        if flt == FILTER_UNREAD:
            return [d for d in self._dialogs if d.unread_count > 0]
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

        unread_mark = "• " if flt == FILTER_UNREAD else ""
        all_mark = "• " if flt == FILTER_ALL else ""
        rows = [
            [
                InlineKeyboardButton(f"{unread_mark}📬 Непрочитанные", callback_data="l:u:0"),
                InlineKeyboardButton(f"{all_mark}📋 Все", callback_data="l:a:0"),
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

    def chat_screen(self, d: DialogInfo, back: str) -> tuple[str, InlineKeyboardMarkup]:
        """Text and keyboard of the mode picker for one chat."""
        kind = _KIND_NAMES.get(d.kind, "группа")
        lines = [f"{icon(d)} <b>{html.escape(d.title)}</b>", f"Тип: {kind}"]
        if d.archived:
            lines[-1] += " · в архиве"
        lines.append(f"Непрочитанных: {d.unread_count}")
        if not d.linkable:
            lines.append(
                "\nℹ️ Ссылок на сообщения не будет: Telegram не даёт их "
                "для личных чатов и обычных групп."
            )
        lines.append("\nЧто суммировать?")

        rows = []
        if d.unread_count:
            cap = self.cfg.max_messages
            shown = f"{cap}+" if d.unread_count > cap else str(d.unread_count)
            rows.append(
                [InlineKeyboardButton(f"📬 Непрочитанные ({shown})", callback_data=f"m:{d.id}:u")]
            )
        rows.append(
            [
                InlineKeyboardButton(f"Последние {n}", callback_data=f"m:{d.id}:{n}")
                for n in self.cfg.message_counts
            ]
        )
        rows.append([InlineKeyboardButton("⬅️ К списку", callback_data=back)])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

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
                await self._show_chat(update, context, int(rest))
            elif kind == "m":
                chat_id, _, mode = rest.rpartition(":")
                await self._start_job(update, context, int(chat_id), mode)
            else:
                await query.answer()
        except ValueError:
            await query.answer("Кнопка устарела, открой /start", show_alert=True)

    async def _show_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, rest: str, reload: bool
    ) -> None:
        query = update.callback_query
        assert query is not None
        flt, _, page_s = rest.partition(":")
        if flt not in (FILTER_UNREAD, FILTER_ALL, FILTER_SEARCH):
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
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int
    ) -> None:
        query = update.callback_query
        assert query is not None
        dialog = await self._find_dialog(chat_id)
        if dialog is None:
            await query.answer("Чат не найден, обнови список", show_alert=True)
            return
        await query.answer()
        text, kb = self.chat_screen(dialog, self._back(context))
        await self._edit(query, text, kb)

    async def _find_dialog(self, chat_id: int) -> Optional[DialogInfo]:
        if chat_id not in self._by_id:  # e.g. the bot restarted since the button was sent
            try:
                await self._ensure_dialogs(force=self._loaded_at is not None)
            except Exception as e:
                self.logger.error(f"Dialog reload failed: {e}")
                return None
        return self._by_id.get(chat_id)

    async def _start_job(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, mode_s: str
    ) -> None:
        query = update.callback_query
        assert query is not None and query.message is not None
        if mode_s == "u":
            mode, limit = MODE_UNREAD, self.cfg.max_messages
        else:
            mode, limit = MODE_LAST, int(mode_s)
            if not 1 <= limit <= self.cfg.max_messages:
                raise ValueError(mode_s)
        if self._busy is not None:
            await query.answer(f"Уже делаю саммари «{self._busy}», подожди", show_alert=True)
            return
        self._busy = "…"  # claim the slot before any await: updates are handled concurrently
        try:
            dialog = await self._find_dialog(chat_id)
            if dialog is None:
                self._busy = None
                await query.answer("Чат не найден, обнови список", show_alert=True)
                return
            self._busy = dialog.title
            await query.answer()
            back = self._back(context)
            header = f"{icon(dialog)} <b>{html.escape(dialog.title)}</b>"
            chat = query.message.chat.id
            status = StatusMessage(context.bot, chat, query.message.message_id, header, self.logger)
            context.application.create_task(
                self._run_job(context.bot, chat, status, dialog, mode, limit, back),
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
        dialog: DialogInfo,
        mode: str,
        limit: int,
        back: str,
    ) -> None:
        done_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔁 Этот чат", callback_data=f"c:{dialog.id}"),
                    InlineKeyboardButton("📋 К списку", callback_data=back),
                ]
            ]
        )
        try:
            what = "непрочитанные" if mode == MODE_UNREAD else f"последние {limit}"
            await status.stage(f"📥 Загружаю {what}…", force=True)
            status.start_heartbeat()

            async def on_fetch(done: int, expected: int) -> None:
                await status.stage(f"📥 Загружаю сообщения: {done} из ~{expected}")

            fetched = await self.reader.fetch(dialog, mode, limit, progress=on_fetch)
            if not fetched.messages:
                empty = "Непрочитанных нет." if mode == MODE_UNREAD else "Сообщений нет."
                await status.finish(f"🤷 {empty}", done_kb)
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
            summary = await summarizer.summarize(dialog, fetched.messages, progress=on_summary)

            intro = self._summary_intro(dialog, mode, count, summary, fetched)
            await self._send_summary(bot, chat_id, intro, summary.text, summary.links, done_kb)
            await status.finish(
                f"✅ Готово: {messages_word(count)} за {status.elapsed} c. Саммари ниже ⬇️"
            )
        except TelegramSessionError as e:
            self.logger.error(f"Telegram session problem: {e}")
            await status.finish(self._error_text(e), done_kb)
        except Exception as e:
            self.logger.error(f"Chat summary failed for {dialog.title!r}: {e}", exc_info=True)
            await status.finish(self._error_text(e), done_kb)
        finally:
            await status.stop()
            self._busy = None

    def _summary_intro(
        self,
        dialog: DialogInfo,
        mode: str,
        count: int,
        summary: ChatSummary,
        fetched: FetchResult,
    ) -> str:
        what = "📬 Непрочитанные" if mode == MODE_UNREAD else f"🕘 Последние {fetched.limit}"
        lines = [
            f"{icon(dialog)} <b>{html.escape(dialog.title)}</b>",
            f"{what} · {messages_word(count)}",
        ]
        if summary.first_at and summary.last_at:
            start = summary.first_at.astimezone(self._tz).strftime("%d.%m %H:%M")
            end = summary.last_at.astimezone(self._tz).strftime("%d.%m %H:%M")
            lines[-1] += f" · {start} – {end}"
        if fetched.capped:
            lines.append(
                f"⚠️ Непрочитанных {fetched.unread_total}, взяты последние {fetched.limit}."
            )
        return "\n".join(lines)

    async def _send_summary(
        self,
        bot,
        chat_id: int,
        intro: str,
        text: str,
        links: dict[int, str],
        keyboard: InlineKeyboardMarkup,
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
