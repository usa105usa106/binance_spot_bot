from __future__ import annotations

import logging

# Single-file Railway version. No local package imports are required.


# ===== bot/config.py =====
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_token: str
    binance_base_url: str = "https://api.binance.com"
    default_quote: str = "USDT"
    orderbook_limit: int = 1000
    monitor_interval_minutes: int = 30
    strong_change_threshold: float = 35.0
    bot_version: str = "00001"


def get_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    return Config(
        telegram_token=token,
        binance_base_url=os.getenv("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/"),
        default_quote=os.getenv("DEFAULT_QUOTE", "USDT").upper(),
        orderbook_limit=int(os.getenv("ORDERBOOK_LIMIT", "1000")),
        monitor_interval_minutes=int(os.getenv("MONITOR_INTERVAL_MINUTES", "30")),
        strong_change_threshold=float(os.getenv("STRONG_CHANGE_THRESHOLD", "35")),
        bot_version=os.getenv("BOT_VERSION", "00001"),
    )

# ===== bot/binance_client.py =====
import time
from typing import Any

import requests


class BinanceAPIError(RuntimeError):
    pass


class BinanceClient:
    def __init__(self, base_url: str, timeout: int = 15):
        self.base_urls = self._build_base_urls(base_url)
        self.base_url = self.base_urls[0]
        self.timeout = timeout
        self.session = requests.Session()
        self._symbols_cache: tuple[float, set[str]] | None = None

    @staticmethod
    def _build_base_urls(base_url: str) -> list[str]:
        configured = [x.strip().rstrip("/") for x in os.getenv("BINANCE_BASE_URLS", "").split(",") if x.strip()]
        defaults = [
            base_url.rstrip("/"),
            "https://api1.binance.com",
            "https://api2.binance.com",
            "https://api3.binance.com",
            "https://api4.binance.com",
            "https://data-api.binance.vision",
        ]
        result: list[str] = []
        for url in configured + defaults:
            if url and url not in result:
                result.append(url)
        return result

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error = "неизвестная ошибка"
        for base_url in self.base_urls:
            url = f"{base_url}{path}"
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code in {451, 403, 418, 429} or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    continue
                response.raise_for_status()
                self.base_url = base_url
                if not response.content:
                    return {}
                return response.json()
            except requests.RequestException as exc:
                last_error = exc.__class__.__name__
                continue
            except ValueError:
                last_error = "некорректный JSON от Binance"
                continue
        raise BinanceAPIError(
            "Binance API сейчас недоступен с этого сервера. "
            "Попробуйте позже или задайте BINANCE_BASE_URLS с доступным зеркалом/API endpoint."
        )

    def exchange_symbols(self) -> set[str]:
        now = time.time()
        if self._symbols_cache and now - self._symbols_cache[0] < 3600:
            return self._symbols_cache[1]
        data = self._get("/api/v3/exchangeInfo")
        symbols = {s["symbol"] for s in data.get("symbols", []) if s.get("status") == "TRADING"}
        self._symbols_cache = (now, symbols)
        return symbols

    def normalize_symbol(self, coin: str, default_quote: str = "USDT") -> str:
        raw = coin.strip().upper().replace("/", "").replace("-", "")
        symbols = self.exchange_symbols()
        if raw in symbols:
            return raw
        candidate = f"{raw}{default_quote.upper()}"
        if candidate in symbols:
            return candidate
        raise ValueError(f"Пара {raw} или {candidate} не найдена на Binance Spot")

    def order_book(self, symbol: str, limit: int = 1000) -> dict[str, Any]:
        return self._get("/api/v3/depth", {"symbol": symbol, "limit": limit})

    def klines(self, symbol: str, interval: str, limit: int = 160) -> list[list[Any]]:
        return self._get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    def ticker_price(self, symbol: str) -> float:
        return float(self._get("/api/v3/ticker/price", {"symbol": symbol})["price"])

    def top_symbols_by_quote_volume(self, quote: str = "USDT", limit: int = 50) -> list[str]:
        symbols = self.exchange_symbols()
        tickers = self._get("/api/v3/ticker/24hr")
        quote = quote.upper()
        rows = []
        for item in tickers:
            symbol = item.get("symbol", "")
            if symbol in symbols and symbol.endswith(quote):
                try:
                    rows.append((symbol, float(item.get("quoteVolume", 0.0)), int(item.get("count", 0))))
                except (TypeError, ValueError):
                    continue
        rows.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return [symbol for symbol, _, _ in rows[:limit]]

