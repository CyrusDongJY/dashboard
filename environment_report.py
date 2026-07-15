# -*- coding: utf-8 -*-
"""生成环境指数影子模式PNG报告。"""
import os
import tempfile

import pandas as pd

from environment_indices import INDEX_DEFS, composite_score, score_frame
from market_utils import trading_days_back


def _fetch_metric_rows(supabase, start_date, end_date, session="EOD", page_size=1000):
    rows, offset = [], 0
    while True:
        result = (supabase.table("metric_daily")
                  .select("report_date,metric,scope,percentile,sample_len,effective_obs_count,lag_days")
                  .eq("session", session)
                  .neq("scope", "COMPOSITE")
                  .gte("report_date", start_date)
                  .lte("report_date", end_date)
                  .order("report_date").order("metric").order("scope")
                  .range(offset, offset + page_size - 1)
                  .execute())
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return pd.DataFrame(rows)


def build_history(df):
    if df.empty:
        return pd.DataFrame()
    records = []
    for report_date, group in df.groupby("report_date", sort=True):
        scores, coverage = score_frame(group)
        record = {"report_date": pd.to_datetime(report_date), **scores}
        record["composite"] = composite_score(scores, coverage)
        records.append(record)
    return pd.DataFrame(records).sort_values("report_date")


def generate_environment_chart(supabase, report_date, lookback_days=252, output_path=None):
    """返回PNG路径；无可用数据时返回 None。"""
    start_date = trading_days_back(lookback_days, end_date=report_date)
    history = build_history(_fetch_metric_rows(supabase, start_date, report_date))
    if history.empty:
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = {
        "idx_risk_pressure": "Volatility pressure",
        "idx_liquidity_stress": "Credit / financial conditions",
        "idx_breadth_decay": "Breadth deterioration",
        "idx_options_fragility": "Options fragility proxy",
        "idx_flow_behavior": "Flow proxy",
        "composite": "Composite (eligible panels only)",
    }
    fig, axes = plt.subplots(3, 2, figsize=(14, 10), sharex=True, sharey=True)
    for ax, key in zip(axes.flat, labels):
        series = history.dropna(subset=[key]) if key in history else pd.DataFrame()
        if series.empty:
            ax.text(0.5, 0.5, "Accumulating history", ha="center", va="center",
                    transform=ax.transAxes, color="#6b7280")
        else:
            ax.plot(series["report_date"], series[key], color="#0f766e",
                    linewidth=1.8, marker=".", markersize=2.5)
        ax.axhspan(80, 100, color="#dc2626", alpha=0.08)
        ax.axhline(80, color="#dc2626", linewidth=0.8, linestyle="--", alpha=0.7)
        ax.axhline(50, color="#9ca3af", linewidth=0.7, linestyle=":")
        ax.set_title(labels[key], fontsize=10)
        ax.set_ylim(0, 100)
        ax.grid(alpha=0.18)
    fig.suptitle(
        f"Market Environment Shadow Monitor | {report_date}\n"
        "Research only: not connected to alert gates or position sizing",
        fontsize=14, fontweight="bold")
    fig.autofmt_xdate(rotation=25)
    fig.tight_layout(rect=(0, 0, 1, 0.94))

    path = output_path or os.path.join(
        tempfile.gettempdir(), f"environment_shadow_{report_date}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path
