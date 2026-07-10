from __future__ import annotations

import math
import tempfile
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import mplfinance as mpf

try:
    from .analysis import AnalysisResult, _chart_fib_items
except ImportError:  # direct module execution / Railway root
    from analysis import AnalysisResult, _chart_fib_items


def _fmt(value: float) -> str:
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


def make_chart(result: AnalysisResult, full_analysis_text: str | None = None) -> Path:
    """Создает крупный, читаемый PNG 1920x1080 с уровнями Fib, стаканом, наклонкой и прогнозом."""
    df = result.df.tail(140).copy()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.close()
    out = Path(tmp.name)

    market_colors = mpf.make_marketcolors(up="#14b87a", down="#ef4444", edge="inherit", wick="inherit", volume="inherit")
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=market_colors,
        gridstyle="-",
        gridcolor="#1f2a3a",
        facecolor="#0b1220",
        figcolor="#08111f",
        rc={
            "font.size": 13,
            "axes.labelsize": 13,
            "axes.titlesize": 18,
            "xtick.labelsize": 12,
            "ytick.labelsize": 12,
        },
    )

    fig, axes = mpf.plot(
        df,
        type="candle",
        volume=True,
        style=style,
        returnfig=True,
        figsize=(19.2, 10.8),
        tight_layout=False,
        panel_ratios=(5, 1),
        datetime_format="%d.%m %H:%M",
        xrotation=0,
        warn_too_much_data=300,
    )
    fig.subplots_adjust(left=0.055, right=0.86, top=0.90, bottom=0.11, hspace=0.06)
    ax = axes[0]
    vol_ax = axes[2] if len(axes) > 2 else axes[-1]
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: _fmt(value)))
    ax.yaxis.offsetText.set_visible(False)

    title = f"{result.symbol} · BINANCE SPOT · TF {result.interval} · цена {_fmt(result.price)}"
    ax.set_title(title, loc="left", color="white", pad=18, fontsize=21, fontweight="bold")

    # Горизонтальные уровни стакана.
    ax.axhline(result.support.price, color="#22c55e", linewidth=2.4, linestyle="-", alpha=0.95)
    ax.axhline(result.resistance.price, color="#f43f5e", linewidth=2.4, linestyle="-", alpha=0.95)

    right_x = len(df) - 1
    ax.text(right_x + 1, result.support.price, f"  SUPPORT стакан {_fmt(result.support.price)}", color="#22c55e", va="center", fontsize=12, fontweight="bold")
    ax.text(right_x + 1, result.resistance.price, f"  RESISTANCE стакан {_fmt(result.resistance.price)}", color="#f43f5e", va="center", fontsize=12, fontweight="bold")

    # Fibonacci по выбранному таймфрейму.
    fib_colors = {
        "0%": "#e5e7eb",
        "23.6%": "#f97316",
        "38.2%": "#facc15",
        "50%": "#a3e635",
        "61.8%": "#2dd4bf",
        "78.6%": "#60a5fa",
        "100%": "#e5e7eb",
    }
    for name in ["0%", "23.6%", "38.2%", "50%", "61.8%", "78.6%", "100%"]:
        if name not in result.fib_levels:
            continue
        level = result.fib_levels[name]
        color = fib_colors.get(name, "#94a3b8")
        ax.axhline(level, color=color, linewidth=1.35, linestyle="--", alpha=0.82)
        ax.text(right_x + 1, level, f"  Fib {name}  {_fmt(level)}", color=color, va="center", fontsize=11, fontweight="bold")

    # Наклонная трендовая линия: сверху при сопротивлении, снизу при поддержке.
    tl = result.trendline
    trend_color = "#22c55e" if tl.kind == "support" else "#f59e0b"
    ax.plot([tl.start_index, tl.end_index], [tl.start_price, tl.end_price], color=trend_color, linewidth=2.8, alpha=0.95)
    ax.scatter([tl.start_index, tl.end_index], [tl.start_price, tl.end_price], color=trend_color, s=55, zorder=5)
    ax.text(max(1, tl.end_index - 30), tl.end_price, f"  {tl.text}", color=trend_color, fontsize=12, fontweight="bold", va="bottom")

    # Прогноз: стрелка и цели движения по ближайшим уровням.
    proj = result.projection
    arrow_color = "#22c55e" if proj.direction == "LONG" else "#ef4444"
    future_x1 = right_x + 8
    future_x2 = right_x + 20
    ax.annotate(
        "",
        xy=(future_x1, proj.target_1),
        xytext=(right_x, result.price),
        arrowprops=dict(arrowstyle="->", color=arrow_color, linewidth=2.6, linestyle="--"),
        annotation_clip=False,
    )
    ax.annotate(
        "",
        xy=(future_x2, proj.target_2),
        xytext=(future_x1, proj.target_1),
        arrowprops=dict(arrowstyle="->", color=arrow_color, linewidth=2.2, linestyle="--"),
        annotation_clip=False,
    )
    ax.text(future_x1, proj.target_1, f"  Цель 1 {_fmt(proj.target_1)} ({proj.move_1_percent:+.2f}%)", color=arrow_color, fontsize=12, fontweight="bold", va="center")
    ax.text(future_x2, proj.target_2, f"  Цель 2 {_fmt(proj.target_2)} ({proj.move_2_percent:+.2f}%)", color=arrow_color, fontsize=12, fontweight="bold", va="center")
    ax.axhline(proj.invalidation, color="#94a3b8", linewidth=1.2, linestyle=":", alpha=0.75)
    ax.text(1, proj.invalidation, f"Отмена сценария: {_fmt(proj.invalidation)}", color="#cbd5e1", fontsize=10, va="center")

    # Информационная панель на графике.
    info = (
        f"LONG {result.long_probability:.1f}%  |  SHORT {result.short_probability:.1f}%\n"
        f"Прогноз: {proj.direction}\n"
        f"Фибо {result.fib_direction}: {_fmt(result.fib_start_price)} → {_fmt(result.fib_end_price)}\n"
        f"{proj.text}\n"
        f"Стакан: {result.orderbook_bias * 100:+.1f}% · Тренд: {result.trend_bias * 100:+.1f}%"
    )
    ax.text(
        0.012, 0.965, info,
        transform=ax.transAxes,
        fontsize=13,
        color="white",
        va="top",
        bbox=dict(boxstyle="round,pad=0.55", facecolor="#0f172a", edgecolor="#334155", alpha=0.92),
    )

    # Чтобы справа поместились подписи и стрелки прогноза.
    ax.set_xlim(-2, len(df) + 28)
    lows = [df["low"].min(), *result.fib_levels.values(), result.support.price, proj.target_1, proj.target_2, proj.invalidation]
    highs = [df["high"].max(), *result.fib_levels.values(), result.resistance.price, proj.target_1, proj.target_2, proj.invalidation]
    ymin, ymax = min(lows), max(highs)
    pad = max((ymax - ymin) * 0.10, result.price * 0.005)
    ax.set_ylim(ymin - pad, ymax + pad)

    ax.set_ylabel("Цена", color="#cbd5e1")
    vol_ax.set_ylabel("Объем", color="#cbd5e1")
    fig.text(0.055, 0.035, "Аналитический сигнал. Не является финансовой рекомендацией.", color="#94a3b8", fontsize=11)

    fig.savefig(out, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out
