#!/usr/bin/env python3
"""
tg_brief (Telebrief fork) - AI summaries of your Telegram chats.

Main entry point for the application.
Starts the bot (chat summaries, optional digest commands) and, when channels
are configured, the daily digest scheduler.
"""

import asyncio
import signal
import sys
from contextlib import suppress

from src.bot_commands import BotCommandHandler
from src.config_loader import load_config
from src.mcp_server import build_server
from src.scheduler import DigestScheduler
from src.telegram_session import TelegramSessionError, check_session_file
from src.utils import setup_logging


class TelebriefApp:
    """Main application controller."""

    def __init__(self):
        """Initialize the application."""
        self.config = None
        self.logger = None
        self.scheduler = None
        self.bot_handler = None
        self.mcp = None
        self.mcp_task = None
        self.shutdown_event = asyncio.Event()

    async def initialize(self):
        """Load configuration and set up components."""
        try:
            # Load configuration
            print("Loading configuration...")
            self.config = load_config()

            # Set up logging
            self.logger = setup_logging(self.config.log_level)
            self.logger.info("=" * 70)
            self.logger.info("🚀 TELEBRIEF STARTING")
            self.logger.info("=" * 70)

            # The Telegram user session is needed by every feature; fail fast without it
            check_session_file()

            # Display configuration
            settings = self.config.settings
            chat_cfg = self.config.chat_summary
            self.logger.info(f"Target user: {settings.target_user_id}")
            self.logger.info(f"AI provider: {settings.ai_provider}, model: {settings.ai_model}")
            if chat_cfg.enabled:
                self.logger.info(
                    f"Chat summaries: on (model: {chat_cfg.ai_model or settings.ai_model}, "
                    f"modes: unread + last {chat_cfg.message_counts}, cap {chat_cfg.max_messages})"
                )
            self.logger.info(f"Configured digest channels: {len(self.config.channels)}")
            for ch in self.config.channels:
                self.logger.info(f"  • {ch.name} ({ch.id})")

            # Initialize scheduler (daily digest of configured channels only)
            if self.config.channels:
                self.logger.info(f"Schedule: Daily at {settings.schedule_time} {settings.timezone}")
                self.scheduler = DigestScheduler(self.config, self.logger)
            else:
                self.logger.info("Daily digest: off (no channels configured)")

            # Initialize bot command handler
            self.logger.info("Initializing bot command handler...")
            self.bot_handler = BotCommandHandler(self.config, self.logger, self.scheduler)
            self.bot_handler.setup_application()

            # Initialize MCP server (optional)
            if self.config.mcp.enabled:
                self.logger.info("Initializing MCP server...")
                self.mcp = build_server(self.config, self.logger)

            self.logger.info("✅ Initialization complete")
            return True

        except TelegramSessionError as e:
            print(f"❌ {e}")
            return False

        except FileNotFoundError as e:
            print(f"❌ Configuration error: {e}")
            print("\nPlease ensure:")
            print("1. config.yaml exists (copy config.yaml.example and edit it)")
            print("2. .env file exists with required API credentials (see .env.example)")
            return False

        except ValueError as e:
            print(f"❌ Configuration error: {e}")
            return False

        except Exception as e:
            print(f"❌ Initialization failed: {e}")
            import traceback

            traceback.print_exc()
            return False

    async def run(self):
        """Run the application."""
        # Start scheduler
        if self.scheduler:
            self.logger.info("Starting scheduler...")
            self.scheduler.start()

        # Start bot
        self.logger.info("Starting bot command handler...")
        await self.bot_handler.run()

        # Start MCP server in the same event loop, so it shares the Telegram session
        if self.mcp:
            mcp_cfg = self.config.mcp
            self.logger.info(
                f"Starting MCP server on http://{mcp_cfg.host}:{mcp_cfg.port}{mcp_cfg.path}"
            )
            self.mcp_task = asyncio.create_task(
                self.mcp.run_streamable_http_async(
                    host=mcp_cfg.host,
                    port=mcp_cfg.port,
                    streamable_http_path=mcp_cfg.path,
                )
            )

        self.logger.info("=" * 70)
        self.logger.info("✅ TELEBRIEF IS RUNNING")
        self.logger.info("=" * 70)
        if self.scheduler:
            self.logger.info(f"Next digest: {self.scheduler.get_next_run_time()}")
        self.logger.info("Bot commands: Active")
        if self.mcp_task:
            mcp_cfg = self.config.mcp
            self.logger.info(f"MCP server: http://{mcp_cfg.host}:{mcp_cfg.port}{mcp_cfg.path}")
        self.logger.info("")
        self.logger.info("Available commands in Telegram:")
        if self.config.chat_summary.enabled:
            self.logger.info("  /start, /chats - Pick a chat and summarize it")
        if self.config.channels:
            self.logger.info("  /digest - Generate digest instantly")
        self.logger.info("  /status - Show status")
        self.logger.info("  /help - Show help")
        self.logger.info("")
        self.logger.info("Press Ctrl+C to stop")
        self.logger.info("=" * 70)

        # Wait for shutdown signal
        await self.shutdown_event.wait()

    async def shutdown(self):
        """Graceful shutdown."""
        self.logger.info("=" * 70)
        self.logger.info("🛑 SHUTTING DOWN TELEBRIEF")
        self.logger.info("=" * 70)

        # Stop scheduler
        if self.scheduler:
            self.logger.info("Stopping scheduler...")
            self.scheduler.stop()

        # Stop bot
        if self.bot_handler:
            self.logger.info("Stopping bot...")
            await self.bot_handler.stop()

        # Stop MCP server
        # ponytail: cancelling the task makes uvicorn log a CancelledError traceback on
        # the way out — cosmetic, right after the line below. Build uvicorn.Server here
        # and flip should_exit instead if that log noise ever matters.
        if self.mcp_task:
            self.logger.info("Stopping MCP server...")
            self.mcp_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.mcp_task

        self.logger.info("✅ Shutdown complete")
        self.logger.info("=" * 70)

        # Signal that shutdown is complete
        self.shutdown_event.set()


async def main():
    """Main entry point."""
    app = TelebriefApp()

    # Initialize
    if not await app.initialize():
        sys.exit(1)

    # Set up signal handlers for graceful shutdown. Windows event loops do not support
    # them; there Ctrl+C arrives as KeyboardInterrupt and the finally block shuts down.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(app.shutdown()))
        except NotImplementedError:  # Windows event loops
            break

    try:
        # Run application
        await app.run()

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    except Exception as e:
        app.logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

    finally:
        # Ensure clean shutdown
        if not app.shutdown_event.is_set():
            await app.shutdown()


if __name__ == "__main__":
    print(
        """
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   ████████╗███████╗██╗     ███████╗██████╗ ██████╗     ║
║   ╚══██╔══╝██╔════╝██║     ██╔════╝██╔══██╗██╔══██╗    ║
║      ██║   █████╗  ██║     █████╗  ██████╔╝██████╔╝    ║
║      ██║   ██╔══╝  ██║     ██╔══╝  ██╔══██╗██╔══██╗    ║
║      ██║   ███████╗███████╗███████╗██████╔╝██║  ██║    ║
║      ╚═╝   ╚══════╝╚══════╝╚══════╝╚═════╝ ╚═╝  ╚═╝    ║
║                                                          ║
║        tg_brief: AI summaries of your Telegram          ║
║              (based on Telebrief, MIT)                   ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
    """
    )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nGoodbye! 👋")
