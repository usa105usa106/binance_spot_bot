from __future__ import annotations

import os
import sys
from pathlib import Path

# Railway sometimes starts the app with a working directory that is not added
# to Python imports. This guarantees that /app/bot is importable.
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import asyncio
import logging

from bot.binance_client import BinanceClient
from bot.config import get_config
from bot.monitor import monitor_orderbooks
from bot.storage import Storage
from bot.telegram_bot import TradingBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("trading-bot")


def main() -> None:
    config = get_config()
    storage = Storage()
    binance = BinanceClient(config.binance_base_url)
    bot = TradingBot(config, storage, binance)
    app = bot.build_application()

    async def post_init(application):
        application.create_task(monitor_orderbooks(application, config, storage, binance))
        logger.info("Order book monitor started")

    app.post_init = post_init
    logger.info("Bot version %s starting", config.bot_version)
    app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
