from __future__ import annotations

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
