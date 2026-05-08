from __future__ import annotations

import logging
import re
import textwrap

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
    bot_version: str = "00007"


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
        bot_version=os.getenv("BOT_VERSION", "00007"),
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

    ratios = [("0%", 0.0), ("23.6%", 0.236), ("38.2%", 0.382), ("50%", 0.5), ("61.8%", 0.618), ("78.6%", 0.786), ("100%", 1.0)]

    # Для восходящего импульса уровни отката считаются от high вниз.
    # Для нисходящего — от low вверх.
    if end_price >= start_price:
        high = float(end_price)
        low = float(start_price)
        diff = high - low
        return {
            "0%": high,
            "23.6%": high - diff * 0.236,
            "38.2%": high - diff * 0.382,
            "50%": high - diff * 0.5,
            "61.8%": high - diff * 0.618,
            "78.6%": high - diff * 0.786,
            "100%": low,
        }
    else:
        high = float(start_price)
        low = float(end_price)
        diff = high - low
        return {
            "0%": low,
            "23.6%": low + diff * 0.236,
            "38.2%": low + diff * 0.382,
            "50%": low + diff * 0.5,
            "61.8%": low + diff * 0.618,
            "78.6%": low + diff * 0.786,
            "100%": high,
        }

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




def _select_structural_swings(df: pd.DataFrame, mode: str, bearish: bool, lookback: int = 90, max_points: int = 5) -> list[tuple[int, float]]:
    """Возвращает реальные Swing High / Swing Low, которые образуют структуру рынка.

    SHORT: понижающиеся SH и понижающиеся SL.
    LONG: повышающиеся SH и повышающиеся SL.

    Это не случайные свечи: точка берётся только если она является локальным экстремумом
    относительно соседних свечей, после чего выбирается монотонная структурная цепочка.
    """
    if mode not in {"high", "low"} or df.empty:
        return []
    tail_len = min(lookback, len(df))
    tail = df.tail(tail_len).reset_index(drop=True)
    source = tail["high"] if mode == "high" else tail["low"]
    pivots = _pivot_points(source, window=3, mode=mode)
    if len(pivots) < 2:
        return pivots[-max_points:]

    # Ищем лучшую монотонную подпоследовательность по времени.
    # Для нисходящего рынка цены экстремумов должны снижаться, для восходящего — расти.
    def ok(prev: float, cur: float) -> bool:
        return cur < prev if bearish else cur > prev

    n = len(pivots)
    dp = [1] * n
    prev_idx = [-1] * n
    for i in range(n):
        for j in range(i):
            if ok(pivots[j][1], pivots[i][1]) and dp[j] + 1 > dp[i]:
                dp[i] = dp[j] + 1
                prev_idx[i] = j

    # Предпочитаем длинную и свежую цепочку.
    best = max(range(n), key=lambda i: (dp[i], pivots[i][0]))
    seq: list[tuple[int, float]] = []
    while best != -1:
        seq.append(pivots[best])
        best = prev_idx[best]
    seq.reverse()

    if len(seq) < 2:
        seq = pivots[-max_points:]
    return seq[-max_points:]


def _find_sh_spikes(
    df: pd.DataFrame,
    structural_highs: list[tuple[int, float]],
    bearish: bool,
    lookback: int = 160,
    max_spikes: int = 3,
) -> list[tuple[int, float]]:
    """Находит Swing High-выносы, которые являются локальными максимумами, но не входят
    в основную структурную цепочку SH.

    Для SHORT это важный случай: точка может быть настоящим Swing High, но она выше
    линии понижающихся SH и ломает чистую нисходящую структуру. Поэтому её лучше
    показать красным кругом как "SH вынос", но не включать в синюю трендовую линию.
    """
    if df.empty or not bearish or len(structural_highs) < 2:
        return []

    tail_len = min(lookback, len(df))
    tail = df.tail(tail_len).reset_index(drop=True)
    pivots = _pivot_points(tail["high"], window=3, mode="high")
    if not pivots:
        return []

    structural_idx = {int(i) for i, _ in structural_highs}
    x1, y1 = structural_highs[0]
    x2, y2 = structural_highs[-1]
    if x2 == x1:
        return []
    slope = (y2 - y1) / (x2 - x1)

    high_low = (tail["high"].astype(float) - tail["low"].astype(float)).replace([np.inf, -np.inf], np.nan).dropna()
    typical_range = float(high_low.median()) if not high_low.empty else float(tail["close"].iloc[-1]) * 0.002
    threshold = max(typical_range * 0.7, float(tail["close"].iloc[-1]) * 0.001)

    spikes: list[tuple[int, float, float]] = []
    for x, y in pivots:
        if x in structural_idx:
            continue
        trend_y = y1 + slope * (x - x1)
        excess = float(y) - float(trend_y)
        # Вынос должен быть заметно выше основной линии SH, а не просто мелким шумом.
        if excess > threshold:
            spikes.append((int(x), float(y), excess))

    # Показываем самые заметные выносы, по времени слева направо.
    spikes = sorted(spikes, key=lambda item: item[2], reverse=True)[:max_spikes]
    spikes = sorted(spikes, key=lambda item: item[0])
    return [(x, y) for x, y, _ in spikes]

