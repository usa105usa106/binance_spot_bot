"""Compatibility facade for the canonical SQLite storage in :mod:`bot`."""
from __future__ import annotations

try:
    from . import bot as _runtime
except ImportError:
    import bot as _runtime

Storage = _runtime.Storage

__all__ = ["Storage"]
