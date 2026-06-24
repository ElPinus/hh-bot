import asyncio
import logging
import signal
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import TELEGRAM_BOT_TOKEN, MANUAL_MODE, TG_MONITOR_ENABLED
from db.models import init_db
from bot.handlers import router, set_hh_client
from bot.autopilot import autopilot_loop
from bot.messages_loop import messages_loop
from bot.tg_loop import tg_loop
from parser.hh_client import HHClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    init_db()

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    hh = HHClient()
    try:
        logger.info("Starting hh.ru client (browser)...")
        status = await hh.start()
        set_hh_client(hh)

        if status == "need_login":
            logger.info("No saved session. Use /login in Telegram to authorize.")
        else:
            logger.info("hh.ru session loaded")

        if MANUAL_MODE:
            logger.info(
                "MANUAL_MODE on: autopilot and messages loop disabled. "
                "Bot only drafts cover letters from pasted hh.ru vacancy links; "
                "you send them on hh.ru yourself."
            )
        else:
            logger.info("Starting autopilot...")
            asyncio.create_task(autopilot_loop(bot, hh))

            logger.info("Starting messages loop...")
            asyncio.create_task(messages_loop(bot, hh))

        # Telegram channel monitor runs in BOTH modes — it only notifies
        # about relevant posts (never auto-applies), so it's safe under
        # MANUAL_MODE. Gated on TG_MONITOR_ENABLED + credentials (the loop
        # self-disables if creds/session are missing).
        if TG_MONITOR_ENABLED:
            logger.info("Starting Telegram channel monitor...")
            asyncio.create_task(tg_loop(bot))

        logger.info("Starting bot polling...")
        # drop_pending_updates: discard commands/button-presses that queued
        # while the bot was DOWN. Without this, a stale /login (or any command)
        # accumulated during downtime fires on startup — e.g. a queued /login
        # opened the interactive login browser right after a valid session load
        # and wedged the autopilot into "not logged in".
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        logger.info("Shutting down: closing hh.ru client and bot session")
        await hh.stop()
        await bot.session.close()


def _graceful_exit(signum, frame):
    """Convert SIGTERM into KeyboardInterrupt.

    asyncio.run() unwinds KeyboardInterrupt through main()'s `finally`,
    so hh.stop() gets to close Playwright cleanly. Without this, a
    `taskkill` (SIGTERM) force-terminates the process, orphaning the
    Playwright Node subprocess — it then crashes with EPIPE noise in
    the log. SIGINT (Ctrl+C) already raises KeyboardInterrupt natively.
    """
    raise KeyboardInterrupt()


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGTERM, _graceful_exit)
    except (ValueError, OSError):
        pass  # SIGTERM not settable in this environment
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by signal / Ctrl+C")
