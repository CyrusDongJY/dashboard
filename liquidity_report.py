# -*- coding: utf-8 -*-
"""PNG report for the market liquidity water-level monitor."""
from __future__ import annotations

import os
import tempfile
from typing import Optional

import pandas as pd

from liquidity_monitor import LiquidityResult, PILLARS, score_liquidity_frame


COLORS = {
    "ink": "#17212b",
    "muted": "#667085",
    "line": "#d0d5dd",
    "good": "#16856a",
    "watch": "#d08a16",
    "bad": "#c83b4c",
    "blue": "#2f6f9f",
    "panel": "#f7f9fb",
}


def _score_color(value):
    if value is None:
        return COLORS["line"]
    if value >= 60:
        return COLORS["good"]
    if value >= 40:
        return COLORS["watch"]
    return COLORS["bad"]


def _state_en(state):
    mapping = (
        ("融资管道承压", "Funding pipes under pressure"),
        ("信用收缩", "Credit transmission tightening"),
        ("系统性收紧", "Systemic liquidity tightening"),
        ("宽松扩张", "Liquidity expansion"),
        ("高水位平流", "High stock / neutral flow"),
        ("高水位抽水", "High stock / draining flow"),
        ("边际补水", "Marginal replenishment"),
        ("平衡水位", "Balanced liquidity"),
        ("温和收紧", "Moderate tightening"),
        ("低位修复", "Low-level recovery"),
        ("低水位观察", "Low liquidity watch"),
        ("数据不足", "Insufficient data"),
    )
    for prefix, label in mapping:
        if state.startswith(prefix):
            return label + (" / concentrated" if "分配集中" in state else "")
    return state


def _warning_en(warning):
    mapping = (
        ("RRP", "RRP buffer is nearly depleted; Treasury settlement impact is less cushioned."),
        ("准备金", "Reserve balances are in the sensitivity zone; confirm with repo spreads."),
        ("TGA", "TGA increased by more than $100B over five business days."),
        ("资金分配集中", "Liquidity distribution is concentrated; cap-weighted indexes may overstate breadth."),
        ("融资管道覆盖不足", "Funding-pipe coverage is incomplete; prioritize SOFR/TGCR/EFFR spreads to IORB."),
        ("融资指标处于历史弱分位", "Funding is weak by percentile but remains inside absolute stress guards."),
        ("信用指标处于历史弱分位", "Credit is weak by percentile but remains inside absolute spread guards."),
        ("写入失败", "The daily liquidity result could not be persisted; review the job log."),
    )
    for marker, label in mapping:
        if marker in warning:
            return label
    return "Structural condition requires review in the email detail."


def build_liquidity_history(frame: pd.DataFrame, lookback_days: int = 126):
    """Recompute trailing states without look-ahead for the trend panel."""
    if frame.empty:
        return pd.DataFrame()
    dates = pd.DatetimeIndex(frame.index).drop_duplicates().sort_values()
    rows = []
    for report_date in dates[-lookback_days:]:
        result = score_liquidity_frame(frame.loc[:report_date], str(report_date.date()))
        rows.append({
            "report_date": report_date,
            "composite": result.composite,
            "water_stock": (
                result.pillars.get("water_stock").score
                if result.pillars.get("water_stock") else None
            ),
            "flow_pulse": (
                result.pillars.get("flow_pulse").score
                if result.pillars.get("flow_pulse") else None
            ),
            "coverage": result.coverage,
        })
    return pd.DataFrame(rows)


