from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from telegram.constants import ParseMode
from telegram.ext import Application

try:
    from .analysis import analyze, compact_orderbook_signature, signature_change_percent
    from .binance_client import BinanceClient
    from .charting import make_chart
    from .config import Config
    from .storage import Storage
except ImportError:  # direct module execution / Railway root
    from analysis import analyze, compact_orderbook_signature, signature_change_percent
    from binance_client import BinanceClient
    from charting import make_chart
    from config import Config
    from storage import Storage

logger = logging.getLogger("trading-bot.monitor")


async def _monitor_one_symbol(
    app: Application,
    config: Config,
    storage: Storage,
    binance: BinanceClient,
    chat_id: int,
    symbol: str,
    user: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        # Задача могла ждать свободный слот: перед запросом повторно проверяем, что авто всё ещё включено.
        current_user = storage.get_user(chat_id)
        if (
            symbol not in storage.list_symbols(chat_id)
            or not bool(current_user.get("stakan_enabled"))
            or not bool(current_user.get("bot_enabled", 1))
        ):
            logger.info("AUTO_SCAN_SKIPPED_DISABLED chat=%s symbol=%s", chat_id, symbol)
            return

        started_at = int(time.time())
        interval_minutes = max(1, int(current_user.get("monitor_interval_minutes") or config.monitor_interval_minutes))
        storage.mark_monitor_attempt(chat_id, symbol, started_at)
        try:
            old, _ = storage.get_snapshot_meta(chat_id, symbol)
            order_book = await asyncio.to_thread(binance.order_book, symbol, config.orderbook_limit)
            # Тикер могли удалить, пока сетевой запрос выполнялся. Не воскрешаем удалённое состояние.
            if symbol not in storage.list_symbols(chat_id):
                logger.info("AUTO_SCAN_SKIPPED_REMOVED chat=%s symbol=%s", chat_id, symbol)
                return
            signature = compact_orderbook_signature(order_book)
            completed_at = int(time.time())
            current_user = storage.get_user(chat_id)
            interval_minutes = max(1, int(current_user.get("monitor_interval_minutes") or config.monitor_interval_minutes))
            storage.set_snapshot(chat_id, symbol, signature, completed_at)
            # Держим заданный ритм от начала проверки, а не накапливаем задержку сети каждый цикл.
            next_check_at = max(started_at + interval_minutes * 60, completed_at + 5)
            storage.mark_monitor_success(chat_id, symbol, completed_at, next_check_at)
            logger.info(
                "AUTO_SCAN_OK chat=%s symbol=%s interval=%sm duration=%ss next=%s",
                chat_id, symbol, interval_minutes, completed_at - started_at, next_check_at,
            )

            if not old:
                return
            change = signature_change_percent(old, signature)
            if change < config.strong_change_threshold:
                return
            if not bool(current_user.get("stakan_enabled")) or not bool(current_user.get("bot_enabled", 1)):
                return

            timeframe = current_user.get("timeframe", "1h")
            klines = await asyncio.to_thread(binance.klines, symbol, timeframe, 180)
            result = await asyncio.to_thread(analyze, symbol, timeframe, order_book, klines)
            text = "🚨 *Сильное изменение стакана*\n" f"Изменение ликвидности: *{change:.1f}%*\n\n" + result.text
            chart = await asyncio.to_thread(make_chart, result, text if current_user.get("visualization") == "split" else None)
            try:
                with chart.open("rb") as photo:
                    if current_user.get("visualization") == "split":
                        await app.bot.send_photo(chat_id=chat_id, photo=photo)
                    else:
                        await app.bot.send_photo(
                            chat_id=chat_id,
                            photo=photo,
                            caption=text[:1024],
                            parse_mode=ParseMode.MARKDOWN,
                        )
                if current_user.get("visualization") != "split" and len(text) > 1024:
                    await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
            finally:
                Path(chart).unlink(missing_ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if symbol not in storage.list_symbols(chat_id):
                logger.info("AUTO_SCAN_ERROR_IGNORED_REMOVED chat=%s symbol=%s error=%r", chat_id, symbol, exc)
                return
            state = storage.get_monitor_state(chat_id, symbol)
            previous_failures = int(state.get("consecutive_failures") or 0)
            retry_delay = min(300, 30 * (2 ** min(previous_failures, 4)))
            retry_at = int(time.time()) + retry_delay
            failures = storage.mark_monitor_failure(chat_id, symbol, int(time.time()), retry_at, repr(exc))
            logger.exception(
                "AUTO_SCAN_ERROR chat=%s symbol=%s failures=%s retry=%ss",
                chat_id, symbol, failures, retry_delay,
            )


async def monitor_orderbooks(app: Application, config: Config, storage: Storage, binance: BinanceClient) -> None:
    """Точный неблокирующий планировщик с отдельным next_check_at для каждого тикера."""
    semaphore = asyncio.Semaphore(4)
    in_flight: dict[tuple[int, str], asyncio.Task] = {}
    max_in_flight = 40
    logger.info("Order book monitor loop running")

    def finish_job(key: tuple[int, str], task: asyncio.Task) -> None:
        in_flight.pop(key, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "AUTO_MONITOR_JOB_CRASH chat=%s symbol=%s error=%r",
                key[0], key[1], error,
                exc_info=(type(error), error, error.__traceback__),
            )

    try:
        while True:
            try:
                now = int(time.time())
                capacity = max_in_flight - len(in_flight)
                if capacity > 0:
                    for chat_id, symbols in storage.enabled_watchlists():
                        if capacity <= 0:
                            break
                        user = storage.get_user(chat_id)
                        for symbol in symbols:
                            if capacity <= 0:
                                break
                            key = (chat_id, symbol)
                            if key in in_flight:
                                continue
                            state = storage.get_monitor_state(chat_id, symbol)
                            if int(state.get("next_check_at") or 0) > now:
                                continue
                            task = asyncio.create_task(
                                _monitor_one_symbol(app, config, storage, binance, chat_id, symbol, user, semaphore),
                                name=f"orderbook-{chat_id}-{symbol}",
                            )
                            in_flight[key] = task
                            task.add_done_callback(lambda done, job_key=key: finish_job(job_key, done))
                            capacity -= 1
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AUTO_MONITOR_LOOP_ERROR; loop continues in 5 seconds")
                await asyncio.sleep(5)
    finally:
        tasks = list(in_flight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Order book monitor cancelled")


async def monitor_supervisor(app: Application, config: Config, storage: Storage, binance: BinanceClient) -> None:
    while True:
        try:
            await monitor_orderbooks(app, config, storage, binance)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AUTO_MONITOR_CRASH; restarting in 5 seconds")
            await asyncio.sleep(5)
