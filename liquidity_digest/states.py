"""Read-only state presentation. Levels and changes are never collapsed into a score."""
from datetime import date, timedelta

from temporal_monitor import HIGH_PRESSURE, LOW_PRESSURE, IMPULSE_THRESHOLD, SHOCK_THRESHOLD, PANEL_SPECS
from liquidity_monitor import FUNDING_STRESS_GUARDS
from .model import finite, sessions

STATE_VERSION = "digest_state_v1.2-shadow"
PANEL_NAMES = {
    "liquidity": "综合流动性支持", "environment": "综合环境压力",
    "tactical_stress": "战术压力", "risk_capital": "风险资本",
}
DIRECTION_NAMES = {
    "liquidity": {"UP": "趋于收紧", "DOWN": "趋于放松", "FLAT": "变化不明显", "UNKNOWN": "方向待确认"},
    "risk_capital": {"UP": "扩散减弱", "DOWN": "扩散增强", "FLAT": "变化不明显", "UNKNOWN": "方向待确认"},
    "environment": {"UP": "压力增加", "DOWN": "压力减轻", "FLAT": "变化不明显", "UNKNOWN": "方向待确认"},
    "tactical_stress": {"UP": "压力增加", "DOWN": "压力减轻", "FLAT": "变化不明显", "UNKNOWN": "方向待确认"},
}
LEVEL_NAMES = {
    "liquidity": {"HIGH": "支持偏弱", "MID": "中间区间", "LOW": "支持偏强", "UNKNOWN": "水位未知"},
    "risk_capital": {"HIGH": "扩散偏弱", "MID": "中间区间", "LOW": "扩散偏强", "UNKNOWN": "水位未知"},
    "environment": {"HIGH": "压力偏高", "MID": "中间区间", "LOW": "压力偏低", "UNKNOWN": "水位未知"},
    "tactical_stress": {"HIGH": "压力偏高", "MID": "中间区间", "LOW": "压力偏低", "UNKNOWN": "水位未知"},
}
STATE_COLORS = {"UP": "#b33143", "DOWN": "#087f75", "FLAT": "#586d77", "UNKNOWN": "#7c858a"}
MATRIX_TEXT = {
    ("HIGH", "DOWN"): "高压缓解", ("HIGH", "FLAT"): "高压观察", ("HIGH", "UP"): "高压加剧",
    ("MID", "DOWN"): "压力回落", ("MID", "FLAT"): "区间观察", ("MID", "UP"): "压力累积",
    ("LOW", "DOWN"): "低压改善", ("LOW", "FLAT"): "低压观察", ("LOW", "UP"): "低位升压",
}


def zone(value):
    if value is None:
        return "UNKNOWN"
    return "HIGH" if value >= HIGH_PRESSURE else "LOW" if value <= LOW_PRESSURE else "MID"


def direction(delta):
    if delta is None:
        return "UNKNOWN"
    return "UP" if delta >= IMPULSE_THRESHOLD else "DOWN" if delta <= -IMPULSE_THRESHOLD else "FLAT"


def eligible(row, key):
    risk = finite(row.get("risk_value"))
    coverage, confidence = finite(row.get("coverage")), finite(row.get("confidence"))
    details = row.get("details") or {}
    return (
        row.get("quality_status") == "OK" and row.get("observation_mode") == "LIVE_SHADOW"
        and row.get("source_date") == row.get("report_date")
        and risk is not None and 0 <= risk <= 100
        and coverage is not None and PANEL_SPECS[key].min_coverage <= coverage <= 1
        and confidence is not None and .75 <= confidence <= 1
        and bool(row.get("calc_version")) and isinstance(details, dict)
        and bool(details.get("source_calc_version"))
        and not str(row.get("source_state") or "").startswith("数据不足")
    )


