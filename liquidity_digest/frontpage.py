"""Auditable front-page observations; never recompute or combine producer scores."""
from datetime import date, timedelta

from liquidity_monitor import CALC_VERSION as LIQUIDITY_VERSION, CREDIT_STRESS_GUARDS
from .model import finite, sessions, stamp, valid_rows, NY

CORE_VERSION = "core_funding_v1.2.1_m2lag3"
INDEX_SPECS = {
    "core_funding": ("核心资金指数", "core_funding_daily", CORE_VERSION, "#147d70"),
    "liquidity": ("市场流动性水位仪", "liquidity_daily", LIQUIDITY_VERSION, "#366cb5"),
}
CORE_STATES = {"LOW": "低水位", "TIGHT": "偏紧", "MIDDLE": "中间区间",
               "SUPPORTIVE": "较充裕", "HIGH": "高水位", "INSUFFICIENT": "数据不足"}


def _score(value):
    value = finite(value)
    return value if value is not None and 0 <= value <= 100 else None


def _value(row, key):
    coverage = finite(row.get("coverage"))
    if coverage is None or not .999999 <= coverage <= 1:
        return None
    if key == "core_funding":
        result = row.get("result")
        if not isinstance(result, dict):
            return None
        if any(result.get(k) != row.get(k) for k in
               ("report_date", "calc_version", "observation_mode", "coverage", "score")):
            return None
        try:
            # PostgREST normalizes timestamptz formatting, while JSON preserves original text.
            if stamp(result["as_of"]) != stamp(row["as_of"]):
                return None
        except (KeyError, ValueError, TypeError):
            return None
        return _score(row.get("score"))
    details = row.get("details") or {}
    if not isinstance(details, dict) or details.get("weight_policy") != "FIXED_BASKET_FULL_COVERAGE":
        return None
    return _score(row.get("composite"))


