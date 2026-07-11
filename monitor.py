"""Compatibility facade for the canonical auto-monitor in :mod:`bot`."""
from __future__ import annotations

try:
    from . import bot as _runtime
except ImportError:
    import bot as _runtime

compact_orderbook_signature = _runtime.compact_orderbook_signature
signature_change_percent = _runtime.signature_change_percent
_monitor_one_symbol = _runtime._monitor_one_symbol
monitor_orderbooks = _runtime.monitor_orderbooks
monitor_supervisor = _runtime.monitor_supervisor

__all__ = [
    "compact_orderbook_signature",
    "signature_change_percent",
    "_monitor_one_symbol",
    "monitor_orderbooks",
    "monitor_supervisor",
]