def panel_state(report, key):
    result = {
        "panel": key, "label": PANEL_NAMES[key], "risk_value": None, "display_value": None,
        "zone": "UNKNOWN", "direction": "UNKNOWN", "direction_cn": "方向待确认",
        "level_cn": "水位未知", "source_date": None, "coverage": None,
        "confidence": None, "source_version": None, "temporal_version": None,
        "changes": {"1d": None, "5d": None, "21d": None},
        "windows": {},
        "high_zone_days": None, "color": STATE_COLORS["UNKNOWN"],
        "reason": "当日合格状态记录不足", "timeline": [],
    }
    audit = report["source_audit"].get("db_temporal_state_daily", {})
    if audit.get("status") != "OK":
        result["reason"] = "时间状态源未刷新，旧缓存不参与当前判断"
        return result
    raw = report["temporal"].get(key, [])
    current = next((r for r in raw if r["report_date"] == report["market_date"]), None)
    if not current or not eligible(current, key):
        return result
    version = current["details"]["source_calc_version"]
    signature = current["details"].get("comparison_signature")
    start = current["details"].get("comparison_start_date", "")
    records = {r["report_date"]: r for r in raw if eligible(r, key)
               and r["calc_version"] == current["calc_version"]
               and r["details"]["source_calc_version"] == version
               and r["details"].get("comparison_signature") == signature
               and r["report_date"] >= start}
    days = sessions(date.fromisoformat(report["market_date"]) - timedelta(days=100), report["market_date"])
    value = float(current["risk_value"])
    changes = {}
    windows = {}
    for count in (1, 5, 21):
        window = days[-count - 1:]
        # A direction label requires a complete real-session window of the same source version.
        ready = len(window) == count + 1 and all(d in records for d in window)
        changes[str(count) + "d"] = round(value - float(records[window[0]]["risk_value"]), 6) if ready else None
        windows[str(count) + "d"] = {"required": count + 1,
            "available": sum(d in records for d in window),
            "missing_dates": [d for d in window if d not in records]}
    high_days = 0
    for day in reversed(days):
        row = records.get(day)
        if not row or zone(float(row["risk_value"])) != "HIGH":
            break
        high_days += 1
    state_zone, state_direction = zone(value), direction(changes["5d"])
    display_value = 100 - value if key in ("liquidity", "risk_capital") else value
    result.update(
        risk_value=value, display_value=display_value, zone=state_zone, direction=state_direction,
        direction_cn=DIRECTION_NAMES[key][state_direction], level_cn=LEVEL_NAMES[key][state_zone],
        source_date=current["source_date"], coverage=float(current["coverage"]),
        confidence=float(current["confidence"]), source_version=version,
        temporal_version=current["calc_version"], changes=changes, windows=windows, high_zone_days=high_days,
        color=STATE_COLORS[state_direction],
        reason=(f'同版5日窗口 {windows["5d"]["available"]}/6 点；缺失 '
                + ", ".join(windows["5d"]["missing_dates"])) if state_direction == "UNKNOWN"
                else "沿用既有影子规则，不是交易信号",
        timeline=[{"date": day, "risk_value": float(records[day]["risk_value"]) if day in records else None}
                  for day in days[-22:]],
    )
    return result


def describe_pressure(state):
    if state["zone"] == "UNKNOWN":
        return "压力水位未知，不能按低风险处理。"
    if state["direction"] == "UNKNOWN":
        return state["level_cn"] + "，但连续历史不足，变化方向暂不判断。"
    if state["zone"] == "HIGH":
        if state["direction"] == "DOWN":
            return "压力有所减轻，但仍在高压区，不等于已恢复正常。"
        return "仍在高压区，优先关注持续时间和融资、信用的确认。"
    if state["zone"] == "LOW" and state["direction"] == "UP":
        return "当前水位偏低，但压力正在增加；不能仅凭低水位忽略变化。"
    if state["direction"] == "FLAT":
        return "近5日未越变化观察线，不表示没有风险，也不表示每天持平。"
    return "方向与当前水位分开观察，尚不能据此确认市场反转。"


def matrix_note(state):
    if state["zone"] == "UNKNOWN":
        return "当前无法定位：水位数据不足，不按平稳处理。"
    known = f'{state["level_cn"]} {state["risk_value"]:.1f}/100'
    if state["direction"] == "UNKNOWN":
        count = state.get("windows", {}).get("5d", {}).get("available", 0)
        return known + f'；方向待确认（同版 {count}/6 点）'
    return known + "；缓解不等于低压"


