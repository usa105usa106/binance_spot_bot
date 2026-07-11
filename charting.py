"""Compatibility facade for the canonical chart renderer in :mod:`bot`."""
from __future__ import annotations

try:
    from . import bot as _runtime
except ImportError:
    import bot as _runtime

AnalysisResult = _runtime.AnalysisResult
make_chart = _runtime.make_chart
_fmt = _runtime._fmt
_chart_fib_items = _runtime._chart_fib_items
_trade_plan_metrics = _runtime._trade_plan_metrics
_plan_expectancy_r = _runtime._plan_expectancy_r

__all__ = [
    "AnalysisResult",
    "make_chart",
    "_fmt",
    "_chart_fib_items",
    "_trade_plan_metrics",
    "_plan_expectancy_r",
]