# ===== bot/storage.py =====
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, path: str = "bot_data.sqlite3"):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    timeframe TEXT NOT NULL DEFAULT '1h',
                    visualization TEXT NOT NULL DEFAULT 'combined',
                    stakan_enabled INTEGER NOT NULL DEFAULT 0,
                    bot_enabled INTEGER NOT NULL DEFAULT 1,
                    monitor_interval_minutes INTEGER NOT NULL DEFAULT 30
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    PRIMARY KEY(chat_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, symbol)
                );
                """
            )
            # Миграция старой SQLite-базы без потери сохраненных монет.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "bot_enabled" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN bot_enabled INTEGER NOT NULL DEFAULT 1")
            if "monitor_interval_minutes" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN monitor_interval_minutes INTEGER NOT NULL DEFAULT 30")

    def ensure_user(self, chat_id: int) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO users(chat_id) VALUES(?)", (chat_id,))

    def get_user(self, chat_id: int) -> dict[str, Any]:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            return dict(row)

    def set_timeframe(self, chat_id: int, timeframe: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE users SET timeframe=? WHERE chat_id=?", (timeframe, chat_id))

    def set_visualization(self, chat_id: int, mode: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE users SET visualization=? WHERE chat_id=?", (mode, chat_id))

    def set_stakan(self, chat_id: int, enabled: bool) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE users SET stakan_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))

    def set_bot_enabled(self, chat_id: int, enabled: bool) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE users SET bot_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))

    def set_monitor_interval(self, chat_id: int, minutes: int) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE users SET monitor_interval_minutes=? WHERE chat_id=?", (minutes, chat_id))

    def add_symbol(self, chat_id: int, symbol: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES(?,?)", (chat_id, symbol))

    def del_symbol(self, chat_id: int, symbol: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=? AND symbol=?", (chat_id, symbol))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=? AND symbol=?", (chat_id, symbol))

    def clear_symbols(self, chat_id: int) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=?", (chat_id,))

    def replace_symbols(self, chat_id: int, symbols: list[str]) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=?", (chat_id,))
            conn.executemany("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES(?,?)", [(chat_id, s) for s in symbols])

    def list_symbols(self, chat_id: int) -> list[str]:
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id=? ORDER BY symbol", (chat_id,)).fetchall()
            return [r["symbol"] for r in rows]

    def enabled_watchlists(self) -> list[tuple[int, list[str]]]:
        with self.lock, self._connect() as conn:
            users = conn.execute("SELECT chat_id FROM users WHERE stakan_enabled=1 AND bot_enabled=1").fetchall()
            result: list[tuple[int, list[str]]] = []
            for user in users:
                result.append((user["chat_id"], self.list_symbols(user["chat_id"])))
            return result

    def get_snapshot(self, chat_id: int, symbol: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM orderbook_snapshots WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            return json.loads(row["snapshot_json"]) if row else None

    def get_snapshot_meta(self, chat_id: int, symbol: str) -> tuple[dict[str, Any] | None, int | None]:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json, updated_at FROM orderbook_snapshots WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            return (json.loads(row["snapshot_json"]), int(row["updated_at"])) if row else (None, None)

    def set_snapshot(self, chat_id: int, symbol: str, snapshot: dict[str, Any], updated_at: int) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orderbook_snapshots(chat_id, symbol, snapshot_json, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(chat_id, symbol) DO UPDATE SET
                    snapshot_json=excluded.snapshot_json,
                    updated_at=excluded.updated_at
                """,
                (chat_id, symbol, json.dumps(snapshot), updated_at),
            )

# ===== bot/analysis.py =====
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class OrderBookLevel:
    price: float
    volume: float


@dataclass
class TrendLine:
    kind: str  # support or resistance
    start_index: int
    start_price: float
    end_index: int
    end_price: float
    current_value: float
    slope_percent: float
    touches: int
    text: str


@dataclass
class PriceProjection:
    direction: str
    target_1: float
    target_2: float
    invalidation: float
    move_1_percent: float
    move_2_percent: float
    text: str


@dataclass
class AnalysisResult:
    symbol: str
    interval: str
    price: float
    support: OrderBookLevel
    resistance: OrderBookLevel
    fib_levels: dict[str, float]
    long_probability: float
    short_probability: float
    orderbook_bias: float
    trend_bias: float
    recommendation: str
    text: str
    df: pd.DataFrame
    trendline: TrendLine
    projection: PriceProjection


