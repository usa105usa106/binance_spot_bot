from __future__ import annotations

import time
from typing import Any

import requests


class BinanceClient:
    def __init__(self, base_url: str, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._symbols_cache: tuple[float, set[str]] | None = None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        response = requests.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

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