def index_observation(report_plan, capture, key):
    label, table, version, color = INDEX_SPECS[key]
    end = report_plan["market_date"]
    result = dict(key=key, label=label, color=color, source=table, version=version,
                  value=None, source_date=None, coverage=None, state="数据不足", status="MISSING",
                  history=[], replay_history=[], changes={"1d": None, "5d": None, "21d": None},
                  window_coverage={},
                  reason="尚无昨日合格记录", details={}, fetched_at=capture.get("captured_at"))
    try:
        captured = stamp(capture["captured_at"])
        if (capture.get("status") != "OK" or captured > stamp(report_plan["cutoff"])
                or str(captured.astimezone(NY).date()) != report_plan["report_date"]):
            raise ValueError("Capture unavailable")
    except (KeyError, TypeError, ValueError):
        result.update(status="UNAVAILABLE", reason="数据源未及时刷新，旧缓存不参与当前判断")
        return result
    rows = valid_rows(capture.get("data", []), "report_date", end, report_plan["cutoff"])
    accepted, other_versions = [], []
    for row in rows:
        try:
            # Both actual ingestion and the producer's cutoff must be known before this report.
            if stamp(row["created_at"]) > stamp(report_plan["cutoff"]):
                continue
            if key == "core_funding":
                if row.get("observation_mode") not in ("LIVE_SHADOW", "PIT_REPLAY"):
                    continue
                if stamp(row["as_of"]) > stamp(report_plan["cutoff"]):
                    continue
                if str(stamp(row["as_of"]).astimezone(NY).date()) != row["report_date"]:
                    continue
        except (KeyError, ValueError, TypeError):
            continue
        if row.get("calc_version") != version:
            other_versions.append(row)
            continue
        accepted.append(row)
    live = [r for r in accepted if key != "core_funding" or r["observation_mode"] == "LIVE_SHADOW"]
    # Deterministic latest record; an invalid new row must not resurrect an older good one.
    indexed = {r["report_date"]: r for r in sorted(live, key=lambda r: (r["report_date"], stamp(r["created_at"])))}
    result["history"] = [{"date": d, "value": _value(r, key)} for d, r in sorted(indexed.items())]
    days = sessions(date.fromisoformat(end) - timedelta(days=70), end)
    for count in (1, 5, 21):
        window = days[-count - 1:]
        missing = [d for d in window if d not in indexed or _value(indexed[d], key) is None]
        result["window_coverage"][str(count) + "d"] = {
            "required": count + 1, "valid": len(window) - len(missing), "missing_dates": missing}
    if key == "core_funding":
        replay = {r["report_date"]: r for r in sorted(accepted, key=lambda r: stamp(r["created_at"]))
                  if r["observation_mode"] == "PIT_REPLAY"}
        result["replay_history"] = [{"date": d, "value": _value(r, key)} for d, r in sorted(replay.items())]
    current = indexed.get(end)
    replacements = [r for r in other_versions if r["report_date"] == end
                    and (key != "core_funding" or r["observation_mode"] == "LIVE_SHADOW")]
    if replacements and (current is None or max(stamp(r["created_at"]) for r in replacements) >= stamp(current["created_at"])):
        result.update(source_date=end, status="UNSUPPORTED_VERSION", reason="最新快照规则版本变更，需核对适配；不回退到旧分数")
        result["history"] = [p for p in result["history"] if p["date"] != end]
        return result
    if current is None:
        if indexed:
            result.update(source_date=max(indexed), status="STALE", reason="昨日快照缺失，历史曲线仅供回顾")
        return result
    value = _value(current, key)
    detail = (current.get("result") if key == "core_funding" else current.get("details")) or {}
    coverage = finite(current.get("coverage"))
    result.update(value=value, source_date=end, coverage=coverage if coverage is not None and 0 <= coverage <= 1 else None,
                  details=detail, status="OK" if value is not None else "INCOMPLETE",
                  state=(CORE_STATES.get(detail.get("state"), "状态待核实") if key == "core_funding"
                         else current.get("state") or "状态待核实") if value is not None else "数据不足",
                  reason="正式固定权重分数；越高代表支持越强" if value is not None else
                         "正式总分不可用；部分覆盖诊断值不替代正式分数")
    if value is None:
        components = detail.get("components") or []
        if isinstance(components, dict):
            components = list(components.values())
        missing = []
        for component in components:
            if not isinstance(component, dict) or component.get("score") is not None:
                continue
            label = ("VX标准月期货曲线" if component.get("key") == "vix_futures_curve"
                     else component.get("label") or component.get("key") or "未命名分项")
            count, required = component.get("valid_obs_count"), component.get("required_obs_count")
            suffix = f"（有效历史{count}/{required}）" if isinstance(count, int) and isinstance(required, int) else ""
            missing.append(label + suffix)
        if missing:
            result["reason"] = "缺少合格分项：" + "、".join(missing) + "；不以诊断值替代正式总分"
    for count in (1, 5, 21):
        window = days[-count - 1:]
        if len(window) == count + 1 and all(d in indexed and _value(indexed[d], key) is not None for d in window):
            result["changes"][str(count) + "d"] = round(value - _value(indexed[window[0]], key), 4)
    return result


