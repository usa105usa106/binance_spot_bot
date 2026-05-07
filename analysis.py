from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    long_probability: float
    short_probability: float
    orderbook_bias: float
    trend_bias: float
    recommendation: str
    text: str
    df: pd.DataFrame
    trendline: TrendLine
    projection: PriceProjection


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


def fibonacci_levels(df: pd.DataFrame, lookback: int = 160) -> dict[str, float]:
    window = df.tail(min(lookback, len(df)))
    high = float(window["high"].max())
    low = float(window["low"].min())
    diff = high - low
    return {
        "0%": high,
        "23.6%": high - diff * 0.236,
        "38.2%": high - diff * 0.382,
        "50%": high - diff * 0.5,
        "61.8%": high - diff * 0.618,
        "78.6%": high - diff * 0.786,
        "100%": low,
    }


def _pivot_points(series: pd.Series, window: int, mode: str) -> list[tuple[int, float]]:
    values = series.to_numpy(dtype=float)
    pivots: list[tuple[int, float]] = []
    for i in range(window, len(values) - window):
        local = values[i - window:i + window + 1]
        if mode == "low" and values[i] == np.min(local):
            pivots.append((i, float(values[i])))
        if mode == "high" and values[i] == np.max(local):
            pivots.append((i, float(values[i])))
    return pivots


def _build_trendline(df: pd.DataFrame, trend_bias: float) -> TrendLine:
    # При восходящем уклоне строим наклонку по повышающимся минимумам, при нисходящем — по понижающимся максимумам.
    mode = "low" if trend_bias >= 0 else "high"
    kind = "support" if mode == "low" else "resistance"
    label = "наклонная поддержка по минимумам" if mode == "low" else "наклонное сопротивление по максимумам"
    source = df["low"] if mode == "low" else df["high"]
    pivots = _pivot_points(source.tail(140).reset_index(drop=True), window=3, mode=mode)

    if len(pivots) >= 2:
        # Берем две наиболее свежие опорные точки с правильной структурой, иначе последние две.
        chosen = None
        recent = pivots[-8:]
        for a in range(len(recent) - 2, -1, -1):
            p1 = recent[a]
            for p2 in recent[a + 1:]:
                rising_lows = mode == "low" and p2[1] >= p1[1]
                falling_highs = mode == "high" and p2[1] <= p1[1]
                if rising_lows or falling_highs:
                    chosen = (p1, p2)
        if chosen is None:
            chosen = (pivots[-2], pivots[-1])
        (i1, y1), (i2, y2) = chosen
    else:
        # Fallback: линейная регрессия по low/high на видимом участке.
        source_tail = source.tail(140).reset_index(drop=True)
        x = np.arange(len(source_tail), dtype=float)
        slope, intercept = np.polyfit(x, source_tail.to_numpy(dtype=float), 1)
        i1, i2 = 0, len(source_tail) - 1
        y1, y2 = float(intercept), float(intercept + slope * i2)

    if i2 == i1:
        i2 = i1 + 1
    slope = (y2 - y1) / (i2 - i1)
    current_value = float(y1 + slope * ((min(140, len(df)) - 1) - i1))
    slope_percent = float((current_value / max(y1, 1e-9) - 1) * 100)

    # Количество касаний около линии, чтобы показать надежность наклонки.
    tail = df.tail(140).reset_index(drop=True)
    line = np.array([y1 + slope * (i - i1) for i in range(len(tail))], dtype=float)
    tolerance = max(float(tail["close"].iloc[-1]) * 0.004, 1e-12)
    touches_source = tail["low"].to_numpy(dtype=float) if mode == "low" else tail["high"].to_numpy(dtype=float)
    touches = int(np.sum(np.abs(touches_source - line) <= tolerance))

    return TrendLine(
        kind=kind,
        start_index=int(i1),
        start_price=float(y1),
        end_index=int(len(tail) - 1),
        end_price=float(current_value),
        current_value=current_value,
        slope_percent=slope_percent,
        touches=touches,
        text=f"{label}: {current_value:.6g} ({slope_percent:+.2f}%, касаний: {touches})",
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
        text = f"При удержании поддержки цель: {t1:.6g} → {t2:.6g}; отмена ниже {inv:.6g}"
    else:
        t1 = below[-1] if below else price * 0.985
        t2 = below[-2] if len(below) > 1 else t1 * 0.988
        inv = above[0] if above else price * 1.015
        direction = "SHORT"
        text = f"При пробое вниз цель: {t1:.6g} → {t2:.6g}; отмена выше {inv:.6g}"
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
    fibs = fibonacci_levels(df)

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
    projection = _projection(price, fibs, support, resistance, long_probability)

    if long_probability >= 58:
        recommendation = "LONG / покупка от поддержки или после пробоя сопротивления"
    elif short_probability >= 58:
        recommendation = "SHORT / продажа от сопротивления или после пробоя поддержки"
    else:
        recommendation = "NEUTRAL / нет сильного перевеса, ждать подтверждения"

    nearest_fibs = sorted(fibs.items(), key=lambda x: abs(x[1] - price))[:4]
    fib_text = "\n".join([f"• Fib {k}: `{v:.6g}`" for k, v in nearest_fibs])
    text = (
        f"📊 *{symbol}* · TF `{interval}`\n"
        f"Цена: `{price:.6g}`\n\n"
        f"🟢 Поддержка по стакану: `{support.price:.6g}` · ликвидность `{support.volume:,.0f}`\n"
        f"🔴 Сопротивление по стакану: `{resistance.price:.6g}` · ликвидность `{resistance.volume:,.0f}`\n"
        f"📐 Наклонка: `{trendline.text}`\n\n"
        f"📏 Ближайшие Fibonacci:\n{fib_text}\n\n"
        f"📈 Проходимость LONG: *{long_probability:.1f}%*\n"
        f"📉 Проходимость SHORT: *{short_probability:.1f}%*\n"
        f"🎯 Прогноз движения: `{projection.text}`\n"
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
        long_probability=long_probability,
        short_probability=short_probability,
        orderbook_bias=orderbook_bias,
        trend_bias=trend_bias,
        recommendation=recommendation,
        text=text,
        df=df,
        trendline=trendline,
        projection=projection,
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
    return float(max(values))