def build_states(report):
    panels = {key: panel_state(report, key) for key in PANEL_NAMES}
    funding = panels["liquidity"]
    # These two share risk inputs; do not turn agreement into two independent votes.
    pressure = panels["environment"] if panels["environment"]["risk_value"] is not None else panels["tactical_stress"]
    pressure_title = MATRIX_TEXT.get((pressure["zone"], pressure["direction"]), pressure["direction_cn"])
    title = f'综合流动性：{funding["direction_cn"]}｜{pressure["label"]}：{pressure_title}'
    alerts = []
    guards = []
    for metric, guard in (("sofr_iorb", "sofr_iorb_bps"), ("effr_iorb", "effr_iorb_bps"),
                          ("sofr_tail", "sofr_dispersion_bps")):
        item = report.get("metrics", {}).get(metric, {})
        value = finite(item.get("value"))
        threshold = FUNDING_STRESS_GUARDS[guard]
        if (item.get("status") == "OK" and item.get("acquisition") == "OK"
                and item.get("source_date") == report["market_date"]
                and value is not None and value > threshold):
            guards.append({"metric": metric, "value_bps": value, "threshold_bps": threshold,
                           "source_date": item["source_date"]})
    if guards:
        alerts.append({"kind": "raw_guard", "text": "晨间观察：融资数据触及原有绝对观察线；"
                       "单项越线，不等于系统性收紧。", "evidence": guards})
    if pressure["zone"] == "HIGH":
        alerts.append({"kind": "pressure", "text": "高压提示：" + describe_pressure(pressure)})
    if pressure["zone"] == "LOW" and pressure["direction"] == "UP":
        alerts.append({"kind": "watch", "text": "早期变化：水位虽低，压力已在增加。"})
    for observed in (funding, pressure):
        shock = observed["changes"]["1d"]
        if shock is not None and abs(shock) >= SHOCK_THRESHOLD:
            alerts.append({"kind": "shock",
                           "text": f"单日突变：{observed['label']}压力方向值变化 {shock:+.1f} 点。"})
    fd, pd = funding["direction"], pressure["direction"]
    if fd in ("UP", "DOWN") and pd in ("UP", "DOWN") and fd != pd:
        alerts.append({"kind": "divergence", "text": "综合流动性与压力背离，暂不形成单一结论。"})
    env, tactical = panels["environment"], panels["tactical_stress"]
    if env["direction"] in ("UP", "DOWN") and tactical["direction"] in ("UP", "DOWN") and env["direction"] != tactical["direction"]:
        alerts.append({"kind": "divergence", "text": "环境与战术压力方向相反，需分别观察，不能平均抵消。"})
    missing = [p["label"] for p in (funding, pressure) if p["direction"] == "UNKNOWN"]
    if missing:
        alerts.insert(0, {"kind": "quality", "text": "待确认：" + "、".join(missing) + "；缺数不等于平稳。"})
    if funding["zone"] == "HIGH" and funding["direction"] == "DOWN":
        alerts.append({"kind": "watch", "text": "综合流动性支持边际改善，但水位仍弱，不能视为资金充裕。"})
    if pressure["panel"] == "tactical_stress" and pressure["risk_value"] is not None:
        alerts.append({"kind": "context", "text": "环境记录不可用，主图采用战术压力代理，未与环境指数相加。"})
    capital = panels["risk_capital"]
    if fd in ("UP", "DOWN") and capital["direction"] in ("UP", "DOWN") and fd != capital["direction"]:
        text = ("综合流动性支持改善，但风险资本扩散仍在转弱。" if fd == "DOWN"
                else "风险资本扩散改善，但综合流动性支持仍在转弱。")
        alerts.append({"kind": "divergence", "text": text})
    if not alerts:
        alerts.append({"kind": "context", "text": "未检出上述状态提示；不代表没有风险。"})
    priority = {"shock": 0, "quality": 1, "pressure": 2, "raw_guard": 3,
                "divergence": 4, "watch": 5, "context": 6}
    alerts.sort(key=lambda item: priority[item["kind"]])
    return {
        "version": STATE_VERSION, "panels": panels, "funding": funding, "pressure": pressure,
        "headline": title, "alerts": alerts, "pressure_note": describe_pressure(pressure),
        "thresholds": {"low": LOW_PRESSURE, "high": HIGH_PRESSURE,
                       "impulse_5d": IMPULSE_THRESHOLD, "shock_1d": SHOCK_THRESHOLD},
        "scope_note": "昨日收盘状态；今晨原始融资数据另列，不据此重算旧评分。展示规则尚非经过回测的交易信号。",
    }