def build_frontpage(report, inputs):
    indices = {key: index_observation(report, inputs.get("db_" + spec[1], {}), key)
               for key, spec in INDEX_SPECS.items()}
    water = indices["liquidity"]
    reference = report["states"]["panels"]["liquidity"].get("history_reference", {})
    if reference.get("risk_value") is not None and water["status"] == "OK":
        water["replay_history"] = [{"date": r["report_date"], "value":
                                    100 - r["risk_value"] if r.get("risk_value") is not None else None}
                                   for r in report.get("reference_history", {}).get("liquidity", [])]
        water["reference_changes"] = {k: -v if v is not None else None for k, v in reference["changes"].items()}
        water["reason"] += "；虚线为同公式历史重算，非当时可得，不覆盖发布快照"
    alerts, gaps = [], []
    for alert in report["states"]["alerts"]:
        if alert["kind"] == "quality":
            gaps.append(alert["text"])
        elif alert["kind"] == "raw_guard":
            for item in alert["evidence"]:
                m = report["metrics"][item["metric"]]
                alerts.append({"kind": "raw_guard", "text": (
                    f'{m["label"]} {item["value_bps"]:.2f} 基点 > 观察线 {item["threshold_bps"]:.2f} 基点'
                    f'（{item["source_date"]}）；单项越线，不等于系统性收紧。')})
        elif alert["kind"] != "context":
            entry = dict(alert)
            if alert["kind"] == "pressure":
                pressure = report["states"]["pressure"]
                entry["text"] = (f'{pressure["label"]} {pressure["risk_value"]:.2f}/100 ≥ 高压观察线 '
                                 f'{report["states"]["thresholds"]["high"]}（{pressure["source_date"]}）；'
                                 + alert["text"])
            alerts.append(entry)
    # Reuse the existing macro/micro level flags with their original raw thresholds.
    for key, limit, unit, below in (
        ("ig", CREDIT_STRESS_GUARDS["ig_spread"] * 100, "基点", False),
        ("hy", CREDIT_STRESS_GUARDS["hy_spread"] * 100, "基点", False),
        ("pct_200ma", 50, "%", True),
        ("spy_vwap_time_acceptance_pct", 50, "%", True),
        ("qqq_vwap_time_acceptance_pct", 50, "%", True),
    ):
        m = report["metrics"][key]
        value = finite(m["value"])
        hit = value is not None and (0 <= value < limit if below else value > limit)
        if m["status"] == m["acquisition"] == "OK" and hit:
            alerts.append({"kind": "watch" if below else "raw_guard", "text": (
                f'{m["label"]} {value:.2f}{unit} {"<" if below else ">"} 观察线 {limit:.2f}{unit}'
                f'（{m["source_date"]}）；' + ("市场承接偏弱，非独立交易信号。" if below else "单项观察，需交叉确认。"))})
    tactical = report["states"]["panels"]["tactical_stress"]
    if tactical["zone"] == "HIGH" and report["states"]["pressure"]["panel"] != "tactical_stress":
        alerts.append({"kind": "pressure", "text": (
            f'战术压力 {tactical["risk_value"]:.2f}/100 ≥ 高压观察线 '
            f'{report["states"]["thresholds"]["high"]}（{tactical["source_date"]}）；'
            f'{tactical["direction_cn"]}，影子观察，不触发独立预警。')})
    for item in indices.values():
        if item["status"] != "OK":
            gaps.append(item["label"] + "：" + item["reason"])
        elif item["changes"]["5d"] is None and item.get("window_coverage", {}).get("5d"):
            window = item["window_coverage"]["5d"]
            if item.get("reference_changes", {}).get("5d") is not None:
                gaps.append(f'{item["label"]}：存档快照{window["valid"]}/6，已另列完整5日历史重算参考')
            else:
                gaps.append(f'{item["label"]}：5日比较需6个同版有效快照，当前{window["valid"]}/6；历史回放不冒充实测')
    ici = inputs.get("ici_flows", {})
    if ici.get("status") and ici["status"] != "OK":
        gaps.append("基金净申赎：ICI来源本次不可用；不据此推断资金流入或流出")
    core = indices["core_funding"]
    pressure = core["details"].get("funding_pressure") or {}
    if core["source_date"] == report["market_date"] and pressure.get("state") in ("ACUTE", "WATCH"):
        components = core["details"].get("components") or {}
        for key in pressure.get("drivers", []):
            m = components.get(key) or {}
            value = finite(m.get("value"))
            if key in report["metrics"] and m.get("status") in ("OK", "WARMUP") and value is not None:
                alerts.append({"kind": "raw_guard", "text": (
                    f'核心资金融资提示：{report["metrics"][key]["label"]} {value:.2f} 基点'
                    f'（原始观测 {m.get("source_date") or "未知"}；指数快照 {core["source_date"]}）；'
                    + ("原模型显著压力条件已触发。" if pressure["state"] == "ACUTE" else "原模型压力观察条件已触发。"))})
    priority = {"raw_guard": 0, "shock": 1, "pressure": 2, "divergence": 3, "watch": 4}
    alerts.sort(key=lambda a: priority.get(a["kind"], 5))
    if report["gaps"]:
        gaps.append("原始数据待核查：" + "、".join(report["gaps"]))
    for alert in alerts:
        alert["category"] = "EVIDENCE_DIVERGENCE" if alert["kind"] == "divergence" else "MARKET_OBSERVATION"
    return {"indices": indices, "alerts": alerts, "gaps": list(dict.fromkeys(gaps)),
            "notices": alerts + [{"category": "DATA_QUALITY", "text": gap} for gap in dict.fromkeys(gaps)],
            "scope_note": "两项资金分数越高代表支持越强；战术压力越高代表压力越大。各自独立，不相加。"}


