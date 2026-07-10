from __future__ import annotations

import logging
from contextlib import contextmanager
import math
import re
import textwrap
import threading
import unicodedata
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Single-file Railway version. No local package imports are required.


# ===== bot/config.py =====
import os
from dataclasses import dataclass, field
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
    bot_version: str = "00012"


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
        bot_version=os.getenv("BOT_VERSION", "00012"),
    )

# ===== bot/binance_client.py =====
import time
from typing import Any

import requests


class BinanceAPIError(RuntimeError):
    pass


class BinanceClient:
    def __init__(self, base_url: str, timeout: int = 8):
        self.base_urls = self._build_base_urls(base_url)
        self.base_url = self.base_urls[0]
        self.timeout = max(int(timeout), 2)
        self._symbols_cache: tuple[float, set[str]] | None = None
        self._endpoint_lock = threading.RLock()

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
        deadline = time.monotonic() + max(18.0, self.timeout * 2.5)
        with self._endpoint_lock:
            endpoints = list(self.base_urls)
        for base_url in endpoints:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            url = f"{base_url}{path}"
            attempt_timeout = max(2.0, min(float(self.timeout), remaining))
            try:
                response = requests.get(url, params=params, timeout=attempt_timeout)
                if response.status_code in {451, 403, 418, 429} or response.status_code >= 500:
                    last_error = f"{base_url}: HTTP {response.status_code}"
                    continue
                response.raise_for_status()
                payload = {} if not response.content else response.json()
                with self._endpoint_lock:
                    self.base_url = base_url
                    if base_url in self.base_urls:
                        self.base_urls.remove(base_url)
                    self.base_urls.insert(0, base_url)
                return payload
            except requests.RequestException as exc:
                last_error = f"{base_url}: {exc.__class__.__name__}"
            except ValueError:
                last_error = f"{base_url}: некорректный JSON"
        raise BinanceAPIError(f"Binance API недоступен после резервных endpoints: {last_error}")

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
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        """SQLite context that commits/rolls back and always closes the connection."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.lock, self._connection() as conn:
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
                CREATE TABLE IF NOT EXISTS monitor_state (
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    next_check_at INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at INTEGER,
                    last_success_at INTEGER,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, symbol)
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "bot_enabled" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN bot_enabled INTEGER NOT NULL DEFAULT 1")
            if "monitor_interval_minutes" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN monitor_interval_minutes INTEGER NOT NULL DEFAULT 30")

    def ensure_user(self, chat_id: int) -> None:
        with self.lock, self._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO users(chat_id) VALUES(?)", (chat_id,))

    def get_user(self, chat_id: int) -> dict[str, Any]:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            return dict(row)

    def set_timeframe(self, chat_id: int, timeframe: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET timeframe=? WHERE chat_id=?", (timeframe, chat_id))

    def set_visualization(self, chat_id: int, mode: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET visualization=? WHERE chat_id=?", (mode, chat_id))

    def set_stakan(self, chat_id: int, enabled: bool) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET stakan_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))
        if enabled:
            self.schedule_monitor_now(chat_id)

    def set_bot_enabled(self, chat_id: int, enabled: bool) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET bot_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))
        if enabled:
            self.schedule_monitor_now(chat_id)

    def set_monitor_interval(self, chat_id: int, minutes: int) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET monitor_interval_minutes=? WHERE chat_id=?", (minutes, chat_id))
        self.schedule_monitor_now(chat_id)

    def add_symbol(self, chat_id: int, symbol: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES(?,?)", (chat_id, symbol))
            conn.execute(
                "INSERT INTO monitor_state(chat_id, symbol, next_check_at) VALUES(?,?,0) "
                "ON CONFLICT(chat_id, symbol) DO UPDATE SET next_check_at=0, last_error=NULL",
                (chat_id, symbol),
            )

    def del_symbol(self, chat_id: int, symbol: str) -> None:
        with self.lock, self._connection() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=? AND symbol=?", (chat_id, symbol))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=? AND symbol=?", (chat_id, symbol))
            conn.execute("DELETE FROM monitor_state WHERE chat_id=? AND symbol=?", (chat_id, symbol))

    def clear_symbols(self, chat_id: int) -> None:
        with self.lock, self._connection() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM monitor_state WHERE chat_id=?", (chat_id,))

    def replace_symbols(self, chat_id: int, symbols: list[str]) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM monitor_state WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=?", (chat_id,))
            conn.executemany("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES(?,?)", [(chat_id, s) for s in symbols])
            conn.executemany("INSERT OR IGNORE INTO monitor_state(chat_id, symbol, next_check_at) VALUES(?,?,0)", [(chat_id, s) for s in symbols])

    def list_symbols(self, chat_id: int) -> list[str]:
        with self.lock, self._connection() as conn:
            rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id=? ORDER BY symbol", (chat_id,)).fetchall()
            return [r["symbol"] for r in rows]

    def enabled_watchlists(self) -> list[tuple[int, list[str]]]:
        with self.lock, self._connection() as conn:
            users = conn.execute("SELECT chat_id FROM users WHERE stakan_enabled=1 AND bot_enabled=1").fetchall()
            result: list[tuple[int, list[str]]] = []
            for user in users:
                rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id=? ORDER BY symbol", (user["chat_id"],)).fetchall()
                result.append((int(user["chat_id"]), [r["symbol"] for r in rows]))
            return result

    def get_snapshot(self, chat_id: int, symbol: str) -> dict[str, Any] | None:
        with self.lock, self._connection() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM orderbook_snapshots WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            return json.loads(row["snapshot_json"]) if row else None

    def get_snapshot_meta(self, chat_id: int, symbol: str) -> tuple[dict[str, Any] | None, int | None]:
        with self.lock, self._connection() as conn:
            row = conn.execute(
                "SELECT snapshot_json, updated_at FROM orderbook_snapshots WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            return (json.loads(row["snapshot_json"]), int(row["updated_at"])) if row else (None, None)

    def set_snapshot(self, chat_id: int, symbol: str, snapshot: dict[str, Any], updated_at: int) -> None:
        with self.lock, self._connection() as conn:
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

    def schedule_monitor_now(self, chat_id: int, symbol: str | None = None) -> None:
        with self.lock, self._connection() as conn:
            if symbol is not None:
                symbols = [symbol]
            else:
                rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id=?", (chat_id,)).fetchall()
                symbols = [r["symbol"] for r in rows]
            conn.executemany(
                "INSERT INTO monitor_state(chat_id, symbol, next_check_at) VALUES(?,?,0) "
                "ON CONFLICT(chat_id, symbol) DO UPDATE SET next_check_at=0, last_error=NULL",
                [(chat_id, item) for item in symbols],
            )

    def get_monitor_state(self, chat_id: int, symbol: str) -> dict[str, Any]:
        with self.lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM monitor_state WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            if row:
                return dict(row)
            conn.execute("INSERT OR IGNORE INTO monitor_state(chat_id, symbol, next_check_at) VALUES(?,?,0)", (chat_id, symbol))
            return {
                "chat_id": chat_id, "symbol": symbol, "next_check_at": 0,
                "last_attempt_at": None, "last_success_at": None, "last_error": None,
                "consecutive_failures": 0,
            }

    def mark_monitor_attempt(self, chat_id: int, symbol: str, timestamp: int) -> None:
        with self.lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO monitor_state(chat_id, symbol, next_check_at, last_attempt_at) VALUES(?,?,0,?) "
                "ON CONFLICT(chat_id, symbol) DO UPDATE SET last_attempt_at=excluded.last_attempt_at",
                (chat_id, symbol, timestamp),
            )

    def mark_monitor_success(self, chat_id: int, symbol: str, timestamp: int, next_check_at: int) -> None:
        with self.lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO monitor_state(chat_id, symbol, next_check_at, last_attempt_at, last_success_at, last_error, consecutive_failures)
                VALUES(?,?,?,?,?,NULL,0)
                ON CONFLICT(chat_id, symbol) DO UPDATE SET
                    next_check_at=excluded.next_check_at,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,
                    last_error=NULL,
                    consecutive_failures=0
                """,
                (chat_id, symbol, next_check_at, timestamp, timestamp),
            )

    def mark_monitor_failure(self, chat_id: int, symbol: str, timestamp: int, retry_at: int, error: str) -> int:
        with self.lock, self._connection() as conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM monitor_state WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            failures = (int(row["consecutive_failures"]) if row else 0) + 1
            conn.execute(
                """
                INSERT INTO monitor_state(chat_id, symbol, next_check_at, last_attempt_at, last_error, consecutive_failures)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(chat_id, symbol) DO UPDATE SET
                    next_check_at=excluded.next_check_at,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error,
                    consecutive_failures=excluded.consecutive_failures
                """,
                (chat_id, symbol, retry_at, timestamp, error[:1500], failures),
            )
            return failures

    def monitor_summary(self, chat_id: int) -> dict[str, Any]:
        with self.lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM monitor_state WHERE chat_id=? ORDER BY COALESCE(last_attempt_at, 0) DESC",
                (chat_id,),
            ).fetchall()
            if not rows:
                return {"last_success_at": None, "next_check_at": None, "last_error": None, "failures": 0}
            last_success = max((int(r["last_success_at"]) for r in rows if r["last_success_at"] is not None), default=None)
            next_check = min((int(r["next_check_at"]) for r in rows), default=None)
            error_row = next((r for r in rows if r["last_error"]), None)
            return {
                "last_success_at": last_success,
                "next_check_at": next_check,
                "last_error": str(error_row["last_error"]) if error_row else None,
                "failures": sum(int(r["consecutive_failures"] or 0) for r in rows),
            }

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
    touch_points: list[tuple[int, float]] = field(default_factory=list)
    valid: bool = True


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
    fib_direction: str
    fib_start_price: float
    fib_end_price: float
    long_probability: float
    short_probability: float
    orderbook_bias: float
    trend_bias: float
    recommendation: str
    text: str
    df: pd.DataFrame
    trendline: TrendLine
    projection: PriceProjection
    fib_start_index: int = 0
    fib_end_index: int = 0


