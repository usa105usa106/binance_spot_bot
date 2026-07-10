from __future__ import annotations

import html
import os
import time
from pathlib import Path

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

try:
    from .analysis import analyze
    from .binance_client import BinanceClient
    from .charting import make_chart
    from .config import Config
    from .storage import Storage
except ImportError:  # direct module execution / Railway root
    from analysis import analyze
    from binance_client import BinanceClient
    from charting import make_chart
    from config import Config
    from storage import Storage

TIMEFRAMES = ["15m", "1h", "4h", "1d", "1w"]
START_TIME = time.time()


class TradingBot:
    def __init__(self, config: Config, storage: Storage, binance: BinanceClient):
        self.config = config
        self.storage = storage
        self.binance = binance

    def build_application(self) -> Application:
        app = Application.builder().token(self.config.telegram_token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_router))
        app.add_handler(CallbackQueryHandler(self.callback_router))
        return app

    def main_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 Стакан ордеров", callback_data="stakan_toggle"), InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("🖼 Визуализация", callback_data="visualization"), InlineKeyboardButton("🏓 Пинг", callback_data="ping")],
        ])

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        self.storage.ensure_user(chat_id)
        await update.message.reply_text(
            "Готов к анализу Binance Spot. Напишите тикер, например `btc`, или используйте кнопки.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.main_keyboard(),
        )

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(self.help_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=self.main_keyboard())

    def help_text(self) -> str:
        return (
            "*Команды бота*\n\n"
            "`btc`, `eth`, `sol` — анализ монеты Binance Spot.\n"
            "`new btc` — добавить монету в мониторинг стакана.\n"
            "`del btc` — удалить монету из мониторинга.\n"
            "`stakan on` — включить автоотслеживание стакана каждые 30 минут.\n"
            "`stakan off` — выключить автоотслеживание.\n"
            "`list` — список монет в памяти.\n"
            "`/help` — помощь.\n\n"
            "Кнопки: стакан ордеров, настройки таймфрейма, визуализация, пинг."
        )

    async def text_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()
        if not text:
            return
        lower = text.lower()

        if lower == "list":
            symbols = self.storage.list_symbols(chat_id)
            await update.message.reply_text("Монеты в памяти: " + (", ".join(symbols) if symbols else "пусто"))
            return

        if lower.startswith("stakan "):
            arg = lower.split(maxsplit=1)[1]
            enabled = arg in {"on", "вкл", "1", "true"}
            self.storage.set_stakan(chat_id, enabled)
            await update.message.reply_text("Стакан ордеров: " + ("включен" if enabled else "выключен"), reply_markup=self.main_keyboard())
            return

        if lower.startswith("new "):
            await self.add_coin(update, lower.split(maxsplit=1)[1])
            return

        if lower.startswith("del "):
            await self.del_coin(update, lower.split(maxsplit=1)[1])
            return

        await self.send_analysis(update, text)

    async def add_coin(self, update: Update, coin: str) -> None:
        chat_id = update.effective_chat.id
        try:
            symbol = self.binance.normalize_symbol(coin, self.config.default_quote)
            self.storage.add_symbol(chat_id, symbol)
            await update.message.reply_text(f"Добавлено в память: `{symbol}`", parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            await update.message.reply_text(f"Не удалось добавить: {html.escape(str(exc))}")

    async def del_coin(self, update: Update, coin: str) -> None:
        chat_id = update.effective_chat.id
        try:
            symbol = self.binance.normalize_symbol(coin, self.config.default_quote)
        except Exception:
            symbol = coin.strip().upper()
            if not symbol.endswith(self.config.default_quote):
                symbol += self.config.default_quote
        self.storage.del_symbol(chat_id, symbol)
        await update.message.reply_text(f"Удалено из памяти: `{symbol}`", parse_mode=ParseMode.MARKDOWN)

    async def send_analysis(self, update: Update, coin: str) -> None:
        chat_id = update.effective_chat.id
        user = self.storage.get_user(chat_id)
        try:
            symbol = self.binance.normalize_symbol(coin, self.config.default_quote)
            order_book = self.binance.order_book(symbol, self.config.orderbook_limit)
            klines = self.binance.klines(symbol, user["timeframe"], 180)
            result = analyze(symbol, user["timeframe"], order_book, klines)
            chart = make_chart(result)
            caption = result.text

            if user["visualization"] == "split":
                await update.message.reply_photo(photo=chart.open("rb"))
                await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=self.main_keyboard())
            else:
                await update.message.reply_photo(photo=chart.open("rb"), caption=caption[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=self.main_keyboard())
                if len(caption) > 1024:
                    await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)
            Path(chart).unlink(missing_ok=True)
        except Exception as exc:
            await update.message.reply_text(f"Ошибка анализа: {html.escape(str(exc))}", reply_markup=self.main_keyboard())

    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat_id
        data = query.data
        self.storage.ensure_user(chat_id)

        if data == "settings":
            user = self.storage.get_user(chat_id)
            rows = [[InlineKeyboardButton(("✅ " if tf == user["timeframe"] else "") + tf, callback_data=f"tf:{tf}") for tf in TIMEFRAMES[:3]],
                    [InlineKeyboardButton(("✅ " if tf == user["timeframe"] else "") + tf, callback_data=f"tf:{tf}") for tf in TIMEFRAMES[3:]],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back")]]
            await query.edit_message_text("Выберите таймфрейм:", reply_markup=InlineKeyboardMarkup(rows))
            return

        if data.startswith("tf:"):
            tf = data.split(":", 1)[1]
            if tf in TIMEFRAMES:
                self.storage.set_timeframe(chat_id, tf)
            await query.edit_message_text(f"Таймфрейм установлен: {tf}", reply_markup=self.main_keyboard())
            return

        if data == "visualization":
            user = self.storage.get_user(chat_id)
            mode = user["visualization"]
            rows = [
                [InlineKeyboardButton(("✅ " if mode == "combined" else "") + "Одно сообщение: картинка + анализ", callback_data="vis:combined")],
                [InlineKeyboardButton(("✅ " if mode == "split" else "") + "Два сообщения: график сверху + текст", callback_data="vis:split")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
            ]
            await query.edit_message_text("Режим визуализации:", reply_markup=InlineKeyboardMarkup(rows))
            return

        if data.startswith("vis:"):
            mode = data.split(":", 1)[1]
            self.storage.set_visualization(chat_id, mode)
            label = "одно сообщение" if mode == "combined" else "два сообщения"
            await query.edit_message_text(f"Визуализация установлена: {label}", reply_markup=self.main_keyboard())
            return

        if data == "stakan_toggle":
            user = self.storage.get_user(chat_id)
            enabled = not bool(user["stakan_enabled"])
            self.storage.set_stakan(chat_id, enabled)
            await query.edit_message_text("Стакан ордеров: " + ("включен" if enabled else "выключен"), reply_markup=self.main_keyboard())
            return

        if data == "ping":
            await query.edit_message_text(self.ping_text(), reply_markup=self.main_keyboard())
            return

        if data == "back":
            await query.edit_message_text("Главное меню", reply_markup=self.main_keyboard())

    def ping_text(self) -> str:
        process = psutil.Process(os.getpid())
        memory_mb = process.memory_info().rss / 1024 / 1024
        uptime = int(time.time() - START_TIME)
        hours, rem = divmod(uptime, 3600)
        minutes, seconds = divmod(rem, 60)
        started = time.perf_counter()
        try:
            self.binance._get("/api/v3/ping")
            response_ms = (time.perf_counter() - started) * 1000
            status = "OK"
        except Exception:
            response_ms = -1
            status = "ERROR"
        return (
            f"🏓 Binance ping: {status}\n"
            f"⏱ Отклик: {response_ms:.0f} ms\n"
            f"🧠 Memory: {memory_mb:.1f} MB\n"
            f"⏳ Uptime: {hours}h {minutes}m {seconds}s\n"
            f"🔖 Version: {self.config.bot_version}"
        )
