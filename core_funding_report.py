# -*- coding: utf-8 -*-
"""Text and aligned benchmark charts for the shadow funding index."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from core_funding_index import GROUPS

STATES = {"INSUFFICIENT": "数据不足", "LOW": "低水位", "TIGHT": "偏紧",
          "MIDDLE": "中间区间", "SUPPORTIVE": "较充裕", "HIGH": "高水位"}
PRESSURES = {"ACUTE": "显著压力", "WATCH": "压力观察", "NOT_TRIGGERED": "未触及压力条件",
             "UNAVAILABLE": "融资压力数据不足"}


def format_core_funding(result):
    number = lambda value: "NA" if value is None else f"{value:.1f}"
    lines = ["=== 核心资金充裕度指数（独立影子观察，不触发预警） ===",
             f"资金充裕度：{number(result['score'])}/100 | 状态：{STATES[result['state']]}",
             f"有效覆盖：{result['coverage']:.0%} | 融资压力：{PRESSURES[result['funding_pressure']['state']]}",
             f"20交易日变化：{number(result['change_20_sessions'])} | 趋势候选：{result['trend']}",
             f"历史周样本百分位：{number(result['historical_percentile'])}"]
    for key, (label, _) in GROUPS.items():
        group = result["groups"][key]
        lines.append(f"{label}：{number(group['score'])}/100（覆盖{group['coverage']:.0%}）")
    m2 = result["components"].get("m2_yoy", {})
    if m2.get("lag_months") is not None:
        lines.append(f"M2口径：美国M2同比按观测月末延后{m2['lag_months']}个自然月计分，总权重10%；"
                     "用户指定传导假设，尚未验证。")
        lines.append(f"M2对应观测月：{m2['expected_observation_month']} | "
                     f"对齐日期：{m2['expected_effective_date']} | 状态：{m2['status']}")
    for key, item in result["components"].items():
        if item["status"] != "OK":
            lines.append(f"缺口 {key}: {item['status']}；历史原生样本 "
                         f"{item['prior_native_observations']}/{item['minimum']}；来源日 {item['source_date']}")
    lines.append("解释：" + result.get("interpretation", "请同时查看分组与融资压力"))
    for window in ("1w", "4w", "13w"):
        change = result["net_liquidity_attribution"].get(window)
        if not change:
            continue
        lines.append(f"{window[:-1]}周净流动性变化 {change['net_change_b']:+.2f}B = "
                     f"Fed {change['fed_assets_b']:+.2f}B / TGA {change['tga_b']:+.2f}B / "
                     f"RRP {change['rrp_b']:+.2f}B（截至 {change['to_date']}）")
    bridge = result.get("rolling_base_effect", {}).get("4w", {})
    if bridge.get("status") == "OK":
        lines.append(f"4周增量较上周变化 {bridge['rolling_change_delta_b']:+.2f}B = "
                     f"当周真实净变化 {bridge['current_week_b']:+.2f}B + "
                     f"旧基数移出贡献 {bridge['base_rolloff_b']:+.2f}B")
        if bridge["base_effect_dominant"]:
            lines.append("基数效应占主导：四周窗口改善/恶化不等于当周同规模投放/回笼。")
    bank = result.get("bank_activity_context", {})
    if bank.get("deposit_growth_13w_pct") is not None:
        lines.append(f"银行存款13周变化：{bank['deposit_growth_13w_pct']:+.2f}% "
                     f"（来源日 {bank['deposit_source_date']}；仅交叉核验，不计分）")
    lines.extend(bank.get("notes", []))
    reference_m2 = result.get("m2_unshifted_reference", {})
    if reference_m2:
        lines.append(f"M2不移位研究对照：{number(reference_m2.get('score'))}；"
                     "与正式滞后3个月版本分开，不作为另一条已发布历史。")
    lines.extend([
        f"落库状态：{result['persistence_status']} | 版本：{result['calc_version']}",
        "口径：ALFRED首次发布值；未知发布时间按下一纽约自然日生效，实际抓取可证明更早可用。",
        "数值越高代表相对更充裕，不是现金比例、上涨概率或交易信号；阈值尚未校准。",
        "M1/G3/NFCI/股价/广度/VIX不参与本指数；完整固定权重不足时不输出总分。",
    ])
    reference = result.get("replay_reference")
    if reference and result["observation_mode"] == "LIVE_SHADOW":
        lines.append(f"历史回放参考（不算实测发布）：20日变化 {number(reference['change_20_sessions'])}；"
                     f"周样本百分位 {number(reference['historical_percentile'])}；截至 {reference['as_of']}")
    if any(value in {"UNAVAILABLE", "CACHED_AFTER_ERROR"}
           for value in result.get("source_status", {}).values()):
        lines.append("数据抓取存在失败或缓存回退；请结合来源日期与覆盖率查看。")
    return "\n".join(lines)


def rebase_benchmarks(prices):
    if prices is None or prices.empty:
        return pd.DataFrame()
    frame = prices.reindex(columns=["SPY", "QQQ"]).apply(pd.to_numeric, errors="coerce")
    frame = frame.where(frame > 0).sort_index()
    common = frame.dropna()
    if common.empty:
        return pd.DataFrame()
    frame = frame.loc[common.index[0]:]
    return frame.div(common.iloc[0]).mul(100)


def generate_core_funding_chart(result, history, prices=None, output_path=None, dataset_label=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    if not output_path:
        raise ValueError("An explicit persistent chart path is required")
    frame = pd.DataFrame([{
        "date": row["report_date"], "score": row["score"],
        "change": row.get("change_20_sessions"), "coverage": row["coverage"],
        "pressure": row["funding_pressure"]["state"],
    } for row in history])
    if frame.empty:
        frame = pd.DataFrame([{"date": result["report_date"], "score": result["score"],
                               "change": None, "coverage": result["coverage"],
                               "pressure": result["funding_pressure"]["state"]}])
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").drop_duplicates("date")
    score = "NA" if result["score"] is None else f"{result['score']:.1f}"
    fig, axes = plt.subplots(2, 1, figsize=(13, 8.2), sharex=True,
                             gridspec_kw={"height_ratios": [2.6, 1]})
    fig.subplots_adjust(left=.075, right=.92, top=.81, bottom=.18, hspace=.38)
    funding_axis = axes[0].twinx()
    funding_color = "#b34050"
    fig.text(.075, .955, "CORE FUNDING AVAILABILITY", fontsize=19, weight="bold")
    fig.text(.075, .918, f"{result['report_date']}  |  {score}/100  |  Coverage {result['coverage']:.0%}  |  "
             f"Funding pressure: {result['funding_pressure']['state']}", fontsize=11)
    fig.text(.075, .883, dataset_label or "Shadow only | First-release vintage replay | Higher = more abundant | Not a price forecast",
             fontsize=9, color="#59636e")
    prices = prices if prices is not None else pd.DataFrame()
    if not prices.empty:
        prices = prices.loc[(prices.index >= frame["date"].min()) &
                            (prices.index <= frame["date"].max())]
    benchmark = rebase_benchmarks(prices)
    if benchmark.empty:
        axes[0].text(.5, .65, "Adjusted SPY / QQQ history unavailable", ha="center",
                     transform=axes[0].transAxes, color="#59636e")
    else:
        for key, color in (("SPY", "#2665b2"), ("QQQ", "#16806b")):
            axes[0].plot(benchmark.index, benchmark[key], label=key, color=color, linewidth=1.6)
    axes[0].set_ylabel("Adjusted price / common start = 100", fontsize=9)
    funding_axis.plot(frame["date"], pd.to_numeric(frame["score"]), color=funding_color,
                      linewidth=2, linestyle="--", label="Funding index (PIT replay)")
    funding_axis.axhspan(0, 20, color="#b83c46", alpha=.05)
    funding_axis.axhspan(80, 100, color="#188668", alpha=.05)
    funding_axis.axhline(50, color=funding_color, linestyle=":", linewidth=.8, alpha=.45)
    funding_axis.set_ylim(0, 100)
    funding_axis.set_ylabel("Funding index / 0-100", fontsize=9, color=funding_color)
    if frame["score"].notna().sum() == 0:
        funding_axis.text(.5, .4, "Insufficient native observations or stale inputs",
                          ha="center", transform=funding_axis.transAxes, color=funding_color)
    if result["score"] is not None:
        funding_axis.scatter([pd.Timestamp(result["report_date"])], [result["score"]],
                             s=28, color=funding_color, zorder=5,
                             label="Issued snapshot" if result["observation_mode"] == "LIVE_SHADOW" else "Replay cutoff")
    price_handles, price_labels = axes[0].get_legend_handles_labels()
    funding_handles, funding_labels = funding_axis.get_legend_handles_labels()
    axes[0].legend(price_handles + funding_handles, price_labels + funding_labels,
                   loc="lower left", bbox_to_anchor=(0, 1.015), ncol=4,
                   borderaxespad=0, frameon=False, fontsize=9)
    changes = pd.to_numeric(frame["change"])
    axes[1].bar(frame["date"], changes, width=1.5,
                color=["#16806b" if value >= 0 else "#b83c46" for value in changes.fillna(0)])
    axes[1].axhline(0, color="#a5aab0", linewidth=.8)
    axes[1].set_title("20-session change / shaded dates = funding pressure or missing coverage", loc="left", fontsize=11)
    axes[1].set_ylabel("Index points", fontsize=9)
    for _, row in frame.iterrows():
        color = "#b83c46" if row["pressure"] in {"WATCH", "ACUTE"} else "#808890" if row["coverage"] < .999 else None
        if color:
            axes[1].axvspan(row["date"], row["date"] + pd.Timedelta(days=1), color=color, alpha=.13)
    for axis in axes:
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", alpha=.15)
        axis.tick_params(labelsize=8)
    funding_axis.spines[["top", "left", "bottom"]].set_visible(False)
    funding_axis.spines["right"].set_color(funding_color)
    funding_axis.tick_params(axis="y", labelsize=8, colors=funding_color)
    locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
    axes[1].xaxis.set_major_locator(locator)
    axes[1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.text(.075, .105, "Dual axes: compare timing, not amplitudes. PIT replay is not issued history; "
             "reference bands are uncalibrated.", fontsize=8, color="#59636e")
    fig.text(.075, .078, "Sources: FRED / ALFRED initial releases; Yahoo adjusted prices (comparison only). "
             "Missing data is not neutral liquidity.", fontsize=8, color="#59636e")
    fig.text(.075, .05, f"{result['calc_version']} | Capture: {result['as_of']} | "
             "Quantity and financing conditions, not equity buying power.", fontsize=8, color="#59636e")
    m2 = result.get("components", {}).get("m2_yoy", {})
    if m2.get("lag_months") is not None:
        fig.text(.075, .023, f"US M2 YoY: observation month-end + {m2['lag_months']} calendar months; "
                 "10% total weight. User-specified lag, unvalidated hypothesis.", fontsize=8, color="#59636e")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.savefig(path, dpi=150, facecolor="white")
    finally:
        plt.close(fig)
    return str(path)


def generate_diagnostics_chart(result, diagnostics, output_path, dataset_label=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(12, 9), gridspec_kw={"height_ratios": [1, 1, 1.1]})
    fig.subplots_adjust(left=.10, right=.94, top=.84, bottom=.13, hspace=.70)
    fig.text(.10, .955, "FUNDING CHANGES AND CREDIT TRANSMISSION", fontsize=17, weight="bold")
    fig.text(.10, .916, f"{result['report_date']} | {result['observation_mode']} | {diagnostics['version']}", fontsize=10)
    fig.text(.10, .883, dataset_label or "Independent diagnostics | Not added to the core score | No trading eligibility", fontsize=9)
    labels, values = [], []
    for count, item in diagnostics["dynamics"].items():
        labels.append(f"{count} session" if count == "1" else f"{count} sessions")
        values.append(item["delta"])
    axes[0].bar(range(len(labels)), [float("nan") if value is None else value for value in values], width=.48,
                color=["#237a68" if value is not None and value >= 0 else "#b34050" for value in values])
    axes[0].set_xticks(range(len(labels)), labels)
    axes[0].set_xlim(-.5, len(labels) - .5)
    if all(value is None for value in values):
        axes[0].set_ylim(-1, 1)
        axes[0].set_yticks([])
    for i, value in enumerate(values):
        if value is None:
            axes[0].text(i, .5, "NA", transform=axes[0].get_xaxis_transform(), ha="center", color="#59636e")
    axes[0].set_title("Index change / exact NYSE sessions, same formula and snapshot mode", loc="left", fontsize=11)
    axes[0].set_ylabel("Index points")
    axes[0].axhline(0, color="#8f969c", linewidth=.8)
    group_labels = {"stock": "Stock / buffer", "flow": "Net supply", "funding": "Financing", "credit": "Bank credit"}
    week = diagnostics["dynamics"]["5"]
    contributions = week["group_contributions"]
    if contributions:
        pairs = list(contributions.items())
        axes[1].barh([group_labels[key] for key, _ in pairs], [value for _, value in pairs],
                     color=["#237a68" if value >= 0 else "#b34050" for _, value in pairs], height=.55)
        axes[1].invert_yaxis()
    else:
        axes[1].text(.5, .5, "NA: complete 5-session attribution not available", ha="center", transform=axes[1].transAxes)
        axes[1].set_yticks([])
        axes[1].set_xticks([])
    axes[1].axvline(0, color="#8f969c", linewidth=.8)
    axes[1].set_title("5-session component contribution / includes percentile-baseline changes", loc="left", fontsize=11)
    axes[1].set_xlabel("Contribution to index points")
    credit = diagnostics["credit"]
    axes[2].set_title(f"Independent credit watch: {credit['status']} | Fresh coverage {credit['coverage']:.0%}",
                      loc="left", fontsize=11, pad=14)
    axes[2].axis("off")
    cells = []
    for key, label in (("hy_oas", "HY OAS"), ("ig_oas", "IG OAS"), ("bbb_oas", "BBB OAS")):
        item = credit["series"][key]
        value = "NA" if item["value_bp"] is None else f"{item['value_bp']:.1f}"
        change = "NA" if item["change_5_sessions_bp"] is None else f"{item['change_5_sessions_bp']:+.1f}"
        cells.append([label, value, change, item["source_date"] or "NA",
                      ", ".join(item["flags"]) if item["flags"] else item["status"]])
    table = axes[2].table(cellText=cells, colLabels=["Series", "bp", "5-session bp", "Observation date", "Status / flags"],
                          colWidths=[.14, .10, .17, .19, .40], bbox=[0, .08, 1, .88], cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    for (row, _), cell in table.get_celld().items():
        cell.set_edgecolor("#d9dde0")
        cell.set_facecolor("#eef2f5" if row == 0 else "white")
    for axis in axes[:2]:
        axis.spines[["top", "right"]].set_visible(False)
        axis.tick_params(labelsize=9)
        axis.grid(axis="y", alpha=.12)
    fig.text(.10, .077, "Level is not direction. Unavailable inputs are not healthy. Credit thresholds are provisional shadow rules.", fontsize=8)
    fig.text(.10, .046, "Source: FRED / ALFRED first releases, ICE BofA OAS. Private research; no valuation or causal attribution.", fontsize=8)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.savefig(path, dpi=150, facecolor="white")
    finally:
        plt.close(fig)
    return str(path)