FIB_LEVEL_SEQUENCE = ["0%", "23.6%", "38.2%", "50%", "61.8%", "78.6%", "100%"]
FIB_DISPLAY_SEQUENCE = ["23.6%", "38.2%", "50%", "61.8%", "78.6%", "100%"]
FIB_CHART_SEQUENCE = ["38.2%", "50%", "61.8%", "78.6%"]
FIB_LOOKBACK = 90


def _fmt_plain(value: float) -> str:
    value = float(value)
    if not math.isfinite(value):
        return str(value)
    abs_value = abs(value)
    if abs_value >= 1000:
        decimals = 2
    elif abs_value >= 1:
        decimals = 4
    elif abs_value >= 0.01:
        decimals = 6
    elif abs_value >= 0.0001:
        decimals = 8
    elif abs_value >= 0.000001:
        decimals = 10
    else:
        decimals = 12
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _ordered_fib_items(fib_levels: dict[str, float]) -> list[tuple[str, float]]:
    return [(name, float(fib_levels[name])) for name in FIB_DISPLAY_SEQUENCE if name in fib_levels]


def _chart_fib_items(fib_levels: dict[str, float]) -> list[tuple[str, float]]:
    """Четыре стандартных retracement-уровня, всегда в одном порядке."""
    return [(name, float(fib_levels[name])) for name in FIB_CHART_SEQUENCE if name in fib_levels]


def _spread_label_positions(items: list[dict[str, float | str]], min_gap: float, lower: float, upper: float) -> list[dict[str, float | str]]:
    if not items:
        return []
    ordered = []
    for item in sorted(items, key=lambda x: float(x["y_actual"])):
        target = float(item["y_actual"])
        if ordered:
            target = max(target, float(ordered[-1]["y_label"]) + min_gap)
        placed = dict(item)
        placed["y_label"] = target
        ordered.append(placed)
    overflow = float(ordered[-1]["y_label"]) - upper
    if overflow > 0:
        for item in ordered:
            item["y_label"] = float(item["y_label"]) - overflow
    underflow = lower - float(ordered[0]["y_label"])
    if underflow > 0:
        for item in ordered:
            item["y_label"] = float(item["y_label"]) + underflow
    return ordered


def _trade_plan_values(price: float, support: OrderBookLevel, resistance: OrderBookLevel, projection: PriceProjection) -> tuple[float, float, float, float, float]:
    direction = projection.direction
    entry = float(price)
    stop = float(projection.invalidation)
    if direction == "SHORT":
        stop = max(stop, float(resistance.price))
        if float(support.price) < float(price):
            entry = float(support.price)
    else:
        stop = min(stop, float(support.price))
        if float(resistance.price) > float(price):
            entry = float(resistance.price)
    tp1 = float(projection.target_1)
    tp2 = float(projection.target_2)
    risk = abs(entry - stop)
    reward = abs(tp1 - entry)
    rr = reward / risk if risk > 0 else 0.0
    return float(entry), float(stop), tp1, tp2, float(rr)


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


def _fibonacci_move_threshold(window: pd.DataFrame) -> tuple[float, float]:
    close = window["close"].to_numpy(dtype=float)
    high = window["high"].to_numpy(dtype=float)
    low = window["low"].to_numpy(dtype=float)
    prev_close = np.r_[close[0], close[:-1]]
    true_range = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    atr = float(np.nanmedian(true_range[-14:])) if len(true_range) else 0.0
    price = max(abs(float(close[-1])), 1e-12)
    return max(atr * 1.5, price * 0.004), max(atr * 0.08, price * 0.00025, 1e-12)


