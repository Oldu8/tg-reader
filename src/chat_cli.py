"""
Command-line check of chat summaries, without the bot.

    python -m src.chat_cli list [--unread]
    python -m src.chat_cli summarize "part of chat name" [--last 100 | --unread]
"""

import argparse
import asyncio
import sys
import time

from src.chat_reader import MODE_LAST, MODE_UNREAD, ChatReader, DialogInfo
from src.chat_summarizer import ChatSummarizer
from src.config_loader import load_config
from src.tg_render import render_plain
from src.utils import setup_logging


def _pick(dialogs: list[DialogInfo], query: str) -> DialogInfo:
    q = query.casefold()
    exact = [d for d in dialogs if d.title.casefold() == q]
    matches = exact or [d for d in dialogs if q in d.title.casefold()]
    if not matches:
        sys.exit(f"No chat matches {query!r}")
    if len(matches) > 1:
        names = "\n  ".join(f"{d.title} ({d.id})" for d in matches[:15])
        sys.exit(f"{len(matches)} chats match {query!r}, be more specific:\n  {names}")
    return matches[0]


async def _list(reader: ChatReader, unread_only: bool) -> None:
    dialogs = await reader.list_dialogs()
    if unread_only:
        dialogs = [d for d in dialogs if d.unread_count]
    for d in dialogs:
        unread = f"  unread={d.unread_count}" if d.unread_count else ""
        print(f"{d.id:>16}  {d.kind:<10}  {d.title}{unread}")
    print(f"\n{len(dialogs)} chats")


async def _summarize(config, logger, reader: ChatReader, query: str, mode: str, limit: int):
    dialog = _pick(await reader.list_dialogs(), query)
    started = time.monotonic()

    async def on_fetch(done: int, expected: int) -> None:
        print(f"  fetched {done}/~{expected}", file=sys.stderr)

    fetched = await reader.fetch(dialog, mode, limit, progress=on_fetch)
    print(f"{dialog.title}: {len(fetched.messages)} messages", file=sys.stderr)
    if not fetched.messages:
        return

    async def on_summary(stage: str, done: int, total: int) -> None:
        print(f"  {stage}: {done}/{total}", file=sys.stderr)

    summary = await ChatSummarizer(config, logger).summarize(
        dialog, fetched.messages, progress=on_summary
    )
    print(render_plain(summary.text, summary.links))
    print(
        f"\n[{summary.message_count} messages, {summary.parts} part(s), "
        f"{time.monotonic() - started:.0f}s]",
        file=sys.stderr,
    )


def main() -> None:
    """Parse arguments and run the chosen command."""
    parser = argparse.ArgumentParser(prog="python -m src.chat_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    list_cmd = sub.add_parser("list", help="list chats of the account")
    list_cmd.add_argument("--unread", action="store_true", help="only chats with unread")
    sum_cmd = sub.add_parser("summarize", help="summarize one chat")
    sum_cmd.add_argument("query", help="chat title or part of it")
    group = sum_cmd.add_mutually_exclusive_group()
    group.add_argument("--last", type=int, default=100, help="last N messages (default 100)")
    group.add_argument(
        "--unread", action="store_true", help="oldest unread first (not marked as read)"
    )
    args = parser.parse_args()

    config = load_config()
    logger = setup_logging(config.log_level)
    reader = ChatReader(config, logger)
    if args.command == "list":
        asyncio.run(_list(reader, args.unread))
        return
    mode = MODE_UNREAD if args.unread else MODE_LAST
    limit = config.chat_summary.max_messages if args.unread else args.last
    if not 1 <= limit <= config.chat_summary.max_messages:
        sys.exit(f"--last must be between 1 and {config.chat_summary.max_messages}")
    asyncio.run(_summarize(config, logger, reader, args.query, mode, limit))


if __name__ == "__main__":
    main()
