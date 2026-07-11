"""Compatibility facade for the canonical single-file runtime.

The deployed application is implemented in :mod:`bot`.  Keeping a second copy
of the analysis engine here previously caused the modular path to drift and
crash.  This module now re-exports the exact runtime objects, including private
helpers used by regression tests and legacy imports.
"""
from __future__ import annotations

try:  # package import
    from . import bot as _runtime
except ImportError:  # Railway/root import
    import bot as _runtime

for _name, _value in vars(_runtime).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

__all__ = [name for name in vars(_runtime) if not name.startswith("__")]
