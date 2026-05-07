from __future__ import annotations

import asyncio
import time
from pathlib import Path

from telegram.constants import ParseMode
from telegram.ext import Application

from .analysis import analyze, compact_orderbook_signature, signature_change_percent
from .binance_client import BinanceClient
from .charting import make_chart
from .config import Config
from .storage import Storage


async def monitor_orderbooks(app: Application, config: Config, storage: Storage, binance: BinanceClient) -> None:
    while True:
        await asyncio.sleep(config.monitor_interval_minutes * 60)
        for chat_id, symbols in storage.enabled_watchlists():
            user = storage.get_user(chat_id)
            for symbol in symbols:
                try:
                    order_book = await asyncio.to_thread(binance.order_book, symbol, config.orderbook_limit)
                    signature = compact_orderbook_signature(order_book)
                    old = storage.get_snapshot(chat_id, symbol)
                    storage.set_snapshot(chat_id, symbol, signature, int(time.time()))
                    if not old:
                        continue
                    change = signature_change_percent(old, signature)
                    if change < config.strong_change_threshold:
                        continue

                    klines = await asyncio.to_thread(binance.klines, symbol, user["timeframe"], 180)
                    result = analyze(symbol, user["timeframe"], order_book, klines)
                    chart = make_chart(result)
                    text = "🚨 *Сильное изменение стакана*\n" f"Изменение ликвидности: *{change:.1f}%*\n\n" + result.text
                    await app.bot.send_photo(
                        chat_id=chat_id,
                        photo=chart.open("rb"),
                        caption=text[:1024],
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    if len(text) > 1024:
                        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
                    Path(chart).unlink(missing_ok=True)
                except Exception as exc:
                    await app.bot.send_message(chat_id=chat_id, text=f"Ошибка мониторинга {symbol}: {exc}")