def _alternating_structural_pivots(window: pd.DataFrame) -> list[tuple[int, str, float]]:
    highs = [(i, "high", v) for i, v in _filtered_pivots(window, mode="high", lookback=len(window), window=3)]
    lows = [(i, "low", v) for i, v in _filtered_pivots(window, mode="low", lookback=len(window), window=3)]
    events = sorted(highs + lows, key=lambda item: (item[0], 0 if item[1] == "low" else 1))
    collapsed: list[tuple[int, str, float]] = []
    for event in events:
        if not collapsed or collapsed[-1][1] != event[1]:
            collapsed.append(event)
            continue
        prev = collapsed[-1]
        more_extreme = event[2] > prev[2] if event[1] == "high" else event[2] < prev[2]
        if more_extreme:
            collapsed[-1] = event
    return collapsed


def _detect_fibonacci_direction(df: pd.DataFrame, lookback: int = FIB_LOOKBACK) -> str:
    """Направление Fibonacci определяется структурой цены, а не стаканом/вероятностью сделки."""
    window = df.tail(min(lookback, len(df))).reset_index(drop=True)
    if len(window) < 3:
        return "LONG" if float(window["close"].iloc[-1]) >= float(window["close"].iloc[0]) else "SHORT"

    highs = _filtered_pivots(window, mode="high", lookback=len(window), window=3)
    lows = _filtered_pivots(window, mode="low", lookback=len(window), window=3)
    atr = max(_atr_value(window), abs(float(window["close"].iloc[-1])) * 0.0002, 1e-12)
    tolerance = atr * 0.12

    high_sign = 0
    low_sign = 0
    if len(highs) >= 2:
        delta = float(highs[-1][1] - highs[-2][1])
        high_sign = 1 if delta > tolerance else -1 if delta < -tolerance else 0
    if len(lows) >= 2:
        delta = float(lows[-1][1] - lows[-2][1])
        low_sign = 1 if delta > tolerance else -1 if delta < -tolerance else 0

    if high_sign > 0 and low_sign > 0:
        return "LONG"
    if high_sign < 0 and low_sign < 0:
        return "SHORT"

    # При переходной структуре берём последний значимый фактический импульс.
    min_move, _ = _fibonacci_move_threshold(window)
    pivots = _alternating_structural_pivots(window)
    for left, right in zip(reversed(pivots[:-1]), reversed(pivots[1:])):
        if left[1] == right[1]:
            continue
        move = abs(float(right[2] - left[2]))
        if move < min_move:
            continue
        return "LONG" if left[1] == "low" and right[1] == "high" else "SHORT"

    # Без подтверждённых swing-точек используем только движение цены, не стакан.
    return "LONG" if _trend_score(window) >= 0 else "SHORT"


def _best_recent_ordered_fibonacci_move(
    window: pd.DataFrame,
    direction: str,
    min_move: float,
    tolerance: float,
    recent_bars: int = 48,
) -> tuple[int, float, int, float]:
    """Fallback только по свежему участку; старые экстремумы всего окна не используются."""
    n = len(window)
    size = min(max(12, recent_bars), n)
    offset = n - size
    recent = window.iloc[offset:].reset_index(drop=True)
    highs = recent["high"].to_numpy(dtype=float)
    lows = recent["low"].to_numpy(dtype=float)

    if direction == "LONG":
        for end in range(size - 1, 0, -1):
            start = int(np.argmin(lows[:end]))
            start_price = float(lows[start])
            end_price = float(highs[end])
            if end_price - start_price < min_move:
                continue
            if float(np.min(lows[end:])) < start_price - tolerance:
                continue
            return offset + start, start_price, offset + end, end_price
        start = int(np.argmin(lows[:-1])) if size > 1 else 0
        end = start + int(np.argmax(highs[start:]))
        if end <= start:
            end = min(size - 1, start + 1)
        return offset + start, float(lows[start]), offset + end, float(highs[end])

    for end in range(size - 1, 0, -1):
        start = int(np.argmax(highs[:end]))
        start_price = float(highs[start])
        end_price = float(lows[end])
        if start_price - end_price < min_move:
            continue
        if float(np.max(highs[end:])) > start_price + tolerance:
            continue
        return offset + start, start_price, offset + end, end_price
    start = int(np.argmax(highs[:-1])) if size > 1 else 0
    end = start + int(np.argmin(lows[start:]))
    if end <= start:
        end = min(size - 1, start + 1)
    return offset + start, float(highs[start]), offset + end, float(lows[end])