def klines_to_df(klines: list[list[Any]]) -> pd.DataFrame:
    df = pd.DataFrame(
        klines,
        columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_asset_volume", "number_of_trades", "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("date")
    return df[["open", "high", "low", "close", "volume"]]


def _aggregate_levels(levels: list[list[str]], current_price: float, side: str, bins: int = 45) -> OrderBookLevel:
    parsed = np.array([[float(p), float(q)] for p, q in levels if float(q) > 0], dtype=float)
    if parsed.size == 0:
        return OrderBookLevel(current_price, 0.0)
    if side == "bid":
        parsed = parsed[parsed[:, 0] < current_price]
    else:
        parsed = parsed[parsed[:, 0] > current_price]
    if len(parsed) == 0:
        return OrderBookLevel(current_price, 0.0)

    prices = parsed[:, 0]
    quantities = parsed[:, 1]
    notional = prices * quantities
    hist, edges = np.histogram(prices, bins=min(bins, max(8, len(parsed) // 7)), weights=notional)
    idx = int(np.argmax(hist))
    left, right = edges[idx], edges[idx + 1]
    mask = (prices >= left) & (prices <= right)
    cluster_price = float(np.average(prices[mask], weights=notional[mask])) if mask.any() else float(prices[np.argmax(notional)])
    return OrderBookLevel(price=cluster_price, volume=float(hist[idx]))


def fibonacci_levels(df: pd.DataFrame, lookback: int = 160) -> dict[str, float]:
    """Fibonacci от последнего заметного свинга, а не просто от max/min окна.

    Если последний свинг был вверх: 0% = swing low, 100% = swing high.
    Если последний свинг был вниз: 0% = swing high, 100% = swing low.
    Так уровни на графике совпадают с направлением движения.
    """
    window = df.tail(min(lookback, len(df))).reset_index(drop=True)
    if len(window) < 10:
        high = float(window["high"].max())
        low = float(window["low"].min())
        diff = high - low
        return {"0%": low, "23.6%": low + diff * 0.236, "38.2%": low + diff * 0.382, "50%": low + diff * 0.5, "61.8%": low + diff * 0.618, "78.6%": low + diff * 0.786, "100%": high}

    highs = _pivot_points(window["high"], window=3, mode="high")
    lows = _pivot_points(window["low"], window=3, mode="low")
    pivots = sorted([(i, v, "high") for i, v in highs] + [(i, v, "low") for i, v in lows], key=lambda x: x[0])

    start_price: float
    end_price: float
    if len(pivots) >= 2:
        end_i, end_price, end_kind = pivots[-1]
        opposite = "low" if end_kind == "high" else "high"
        prior = [p for p in pivots[:-1] if p[2] == opposite]
        if prior:
            _, start_price, _ = prior[-1]
        else:
            start_price = float(window["low"].min() if end_kind == "high" else window["high"].max())
    else:
        high_idx = int(window["high"].idxmax())
        low_idx = int(window["low"].idxmin())
        if low_idx < high_idx:
            start_price, end_price = float(window.loc[low_idx, "low"]), float(window.loc[high_idx, "high"])
        else:
            start_price, end_price = float(window.loc[high_idx, "high"]), float(window.loc[low_idx, "low"])

    diff = end_price - start_price
    ratios = [("0%", 0.0), ("23.6%", 0.236), ("38.2%", 0.382), ("50%", 0.5), ("61.8%", 0.618), ("78.6%", 0.786), ("100%", 1.0)]
    return {name: float(start_price + diff * ratio) for name, ratio in ratios}

def _pivot_points(series: pd.Series, window: int, mode: str) -> list[tuple[int, float]]:
    values = series.to_numpy(dtype=float)
    pivots: list[tuple[int, float]] = []
    for i in range(window, len(values) - window):
        local = values[i - window:i + window + 1]
        if mode == "low" and values[i] == np.min(local):
            pivots.append((i, float(values[i])))
        if mode == "high" and values[i] == np.max(local):
            pivots.append((i, float(values[i])))
    return pivots


def _build_trendline(df: pd.DataFrame, trend_bias: float) -> TrendLine:
    # При восходящем уклоне строим наклонку по повышающимся минимумам, при нисходящем — по понижающимся максимумам.
    mode = "low" if trend_bias >= 0 else "high"
    kind = "support" if mode == "low" else "resistance"
    label = "наклонная поддержка по минимумам" if mode == "low" else "наклонное сопротивление по максимумам"
    source = df["low"] if mode == "low" else df["high"]
    pivots = _pivot_points(source.tail(140).reset_index(drop=True), window=3, mode=mode)

    if len(pivots) >= 2:
        # Берем две наиболее свежие опорные точки с правильной структурой, иначе последние две.
        chosen = None
        recent = pivots[-8:]
        for a in range(len(recent) - 2, -1, -1):
            p1 = recent[a]
            for p2 in recent[a + 1:]:
                rising_lows = mode == "low" and p2[1] >= p1[1]
                falling_highs = mode == "high" and p2[1] <= p1[1]
                if rising_lows or falling_highs:
                    chosen = (p1, p2)
        if chosen is None:
            chosen = (pivots[-2], pivots[-1])
        (i1, y1), (i2, y2) = chosen
    else:
        # Fallback: линейная регрессия по low/high на видимом участке.
        source_tail = source.tail(140).reset_index(drop=True)
        x = np.arange(len(source_tail), dtype=float)
        slope, intercept = np.polyfit(x, source_tail.to_numpy(dtype=float), 1)
        i1, i2 = 0, len(source_tail) - 1
        y1, y2 = float(intercept), float(intercept + slope * i2)

    if i2 == i1:
        i2 = i1 + 1
    slope = (y2 - y1) / (i2 - i1)
    current_value = float(y1 + slope * ((min(140, len(df)) - 1) - i1))
    slope_percent = float((current_value / max(y1, 1e-9) - 1) * 100)

    # Количество касаний около линии, чтобы показать надежность наклонки.
    tail = df.tail(140).reset_index(drop=True)
    line = np.array([y1 + slope * (i - i1) for i in range(len(tail))], dtype=float)
    tolerance = max(float(tail["close"].iloc[-1]) * 0.004, 1e-12)
    touches_source = tail["low"].to_numpy(dtype=float) if mode == "low" else tail["high"].to_numpy(dtype=float)
    touches = int(np.sum(np.abs(touches_source - line) <= tolerance))

    return TrendLine(
        kind=kind,
        start_index=int(i1),
        start_price=float(y1),
        end_index=int(len(tail) - 1),
        end_price=float(current_value),
        current_value=current_value,
        slope_percent=slope_percent,
        touches=touches,
        text=f"{label}: {current_value:.6g} ({slope_percent:+.2f}%, касаний: {touches})",
    )


def _trend_score(df: pd.DataFrame) -> float:
    close = df["close"]
    ema_fast = close.ewm(span=12, adjust=False).mean().iloc[-1]
    ema_slow = close.ewm(span=26, adjust=False).mean().iloc[-1]
    momentum = (close.iloc[-1] / close.iloc[-12] - 1.0) * 100 if len(close) >= 12 else 0.0
    ema_score = 1.0 if ema_fast > ema_slow else -1.0
    mom_score = float(np.clip(momentum / 3.0, -1, 1))
    return float(np.clip((ema_score + mom_score) / 2, -1, 1))


def _projection(price: float, fibs: dict[str, float], support: OrderBookLevel, resistance: OrderBookLevel, long_probability: float) -> PriceProjection:
    levels = sorted(set([*fibs.values(), support.price, resistance.price]))
    above = [v for v in levels if v > price]
    below = [v for v in levels if v < price]
    if long_probability >= 50:
        t1 = above[0] if above else price * 1.015
        t2 = above[1] if len(above) > 1 else t1 * 1.012
        inv = below[-1] if below else price * 0.985
        direction = "LONG"
        text = f"При удержании поддержки цель: {t1:.6g} → {t2:.6g}; отмена ниже {inv:.6g}"
    else:
        t1 = below[-1] if below else price * 0.985
        t2 = below[-2] if len(below) > 1 else t1 * 0.988
        inv = above[0] if above else price * 1.015
        direction = "SHORT"
        text = f"При пробое вниз цель: {t1:.6g} → {t2:.6g}; отмена выше {inv:.6g}"
    return PriceProjection(
        direction=direction,
        target_1=float(t1),
        target_2=float(t2),
        invalidation=float(inv),
        move_1_percent=float((t1 / price - 1) * 100),
        move_2_percent=float((t2 / price - 1) * 100),
        text=text,
    )


def analyze(symbol: str, interval: str, order_book: dict[str, Any], klines: list[list[Any]]) -> AnalysisResult:
    df = klines_to_df(klines)
    price = float(df["close"].iloc[-1])
    support = _aggregate_levels(order_book.get("bids", []), price, "bid")
    resistance = _aggregate_levels(order_book.get("asks", []), price, "ask")
    fibs = fibonacci_levels(df)

    bid_notional = sum(float(p) * float(q) for p, q in order_book.get("bids", [])[:300])
    ask_notional = sum(float(p) * float(q) for p, q in order_book.get("asks", [])[:300])
    total = max(bid_notional + ask_notional, 1e-9)
    orderbook_bias = (bid_notional - ask_notional) / total
    trend_bias = _trend_score(df)
    trendline = _build_trendline(df, trend_bias)

    dist_to_support = abs(price - support.price) / price * 100
    dist_to_resistance = abs(resistance.price - price) / price * 100
    structure_bias = np.clip((dist_to_resistance - dist_to_support) / max(dist_to_resistance + dist_to_support, 1e-9), -1, 1)
    trendline_bias = np.clip((price - trendline.current_value) / max(price, 1e-9) * 25, -1, 1) if trendline.kind == "support" else np.clip((trendline.current_value - price) / max(price, 1e-9) * 25, -1, 1)

    combined = 0.40 * orderbook_bias + 0.30 * trend_bias + 0.18 * structure_bias + 0.12 * trendline_bias
    long_probability = float(np.clip(50 + combined * 45, 5, 95))
    short_probability = 100 - long_probability
    projection = _projection(price, fibs, support, resistance, long_probability)

    if long_probability >= 58:
        recommendation = "LONG / покупка от поддержки или после пробоя сопротивления"
    elif short_probability >= 58:
        recommendation = "SHORT / продажа от сопротивления или после пробоя поддержки"
    else:
        recommendation = "NEUTRAL / нет сильного перевеса, ждать подтверждения"

    nearest_fibs = sorted(fibs.items(), key=lambda x: abs(x[1] - price))[:4]
    fib_text = "\n".join([f"• Fib {k}: `{v:.6g}`" for k, v in nearest_fibs])
    text = (
        f"📊 *{symbol}* · TF `{interval}`\n"
        f"Цена: `{price:.6g}`\n\n"
        f"🟢 Поддержка по стакану: `{support.price:.6g}` · ликвидность `{support.volume:,.0f}`\n"
        f"🔴 Сопротивление по стакану: `{resistance.price:.6g}` · ликвидность `{resistance.volume:,.0f}`\n"
        f"📐 Наклонка: `{trendline.text}`\n\n"
        f"📏 Ближайшие Fibonacci:\n{fib_text}\n\n"
        f"📈 Проходимость LONG: *{long_probability:.1f}%*\n"
        f"📉 Проходимость SHORT: *{short_probability:.1f}%*\n"
        f"🎯 Прогноз движения: `{projection.text}`\n"
        f"⚖️ Дисбаланс стакана: `{orderbook_bias * 100:.1f}%`\n"
        f"🧭 Тренд: `{trend_bias * 100:.1f}%`\n\n"
        f"✅ Рекомендация: *{recommendation}*\n\n"
        f"_Не является финансовой рекомендацией. Проверяйте риск-менеджмент._"
    )

    return AnalysisResult(
        symbol=symbol,
        interval=interval,
        price=price,
        support=support,
        resistance=resistance,
        fib_levels=fibs,
        long_probability=long_probability,
        short_probability=short_probability,
        orderbook_bias=orderbook_bias,
        trend_bias=trend_bias,
        recommendation=recommendation,
        text=text,
        df=df,
        trendline=trendline,
        projection=projection,
    )


def compact_orderbook_signature(order_book: dict[str, Any]) -> dict[str, float]:
    bids = order_book.get("bids", [])[:300]
    asks = order_book.get("asks", [])[:300]
    bid_notional = sum(float(p) * float(q) for p, q in bids)
    ask_notional = sum(float(p) * float(q) for p, q in asks)
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0
    return {"bid_notional": bid_notional, "ask_notional": ask_notional, "best_bid": best_bid, "best_ask": best_ask}


def signature_change_percent(old: dict[str, float], new: dict[str, float]) -> float:
    values = []
    for key in ["bid_notional", "ask_notional"]:
        base = max(abs(old.get(key, 0.0)), 1e-9)
        values.append(abs(new.get(key, 0.0) - old.get(key, 0.0)) / base * 100)
    return float(max(values))

# ===== bot/charting.py =====
import tempfile
from pathlib import Path

import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np



def _fmt(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}".rstrip("0")


def make_chart(result: AnalysisResult) -> Path:
    """Создает крупный, читаемый PNG 1920x1080 с уровнями Fib, стаканом, наклонкой и прогнозом."""
    df = result.df.tail(140).copy()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.close()
    out = Path(tmp.name)

    market_colors = mpf.make_marketcolors(up="#14b87a", down="#ef4444", edge="inherit", wick="inherit", volume="inherit")
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=market_colors,
        gridstyle="-",
        gridcolor="#1f2a3a",
        facecolor="#0b1220",
        figcolor="#08111f",
        rc={
            "font.size": 13,
            "axes.labelsize": 13,
            "axes.titlesize": 18,
            "xtick.labelsize": 12,
            "ytick.labelsize": 12,
        },
    )

    fig, axes = mpf.plot(
        df,
        type="candle",
        volume=True,
        style=style,
        returnfig=True,
        figsize=(19.2, 10.8),
        tight_layout=False,
        panel_ratios=(5, 1),
        datetime_format="%d.%m %H:%M",
        xrotation=0,
        warn_too_much_data=300,
    )
    fig.subplots_adjust(left=0.055, right=0.86, top=0.90, bottom=0.11, hspace=0.06)
    ax = axes[0]
    vol_ax = axes[2] if len(axes) > 2 else axes[-1]

    title = f"{result.symbol} · BINANCE SPOT · TF {result.interval} · цена {_fmt(result.price)}"
    ax.set_title(title, loc="left", color="white", pad=18, fontsize=21, fontweight="bold")

    # Горизонтальные уровни стакана.
    ax.axhline(result.support.price, color="#22c55e", linewidth=2.4, linestyle="-", alpha=0.95)
    ax.axhline(result.resistance.price, color="#f43f5e", linewidth=2.4, linestyle="-", alpha=0.95)

    right_x = len(df) - 1
    ax.text(right_x + 1, result.support.price, f"  SUPPORT стакан {_fmt(result.support.price)}", color="#22c55e", va="center", fontsize=12, fontweight="bold")
    ax.text(right_x + 1, result.resistance.price, f"  RESISTANCE стакан {_fmt(result.resistance.price)}", color="#f43f5e", va="center", fontsize=12, fontweight="bold")

    # Fibonacci по выбранному таймфрейму.
    fib_colors = {
        "0%": "#e5e7eb",
        "23.6%": "#f97316",
        "38.2%": "#facc15",
        "50%": "#a3e635",
        "61.8%": "#2dd4bf",
        "78.6%": "#60a5fa",
        "100%": "#e5e7eb",
    }
    for name, level in result.fib_levels.items():
        color = fib_colors.get(name, "#94a3b8")
        ax.axhline(level, color=color, linewidth=1.35, linestyle="--", alpha=0.82)
        ax.text(right_x + 1, level, f"  Fib {name}  {_fmt(level)}", color=color, va="center", fontsize=11, fontweight="bold")

    # Наклонная трендовая линия: сверху при сопротивлении, снизу при поддержке.
    tl = result.trendline
    trend_color = "#22c55e" if tl.kind == "support" else "#f59e0b"
    ax.plot([tl.start_index, tl.end_index], [tl.start_price, tl.end_price], color=trend_color, linewidth=2.8, alpha=0.95)
    ax.scatter([tl.start_index, tl.end_index], [tl.start_price, tl.end_price], color=trend_color, s=55, zorder=5)
    ax.text(max(1, tl.end_index - 30), tl.end_price, f"  {tl.text}", color=trend_color, fontsize=12, fontweight="bold", va="bottom")

    # Прогноз: стрелка и цели движения по ближайшим уровням.
    proj = result.projection
    arrow_color = "#22c55e" if proj.direction == "LONG" else "#ef4444"
    future_x1 = right_x + 8
    future_x2 = right_x + 20
    ax.annotate(
        "",
        xy=(future_x1, proj.target_1),
        xytext=(right_x, result.price),
        arrowprops=dict(arrowstyle="->", color=arrow_color, linewidth=2.6, linestyle="--"),
        annotation_clip=False,
    )
    ax.annotate(
        "",
        xy=(future_x2, proj.target_2),
        xytext=(future_x1, proj.target_1),
        arrowprops=dict(arrowstyle="->", color=arrow_color, linewidth=2.2, linestyle="--"),
        annotation_clip=False,
    )
    ax.text(future_x1, proj.target_1, f"  Цель 1 {_fmt(proj.target_1)} ({proj.move_1_percent:+.2f}%)", color=arrow_color, fontsize=12, fontweight="bold", va="center")
    ax.text(future_x2, proj.target_2, f"  Цель 2 {_fmt(proj.target_2)} ({proj.move_2_percent:+.2f}%)", color=arrow_color, fontsize=12, fontweight="bold", va="center")
    ax.axhline(proj.invalidation, color="#94a3b8", linewidth=1.2, linestyle=":", alpha=0.75)
    ax.text(1, proj.invalidation, f"Отмена сценария: {_fmt(proj.invalidation)}", color="#cbd5e1", fontsize=10, va="center")

    # Информационная панель на графике.
    nearest_fibs = sorted(result.fib_levels.items(), key=lambda x: abs(x[1] - result.price))[:5]
    fib_lines = " | ".join([f"Fib {k}: {_fmt(v)}" for k, v in nearest_fibs])
    info = (
        f"LONG {result.long_probability:.1f}%  |  SHORT {result.short_probability:.1f}%\n"
        f"Рекомендация: {result.recommendation}\n"
        f"Прогноз: {proj.direction} · {proj.text}\n"
        f"Поддержка: {_fmt(result.support.price)} · Сопротивление: {_fmt(result.resistance.price)}\n"
        f"{fib_lines}\n"
        f"Стакан: {result.orderbook_bias * 100:+.1f}% · Тренд: {result.trend_bias * 100:+.1f}% · Наклонка: {tl.text}"
    )
    ax.text(
        0.012, 0.965, info,
        transform=ax.transAxes,
        fontsize=13,
        color="white",
        va="top",
        bbox=dict(boxstyle="round,pad=0.55", facecolor="#0f172a", edgecolor="#334155", alpha=0.92),
    )

    # Чтобы справа поместились подписи и стрелки прогноза.
    ax.set_xlim(-2, len(df) + 28)
    lows = [df["low"].min(), *result.fib_levels.values(), result.support.price, proj.target_1, proj.target_2, proj.invalidation]
    highs = [df["high"].max(), *result.fib_levels.values(), result.resistance.price, proj.target_1, proj.target_2, proj.invalidation]
    ymin, ymax = min(lows), max(highs)
    pad = max((ymax - ymin) * 0.10, result.price * 0.005)
    ax.set_ylim(ymin - pad, ymax + pad)

    ax.set_ylabel("Цена", color="#cbd5e1")
    vol_ax.set_ylabel("Объем", color="#cbd5e1")
    fig.text(0.055, 0.035, "Аналитический сигнал. Не является финансовой рекомендацией.", color="#94a3b8", fontsize=11)

    fig.savefig(out, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out

# ===== bot/telegram_bot.py =====
import html
import os
import time
from pathlib import Path

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters


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

    async def safe_menu_update(self, query, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        """Кнопки должны работать и под текстом, и под фото."""
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=text, reply_markup=reply_markup)
            else:
                await query.edit_message_text(text, reply_markup=reply_markup)
        except BadRequest as exc:
            # Если сообщение без подписи/текст нельзя отредактировать, отправляем новое меню.
            if "Message is not modified" in str(exc):
                return
            await query.message.reply_text(text, reply_markup=reply_markup)

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

        if lower in {"top-50", "top 50", "top50"}:
            await self.load_top_symbols(update, 50)
            return

        if lower in {"top-100", "top 100", "top100"}:
            await self.load_top_symbols(update, 100)
            return

        if lower in {"top-200", "top 200", "top200"}:
            await self.load_top_symbols(update, 200)
            return

        if lower.startswith("bot "):
            arg = lower.split(maxsplit=1)[1]
            enabled = arg in {"on", "вкл", "1", "true"}
            self.storage.set_bot_enabled(chat_id, enabled)
            await update.message.reply_text("Бот: " + ("включен" if enabled else "выключен"), reply_markup=self.main_keyboard())
            return

        if lower.startswith("auto "):
            try:
                minutes = int(lower.split(maxsplit=1)[1])
            except ValueError:
                await update.message.reply_text("Формат: auto 10, auto 30, auto 60 или auto 1000")
                return
            if minutes not in {10, 30, 60, 1000}:
                await update.message.reply_text("Доступные интервалы: 10, 30, 60, 1000 минут")
                return
            self.storage.set_monitor_interval(chat_id, minutes)
            await update.message.reply_text(f"Автоотслеживание стакана: каждые {minutes} мин.", reply_markup=self.main_keyboard())
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

        if lower == "del all":
            self.storage.clear_symbols(chat_id)
            await update.message.reply_text("Все монеты удалены из памяти.", reply_markup=self.main_keyboard())
            return

        if lower.startswith("del "):
            await self.del_coin(update, lower.split(maxsplit=1)[1])
            return

        await self.send_analysis(update, text)

    async def load_top_symbols(self, update: Update, limit: int) -> None:
        chat_id = update.effective_chat.id
        try:
            symbols = self.binance.top_symbols_by_quote_volume(self.config.default_quote, limit)
            self.storage.replace_symbols(chat_id, symbols)
            preview = ", ".join(symbols[:12])
            await update.message.reply_text(
                f"Загружено в память: топ-{len(symbols)} Binance {self.config.default_quote}.\n"
                f"Первые монеты: {preview}{'...' if len(symbols) > 12 else ''}",
                reply_markup=self.main_keyboard(),
            )
        except Exception as exc:
            logging.exception("Top symbols load failed")
            await update.message.reply_text(f"Не удалось загрузить топ монет: {html.escape(str(exc))}", reply_markup=self.main_keyboard())

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
        if not bool(user.get("bot_enabled", 1)):
            await update.message.reply_text("Бот выключен. Включите командой `bot on`.", parse_mode=ParseMode.MARKDOWN, reply_markup=self.main_keyboard())
            return
        try:
            symbol = self.binance.normalize_symbol(coin, self.config.default_quote)
            order_book = self.binance.order_book(symbol, self.config.orderbook_limit)
            klines = self.binance.klines(symbol, user["timeframe"], 180)
            result = analyze(symbol, user["timeframe"], order_book, klines)
            chart = make_chart(result)
            caption = result.text

            if user["visualization"] == "split":
                # Режим визуализации: весь анализ внутри картинки, без отдельного текста под фото.
                await update.message.reply_photo(photo=chart.open("rb"), reply_markup=self.main_keyboard())
            else:
                await update.message.reply_photo(photo=chart.open("rb"), caption=caption[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=self.main_keyboard())
                if len(caption) > 1024:
                    await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)
            Path(chart).unlink(missing_ok=True)
        except Exception as exc:
            logging.exception("Analysis failed")
            await update.message.reply_text(self.user_error_text(exc), reply_markup=self.main_keyboard())


    @staticmethod
    def user_error_text(exc: Exception) -> str:
        if isinstance(exc, BinanceAPIError):
            return "⚠️ Binance API временно недоступен с сервера бота. Я уже пробую резервные endpoints. Повторите запрос чуть позже."
        if isinstance(exc, ValueError):
            return f"⚠️ {html.escape(str(exc))}"
        return "⚠️ Не удалось выполнить анализ. Повторите запрос позже."

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
            await self.safe_menu_update(query, "Выберите таймфрейм:", reply_markup=InlineKeyboardMarkup(rows))
            return

        if data.startswith("tf:"):
            tf = data.split(":", 1)[1]
            if tf in TIMEFRAMES:
                self.storage.set_timeframe(chat_id, tf)
            await self.safe_menu_update(query, f"Таймфрейм установлен: {tf}", reply_markup=self.main_keyboard())
            return

        if data == "visualization":
            user = self.storage.get_user(chat_id)
            mode = user["visualization"]
            rows = [
                [InlineKeyboardButton(("✅ " if mode == "combined" else "") + "Картинка + текстовая подпись", callback_data="vis:combined")],
                [InlineKeyboardButton(("✅ " if mode == "split" else "") + "Весь анализ внутри картинки", callback_data="vis:split")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
            ]
            await self.safe_menu_update(query, "Режим визуализации:", reply_markup=InlineKeyboardMarkup(rows))
            return

        if data.startswith("vis:"):
            mode = data.split(":", 1)[1]
            self.storage.set_visualization(chat_id, mode)
            label = "картинка + подпись" if mode == "combined" else "весь анализ внутри картинки"
            await self.safe_menu_update(query, f"Визуализация установлена: {label}", reply_markup=self.main_keyboard())
            return

        if data == "stakan_toggle":
            user = self.storage.get_user(chat_id)
            enabled = not bool(user["stakan_enabled"])
            self.storage.set_stakan(chat_id, enabled)
            await self.safe_menu_update(query, "Стакан ордеров: " + ("включен" if enabled else "выключен"), reply_markup=self.main_keyboard())
            return

        if data == "ping":
            await self.safe_menu_update(query, self.ping_text(), reply_markup=self.main_keyboard())
            return

        if data == "back":
            await self.safe_menu_update(query, "Главное меню", reply_markup=self.main_keyboard())

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
        except Exception as exc:
            logging.warning("Binance ping failed: %s", exc)
            response_ms = -1
            status = "недоступен"
        return (
            f"🏓 Binance ping: {status}\n"
            f"⏱ Отклик: {response_ms:.0f} ms\n"
            f"🧠 Memory: {memory_mb:.1f} MB\n"
            f"⏳ Uptime: {hours}h {minutes}m {seconds}s\n"
            f"🔖 Version: {self.config.bot_version}"
        )

# ===== bot/monitor.py =====
import asyncio
import time
from pathlib import Path

from telegram.constants import ParseMode
from telegram.ext import Application



async def monitor_orderbooks(app: Application, config: Config, storage: Storage, binance: BinanceClient) -> None:
    while True:
        await asyncio.sleep(60)
        now = int(time.time())
        for chat_id, symbols in storage.enabled_watchlists():
            user = storage.get_user(chat_id)
            interval_minutes = int(user.get("monitor_interval_minutes") or config.monitor_interval_minutes)
            for symbol in symbols:
                try:
                    old, last_check_at = storage.get_snapshot_meta(chat_id, symbol)
                    if last_check_at and now - last_check_at < interval_minutes * 60:
                        continue

                    order_book = await asyncio.to_thread(binance.order_book, symbol, config.orderbook_limit)
                    signature = compact_orderbook_signature(order_book)
                    storage.set_snapshot(chat_id, symbol, signature, now)
                    if not old:
                        continue
                    change = signature_change_percent(old, signature)
                    if change < config.strong_change_threshold:
                        continue

                    klines = await asyncio.to_thread(binance.klines, symbol, user["timeframe"], 180)
                    result = analyze(symbol, user["timeframe"], order_book, klines)
                    chart = make_chart(result)
                    text = "🚨 *Сильное изменение стакана*\n" f"Изменение ликвидности: *{change:.1f}%*\n\n" + result.text
                    if user.get("visualization") == "split":
                        await app.bot.send_photo(chat_id=chat_id, photo=chart.open("rb"))
                    else:
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


# ===== main =====
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
