"""Compatibility facade for runtime configuration in :mod:`bot`."""
from __future__ import annotations

try:
    from . import bot as _runtime
except ImportError:
    import bot as _runtime

Config = _runtime.Config
get_config = _runtime.get_config

__all__ = ["Config", "get_config"]