def _select_fibonacci_anchors(df: pd.DataFrame, direction: str, lookback: int = FIB_LOOKBACK) -> tuple[int, float, int, float]:
    """Выбирает свежий структурный импульс, видимый на графике.

    LONG: подтверждённый swing low → фактический максимум после него.
    SHORT: подтверждённый swing high → фактический минимум после него.
    """
    direction = direction.upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    window = df.tail(min(lookback, len(df))).reset_index(drop=True)
    if window.empty:
        raise ValueError("cannot calculate Fibonacci on empty dataframe")
    if len(window) < 8:
        min_move, tolerance = _fibonacci_move_threshold(window)
        return _best_recent_ordered_fibonacci_move(window, direction, min_move, tolerance, recent_bars=len(window))

    min_move, tolerance = _fibonacci_move_threshold(window)
    highs = window["high"].to_numpy(dtype=float)
    lows = window["low"].to_numpy(dtype=float)
    max_endpoint_age = max(18, len(window) // 3)

    if direction == "LONG":
        candidates = _filtered_pivots(window, mode="low", lookback=len(window), window=3)
        for start_idx, start_price in sorted(candidates, key=lambda item: item[0], reverse=True):
            if start_idx >= len(window) - 1:
                continue
            end_idx = start_idx + int(np.argmax(highs[start_idx:]))
            end_price = float(highs[end_idx])
            if len(window) - 1 - end_idx > max_endpoint_age:
                continue
            if end_idx <= start_idx or end_price - start_price < min_move:
                continue
            if float(np.min(lows[start_idx:end_idx + 1])) < start_price - tolerance:
                continue
            if float(np.min(lows[end_idx:])) < start_price - tolerance:
                continue
            return int(start_idx), float(start_price), int(end_idx), end_price
    else:
        candidates = _filtered_pivots(window, mode="high", lookback=len(window), window=3)
        for start_idx, start_price in sorted(candidates, key=lambda item: item[0], reverse=True):
            if start_idx >= len(window) - 1:
                continue
            end_idx = start_idx + int(np.argmin(lows[start_idx:]))
            end_price = float(lows[end_idx])
            if len(window) - 1 - end_idx > max_endpoint_age:
                continue
            if end_idx <= start_idx or start_price - end_price < min_move:
                continue
            if float(np.max(highs[start_idx:end_idx + 1])) > start_price + tolerance:
                continue
            if float(np.max(highs[end_idx:])) > start_price + tolerance:
                continue
            return int(start_idx), float(start_price), int(end_idx), end_price

    return _best_recent_ordered_fibonacci_move(window, direction, min_move, tolerance)


def _fibonacci_levels_from_anchors(direction: str, start_price: float, end_price: float) -> tuple[dict[str, float], float, float]:
    ratios = [
        ("0%", 0.0), ("23.6%", 0.236), ("38.2%", 0.382),
        ("50%", 0.5), ("61.8%", 0.618), ("78.6%", 0.786), ("100%", 1.0),
    ]
    if direction.upper() == "LONG":
        low, high = sorted((float(start_price), float(end_price)))
        return {name: low + (high - low) * ratio for name, ratio in ratios}, low, high
    low, high = sorted((float(end_price), float(start_price)))
    return {name: high - (high - low) * ratio for name, ratio in ratios}, high, low


def _calculate_fibonacci_details(
    df: pd.DataFrame,
    direction: str,
    lookback: int = FIB_LOOKBACK,
) -> tuple[dict[str, float], float, float, int, int]:
    start_idx, start_price, end_idx, end_price = _select_fibonacci_anchors(df, direction, lookback)
    levels, start, end = _fibonacci_levels_from_anchors(direction, start_price, end_price)
    return levels, start, end, int(start_idx), int(end_idx)


def _calculate_fibonacci(df: pd.DataFrame, direction: str, lookback: int = FIB_LOOKBACK) -> tuple[dict[str, float], float, float]:
    levels, start, end, _, _ = _calculate_fibonacci_details(df, direction, lookback)
    return levels, start, end


def fibonacci_levels(df: pd.DataFrame, direction: str = "LONG", lookback: int = FIB_LOOKBACK) -> dict[str, float]:
    levels, _, _ = _calculate_fibonacci(df, direction, lookback)
    return levels

def _pivot_points(series: pd.Series, window: int, mode: str) -> list[tuple[int, float]]:
    """Локальные экстремумы без дублирования плоских макушек/донышек."""
    if mode not in {"high", "low"}:
        raise ValueError("mode must be high or low")
    values = series.to_numpy(dtype=float)
    if len(values) < window * 2 + 1:
        return []
    pivots: list[tuple[int, float]] = []
    for i in range(window, len(values) - window):
        local = values[i - window:i + window + 1]
        extreme = float(np.max(local) if mode == "high" else np.min(local))
        atol = max(abs(extreme) * 1e-12, 1e-15)
        if not np.isclose(values[i], extreme, rtol=0.0, atol=atol):
            continue
        equal_positions = np.flatnonzero(np.isclose(local, extreme, rtol=0.0, atol=atol))
        # Для плато оставляем одну центральную точку, а не несколько соседних swing-точек.
        chosen_position = int(equal_positions[len(equal_positions) // 2])
        if i - window + chosen_position != i:
            continue
        pivots.append((i, float(values[i])))
    return pivots


def _atr_value(df: pd.DataFrame, period: int = 14) -> float:
    if df.empty:
        return 0.0
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    prev_close = np.r_[close[0], close[:-1]]
    true_range = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    recent = true_range[-min(period, len(true_range)):]
    return float(np.nanmedian(recent)) if len(recent) else 0.0


def _filtered_pivots(
    df: pd.DataFrame,
    mode: str,
    lookback: int = 90,
    window: int = 3,
) -> list[tuple[int, float]]:
    """Фильтрует мелкий шум: swing должен иметь заметную локальную выраженность относительно ATR."""
    if df.empty:
        return []
    tail_len = min(lookback, len(df))
    tail = df.tail(tail_len).reset_index(drop=True)
    source = tail["high"] if mode == "high" else tail["low"]
    raw = _pivot_points(source, window=window, mode=mode)
    if len(raw) <= 2:
        return raw

    price = max(abs(float(tail["close"].iloc[-1])), 1e-12)
    atr = max(_atr_value(tail), price * 0.0002)
    min_prominence = max(atr * 0.18, price * 0.00015, 1e-12)
    values = source.to_numpy(dtype=float)
    filtered: list[tuple[int, float]] = []
    radius = max(window * 2, 4)
    for index, value in raw:
        left = values[max(0, index - radius):index]
        right = values[index + 1:min(len(values), index + radius + 1)]
        if not len(left) or not len(right):
            continue
        if mode == "high":
            prominence = float(value - max(float(np.min(left)), float(np.min(right))))
        else:
            prominence = float(min(float(np.max(left)), float(np.max(right))) - value)
        if prominence >= min_prominence:
            filtered.append((index, value))

    if len(filtered) < 2:
        filtered = raw

    # Соседние экстремумы объединяем: для high оставляем самый высокий, для low — самый низкий.
    deduped: list[tuple[int, float]] = []
    for point in filtered:
        if deduped and point[0] - deduped[-1][0] <= max(2, window - 1):
            prev = deduped[-1]
            replace = point[1] > prev[1] if mode == "high" else point[1] < prev[1]
            if replace:
                deduped[-1] = point
        else:
            deduped.append(point)
    return deduped


def _fit_boundary_trendline(
    df: pd.DataFrame,
    mode: str,
    expected_direction: str | None = None,
    lookback: int = 90,
) -> dict[str, Any]:
    """Подбирает линию именно по swing-точкам и запрещает ей проходить сквозь структуру.

    support/low: минимумы не должны оказаться заметно ниже линии.
    resistance/high: максимумы не должны оказаться заметно выше линии.
    """
    if mode not in {"high", "low"}:
        raise ValueError("mode must be high or low")
    if df.empty:
        raise ValueError("cannot build trendline on empty dataframe")

    tail_len = min(lookback, len(df))
    tail = df.tail(tail_len).reset_index(drop=True)
    source = tail["high"] if mode == "high" else tail["low"]
    if tail_len == 1:
        value = float(source.iloc[0])
        return {
            "i1": 0, "y1": value, "i2": 0, "y2": value,
            "slope": 0.0, "touches": [(0, value)], "score": -1.0,
            "valid": False, "tolerance": 0.0, "tail_len": 1,
            "current_value": value,
        }
    pivots = _filtered_pivots(tail, mode=mode, lookback=tail_len, window=3)
    price = max(abs(float(tail["close"].iloc[-1])), 1e-12)
    atr = max(_atr_value(tail), price * 0.0002)
    tolerance = max(atr * 0.22, price * 0.00025, 1e-12)
    recent = pivots[-20:]

    direction_sign = 0
    if expected_direction == "LONG":
        direction_sign = 1
    elif expected_direction == "SHORT":
        direction_sign = -1

    def search(require_direction: bool) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for a in range(len(recent) - 1):
            i1, y1 = recent[a]
            for i2, y2 in recent[a + 1:]:
                span = i2 - i1
                if span < 5:
                    continue
                slope = (y2 - y1) / span
                if require_direction and direction_sign and slope * direction_sign <= 0:
                    continue

                xs = np.arange(i1, tail_len, dtype=float)
                line = y1 + slope * (xs - i1)
                actual = source.iloc[i1:].to_numpy(dtype=float)
                violation = line - actual if mode == "low" else actual - line
                max_violation = max(float(np.max(violation)), 0.0)
                violation_count = int(np.sum(violation > tolerance))
                if violation_count > 0 or max_violation > tolerance:
                    continue

                touches: list[tuple[int, float]] = []
                for px, py in pivots:
                    if px < i1:
                        continue
                    line_y = y1 + slope * (px - i1)
                    if abs(py - line_y) <= tolerance:
                        touches.append((int(px), float(py)))
                if len(touches) < 2:
                    continue

                recency = i2 / max(tail_len - 1, 1)
                span_score = min(span / max(tail_len - 1, 1), 1.0)
                score = len(touches) * 1000 + recency * 250 + span_score * 120 - (max_violation / tolerance) * 100
                if best is None or score > float(best["score"]):
                    best = {
                        "i1": int(i1), "y1": float(y1), "i2": int(i2), "y2": float(y2),
                        "slope": float(slope), "touches": touches, "score": float(score), "valid": True,
                        "tolerance": float(tolerance), "tail_len": int(tail_len),
                    }
        return best

    solution = search(require_direction=True)
    if solution is None:
        # Не подделываем направление: если ожидаемая структура отсутствует, ищем лучшую реальную границу.
        solution = search(require_direction=False)

    if solution is None:
        if len(recent) >= 2:
            (i1, y1), (i2, y2) = recent[-2], recent[-1]
        elif len(recent) == 1:
            i1, y1 = recent[0]
            i2, y2 = min(i1 + 1, tail_len - 1), y1
        else:
            values = source.to_numpy(dtype=float)
            i1 = int(np.argmax(values) if mode == "high" else np.argmin(values))
            y1 = float(values[i1])
            i2, y2 = min(i1 + 1, tail_len - 1), y1
        if i2 == i1:
            i2 = i1 + 1
        slope = (y2 - y1) / max(i2 - i1, 1)
        solution = {
            "i1": int(i1), "y1": float(y1), "i2": int(i2), "y2": float(y2),
            "slope": float(slope), "touches": [(int(i1), float(y1)), (int(i2), float(y2))],
            "score": -1.0, "valid": False, "tolerance": float(tolerance), "tail_len": int(tail_len),
        }

    current_value = float(solution["y1"] + solution["slope"] * ((tail_len - 1) - solution["i1"]))
    solution["current_value"] = current_value
    return solution


def _select_structural_swings(
    df: pd.DataFrame,
    mode: str,
    bearish: bool,
    lookback: int = 90,
    max_points: int = 5,
) -> list[tuple[int, float]]:
    expected = "SHORT" if bearish else "LONG"
    solution = _fit_boundary_trendline(df, mode=mode, expected_direction=expected, lookback=lookback)
    points = list(solution["touches"])
    return points[-max_points:]


def _find_sh_spikes(
    df: pd.DataFrame,
    structural_highs: list[tuple[int, float]],
    bearish: bool,
    lookback: int = 160,
    max_spikes: int = 3,
) -> list[tuple[int, float]]:
    if df.empty or not bearish or len(structural_highs) < 2:
        return []
    tail_len = min(lookback, len(df))
    tail = df.tail(tail_len).reset_index(drop=True)
    pivots = _filtered_pivots(tail, mode="high", lookback=tail_len, window=3)
    structural_idx = {int(i) for i, _ in structural_highs}
    x1, y1 = structural_highs[0]
    x2, y2 = structural_highs[-1]
    if x2 == x1:
        return []
    slope = (y2 - y1) / (x2 - x1)
    price = max(abs(float(tail["close"].iloc[-1])), 1e-12)
    threshold = max(_atr_value(tail) * 0.35, price * 0.0005, 1e-12)
    spikes: list[tuple[int, float, float]] = []
    for x, y in pivots:
        if x in structural_idx:
            continue
        trend_y = y1 + slope * (x - x1)
        excess = float(y) - float(trend_y)
        if excess > threshold:
            spikes.append((int(x), float(y), excess))
    spikes = sorted(spikes, key=lambda item: item[2], reverse=True)[:max_spikes]
    return [(x, y) for x, y, _ in sorted(spikes, key=lambda item: item[0])]


def _build_trendline(
    df: pd.DataFrame,
    trend_bias: float | None = None,
    mode: str | None = None,
    lookback: int = 90,
    expected_direction: str | None = None,
) -> TrendLine:
    bias = float(trend_bias or 0.0)
    if mode not in {"low", "high"}:
        mode = "high" if bias < 0 else "low"
    if expected_direction not in {"LONG", "SHORT"}:
        expected_direction = "SHORT" if bias < 0 else "LONG"

    solution = _fit_boundary_trendline(df, mode=mode, expected_direction=expected_direction, lookback=lookback)
    slope = float(solution["slope"])
    price = max(abs(float(df["close"].iloc[-1])), 1e-12)
    flat_threshold = max(price * 0.00001, float(solution["tolerance"]) / max(int(solution["tail_len"]), 1) * 0.20)
    if abs(slope) <= flat_threshold:
        label = "горизонтальная поддержка по минимумам" if mode == "low" else "горизонтальное сопротивление по максимумам"
    elif slope > 0:
        label = "повышающиеся минимумы" if mode == "low" else "повышающиеся максимумы"
    else:
        label = "понижающиеся минимумы" if mode == "low" else "понижающиеся максимумы"

    i1 = int(solution["i1"])
    y1 = float(solution["y1"])
    current_value = float(solution["current_value"])
    slope_percent = float((current_value / max(abs(y1), 1e-12) - 1.0) * 100)
    touch_points = [(int(x), float(y)) for x, y in solution["touches"]]
    valid = bool(solution["valid"])
    suffix = "" if valid else " · мало подтверждённых точек"
    return TrendLine(
        kind="support" if mode == "low" else "resistance",
        start_index=i1,
        start_price=y1,
        end_index=int(solution["tail_len"] - 1),
        end_price=current_value,
        current_value=current_value,
        slope_percent=slope_percent,
        touches=len(touch_points),
        text=f"{label}: {_fmt_plain(current_value)} ({slope_percent:+.2f}%, касаний: {len(touch_points)}){suffix}",
        touch_points=touch_points,
        valid=valid,
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
        text = f"При пробое/удержании выше {_fmt_plain(trigger)} цель: {_fmt_plain(t1)} → {_fmt_plain(t2)}; отмена ниже {_fmt_plain(inv)}"
    else:
        trigger = min(price, support.price)
        candidates = [v for v in levels if v < trigger - min_gap]
        t1, t2 = _unique_targets(candidates, trigger, "SHORT", step, 2)
        inv_candidates = [v for v in levels if v > max(price, resistance.price) + min_gap]
        inv = inv_candidates[0] if inv_candidates else max(price, resistance.price) + step * 0.8
        direction = "SHORT"
        text = f"При пробое поддержки {_fmt_plain(support.price)} вниз цель: {_fmt_plain(t1)} → {_fmt_plain(t2)}; отмена выше {_fmt_plain(inv)}"
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
    fib_direction = _detect_fibonacci_direction(df, lookback=FIB_LOOKBACK)
    fibs, fib_start_price, fib_end_price, fib_start_index, fib_end_index = _calculate_fibonacci_details(df, fib_direction, lookback=FIB_LOOKBACK)
    projection = _projection(price, fibs, support, resistance, long_probability, interval, df)

    if long_probability >= 58:
        recommendation = "LONG / покупка от поддержки или после пробоя сопротивления"
    elif short_probability >= 58:
        recommendation = "SHORT / продажа от сопротивления или после пробоя поддержки"
    else:
        recommendation = "NEUTRAL / нет сильного перевеса, ждать подтверждения"

    entry, stop, tp1, tp2, rr = _trade_plan_values(price, support, resistance, projection)
    ordered_fibs = _ordered_fib_items(fibs)
    fib_text = "\n".join([f"• Fib {k}: `{_fmt_plain(v)}`" for k, v in ordered_fibs])
    text = (
        f"📊 *{symbol}* · TF `{interval}`\n"
        f"Цена: `{_fmt_plain(price)}`\n\n"
        f"🟢 Поддержка по стакану: `{_fmt_plain(support.price)}` · ликвидность `{support.volume:,.0f}`\n"
        f"🔴 Сопротивление по стакану: `{_fmt_plain(resistance.price)}` · ликвидность `{resistance.volume:,.0f}`\n"
        f"📐 Наклонка: `{trendline.text}`\n\n"
        f"📏 Fibonacci {fib_direction} по структуре: `{_fmt_plain(fib_start_price)}` → `{_fmt_plain(fib_end_price)}`\n"
        f"Уровни по порядку:\n{fib_text}\n\n"
        f"📈 Проходимость LONG: *{long_probability:.1f}%*\n"
        f"📉 Проходимость SHORT: *{short_probability:.1f}%*\n"
        f"🎯 Прогноз движения: `{projection.text}`\n"
        f"📌 Торговый план {projection.direction}:\n"
        f"Вход: `{_fmt_plain(entry)}`\n"
        f"Стоп: `{_fmt_plain(stop)}`\n"
        f"Цели:\n1) `{_fmt_plain(tp1)}`\n2) `{_fmt_plain(tp2)}`\n"
        f"RR TP1: `{rr:.2f}`\n"
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
        fib_direction=fib_direction,
        fib_start_price=fib_start_price,
        fib_end_price=fib_end_price,
        long_probability=long_probability,
        short_probability=short_probability,
        orderbook_bias=orderbook_bias,
        trend_bias=trend_bias,
        recommendation=recommendation,
        text=text,
        df=df,
        trendline=trendline,
        projection=projection,
        fib_start_index=fib_start_index,
        fib_end_index=fib_end_index,
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
from matplotlib.ticker import FuncFormatter
import mplfinance as mpf
import numpy as np



def _fmt(value: float) -> str:
    value = float(value)
    if not math.isfinite(value):
        return str(value)
    abs_value = abs(value)
    if abs_value >= 1000:
        return f"{value:,.2f}"
    if abs_value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if abs_value >= 0.01:
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if abs_value >= 0.0001:
        return f"{value:.8f}".rstrip("0").rstrip(".")
    if abs_value >= 0.000001:
        return f"{value:.10f}".rstrip("0").rstrip(".")
    return f"{value:.12f}".rstrip("0").rstrip(".")


def make_chart(result: AnalysisResult, full_analysis_text: str | None = None) -> Path:
    """Профессиональный белый график: читаемые боковые подписи без наложения и нормальный формат цен."""
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
    fig.subplots_adjust(left=0.045, right=0.64, top=0.89, bottom=bottom_space)

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

    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: _fmt(value)))
    ax.yaxis.offsetText.set_visible(False)
    ax.set_ylabel("Цена (USDT)", color="black", fontweight="bold")
    ax.set_xlabel(f"Свечи {result.interval}", color="black", fontweight="bold")

    right_x = len(df) - 1
    label_x_side = len(df) + 7.8

    entry, stop, tp1, tp2, rr = _trade_plan_values(result.price, result.support, result.resistance, result.projection)

    ax.axhline(stop, color="#ef4444", linewidth=1.15, linestyle="--", alpha=0.9)
    ax.axhline(entry, color="#111827", linewidth=1.15, linestyle="--", alpha=0.9)
    ax.axhline(result.price, color="#6b7280", linewidth=1.1, linestyle="--", alpha=0.7)

    # На графике всегда четыре стандартных уровня: 38.2 / 50 / 61.8 / 78.6.
    fib_items = _chart_fib_items(result.fib_levels)

    for _, level in fib_items:
        ax.axhline(level, color="#05805c", linewidth=1.25, linestyle="--", alpha=0.95)

    # Показываем реальные свечи-якоря, чтобы было видно, откуда именно натянуто Fibonacci.
    fib_start_x = int(np.clip(result.fib_start_index, 0, len(df) - 1))
    fib_end_x = int(np.clip(result.fib_end_index, 0, len(df) - 1))
    ax.annotate(
        "", xy=(fib_end_x, result.fib_end_price), xytext=(fib_start_x, result.fib_start_price),
        arrowprops=dict(arrowstyle="->", color="#7c3aed", linewidth=1.6, linestyle=":"),
        annotation_clip=True,
    )
    ax.scatter(
        [fib_start_x, fib_end_x], [result.fib_start_price, result.fib_end_price],
        s=78, facecolors="#ffffff", edgecolors="#7c3aed", linewidths=2.2, zorder=12,
    )
    # Цены якорей уже указаны в блоке сценария; на свечах оставляем только фиолетовые точки и стрелку.


    bearish = direction == "SHORT"
    expected_direction = "SHORT" if bearish else "LONG"
    high_line = _build_trendline(df, mode="high", expected_direction=expected_direction, lookback=len(df))
    low_line = _build_trendline(df, mode="low", expected_direction=expected_direction, lookback=len(df))
    swing_highs = high_line.touch_points[-5:]
    swing_lows = low_line.touch_points[-5:]
    sh_spikes = _find_sh_spikes(df, swing_highs, bearish=bearish, lookback=len(df), max_spikes=2)

    def draw_swing_structure(line: TrendLine, points: list[tuple[int, float]], line_color: str, prefix: str, dashed: bool = False) -> int:
        ax.plot(
            [line.start_index, line.end_index], [line.start_price, line.end_price],
            color=line_color, linewidth=1.55, alpha=0.98, linestyle="--" if dashed else "-",
        )
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

    high_touches = draw_swing_structure(high_line, swing_highs, "#2563eb", "SH", dashed=False)
    low_touches = draw_swing_structure(low_line, swing_lows, "#2563eb", "SL", dashed=True)

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

    ax.text(
        0.50, -0.085,
        "SH — локальный максимум   ·   SL — локальный минимум   ·   жёлтый круг — swing-точка   ·   фиолетовая стрелка — якоря Fibonacci 0→100",
        transform=ax.transAxes, ha="center", va="top", fontsize=9.5, color="black",
        bbox=dict(boxstyle="round,pad=0.25", facecolor="#ffffff", edgecolor="#9ca3af", linewidth=0.8, alpha=0.95),
        clip_on=False,
    )

    scenario_box = (
        f"СЦЕНАРИЙ: {direction}\n"
        f"LONG: {result.long_probability:.0f}%\n"
        f"SHORT: {result.short_probability:.0f}%\n"
        f"ФИБО {result.fib_direction} (структура): {_fmt(result.fib_start_price)} → {_fmt(result.fib_end_price)}\n"
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

    ax.text(
        0.78, 0.98,
        f"RR TP1: {rr:.2f}\nРынок: {_fmt(result.price)}\nНаклон/св: {candle_move:+.4f}%",
        transform=ax.transAxes,
        va="top",
        ha="right",
        fontsize=12,
        color="black",
        fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.35", facecolor="#ffffff", edgecolor="#2563eb", linewidth=1.3, alpha=0.97),
    )

    actual_lows = [df["low"].min(), result.price, stop, entry, result.projection.target_1, result.projection.target_2, *[v for _, v in fib_items]]
    actual_highs = [df["high"].max(), result.price, stop, entry, result.projection.target_1, result.projection.target_2, *[v for _, v in fib_items]]
    ymin, ymax = min(actual_lows), max(actual_highs)
    pad = max((ymax - ymin) * 0.12, max(result.price, 1e-9) * 0.004)
    ax.set_ylim(ymin - pad, ymax + pad)
    # Нижние 22% оставляем под блок рекомендации, чтобы STOP/ENTRY/Fib не перекрывались с ним.
    lower_bound = ymin + (ymax - ymin) * 0.28
    upper_bound = ymax - (ymax - ymin) * 0.14
    min_gap = max((ymax - ymin) * 0.055, max(result.price, 1e-9) * 0.0030)

    side_labels = [
        {"y_actual": stop, "text": f"СТОП {_fmt(stop)}", "edge": "#ef4444", "text_color": "black", "linewidth": 1.4},
        {"y_actual": entry, "text": f"ВХОД {_fmt(entry)}", "edge": "#111827", "text_color": "black", "linewidth": 1.4},
        {"y_actual": result.price, "text": f"ЦЕНА {_fmt(result.price)}", "edge": "#9ca3af", "text_color": "#374151", "linewidth": 1.2},
    ] + [
        {"y_actual": level, "text": f"Фибо {name} — {_fmt(level)}", "edge": "#047857", "text_color": "#047857", "linewidth": 1.4}
        for name, level in fib_items
    ]

    placed_labels = _spread_label_positions(side_labels, min_gap, lower_bound, upper_bound)

    def draw_side_label(item: dict[str, float | str]) -> None:
        ax.annotate(
            f" {item['text']} ",
            xy=(right_x + 0.30, float(item["y_actual"])),
            xytext=(label_x_side, float(item["y_label"])),
            textcoords="data",
            ha="left",
            va="center",
            fontsize=11.2,
            fontweight="bold",
            color=str(item["text_color"]),
            bbox=dict(boxstyle="round,pad=0.28", facecolor="#ffffff", edgecolor=str(item["edge"]), linewidth=float(item["linewidth"]), alpha=0.98),
            arrowprops=dict(arrowstyle="-", color=str(item["edge"]), linewidth=0.9, shrinkA=0, shrinkB=0),
            annotation_clip=False,
        )

    for item in placed_labels:
        draw_side_label(item)

    arrow_color = "#111827"
    arrow_target = result.projection.target_1
    arrow_text_y = result.price + (ymax - ymin) * (0.035 if direction == "SHORT" else -0.035)
    ax.annotate(
        direction,
        xy=(right_x + 1.7, arrow_target),
        xytext=(right_x - 8.0, arrow_text_y),
        fontsize=16,
        fontweight="bold",
        color="black",
        arrowprops=dict(arrowstyle="->", color=arrow_color, linewidth=1.6),
        annotation_clip=False,
    )

    rec_text = (
        "РЕКОМЕНДАЦИЯ\n"
        f"Вход: {_fmt(entry)}\n"
        f"Стоп: {_fmt(stop)}\n"
        f"Цели:\n1) {_fmt(tp1)}\n2) {_fmt(tp2)}\nRR TP1: {rr:.2f}"
    )
    rec_y = 0.018 if not full_analysis_text else 0.030
    fig.text(
        0.785, rec_y, rec_text,
        color="black",
        fontsize=11.2,
        va="bottom",
        ha="left",
        fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.45", facecolor="#ffffff", edgecolor="#16a34a", linewidth=1.7, alpha=0.98),
    )

    if full_analysis_text:
        clean = re.sub(r"[`*_]", "", full_analysis_text)
        clean = "".join(ch for ch in clean if unicodedata.category(ch) != "So" and ch != "\ufe0f")
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
        if "Анализ:" not in clean:
            structure = "понижающиеся максимумы и понижающиеся минимумы" if direction == "SHORT" else "повышающиеся максимумы и повышающиеся минимумы"
            clean = (
                f"Анализ: Цена формирует {structure}.\n"
                f"Сценарий: {direction}. Вход {_fmt(entry)}, стоп {_fmt(stop)}, цели {_fmt(tp1)} / {_fmt(tp2)}.\n"
                f"Рекомендация: {result.recommendation}\n"
                + clean
            )
        wrapped_lines = []
        for line in clean.splitlines():
            wrapped_lines.extend(textwrap.wrap(line, width=110) if line.strip() else [""])
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

    ax.set_xlim(-1, len(df) + 15.5)

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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
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
        app.add_handler(CommandHandler("log_full", self.log_full_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_router))
        app.add_handler(CallbackQueryHandler(self.callback_router))
        app.add_error_handler(self.global_error_handler)
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

    async def log_full_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass

        log_path = Path(LOG_FILE_PATH)
        rotated = [Path(f"{LOG_FILE_PATH}.{index}") for index in range(3, 0, -1)]
        sources = [item for item in [*rotated, log_path] if item.exists() and item.stat().st_size > 0]
        if not sources:
            await update.message.reply_text("Лог пока пуст.", reply_markup=self.bottom_menu())
            return

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            with tmp_path.open("wb") as output:
                for source in sources:
                    output.write(f"===== {source.name} =====\n".encode("utf-8"))
                    with source.open("rb") as current:
                        while chunk := current.read(1024 * 1024):
                            output.write(chunk)
                    output.write(b"\n")
            with tmp_path.open("rb") as document:
                await update.message.reply_document(
                    document=document,
                    filename="log_full.log",
                    caption="Полный лог бота, включая traceback и ошибки автоотслеживания.",
                )
        finally:
            tmp_path.unlink(missing_ok=True)

    async def global_error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        logging.getLogger("trading-bot").error(
            "UNHANDLED_TELEGRAM_ERROR update=%r error=%r",
            update, error,
            exc_info=(type(error), error, error.__traceback__) if error else None,
        )

    @staticmethod
    def _relative_time(timestamp: int | None, future: bool = False) -> str:
        if timestamp is None:
            return "не запланирован" if future else "ещё не было"
        if future and timestamp <= int(time.time()):
            return "сейчас"
        delta = int(timestamp - time.time()) if future else int(time.time() - timestamp)
        delta = max(delta, 0)
        if delta < 60:
            value = f"{delta} сек."
        elif delta < 3600:
            value = f"{delta // 60} мин."
        else:
            value = f"{delta // 3600} ч. {delta % 3600 // 60} мин."
        return f"через {value}" if future else f"{value} назад"


    def settings_text(self, chat_id: int) -> str:
        user = self.storage.get_user(chat_id)
        symbols = self.storage.list_symbols(chat_id)
        summary = self.storage.monitor_summary(chat_id)
        visualization = "весь анализ внутри картинки" if user.get("visualization") == "split" else "картинка + текстовая подпись"
        auto_status = "on" if bool(user.get("stakan_enabled")) else "off"
        bot_status = "on" if bool(user.get("bot_enabled", 1)) else "off"
        coins = ", ".join(symbols) if symbols else "пусто"
        last_scan = self._relative_time(summary.get("last_success_at"))
        next_scan = self._relative_time(summary.get("next_check_at"), future=True) if auto_status == "on" and symbols else "не запланирован"
        error_text = summary.get("last_error")
        error_line = f"\n⚠️ Последняя ошибка auto: `{html.escape(str(error_text)).replace("`", "'")[:180]}`" if error_text else ""
        return (
            "ℹ️ *Текущие настройки*\n\n"
            f"🪙 Монеты загруженные/добавленные: `{coins}`\n"
            f"🤖 Bot: `{bot_status}`\n"
            f"📚 Auto: `{auto_status}`\n"
            f"⏱ Auto-сканирование стакана: каждые `{int(user.get('monitor_interval_minutes', 30))}` мин.\n"
            f"✅ Последняя успешная проверка: `{last_scan}`\n"
            f"⏭ Следующая проверка: `{next_scan}`\n"
            f"🕯 Таймфрейм: `{user.get('timeframe', '1h')}`\n"
            f"🖼 Визуализация: `{visualization}`"
            f"{error_line}"
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
            "`/info` — показать текущие настройки.\n"
            "`/log_full` — скачать полный лог с ошибками."
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
            ping = await asyncio.to_thread(self.ping_text)
            await update.message.reply_text(ping, reply_markup=self.bottom_menu())
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
            self.storage.set_stakan(chat_id, True)
            symbols = self.storage.list_symbols(chat_id)
            suffix = " Первый скан запущен." if symbols else " Добавьте монеты командой new btc."
            await update.message.reply_text(
                f"Автоотслеживание стакана: включено, каждые {minutes} мин.{suffix}",
                reply_markup=self.bottom_menu(),
            )
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
            symbols = await asyncio.to_thread(self.binance.top_symbols_by_quote_volume, self.config.default_quote, limit)
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
            symbol = await asyncio.to_thread(self.binance.normalize_symbol, coin, self.config.default_quote)
            self.storage.add_symbol(chat_id, symbol)
            await update.message.reply_text(f"Добавлено в память: `{symbol}`", parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            await update.message.reply_text(f"Не удалось добавить: {html.escape(str(exc))}")

    async def del_coin(self, update: Update, coin: str) -> None:
        chat_id = update.effective_chat.id
        try:
            symbol = await asyncio.to_thread(self.binance.normalize_symbol, coin, self.config.default_quote)
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
            symbol = await asyncio.to_thread(self.binance.normalize_symbol, coin, self.config.default_quote)
            order_book = await asyncio.to_thread(self.binance.order_book, symbol, self.config.orderbook_limit)
            klines = await asyncio.to_thread(self.binance.klines, symbol, user["timeframe"], 180)
            result = await asyncio.to_thread(analyze, symbol, user["timeframe"], order_book, klines)
            caption = result.text
            chart = await asyncio.to_thread(make_chart, result, caption if user["visualization"] == "split" else None)

            try:
                with chart.open("rb") as photo:
                    if user["visualization"] == "split":
                        # Режим визуализации: весь анализ внутри картинки, без отдельного текста под фото.
                        await update.message.reply_photo(photo=photo, reply_markup=self.main_keyboard())
                    else:
                        await update.message.reply_photo(photo=photo, caption=caption[:1024], parse_mode=ParseMode.MARKDOWN, reply_markup=self.main_keyboard())
                if user["visualization"] != "split" and len(caption) > 1024:
                    await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)
            finally:
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
            ping = await asyncio.to_thread(self.ping_text)
            await self.safe_menu_update(query, ping, reply_markup=self.main_keyboard())
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

# ===== main =====
LOG_FILE_PATH = os.getenv("BOT_LOG_FILE", "bot_full.log")


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not any(type(handler) is logging.StreamHandler for handler in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    log_path = Path(LOG_FILE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


logger = logging.getLogger("trading-bot")

def main() -> None:
    configure_logging()
    config = get_config()
    storage = Storage()
    binance = BinanceClient(config.binance_base_url)
    bot = TradingBot(config, storage, binance)
    app = bot.build_application()

    async def post_init(application):
        task = application.create_task(monitor_supervisor(application, config, storage, binance), name="orderbook-monitor")
        application.bot_data["monitor_task"] = task
        logger.info("Order book monitor supervisor started")

    app.post_init = post_init
    logger.info("Bot version %s starting", config.bot_version)
    app.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