def index_images(report):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib import font_manager
    from io import BytesIO
    from .render import embedded_font

    _, font_path = embedded_font()
    font_manager.fontManager.addfont(font_path)
    font = font_manager.FontProperties(fname=font_path).get_name()
    front = report.get("frontpage") or build_frontpage(report, {})
    end = date.fromisoformat(report["market_date"])
    start = end - timedelta(days=150)
    grid = sessions(start, end)
    images = []
    with plt.rc_context({"font.family": [font, "DejaVu Sans"], "axes.unicode_minus": False}):
        for item in front["indices"].values():
            fig, ax = plt.subplots(figsize=(5.4, 2.35))
            for key, label, style, color in (
                ("replay_history", "历史回放参考（非实测）", "--", "#8b9299"),
                ("history", "实测发布" if item["key"] == "core_funding" else "存档计算值（非PIT）", "-", item["color"]),
            ):
                values = {p["date"]: p["value"] for p in item[key] if p["date"] >= str(start)}
                if any(v is not None for v in values.values()):
                    ax.plot([date.fromisoformat(d) for d in grid],
                            [values.get(d) if values.get(d) is not None else float("nan") for d in grid],
                            linestyle=style, color=color, linewidth=1.8, marker=".", markersize=2.5, label=label)
            ax.set(xlim=(start, end), ylim=(0, 100), ylabel="支持分 / 100")
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=5))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", alpha=.15)
            ax.tick_params(labelsize=9)
            if ax.lines:
                ax.legend(fontsize=8, frameon=False, loc="best")
            else:
                ax.text(.5, .5, "暂无同版有效历史\n缺失不画成零值", transform=ax.transAxes, ha="center", va="center", fontsize=10)
            fig.tight_layout(pad=1)
            out = BytesIO()
            fig.savefig(out, format="png", dpi=160, facecolor="white")
            plt.close(fig)
            out.seek(0)
            images.append(out)
    return images


def index_summary(item):
    from .render import number
    coverage = item["coverage"]
    window = item.get("window_coverage", {}).get("5d")
    change = (f'5日 {number(item["changes"]["5d"], True)} 点' if item["changes"]["5d"] is not None
              else f'5日样本 {window["valid"]}/{window["required"]}' if window else '5日不可比较')
    if item["changes"]["5d"] is None and item.get("reference_changes", {}).get("5d") is not None:
        change = f'5日重算参考 {number(item["reference_changes"]["5d"], True)} 点'
    return (f'{number(item["value"])}/100 · {item["state"]} · {change}；'
            f'快照 {item["source_date"] or "缺失"} · 覆盖 {number(coverage * 100 if coverage is not None else None)}%')


