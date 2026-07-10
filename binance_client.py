from __future__ import annotations

import os
import threading
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
