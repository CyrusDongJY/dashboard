# -*- coding: utf-8 -*-
"""生成环境指数影子模式PNG报告。"""
import os

import pandas as pd

from environment_indices import (
    HISTORY_CALENDAR_DAYS,
    INDEX_DEFS,
    MIN_INDEX_COVERAGE,
    MIN_INDEX_MATURITY,
    composite_score,
    rebuild_latest_statistics,
    score_frame_details,
)
from market_utils import trading_days_back
from report_style import configure_chinese_font


def _fetch_metric_rows(supabase, start_date, end_date, session="EOD", page_size=1000):
    rows, offset = [], 0
    while True:
        result = (supabase.table("metric_daily")
                  .select("report_date,metric,scope,value,percentile,sample_len,"
                          "effective_obs_count,source_date,source_name,lag_days")
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


def build_history(df, repair_latest=False):
    if df.empty:
        return pd.DataFrame()
    records = []
    latest_date = str(df["report_date"].max())
    for report_date, group in df.groupby("report_date", sort=True):
        if repair_latest and str(report_date) == latest_date:
            group = rebuild_latest_statistics(df, report_date)
        scores, coverage, maturity, diagnostics = score_frame_details(group)
        record = {"report_date": pd.to_datetime(report_date), **scores}
        for key in INDEX_DEFS:
            record[f"{key}_coverage"] = coverage[key]
            record[f"{key}_maturity"] = maturity[key]
            record[f"{key}_components"] = ",".join(
                item["metric"] for item in diagnostics[key] if item["status"] == "OK")
        record["composite_components"] = ",".join(
            key for key in INDEX_DEFS
            if scores[key] is not None and coverage[key] >= MIN_INDEX_COVERAGE
            and maturity[key] >= MIN_INDEX_MATURITY)
        record["composite"] = composite_score(
            scores, coverage, maturity=maturity)
        records.append(record)
    return pd.DataFrame(records).sort_values("report_date")


def _plot_history(ax, history, key):
    """Preserve missing dates and break lines whenever constituent sets change."""
    if key not in history:
        return
    signatures = history.get(f"{key}_components", pd.Series("", index=history.index))
    segments = signatures.ne(signatures.shift()).cumsum()
    for _, group in history.groupby(segments, sort=False):
        coverage = group.get(f"{key}_coverage", pd.Series(1.0, index=group.index))
        maturity = group.get(f"{key}_maturity", pd.Series(1.0, index=group.index))
        ready = (coverage >= MIN_INDEX_COVERAGE) & (maturity >= MIN_INDEX_MATURITY)
        for mask, color, style in ((ready, "#0f766e", "-"),
                                   (~ready, "#b76e00", "--")):
            values = group[key].where(mask)
            if values.notna().any():
                ax.plot(group["report_date"], values, color=color, linestyle=style,
                        linewidth=1.8, marker=".", markersize=2.5)


def generate_environment_chart(supabase, report_date, lookback_days=252, output_path=None):
    """生成并持久化环境影子图；无可用数据时返回 None。"""
    plot_start = trading_days_back(lookback_days, end_date=report_date)
    # The display window must not shorten the native statistical history.
    start_date = (pd.Timestamp(report_date) - pd.Timedelta(
        days=HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    history = build_history(
        _fetch_metric_rows(supabase, start_date, report_date),
        repair_latest=True)
    if history.empty:
        return None
    history = history[history["report_date"] >= pd.Timestamp(plot_start)]
    if history.empty:
        return None

    import matplotlib
    matplotlib.use("Agg")
    configure_chinese_font()
    import matplotlib.pyplot as plt

    labels = {
        "idx_risk_pressure": "波动率压力",
        "idx_liquidity_stress": "信用与金融条件压力",
        "idx_breadth_decay": "市场广度衰退",
        "idx_options_fragility": "期权脆弱性代理（需结合分量覆盖）",
        "idx_flow_behavior": "资金行为代理（非直接资金流量）",
        "composite": "综合压力（仅纳入覆盖及成熟度达标的面板）",
    }
    if history.iloc[-1].get("idx_flow_behavior_components") == "tqqq_drag_pct":
        labels["idx_flow_behavior"] = "资金行为代理（当前仅TQQQ损耗）"
    fig, axes = plt.subplots(3, 2, figsize=(14, 10), sharex=True, sharey=True)
    for ax, key in zip(axes.flat, labels):
        if key not in history or not history[key].notna().any():
            ax.text(0.5, 0.5, "有效历史仍在积累", ha="center", va="center",
                    transform=ax.transAxes, color="#6b7280")
        else:
            _plot_history(ax, history, key)
        ax.axhspan(80, 100, color="#dc2626", alpha=0.08)
        ax.axhline(80, color="#dc2626", linewidth=0.8, linestyle="--", alpha=0.7)
        ax.axhline(50, color="#9ca3af", linewidth=0.7, linestyle=":")
        ax.set_title(labels[key], fontsize=10)
        ax.set_ylim(0, 100)
        ax.grid(alpha=0.18)
        if key in INDEX_DEFS:
            last = history.iloc[-1]
            ax.text(0.02, 0.94,
                    f"最新分量覆盖 {last[f'{key}_coverage']:.0%} | "
                    f"样本成熟度 {last[f'{key}_maturity']:.0%}",
                    transform=ax.transAxes, fontsize=8, color="#475467", va="top")
    for ax in axes[:, 0]:
        ax.set_ylabel("压力观察分（越高压力越大）", fontsize=9)
    fig.suptitle(
        f"市场环境影子监测 | {report_date}\n"
        "80为历史高压观察线；虚线为覆盖或成熟度不足的分量参考值",
        fontsize=14, fontweight="bold")
    fig.autofmt_xdate(rotation=25)
    fig.tight_layout(rect=(0, 0.04, 1, 0.94))
    fig.text(0.03, 0.012,
             "空白表示缺数或样本不足；分量变化处断线。历史重算可能与当时存档不同。影子观察，不触发预警或仓位调整。",
             fontsize=8, color="#667085")

    path = output_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "reports",
        f"environment_shadow_{report_date}.png")
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fig.savefig(path, dpi=150, bbox_inches="tight")
    finally:
        plt.close(fig)
    return path
