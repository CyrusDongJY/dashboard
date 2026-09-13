# -*- coding: utf-8 -*-
"""Compact chart for cross-panel temporal shadow states."""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from temporal_monitor import PANEL_SPECS


LINE_COLORS = {
    "environment": "#b42318",
    "liquidity": "#175cd3",
    "tactical_stress": "#7a5af8",
    "risk_capital": "#b54708",
}

PANEL_LABELS_EN = {
    "environment": "Environment",
    "liquidity": "Liquidity pressure",
    "tactical_stress": "Tactical stress",
    "risk_capital": "Risk-capital contraction",
    "event_pulse": "Event pulse",
}


def _state_heat_value(row):
    code = str(row.get("temporal_state") or "")
    if code in {"INSUFFICIENT_DATA"}:
        return np.nan
    if code in {"NEW_PRESSURE", "PRESSURE_BUILDING", "PRESSURE_PERSISTENT",
                "HIGH_PRESSURE", "OVERHEAT"}:
        return 2.0
    if code == "SUDDEN_TURN":
        direction = (row.get("details") or {}).get("turn_direction")
        return 2.0 if direction == 1 else -1.0
    if code in {"TURN_WATCH", "MIXED", "ISOLATED_EVENT"}:
        return 1.0
    if code in {"RELIEF", "RECOVERY", "RISK_APPETITE_EXPANDING",
                "RISK_APPETITE_PERSISTENT"}:
        return -1.0
    return 0.0


def generate_temporal_chart(history_rows, report_date, lookback_days=63,
                            output_path=None):
    """Render normalized pressure curves and a state persistence strip."""
    if not history_rows:
        return None
    frame = pd.DataFrame(history_rows)
    if frame.empty or "report_date" not in frame:
        return None
    frame["report_date"] = pd.to_datetime(frame["report_date"], errors="coerce")
    frame = frame.dropna(subset=["report_date"])
    dates = sorted(frame["report_date"].drop_duplicates().tolist())[-lookback_days:]
    if not dates:
        return None
    frame = frame[frame["report_date"].isin(dates)]

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    fig = plt.figure(figsize=(14, 7.8), facecolor="white")
    grid = fig.add_gridspec(2, 1, height_ratios=[2.15, 1.0], hspace=0.34)
    ax_curve = fig.add_subplot(grid[0])
    for panel in LINE_COLORS:
        series = frame[frame["panel"] == panel].sort_values("report_date")
        if series.empty or series["risk_value"].notna().sum() == 0:
            continue
        ax_curve.plot(
            series["report_date"], series["risk_value"], linewidth=1.9,
            marker=".", markersize=3.0, color=LINE_COLORS[panel],
            label=PANEL_LABELS_EN[panel],
        )
    ax_curve.axhspan(65, 100, color="#d92d20", alpha=0.06)
    ax_curve.axhline(65, color="#d92d20", linewidth=0.9, linestyle="--")
    ax_curve.axhline(35, color="#039855", linewidth=0.8, linestyle=":")
    ax_curve.set_ylim(0, 100)
    ax_curve.set_ylabel("Normalized risk pressure")
    ax_curve.set_title(
        f"Cross-panel temporal shadow | {report_date}\n"
        "Higher = more pressure; replay history is not prospective validation",
        loc="left", fontsize=13, fontweight="bold")
    ax_curve.grid(alpha=0.15)
    ax_curve.legend(frameon=False, ncol=2, fontsize=9, loc="upper left")
    for spine in ("top", "right"):
        ax_curve.spines[spine].set_visible(False)

    ax_state = fig.add_subplot(grid[1])
    panels = list(PANEL_SPECS)
    date_index = {pd.Timestamp(date): index for index, date in enumerate(dates)}
    heat = np.full((len(panels), len(dates)), np.nan)
    for row in frame.to_dict("records"):
        panel = row.get("panel")
        date = pd.Timestamp(row.get("report_date"))
        if panel in panels and date in date_index:
            heat[panels.index(panel), date_index[date]] = _state_heat_value(row)
    cmap = ListedColormap(["#12b76a", "#eaecf0", "#f79009", "#d92d20"])
    norm = BoundaryNorm([-1.5, -0.5, 0.5, 1.5, 2.5], cmap.N)
    ax_state.imshow(heat, aspect="auto", interpolation="nearest",
                    cmap=cmap, norm=norm)
    ax_state.set_yticks(
        range(len(panels)), [PANEL_LABELS_EN[panel] for panel in panels],
        fontsize=9)
    tick_count = min(7, len(dates))
    tick_positions = np.linspace(0, len(dates) - 1, tick_count, dtype=int)
    ax_state.set_xticks(
        tick_positions,
        [pd.Timestamp(dates[index]).strftime("%m-%d") for index in tick_positions],
        fontsize=8)
    ax_state.set_title("Temporal state persistence", loc="left",
                       fontsize=11, fontweight="bold")
    ax_state.legend(handles=[
        Patch(facecolor="#d92d20", label="Pressure"),
        Patch(facecolor="#f79009", label="Watch / mixed"),
        Patch(facecolor="#eaecf0", label="Stable"),
        Patch(facecolor="#12b76a", label="Relief / risk-on"),
    ], frameon=False, ncol=4, fontsize=8, loc="upper center",
        bbox_to_anchor=(0.5, -0.20))
    for spine in ax_state.spines.values():
        spine.set_visible(False)

    fig.text(
        0.01, 0.01,
        "Shadow research only. State thresholds are versioned and excluded from alert gates.",
        fontsize=8, color="#667085")
    fig.autofmt_xdate(rotation=0)
    path = output_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "reports",
        f"temporal_shadow_{report_date}.png")
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fig.savefig(path, dpi=150, bbox_inches="tight")
    finally:
        plt.close(fig)
    return path