def generate_liquidity_chart(result: LiquidityResult,
                             history: Optional[pd.DataFrame] = None,
                             output_path: Optional[str] = None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    history = history if history is not None else pd.DataFrame()
    fig = plt.figure(figsize=(14, 9), facecolor="white")
    grid = fig.add_gridspec(
        3, 4, height_ratios=[0.92, 1.45, 1.05],
        width_ratios=[1.05, 1.05, 1.0, 1.0],
        hspace=0.48, wspace=0.36,
    )

    fig.text(0.055, 0.955, "MARKET LIQUIDITY WATERLINE",
             fontsize=17, fontweight="bold", color=COLORS["ink"])
    fig.text(
        0.055, 0.925,
        f"{result.report_date}  |  Shadow monitor  |  {result.calc_version}  |  "
        "Higher score = more liquidity support",
        fontsize=9.5, color=COLORS["muted"],
    )

    ax_score = fig.add_subplot(grid[0, :2])
    ax_score.axis("off")
    score = result.composite
    ax_score.text(
        0.00, 0.62, "--" if score is None else f"{score:.0f}",
        fontsize=54, fontweight="bold", color=_score_color(score),
        va="center",
    )
    ax_score.text(0.20, 0.69, "/ 100", fontsize=13, color=COLORS["muted"])
    ax_score.text(0.20, 0.47, _state_en(result.state),
                  fontsize=15, fontweight="bold", color=COLORS["ink"])
    ax_score.text(
        0.00, 0.12,
        f"Model coverage {result.coverage:.0%}",
        fontsize=10, color=COLORS["muted"],
    )

    context = result.context
    context_parts = []
    for label, key, unit in (
        ("Reserves", "reserves_t", "T"),
        ("TGA", "tga_b", "B"),
        ("RRP", "rrp_b", "B"),
        ("SOFR", "sofr_rate", "%"),
    ):
        value = context.get(key)
        if value is not None:
            context_parts.append(f"{label} {value:.2f}{unit}")
    ax_score.text(
        0.00, -0.08, "   |   ".join(context_parts) or "Official context unavailable",
        fontsize=9.5, color=COLORS["ink"], ha="left",
    )

    ax_quad = fig.add_subplot(grid[0, 2:])
    stock = result.pillars.get("water_stock")
    pulse = result.pillars.get("flow_pulse")
    stock_value = stock.score if stock and stock.score is not None else None
    pulse_value = pulse.score if pulse and pulse.score is not None else None
    ax_quad.axhspan(50, 100, color=COLORS["good"], alpha=0.05)
    ax_quad.axvspan(50, 100, color=COLORS["blue"], alpha=0.04)
    ax_quad.axhline(50, color=COLORS["line"], linewidth=1)
    ax_quad.axvline(50, color=COLORS["line"], linewidth=1)
    if stock_value is not None and pulse_value is not None:
        ax_quad.scatter(
            [pulse_value], [stock_value], s=165,
            color=_score_color(result.composite), edgecolor="white",
            linewidth=1.5, zorder=4,
        )
        point_offset = (7, -12) if stock_value >= 88 else (7, 7)
        ax_quad.annotate(
            "Current", (pulse_value, stock_value), xytext=point_offset,
            textcoords="offset points", fontsize=9, color=COLORS["ink"],
        )
    else:
        ax_quad.text(
            0.5, 0.5, "Insufficient data",
            transform=ax_quad.transAxes, ha="center", va="center",
            fontsize=10, color=COLORS["muted"],
        )
    ax_quad.set_xlim(0, 100)
    ax_quad.set_ylim(0, 100)
    ax_quad.set_xlabel("Flow pulse", fontsize=9)
    ax_quad.set_ylabel("System stock", fontsize=9)
    ax_quad.set_title("Liquidity quadrant", loc="left",
                      fontsize=11, fontweight="bold")
    ax_quad.tick_params(labelsize=8, colors=COLORS["muted"])
    for spine in ("top", "right"):
        ax_quad.spines[spine].set_visible(False)

    ax_bars = fig.add_subplot(grid[1, :3])
    keys = list(PILLARS)
    labels = [PILLARS[key][1] for key in keys]
    values = [
        result.pillars[key].score
        if key in result.pillars and result.pillars[key].score is not None
        else 0
        for key in keys
    ]
    colors = [
        _score_color(result.pillars[key].score)
        if key in result.pillars else COLORS["line"]
        for key in keys
    ]
    y = list(range(len(keys)))
    ax_bars.barh(y, [100] * len(y), color=COLORS["panel"], height=0.56)
    ax_bars.barh(y, values, color=colors, height=0.56)
    for index, key in enumerate(keys):
        pillar = result.pillars.get(key)
        value_text = "--" if not pillar or pillar.score is None else f"{pillar.score:.0f}"
        coverage = 0 if not pillar else pillar.coverage
        ax_bars.text(101.5, index, value_text, va="center",
                     fontsize=10, fontweight="bold", color=COLORS["ink"])
        ax_bars.text(111, index, f"cov {coverage:.0%}", va="center",
                     fontsize=8.5, color=COLORS["muted"])
    ax_bars.set_yticks(y, labels=labels, fontsize=9)
    ax_bars.set_xlim(0, 125)
    ax_bars.invert_yaxis()
    ax_bars.set_title("Six-pillar transmission map", loc="left",
                      fontsize=11, fontweight="bold")
    ax_bars.set_xticks([0, 25, 50, 75, 100])
    ax_bars.grid(axis="x", alpha=0.15)
    ax_bars.tick_params(axis="x", labelsize=8, colors=COLORS["muted"])
    for spine in ("top", "right", "left"):
        ax_bars.spines[spine].set_visible(False)

    ax_notes = fig.add_subplot(grid[1, 3])
    ax_notes.axis("off")
    ax_notes.set_title("What moves the gauge", loc="left",
                       fontsize=11, fontweight="bold")
    y_pos = 0.92
    ax_notes.text(0, y_pos, "SUPPORT", fontsize=8.5,
                  fontweight="bold", color=COLORS["good"])
    y_pos -= 0.09
    supports = result.supports or ["No high-confidence support"]
    for item in supports[:3]:
        ax_notes.text(0, y_pos, f"+ {item}", fontsize=9,
                      color=COLORS["ink"], wrap=True)
        y_pos -= 0.09
    y_pos -= 0.02
    ax_notes.text(0, y_pos, "DRAG", fontsize=8.5,
                  fontweight="bold", color=COLORS["bad"])
    y_pos -= 0.09
    drags = result.drags or ["No high-confidence drag"]
    for item in drags[:3]:
        ax_notes.text(0, y_pos, f"- {item}", fontsize=9,
                      color=COLORS["ink"], wrap=True)
        y_pos -= 0.09
    if result.warnings:
        y_pos -= 0.02
        ax_notes.text(0, y_pos, "STRUCTURAL WATCH", fontsize=8.5,
                      fontweight="bold", color=COLORS["watch"])
        y_pos -= 0.08
        warning = _warning_en(result.warnings[0])
        ax_notes.text(0, y_pos, warning[:110], fontsize=8.3,
                      color=COLORS["muted"], wrap=True, va="top")

    ax_trend = fig.add_subplot(grid[2, :])
    if not history.empty:
        history = history.copy()
        history["report_date"] = pd.to_datetime(history["report_date"])
        for key, label, color, width in (
            ("composite", "Composite", COLORS["ink"], 2.2),
            ("water_stock", "System stock", COLORS["blue"], 1.4),
            ("flow_pulse", "Flow pulse", COLORS["good"], 1.4),
        ):
            if key in history and history[key].notna().any():
                ax_trend.plot(
                    history["report_date"], history[key],
                    label=label, color=color, linewidth=width,
                )
        ax_trend.legend(frameon=False, ncol=3, loc="upper left", fontsize=8.5)
    else:
        ax_trend.text(
            0.5, 0.5, "History is accumulating",
            transform=ax_trend.transAxes, ha="center", va="center",
            color=COLORS["muted"],
        )
    ax_trend.axhspan(0, 35, color=COLORS["bad"], alpha=0.05)
    ax_trend.axhline(50, color=COLORS["line"], linewidth=0.9, linestyle="--")
    ax_trend.set_ylim(0, 100)
    ax_trend.set_title("Trailing waterline (no look-ahead)", loc="left",
                       fontsize=11, fontweight="bold")
    ax_trend.grid(alpha=0.15)
    ax_trend.tick_params(labelsize=8, colors=COLORS["muted"])
    for spine in ("top", "right"):
        ax_trend.spines[spine].set_visible(False)

    fig.text(
        0.055, 0.018,
        "Research only. DIX / FINRA short-volume / GEX are context-only and "
        "do not change the score. Not connected to alerts or position sizing.",
        fontsize=8.3, color=COLORS["muted"],
    )

    path = output_path or os.path.join(
        tempfile.gettempdir(), f"liquidity_waterline_{result.report_date}.png")
    output_dir = os.path.dirname(path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    fig.savefig(path, dpi=155, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path
