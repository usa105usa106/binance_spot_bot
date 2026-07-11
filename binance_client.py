"""Compatibility facade for the canonical Binance client in :mod:`bot`."""
from __future__ import annotations

try:
    from . import bot as _runtime
except ImportError:
    import bot as _runtime

BinanceAPIError = _runtime.BinanceAPIError
BinanceClient = _runtime.BinanceClient

__all__ = ["BinanceAPIError", "BinanceClient"]
