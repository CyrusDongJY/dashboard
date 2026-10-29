"""Offline, display-only time series from the already validated frozen report."""
from base64 import b64encode
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path

from temporal_monitor import comparison_segments, HIGH_PRESSURE, LOW_PRESSURE
from .model import finite, native_sessions, sessions
from .render import CHART_TITLES, COLORS, PANEL_CN
from .states import eligible


def chart_payload(report):
    end = report["market_date"]
    floor = str(date.fromisoformat(end) - timedelta(days=430))
    charts = []

    def series(points, label, color, dashed=False, weekly=False, key=None):
        values = {p["date"]: finite(p.get("value")) for p in points if floor <= p["date"] <= end}
        if not values:
            return dict(label=label, color=color, dashed=dashed, points=[])
        first = min(values)
        if weekly:
            start = date.fromisoformat(first)
            grid = [str(start + timedelta(days=7 * i))
                    for i in range((date.fromisoformat(end) - start).days // 7 + 1)]
        else:
            grid = native_sessions(key, first, end) if key else sessions(first, end)
        return dict(label=label, color=color, dashed=dashed,
                    points=[[d, values.get(d)] for d in grid])

    def add(key, title, unit, lines, bounded=False, guides=None):
        charts.append(dict(id=key, title=title, unit=unit, series=lines,
                           y_bounds=[0, 100] if bounded else None, guides=guides or []))

    for key, item in report["frontpage"]["indices"].items():
        lines = [series(s["points"], "旧版存档 · " + s["version"], "#75858d")
                 for s in item.get("legacy_segments", [])]
        lines.append(series(item["replay_history"],
            "新版同口径重算参考（非PIT）" if key == "liquidity" else "历史回放参考（非实测）",
            "#46699a" if key == "liquidity" else "#8b9299", dashed=True))
        lines.append(series(item["history"],
            "存档计算值（非PIT）" if key == "liquidity" else "实测发布", item["color"]))
        add("index-" + key, item["label"], "支持分 / 100", lines, bounded=True)
    metrics = report["metrics"]
    add("trend-0", CHART_TITLES[0], "十亿美元", [series(
        metrics["net_liquidity"]["history"], "同日资产 - 财政账户 - 逆回购", "#176c8c", weekly=True)])
    lines = []
    for key, label, color, sign in (("fed_assets", "总资产变化", "#176c8c", 1),
            ("tga_weekly", "财政账户反向变化", "#bd5135", -1),
            ("rrp", "逆回购反向变化", "#008c83", -1)):
        raw = {p["date"]: finite(p["value"]) for p in metrics[key]["history"]}
        changes = []
        for point in metrics["net_liquidity"]["history"]:
            day = point["date"]
            old = str(date.fromisoformat(day) - timedelta(days=7))
            value = sign * (raw[day] - raw[old]) if raw.get(day) is not None and raw.get(old) is not None else None
            changes.append(dict(date=day, value=value))
        lines.append(series(changes, label, color, weekly=True))
    add("trend-1", CHART_TITLES[1], "十亿美元 / 周", lines,
        guides=[dict(value=0, label="零变化", color="#63757a")])
    for index, members in ((2, (("sofr_iorb", "SOFR-IORB", "#176c8c"),
                    ("effr_iorb", "EFFR-IORB", "#008c83"), ("sofr_tail", "SOFR尾部", "#bd5135"))),
            (3, (("ig", "投资级", "#176c8c"), ("hy", "高收益", "#bd5135"), ("ccc", "CCC及以下", "#7255a5")))):
        add("trend-" + str(index), CHART_TITLES[index], "基点",
            [series(metrics[k]["history"], label, color, key=k) for k, label, color in members])
    for index, (key, color) in enumerate(COLORS.items(), start=4):
        lines = []
        for reference in (True, False):
            rows = report.get("reference_history" if reference else "temporal", {}).get(key, [])
            for segment in comparison_segments(rows):
                points = [dict(date=r["report_date"], value=r["risk_value"] if eligible(r, key, reference) else None)
                          for r in segment]
                label = "历史重算参考（非当时可得）" if reference else "正式观察记录"
                lines.append(series(points, label, color, dashed=reference))
        add("trend-" + str(index), PANEL_CN[key], "压力方向值 / 100", lines, bounded=True,
            guides=[dict(value=HIGH_PRESSURE, label=f"{HIGH_PRESSURE:g} 高压观察线", color="#9e283b"),
                    dict(value=LOW_PRESSURE, label=f"{LOW_PRESSURE:g} 低压分界线", color="#637a70")])
    valid_dates = [p[0] for c in charts for s in c["series"] for p in s["points"] if p[1] is not None]
    return dict(charts=charts, start=min(valid_dates, default=str(date.fromisoformat(end) - timedelta(days=150))),
                end=end, default_days=150, basis="FROZEN_REPORT_DISPLAY_ONLY")


def assets(report):
    payload = json.dumps(chart_payload(report), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # Never let source text terminate a script element, including the JSON data block.
    for char, replacement in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                              ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        payload = payload.replace(char, replacement)
    script = Path(__file__).with_name("interactive.js").read_text()
    hashes = " ".join("'sha256-" + b64encode(sha256(s.encode()).digest()).decode() + "'" for s in (payload, script))
    return (f"script-src {hashes}; connect-src 'none'; ",
            f'<script id="chart-data" type="application/json">{payload}</script>'
            f'<script id="chart-runtime">{script}</script>')


def toolbar():
    return '''<section class="time-controls" hidden aria-label="图表时间范围">
<strong>图表时间范围</strong><div class="range-presets" role="group" aria-label="预设时间范围">
<label><input type="radio" name="chart-range" value="30">近1月</label>
<label><input type="radio" name="chart-range" value="90">近3月</label>
<label><input type="radio" name="chart-range" value="150" checked>近150日</label>
<label><input type="radio" name="chart-range" value="all">全部</label></div>
<div class="date-range"><label>起始日期<input id="chart-start" type="date"></label>
<label>截止日期<input id="chart-end" type="date"></label></div>
<p id="chart-range-status" role="status" aria-live="polite"></p></section>'''


CSS = '''
[hidden]{display:none!important}.time-controls{border-bottom:1px solid #dbe2e4;padding:0 0 16px;margin-bottom:18px;display:flex;gap:12px 18px;align-items:center;flex-wrap:wrap;font-size:13px}
.range-presets{display:flex;gap:0;flex-wrap:wrap}.range-presets label{cursor:pointer;padding:5px 9px;border-bottom:2px solid transparent;white-space:nowrap}.range-presets label:has(input:checked){border-color:#176c8c;background:#edf3f5;color:#176c8c}.range-presets input{accent-color:#176c8c;margin:0 5px 0 0}
.date-range{display:flex;gap:12px;flex-wrap:wrap}.date-range label{display:flex;align-items:center;gap:7px}.date-range input{font:inherit;border:1px solid #bccbd0;border-radius:4px;padding:5px;min-width:0;width:138px;color:#293b40;background:white}
#chart-range-status{flex-basis:100%;margin:0;font-size:12px;color:#63757a}.time-controls:has(input:invalid) #chart-range-status{color:#a23646}
.interactive-chart{min-width:0;max-width:100%}.interactive-title{font-size:15px;font-weight:600;margin:8px 0}.interactive-chart svg{display:block;width:100%;height:244px;touch-action:pan-y;outline-offset:2px}.interactive-chart svg:focus-visible{outline:2px solid #176c8c}
.chart-legend{display:flex;gap:4px 13px;flex-wrap:wrap;font-size:11px;line-height:1.5;color:#52666d;min-height:17px}.chart-legend span{overflow-wrap:anywhere}.chart-swatch{display:inline-block;width:16px;border-top:2px solid;margin-right:5px;vertical-align:middle}
.chart-values{font-size:12px;color:#52666d;min-height:58px;margin:5px 0 0;overflow-wrap:anywhere}.chart-values strong{color:#293b40}.chart-coverage{font-size:11px;color:#63757a;margin:4px 0}
@media(max-width:380px){.date-range{width:100%;display:grid;grid-template-columns:1fr 1fr;gap:8px}.date-range label{display:block}.date-range input{display:block;width:100%;margin-top:3px}.range-presets label{padding:5px 7px}}
@media print{.time-controls,.interactive-chart{display:none!important}.chart-fallback{display:block!important}}
'''