def domain_overview_html(report):
    from html import escape
    cells = []
    for key in ("macro", "micro"):
        domain = report["domains"][key]
        state = domain["short"]
        rows = "".join(
            f'<div style="font-size:13px;margin:3px 0">{escape(g["label"])}：'
            f'<span style="color:{escape(g["short"]["color"])}">{escape(g["short"]["label"])}</span></div>'
            for g in domain["groups"])
        cells.append(
            f'<td style="width:50%;min-width:0;vertical-align:top;padding:12px 10px;border:0;'
            f'border-top:3px solid {escape(state["color"])};overflow-wrap:anywhere">'
            f'<h2 style="font-size:16px;margin:0 0 6px">{escape(domain["label"])}</h2>'
            f'<p style="font-size:22px;font-weight:600;line-height:1.3;margin:6px 0;color:{escape(state["color"])}">'
            f'{escape(state["label"])}</p>'
            f'<p style="font-size:12px;margin:4px 0">中期：{escape(domain["medium"]["label"])}'
            + ("；近中期背离" if domain["disagreement"] else "") + '</p>' + rows
            + f'<p style="font-size:12px;color:#63757a;margin:6px 0 0">近期有效 {state["ready_groups"]}/3 组</p></td>')
    return (
        '<section id="overview" style="padding:20px 0;border-bottom:1px solid #dbe2e4">'
        '<table role="presentation" style="width:100%;table-layout:fixed;border-collapse:collapse" cellspacing="0" cellpadding="0">'
        '<tr>' + "".join(cells) + '</tr></table>'
        f'<p style="font-size:13px;margin:10px 0 0">{escape(report["domains"]["relation"])}</p>'
        '<p style="font-size:12px;color:#63757a;margin:6px 0 0">宏观按原生观测日；微观截至 '
        f'{escape(report["market_date"])} 收盘。缺失不代表平稳。</p></section>')


def front_html(report, images=None):
    from base64 import b64encode
    from html import escape
    from .render import tactical_lines
    front = report.get("frontpage") or build_frontpage(report, {})
    images = index_images(report) if images is None else images
    if len(images) != 2:
        raise ValueError("Front page requires two index charts")
    figures = []
    for item, image in zip(front["indices"].values(), images):
        figures.append(
            '<section class="index-panel" style="min-width:0;margin:0 0 16px">'
            f'<h3 style="font-size:17px;margin:0 0 6px;color:{item["color"]}">{escape(item["label"])}</h3>'
            f'<p style="font-size:13px;margin:0 0 6px">{escape(index_summary(item))}</p>'
            f'<img style="display:block;width:100%;height:auto;max-width:864px" width="864" height="376" '
            f'src="data:image/png;base64,{b64encode(image.getvalue()).decode("ascii")}" alt="{escape(item["label"])}近150日曲线">'
            f'<p style="font-size:12px;color:#63757a;margin:4px 0">{escape(item["reason"])}</p></section>')
    alerts = front["alerts"]
    warnings = "".join(f'<li style="margin:8px 0">{escape(a["text"])}</li>' for a in alerts[:4])
    if not warnings:
        warnings = "<li>未检出既有观察规则异常；不代表没有风险，请同时查看数据完整性。</li>"
    more = (f'<p>另有 {len(alerts) - 4} 项提示，见网页版异常明细。</p>' if len(alerts) > 4 else "")
    gaps = "；".join(front["gaps"])
    return (
        '<section id="priority" style="padding:20px 0;border-bottom:1px solid #dbe2e4">'
        '<div class="index-grid">' + "".join(figures) + '</div>'
        f'<p style="font-size:12px;color:#63757a">{escape(front["scope_note"])}</p></section>'
        + domain_overview_html(report)
        + '<section id="tactical" style="padding:20px 0;border-bottom:1px solid #dbe2e4">'
        '<h2 style="font-size:20px;margin:0 0 10px">盘后跨资产战术压力观察</h2>'
        + "".join(f'<p style="font-size:14px;margin:6px 0">{escape(line)}</p>' for line in tactical_lines(report))
        + '<p style="font-size:12px;color:#63757a">影子观察，不触发预警；跨资产确认层，不作为宏微观重复投票。</p></section>'
        '<section id="alerts" style="padding:20px 0;border-bottom:1px solid #dbe2e4">'
        '<h2 style="font-size:20px;margin:0 0 10px">关键异常与分歧</h2>'
        f'<ul style="padding-left:20px;color:#9b3545">{warnings}</ul>{more}'
        + (f'<p style="font-size:12px;color:#806120">数据待核查：{escape(gaps)}</p>' if gaps else "")
        + '</section>')
