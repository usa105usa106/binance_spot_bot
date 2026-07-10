from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import math

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


def _format_price(value: float) -> str:
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
        text=f"{label}: {_format_price(current_value)} ({slope_percent:+.2f}%, касаний: {len(touch_points)}){suffix}",
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


def _projection(price: float, fibs: dict[str, float], support: OrderBookLevel, resistance: OrderBookLevel, long_probability: float) -> PriceProjection:
    levels = sorted(set([*fibs.values(), support.price, resistance.price]))
    above = [v for v in levels if v > price]
    below = [v for v in levels if v < price]
    if long_probability >= 50:
        t1 = above[0] if above else price * 1.015
        t2 = above[1] if len(above) > 1 else t1 * 1.012
        inv = below[-1] if below else price * 0.985
        direction = "LONG"
        text = f"При удержании поддержки цель: {_format_price(t1)} → {_format_price(t2)}; отмена ниже {_format_price(inv)}"
    else:
        t1 = below[-1] if below else price * 0.985
        t2 = below[-2] if len(below) > 1 else t1 * 0.988
        inv = above[0] if above else price * 1.015
        direction = "SHORT"
        text = f"При пробое вниз цель: {_format_price(t1)} → {_format_price(t2)}; отмена выше {_format_price(inv)}"
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
    projection = _projection(price, fibs, support, resistance, long_probability)

    if long_probability >= 58:
        recommendation = "LONG / покупка от поддержки или после пробоя сопротивления"
    elif short_probability >= 58:
        recommendation = "SHORT / продажа от сопротивления или после пробоя поддержки"
    else:
        recommendation = "NEUTRAL / нет сильного перевеса, ждать подтверждения"

    entry, stop, tp1, tp2, rr = _trade_plan_values(price, support, resistance, projection)
    ordered_fibs = _ordered_fib_items(fibs)
    fib_text = "\n".join([f"• Fib {k}: `{_format_price(v)}`" for k, v in ordered_fibs])
    text = (
        f"📊 *{symbol}* · TF `{interval}`\n"
        f"Цена: `{_format_price(price)}`\n\n"
        f"🟢 Поддержка по стакану: `{_format_price(support.price)}` · ликвидность `{support.volume:,.0f}`\n"
        f"🔴 Сопротивление по стакану: `{_format_price(resistance.price)}` · ликвидность `{resistance.volume:,.0f}`\n"
        f"📐 Наклонка: `{trendline.text}`\n\n"
        f"📏 Fibonacci {fib_direction} по структуре: `{_format_price(fib_start_price)}` → `{_format_price(fib_end_price)}`\n"
        f"Уровни по порядку:\n{fib_text}\n\n"
        f"📈 Проходимость LONG: *{long_probability:.1f}%*\n"
        f"📉 Проходимость SHORT: *{short_probability:.1f}%*\n"
        f"🎯 Прогноз движения: `{projection.text}`\n"
        f"📌 Торговый план {projection.direction}:\n"
        f"Вход: `{_format_price(entry)}`\n"
        f"Стоп: `{_format_price(stop)}`\n"
        f"Цели:\n1) `{_format_price(tp1)}`\n2) `{_format_price(tp2)}`\n"
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
    return float(max(values)) if values else 0.0
