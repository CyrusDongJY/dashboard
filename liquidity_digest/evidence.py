"""Report-only explanations of existing models; never change their scores."""
from html import escape

from .model import finite
from .capital import capital_lines


def funding_evidence(report):
    indices = report["frontpage"]["indices"]
    core = indices["core_funding"]["details"]
    water = indices["liquidity"]["details"]
    notes = ["核心资金指数：慢频资金条件与近端融资分开看；分数不是资金金额或上涨概率。",
             "水位仪：观察资金向信用及市场承接的传导，不作为另一份独立宏观投票。",
             "低RRP余额本身不等于危机；月末等日历背景不抵消实际融资压力。"]
    for window in ("1w", "4w", "13w"):
        item = core.get("net_liquidity_attribution", {}).get(window) or {}
        values = [finite(item.get(k)) for k in ("net_change_b", "fed_assets_b", "tga_b", "rrp_b")]
        if all(v is not None for v in values):
            notes.append(f'{window}净流动性变化 {values[0]:+.2f}B；Fed/TGA/RRP贡献 '
                         f'{values[1]:+.2f}/{values[2]:+.2f}/{values[3]:+.2f}B；截至 {item.get("to_date")}。')
    base = core.get("rolling_base_effect", {}).get("4w") or {}
    if base.get("status") == "OK" and all(finite(base.get(k)) is not None for k in ("current_week_b", "base_rolloff_b")):
        notes.append(f'四周窗口变化归因：当周真实净变化 {base["current_week_b"]:+.2f}B，'
                     f'旧基数移出贡献 {base["base_rolloff_b"]:+.2f}B；'
                     + ("基数效应占主导，不等于新增投放。" if base.get("base_effect_dominant") else "两项分开观察。"))
    m2 = core.get("components", {}).get("m2_yoy", {})
    notes.append(f'M2正式模型维持滞后{m2.get("lag_months", 3)}个月的传导假设，尚未验证；不移位对照仅为研究，不重复计票。')
    current_m2 = core.get("m2_unshifted_reference", {}).get("m2_component", {})
    if current_m2.get("status") == "OK" and finite(current_m2.get("value")) is not None:
        notes.append(f'不移位的已发布M2同比观测 {current_m2["value"]:.2f}%；'
                     f'观测月 {current_m2.get("observation_month") or current_m2.get("source_date")}，与传导假设分开。')
    bank = core.get("bank_activity_context") or {}
    if finite(bank.get("deposit_growth_13w_pct")) is not None:
        notes.append(f'银行存款13周变化 {bank["deposit_growth_13w_pct"]:+.2f}%；来源 {bank.get("deposit_source_date")}，仅交叉核验。')
    notes.extend(str(x) for x in bank.get("notes", []))
    pillars = []
    for key, item in water.get("pillars", {}).items():
        missing = [str(c.get("label") or c.get("key")) + "：" + str(c.get("status") or c.get("reason") or "不可用")
                   for c in water.get("components", []) if c.get("pillar") == key and c.get("score") is None]
        pillars.append({"label": item.get("label", key), "score": finite(item.get("score")),
                        "coverage": finite(item.get("coverage")), "missing": missing})
    groups = [{"label": item.get("label", key), "score": finite(item.get("score")),
               "coverage": finite(item.get("coverage"))} for key, item in core.get("groups", {}).items()]
    return {"notes": notes, "pillars": pillars, "groups": groups,
            "quality_note": "覆盖率与数据质量不是预测准确率；原始变化、基数变化、成分变化分别解释。"}


def evidence_html(report, compact=False):
    capital = report.get("capital")
    if capital is None:
        return ""
    lines = capital_lines(capital)
    content = '<section id="capital" class="band"><h2>资金配置、持仓与杠杆</h2>'
    content += "".join("<p>" + escape(line) + "</p>" for line in lines)
    content += '<p class="muted">' + escape(capital["scope_note"]) + '</p>'
    content += '<p class="notice">' + escape(capital["etf_note"]) + '</p>'
    if not compact:
        from .html_report import table
        rows = []
        for row in capital["observations"]:
            detail = row["details"]
            parts = [f'{label} {detail[key]:,.0f}' for key, label in
                     (("long", "多头"), ("short", "空头"), ("spreading", "跨期"), ("open_interest", "OI"))
                     if finite(detail.get(key)) is not None]
            if finite(detail.get("net_oi_pct")) is not None:
                parts.append(f'净持仓/OI {detail["net_oi_pct"]:.2f}%')
            parts.extend(f'{label}周变 {detail[key]:+,.0f}' for key, label in
                         (("long_change_1w", "多头"), ("short_change_1w", "空头"))
                         if finite(detail.get(key)) is not None)
            rows.append((row["label"], "；".join(parts) or "见摘要", row["observation_date"],
                         row["acquired_at"], row["status_cn"]))
        content += '<details><summary>持仓分项与时间审计</summary>' + table(
            ("指标", "分项", "观测期", "实际接收时间", "状态"), rows)
        content += '<p>发布时间未核验时保持未知，以实际接收时间作为可用时间。历史为本次可见的修订版本，不是当时已知的回测样本。</p>'
        for row in capital["observations"]:
            content += '<p><a href="' + escape(row["source_url"], quote=True) + '">' + escape(row["label"]) + '官方来源</a></p>'
            for note in row["details"].get("source_notes", []):
                content += '<p class="notice">来源说明：' + escape(note) + '</p>'
        content += '</details>'
        facts = funding_evidence(report)
        content += '<details><summary>已有资金指数的解释与有效分项</summary>'
        content += "".join('<p>' + escape(line) + '</p>' for line in facts["notes"])
        content += table(("核心资金分组", "正式分数", "覆盖"),
            [(p["label"], p["score"] if p["score"] is not None else "数据不足",
              f'{p["coverage"]:.0%}' if p["coverage"] is not None else "未知") for p in facts["groups"]])
        content += table(("水位仪支柱", "正式分数", "覆盖", "缺口"),
            [(p["label"], p["score"] if p["score"] is not None else "数据不足",
              f'{p["coverage"]:.0%}' if p["coverage"] is not None else "未知", "；".join(p["missing"]) or "无已识别缺口")
             for p in facts["pillars"]])
        content += '<p>' + escape(facts["quality_note"]) + '</p></details>'
    return content + '</section>'