def _build_trendline(df: pd.DataFrame, trend_bias: float | None = None, mode: str | None = None, lookback: int = 90) -> TrendLine:
    """Строит профессиональную линию структуры рынка.

    LONG/восходящий рынок:
    - highs: повышающиеся максимумы
    - lows: повышающиеся минимумы

    SHORT/нисходящий рынок:
    - highs: понижающиеся максимумы
    - lows: понижающиеся минимумы
    """
    bias = float(trend_bias or 0.0)
    bearish = bias < 0
    if mode not in {"low", "high"}:
        mode = "high" if bearish else "low"
    kind = "support" if mode == "low" else "resistance"

    if bearish:
        label = "понижающиеся минимумы" if mode == "low" else "понижающиеся максимумы"
        def correct_structure(v1: float, v2: float) -> bool:
            return v2 < v1
    else:
        label = "повышающиеся минимумы" if mode == "low" else "повышающиеся максимумы"
        def correct_structure(v1: float, v2: float) -> bool:
            return v2 > v1

    source = df["low"] if mode == "low" else df["high"]
    tail_len = min(lookback, len(df))
    source_tail = source.tail(tail_len).reset_index(drop=True)
    pivots = _pivot_points(source_tail, window=3, mode=mode)

    chosen = None
    if len(pivots) >= 2:
        recent = pivots[-14:]
        best_score = -10**9
        for a in range(len(recent) - 1):
            p1 = recent[a]
            for p2 in recent[a + 1:]:
                dx = max(p2[0] - p1[0], 1)
                if not correct_structure(p1[1], p2[1]):
                    continue
                slope = (p2[1] - p1[1]) / dx
                line = np.array([p1[1] + slope * (i - p1[0]) for i in range(len(source_tail))], dtype=float)
                tolerance = max(float(df["close"].iloc[-1]) * 0.0035, 1e-12)
                touches = int(np.sum(np.abs(source_tail.to_numpy(dtype=float) - line) <= tolerance))
                # Свежесть + число касаний важнее всего.
                score = touches * 100 + p2[0] * 1.5 - abs(slope / max(float(df["close"].iloc[-1]), 1e-9))
                if score > best_score:
                    best_score = score
                    chosen = (p1, p2)

    if chosen is None and len(pivots) >= 2:
        # Если строгая структура не найдена, берём две последние точки: лучше показать факт касаний, чем пустоту.
        chosen = (pivots[-2], pivots[-1])

    if chosen is not None:
        (i1, y1), (i2, y2) = chosen
    else:
        x = np.arange(len(source_tail), dtype=float)
        slope, intercept = np.polyfit(x, source_tail.to_numpy(dtype=float), 1)
        i1, i2 = 0, len(source_tail) - 1
        y1, y2 = float(intercept), float(intercept + slope * i2)

    if i2 == i1:
        i2 = i1 + 1
    slope = (y2 - y1) / (i2 - i1)
    current_value = float(y1 + slope * ((tail_len - 1) - i1))
    slope_percent = float((current_value / max(abs(y1), 1e-9) - 1) * 100)

    line = np.array([y1 + slope * (i - i1) for i in range(tail_len)], dtype=float)
    tolerance = max(float(df["close"].iloc[-1]) * 0.0035, 1e-12)
    touches_source = source_tail.to_numpy(dtype=float)
    touches = int(np.sum(np.abs(touches_source - line) <= tolerance))

    return TrendLine(
        kind=kind,
        start_index=int(i1),
        start_price=float(y1),
        end_index=int(tail_len - 1),
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


def _timeframe_target_step(interval: str, price: float, df: pd.DataFrame) -> float:
    """Минимальная дистанция целей с учётом таймфрейма и текущей волатильности."""
    tf_mult = {
        "1m": 0.003, "3m": 0.004, "5m": 0.005, "15m": 0.008, "30m": 0.010,
        "1h": 0.015, "2h": 0.020, "4h": 0.030, "6h": 0.040, "8h": 0.045,
        "12h": 0.055, "1d": 0.080, "3d": 0.120, "1w": 0.180, "1M": 0.250,
    }.get(interval, 0.020)
    try:
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        atr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean().iloc[-1]
        atr_step = float(atr) if np.isfinite(atr) and atr > 0 else 0.0
    except Exception:
        atr_step = 0.0
    return max(price * tf_mult, atr_step * 0.8, price * 0.002, 1e-12)


def _unique_targets(candidates: list[float], start: float, direction: str, step: float, count: int = 2) -> list[float]:
    targets: list[float] = []
    last = start
    ordered = sorted(set(float(x) for x in candidates if np.isfinite(x)), reverse=(direction == "SHORT"))
    for value in ordered:
        if direction == "LONG" and value > last + step * 0.35:
            targets.append(value)
            last = value
        elif direction == "SHORT" and value < last - step * 0.35:
            targets.append(value)
            last = value
        if len(targets) >= count:
            break
    while len(targets) < count:
        last = last + step if direction == "LONG" else last - step
        targets.append(last)
    return targets


def _projection(price: float, fibs: dict[str, float], support: OrderBookLevel, resistance: OrderBookLevel, long_probability: float, interval: str, df: pd.DataFrame) -> PriceProjection:
    # Цели LONG/SHORT не должны совпадать с поддержкой/сопротивлением.
    # Дистанция целей учитывает выбранный таймфрейм и ATR, чтобы на 15m цели были ближе, а на 1d/1w дальше.
    levels = sorted(set([*fibs.values(), support.price, resistance.price]))
    step = _timeframe_target_step(interval, price, df)
    min_gap = max(step * 0.35, price * 0.0015, 1e-12)
    if long_probability >= 50:
        trigger = max(price, resistance.price)
        # LONG-цели берём только выше точки входа/сопротивления; если уровней нет — строим по волатильности таймфрейма.
        candidates = [v for v in levels if v > trigger + min_gap]
        t1, t2 = _unique_targets(candidates, trigger, "LONG", step, 2)
        inv_candidates = [v for v in levels if v < min(price, support.price) - min_gap]
        inv = inv_candidates[-1] if inv_candidates else min(price, support.price) - step * 0.8
        direction = "LONG"
        text = f"При пробое/удержании выше {trigger:.6g} цель: {t1:.6g} → {t2:.6g}; отмена ниже {inv:.6g}"
    else:
        trigger = min(price, support.price)
        candidates = [v for v in levels if v < trigger - min_gap]
        t1, t2 = _unique_targets(candidates, trigger, "SHORT", step, 2)
        inv_candidates = [v for v in levels if v > max(price, resistance.price) + min_gap]
        inv = inv_candidates[0] if inv_candidates else max(price, resistance.price) + step * 0.8
        direction = "SHORT"
        text = f"При пробое поддержки {support.price:.6g} вниз цель: {t1:.6g} → {t2:.6g}; отмена выше {inv:.6g}"
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
    projection = _projection(price, fibs, support, resistance, long_probability, interval, df)

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


def make_chart(result: AnalysisResult, full_analysis_text: str | None = None) -> Path:
    """Профессиональный белый график в стиле TradingView/Binance: крупные свечи, правые цены,
    фибо в цельных окошках справа, жёлтые касания структуры и анализ в зелёной рамке снизу."""
    df = result.df.tail(90).copy()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.close()
    out = Path(tmp.name)

    market_colors = mpf.make_marketcolors(
        up="#149560", down="#d23b3b", edge="inherit", wick="inherit", volume="inherit"
    )
    style = mpf.make_mpf_style(
        base_mpf_style="default",
        marketcolors=market_colors,
        gridstyle="-",
        gridcolor="#e9ecef",
        facecolor="#ffffff",
        figcolor="#ffffff",
        rc={
            "font.size": 13,
            "axes.labelsize": 14,
            "axes.titlesize": 20,
            "xtick.labelsize": 12,
            "ytick.labelsize": 13,
        },
    )

    fig, axes = mpf.plot(
        df,
        type="candle",
        volume=False,
        style=style,
        returnfig=True,
        figsize=(16, 10),
        tight_layout=False,
        datetime_format="%H:%M",
        xrotation=0,
        warn_too_much_data=300,
    )
    ax = axes[0]
    bottom_space = 0.23 if full_analysis_text else 0.10
    fig.subplots_adjust(left=0.045, right=0.90, top=0.89, bottom=bottom_space)

    direction = result.projection.direction
    title_symbol = result.symbol.replace("USDT", "_USDT")
    window_move = (df["close"].iloc[-1] / df["close"].iloc[0] - 1.0) * 100
    candle_move = window_move / max(len(df), 1)
    ax.set_title(
        f"{title_symbol} · {result.interval} график\nНаклонка {window_move:+.2f}% за окно ({candle_move:+.4f}%/свечу)",
        loc="center",
        color="black",
        pad=10,
        fontsize=20,
        fontweight="bold",
    )

    # Правая шкала цены.
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.set_ylabel("Цена (USDT)", color="black", fontweight="bold")
    ax.set_xlabel(f"Свечи {result.interval}", color="black", fontweight="bold")

    right_x = len(df) - 1
    label_x = len(df) + 4.0

    def price_box(y: float, text: str, edge: str = "#111827", text_color: str = "black", lw: float = 1.4) -> None:
        ax.text(
            label_x, y, f" {text} ",
            color=text_color,
            va="center",
            ha="left",
            fontsize=12.5,
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.28", facecolor="#ffffff", edgecolor=edge, linewidth=lw, alpha=0.98),
            clip_on=False,
        )

    # Важные уровни справа: стоп, вход, цена.
    entry = result.price
    stop = result.projection.invalidation
    if direction == "SHORT":
        stop = max(stop, result.resistance.price)
        entry = min(result.price, result.support.price) if result.support.price < result.price else result.price
    else:
        stop = min(stop, result.support.price)
        entry = max(result.price, result.resistance.price) if result.resistance.price > result.price else result.price

    ax.axhline(stop, color="#ef4444", linewidth=1.15, linestyle="--", alpha=0.9)
    ax.axhline(entry, color="#111827", linewidth=1.15, linestyle="--", alpha=0.9)
    ax.axhline(result.price, color="#6b7280", linewidth=1.1, linestyle="--", alpha=0.7)
    price_box(stop, f"СТОП {_fmt(stop)}", edge="#ef4444")
    price_box(entry, f"ВХОД {_fmt(entry)}", edge="#111827")
    price_box(result.price, f"ЦЕНА {_fmt(result.price)}", edge="#9ca3af", text_color="#374151")

    # Фибо: линии и цельные окошки справа в стиле примера. Берём ближайшие рабочие уровни.
    fib_order = ["23.6%", "38.2%", "50%", "61.8%", "78.6%", "100%"]
    if direction == "SHORT":
        fib_items = [(k, result.fib_levels[k]) for k in fib_order if k in result.fib_levels and result.fib_levels[k] < entry]
        fib_items = sorted(fib_items, key=lambda kv: kv[1], reverse=True)[:3]
    else:
        fib_items = [(k, result.fib_levels[k]) for k in fib_order if k in result.fib_levels and result.fib_levels[k] > entry]
        fib_items = sorted(fib_items, key=lambda kv: kv[1])[:3]
    if not fib_items:
        fib_items = list(result.fib_levels.items())[-3:]
    for name, level in fib_items:
        ax.axhline(level, color="#05805c", linewidth=1.25, linestyle="--", alpha=0.95)
        ax.text(
            label_x + 2.0, level, f"Фибо {name} — {_fmt(level)}",
            color="#047857",
            va="center",
            ha="left",
            fontsize=11.5,
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.32", facecolor="#ffffff", edgecolor="#047857", linewidth=1.5, alpha=0.98),
            clip_on=False,
        )

    # Структура рынка: реальные Swing High / Swing Low, а не случайные свечи.
    # SHORT: понижающиеся SH + понижающиеся SL. LONG: повышающиеся SH + повышающиеся SL.
    bearish = direction == "SHORT"
    swing_highs = _select_structural_swings(df, mode="high", bearish=bearish, lookback=len(df), max_points=5)
    swing_lows = _select_structural_swings(df, mode="low", bearish=bearish, lookback=len(df), max_points=5)
    sh_spikes = _find_sh_spikes(df, swing_highs, bearish=bearish, lookback=len(df), max_spikes=2)

    def draw_swing_structure(points: list[tuple[int, float]], line_color: str, prefix: str, dashed: bool = False) -> int:
        if len(points) >= 2:
            x1, y1 = points[0]
            x2, y2 = points[-1]
            if x2 == x1:
                x2 = x1 + 1
            slope = (y2 - y1) / (x2 - x1)
            xs = np.arange(max(0, x1 - 1), len(df) + 5, dtype=float)
            ys = np.array([y1 + slope * (x - x1) for x in xs], dtype=float)
            ax.plot(xs, ys, color=line_color, linewidth=1.45, alpha=0.98, linestyle="--" if dashed else "-")
        if points:
            xs = [int(i) for i, _ in points]
            ys = [float(v) for _, v in points]
            ax.scatter(xs, ys, facecolors="none", edgecolors="#f5e600", s=92, zorder=8, linewidths=2.7)
            ax.scatter(xs, ys, color="#f5e600", s=16, zorder=9)
            for n, (x, y) in enumerate(points, start=1):
                offset = 11 if prefix == "SH" else -21
                ax.annotate(
                    f"{prefix}{n}\n{_fmt(y)}", xy=(x, y), xytext=(0, offset), textcoords="offset points",
                    ha="center", va="bottom" if prefix == "SH" else "top",
                    fontsize=8.8, fontweight="bold", color="#1d4ed8", annotation_clip=True
                )
        return len(points)

    high_touches = draw_swing_structure(swing_highs, "#2563eb", "SH", dashed=False)
    low_touches = draw_swing_structure(swing_lows, "#2563eb", "SL", dashed=True)

    # SH-выносы: реальные Swing High, которые выше линии понижающихся SH.
    # Красным кругом показываем их на графике, но не включаем в основную трендовую линию SHORT.
    if sh_spikes:
        xs = [int(i) for i, _ in sh_spikes]
        ys = [float(v) for _, v in sh_spikes]
        ax.scatter(xs, ys, facecolors="none", edgecolors="#e11d48", s=150, zorder=10, linewidths=3.0)
        ax.scatter(xs, ys, color="#e11d48", s=22, zorder=11)
        for n, (x, y) in enumerate(sh_spikes, start=1):
            ax.annotate(
                f"SH вынос {n}\n{_fmt(y)}", xy=(x, y), xytext=(0, 18), textcoords="offset points",
                ha="center", va="bottom", fontsize=8.7, fontweight="bold", color="#e11d48",
                bbox=dict(boxstyle="round,pad=0.18", facecolor="#ffffff", edgecolor="#e11d48", linewidth=0.9, alpha=0.9),
                annotation_clip=True,
            )

    # Мини-легенда под графиком: объясняет, что именно отмечено.
    ax.text(
        0.50, -0.085,
        "SH = Swing High / локальный максимум   ·   SL = Swing Low / локальный минимум   ·   жёлтый круг = структурная swing-точка   ·   красный круг = SH вынос",
        transform=ax.transAxes, ha="center", va="top", fontsize=9.5, color="black",
        bbox=dict(boxstyle="round,pad=0.25", facecolor="#ffffff", edgecolor="#9ca3af", linewidth=0.8, alpha=0.95),
        clip_on=False,
    )

    scenario_box = (
        f"СЦЕНАРИЙ: {direction}\n"
        f"LONG: {result.long_probability:.0f}%\n"
        f"SHORT: {result.short_probability:.0f}%\n"
        f"Касаний минимум SL: {low_touches}\n"
        f"Касаний максимум SH: {high_touches}"
    )
    ax.text(
        0.01, 0.98, scenario_box,
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=13,
        color="black",
        fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.38", facecolor="#ffffff", edgecolor="#111827", linewidth=1.3, alpha=0.97),
    )

    # Верхний правый блок RR/рынок.
    risk = abs(entry - stop)
    reward = abs(result.projection.target_1 - entry)
    rr = reward / risk if risk > 0 else 0.0
    ax.text(
        0.985, 0.98,
        f"RR TP1: {rr:.2f}\nРынок: {_fmt(result.price)}\nНаклон/св: {candle_move:+.4f}%",
        transform=ax.transAxes,
        va="top",
        ha="right",
        fontsize=12,
        color="black",
        fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.35", facecolor="#ffffff", edgecolor="#2563eb", linewidth=1.3, alpha=0.97),
    )

    # Стрелка сценария — тонкая, сбоку, не закрывает свечи.
    arrow_color = "#111827"
    arrow_target = result.projection.target_1
    ax.annotate(
        direction,
        xy=(right_x + 2.0, arrow_target),
        xytext=(right_x - 5.0, result.price),
        fontsize=16,
        fontweight="bold",
        color="black",
        arrowprops=dict(arrowstyle="->", color=arrow_color, linewidth=1.6),
        annotation_clip=False,
    )

    # Рекомендация: зелёная рамка сбоку/снизу, не закрывает свечи и Фибо.
    rec_text = (
        "РЕКОМЕНДАЦИЯ\n"
        f"Вход: {_fmt(entry)}\n"
        f"Стоп: {_fmt(stop)}\n"
        f"Цели:\n1) {_fmt(result.projection.target_1)}\n2) {_fmt(result.projection.target_2)}\nRR TP1: {rr:.2f}"
    )
    if full_analysis_text:
        fig.text(
            0.825, 0.035, rec_text,
            color="black",
            fontsize=11.5,
            va="bottom",
            ha="left",
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.45", facecolor="#ffffff", edgecolor="#16a34a", linewidth=1.7, alpha=0.98),
        )
    else:
        ax.text(
            1.01, 0.08, rec_text,
            transform=ax.transAxes,
            color="black",
            fontsize=11.5,
            va="bottom",
            ha="left",
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.45", facecolor="#ffffff", edgecolor="#16a34a", linewidth=1.7, alpha=0.98),
            clip_on=False,
        )

    # Полный анализ внизу в зелёной рамке, только если выбран режим "вся инфа в картинке".
    if full_analysis_text:
        clean = re.sub(r"[`*_]", "", full_analysis_text)
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
        if "Анализ:" not in clean:
            structure = "понижающиеся максимумы и понижающиеся минимумы" if direction == "SHORT" else "повышающиеся максимумы и повышающиеся минимумы"
            clean = (
                f"Анализ: Цена формирует {structure}.\n"
                f"Сценарий: {direction}. Вход {_fmt(entry)}, стоп {_fmt(stop)}, цели {_fmt(result.projection.target_1)} / {_fmt(result.projection.target_2)}.\n"
                f"Рекомендация: {result.recommendation}\n"
                + clean
            )
        wrapped_lines = []
        for line in clean.splitlines():
            wrapped_lines.extend(textwrap.wrap(line, width=118) if line.strip() else [""])
        bottom_text = "\n".join(wrapped_lines[:9])
        fig.text(
            0.045, 0.035, bottom_text,
            color="black",
            fontsize=11.5,
            va="bottom",
            ha="left",
            bbox=dict(boxstyle="round,pad=0.45", facecolor="#ffffff", edgecolor="#047857", linewidth=1.6, alpha=0.98),
        )
    else:
        fig.text(0.045, 0.035, "Не является финансовой рекомендацией. Соблюдайте риск-менеджмент.", color="#4b5563", fontsize=11)

    # Пространство справа под ценники и Фибо.
    ax.set_xlim(-1, len(df) + 13)
    lows = [df["low"].min(), result.price, stop, entry, result.projection.target_1, result.projection.target_2, *[v for _, v in fib_items]]
    highs = [df["high"].max(), result.price, stop, entry, result.projection.target_1, result.projection.target_2, *[v for _, v in fib_items]]
    ymin, ymax = min(lows), max(highs)
    pad = max((ymax - ymin) * 0.12, result.price * 0.004)
    ax.set_ylim(ymin - pad, ymax + pad)

    for axis in [ax]:
        axis.tick_params(axis="both", colors="black")
        for spine in axis.spines.values():
            spine.set_color("#111827")
            spine.set_linewidth(0.8)

    fig.savefig(out, dpi=150, facecolor="#ffffff", bbox_inches="tight")
    plt.close(fig)
    return out


# ===== bot/telegram_bot.py =====
import html
import os
import time
from pathlib import Path

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, InputMediaPhoto, Update
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
        app.add_handler(CommandHandler("info", self.info_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_router))
        app.add_handler(CallbackQueryHandler(self.callback_router))
        return app

    def main_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 Стакан ордеров", callback_data="stakan_toggle"), InlineKeyboardButton("⏱ Таймфрейм", callback_data="settings")],
            [InlineKeyboardButton("🖼 Визуализация", callback_data="visualization")],
            [InlineKeyboardButton("ℹ️ Info", callback_data="info"), InlineKeyboardButton("🏓 Пинг", callback_data="ping")],
        ])

    def bottom_menu(self) -> ReplyKeyboardMarkup:
        # Постоянное нижнее меню Telegram. Оно остаётся доступным, даже если inline-кнопки под старым сообщением пропали.
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("📚 Стакан ордеров"), KeyboardButton("⏱ Таймфрейм")],
                [KeyboardButton("🖼 Визуализация"), KeyboardButton("ℹ️ Info")],
                [KeyboardButton("/help"), KeyboardButton("🏓 Пинг")],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

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
            reply_markup=self.bottom_menu(),
        )

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(self.help_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=self.bottom_menu())

    async def info_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        await update.message.reply_text(self.settings_text(chat_id), parse_mode=ParseMode.MARKDOWN, reply_markup=self.bottom_menu())

    def settings_text(self, chat_id: int) -> str:
        user = self.storage.get_user(chat_id)
        symbols = self.storage.list_symbols(chat_id)
        visualization = "весь анализ внутри картинки" if user.get("visualization") == "split" else "картинка + текстовая подпись"
        auto_status = "on" if bool(user.get("stakan_enabled")) else "off"
        bot_status = "on" if bool(user.get("bot_enabled", 1)) else "off"
        coins = ", ".join(symbols) if symbols else "пусто"
        return (
            "ℹ️ *Текущие настройки*\n\n"
            f"🪙 Монеты загруженные/добавленные: `{coins}`\n"
            f"🤖 Bot: `{bot_status}`\n"
            f"📚 Auto: `{auto_status}`\n"
            f"⏱ Auto-сканирование стакана: каждые `{int(user.get('monitor_interval_minutes', 30))}` мин. (`auto {int(user.get('monitor_interval_minutes', 30))}`)\n"
            f"🕯 Таймфрейм: `{user.get('timeframe', '1h')}`\n"
            f"🖼 Визуализация: `{visualization}`"
        )

    def help_text(self) -> str:
        return (
            "*Команды бота*\n\n"
            "*Анализ*\n"
            "`btc`, `eth`, `sol` — анализ монеты Binance Spot.\n"
            "`BTCUSDT` — анализ готовой пары.\n\n"
            "*Память / монеты*\n"
            "`new btc` — добавить монету в мониторинг стакана.\n"
            "`del btc` — удалить монету из мониторинга.\n"
            "`del all` — удалить все монеты из памяти.\n"
            "`list` — список монет в памяти.\n"
            "`info` / `/info` — текущие настройки: монеты, auto, таймфрейм и визуализация.\n"
            "`top-50` — загрузить топ 50 монет Binance USDT.\n"
            "`top-100` — загрузить топ 100 монет Binance USDT.\n"
            "`top-200` — загрузить топ 200 монет Binance USDT.\n\n"
            "*Включение / выключение*\n"
            "`bot on` — включить бота.\n"
            "`bot off` — выключить анализ и автоотслеживание.\n"
            "`stakan on` / `auto on` — включить автоотслеживание стакана.\n"
            "`stakan off` / `auto off` — выключить автоотслеживание стакана.\n\n"
            "*Автоотслеживание*\n"
            "`auto 10` — проверять стакан каждые 10 минут.\n"
            "`auto 30` — проверять стакан каждые 30 минут.\n"
            "`auto 60` — проверять стакан каждый час.\n"
            "`auto 1000` — проверять стакан каждые 1000 минут.\n\n"
            "*Кнопки*\n"
            "📚 Стакан ордеров — on/off.\n"
            "⏱ Таймфрейм — переключение таймфрейма по кругу.\n"
            "🖼 Визуализация — картинка+подпись или весь анализ внутри картинки.\n"
            "ℹ️ Info — текущие настройки.\n"
            "🏓 Пинг — статус Binance и сервера.\n\n"
            "`/help` — показать эту справку.\n"
            "`/info` — показать текущие настройки."
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

        if lower in {"info", "ℹ️ info", "инфо", "настройки info"}:
            await update.message.reply_text(self.settings_text(chat_id), parse_mode=ParseMode.MARKDOWN, reply_markup=self.bottom_menu())
            return

        if lower in {"📚 стакан ордеров", "стакан ордеров"}:
            user = self.storage.get_user(chat_id)
            enabled = not bool(user["stakan_enabled"])
            self.storage.set_stakan(chat_id, enabled)
            await update.message.reply_text("Стакан ордеров: " + ("включен" if enabled else "выключен"), reply_markup=self.bottom_menu())
            return

        if lower in {"⏱ таймфрейм", "таймфрейм", "⚙️ настройки", "настройки"}:
            user = self.storage.get_user(chat_id)
            current = user.get("timeframe", "1h")
            idx = TIMEFRAMES.index(current) if current in TIMEFRAMES else 0
            new_tf = TIMEFRAMES[(idx + 1) % len(TIMEFRAMES)]
            self.storage.set_timeframe(chat_id, new_tf)
            await update.message.reply_text(f"Таймфрейм переключен: `{new_tf}`", parse_mode=ParseMode.MARKDOWN, reply_markup=self.bottom_menu())
            return

        if lower in {"🖼 визуализация", "визуализация"}:
            user = self.storage.get_user(chat_id)
            new_mode = "combined" if user.get("visualization") == "split" else "split"
            self.storage.set_visualization(chat_id, new_mode)
            label = "весь анализ внутри картинки" if new_mode == "split" else "картинка + текстовая подпись"
            await update.message.reply_text(f"Визуализация установлена: {label}", reply_markup=self.bottom_menu())
            return

        if lower in {"🏓 пинг", "пинг"}:
            await update.message.reply_text(self.ping_text(), reply_markup=self.bottom_menu())
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
            await update.message.reply_text("Бот: " + ("включен" if enabled else "выключен"), reply_markup=self.bottom_menu())
            return

        if lower in {"auto on", "auto off"}:
            enabled = lower.endswith("on")
            self.storage.set_stakan(chat_id, enabled)
            await update.message.reply_text("Автоотслеживание стакана: " + ("включено" if enabled else "выключено"), reply_markup=self.bottom_menu())
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
            await update.message.reply_text(f"Автоотслеживание стакана: каждые {minutes} мин.", reply_markup=self.bottom_menu())
            return

        if lower.startswith("stakan "):
            arg = lower.split(maxsplit=1)[1]
            enabled = arg in {"on", "вкл", "1", "true"}
            self.storage.set_stakan(chat_id, enabled)
            await update.message.reply_text("Стакан ордеров: " + ("включен" if enabled else "выключен"), reply_markup=self.bottom_menu())
            return

        if lower in TIMEFRAMES:
            self.storage.set_timeframe(chat_id, lower)
            await update.message.reply_text(f"Таймфрейм установлен: {lower}", reply_markup=self.bottom_menu())
            return

        if lower.startswith("new "):
            await self.add_coin(update, lower.split(maxsplit=1)[1])
            return

        if lower == "del all":
            self.storage.clear_symbols(chat_id)
            await update.message.reply_text("Все монеты удалены из памяти.", reply_markup=self.bottom_menu())
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
            await update.message.reply_text(f"Не удалось загрузить топ монет: {html.escape(str(exc))}", reply_markup=self.bottom_menu())

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
            await update.message.reply_text("Бот выключен. Включите командой `bot on`.", parse_mode=ParseMode.MARKDOWN, reply_markup=self.bottom_menu())
            return
        try:
            symbol = self.binance.normalize_symbol(coin, self.config.default_quote)
            order_book = self.binance.order_book(symbol, self.config.orderbook_limit)
            klines = self.binance.klines(symbol, user["timeframe"], 180)
            result = analyze(symbol, user["timeframe"], order_book, klines)
            caption = result.text
            chart = make_chart(result, caption if user["visualization"] == "split" else None)

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
            await update.message.reply_text(self.user_error_text(exc), reply_markup=self.bottom_menu())


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
            current = user.get("timeframe", "1h")
            idx = TIMEFRAMES.index(current) if current in TIMEFRAMES else 0
            new_tf = TIMEFRAMES[(idx + 1) % len(TIMEFRAMES)]
            self.storage.set_timeframe(chat_id, new_tf)
            await self.safe_menu_update(query, f"Таймфрейм переключен: {new_tf}", reply_markup=self.main_keyboard())
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

        if data == "info":
            await self.safe_menu_update(query, self.settings_text(chat_id), reply_markup=self.main_keyboard())
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
                    text = "🚨 *Сильное изменение стакана*\n" f"Изменение ликвидности: *{change:.1f}%*\n\n" + result.text
                    chart = make_chart(result, text if user.get("visualization") == "split" else None)
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
