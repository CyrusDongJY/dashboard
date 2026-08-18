"""EOD Risk Capital Ladder shadow indicator.

This module measures whether equity risk appetite is spreading from liquid
growth leaders into increasingly speculative assets. It is not a measure of
system-wide dollar liquidity and is never connected to alert or position gates.
"""
from datetime import datetime, timezone
import math

import numpy as np
import pandas as pd


CALC_VERSION = "risk_capital_v1.0-shadow"
BENCHMARK = "QQQ"
MIN_HISTORY = 61
ABNORMAL_TURNOVER_Z = 1.0
ACTIVE_RELATIVE_MOVE_PCT = 0.5

LAYER_MEMBERS = {
    "institutional": ("NVDA", "TSLA"),
    "speculative": ("HOOD", "PLTR", "SMCI", "CVNA"),
    "extreme": ("COIN", "MSTR"),
}
LAYER_LABELS = {
    "institutional": "第一层：机构/高Beta成长",
    "speculative": "第二层：投机扩散",
    "extreme": "第三层：加密/反身性极端风险",
}
MIN_LAYER_MEMBERS = {
    "institutional": 2,
    "speculative": 3,
    "extreme": 2,
}
ALL_MEMBERS = tuple(
    symbol for members in LAYER_MEMBERS.values() for symbol in members)
MEMBER_LAYER = {
    symbol: layer for layer, members in LAYER_MEMBERS.items()
    for symbol in members
}

STATE_LABELS = {
    "BROAD_EXPANSION": "风险偏好广泛扩散",
    "SPECULATIVE_BLOWOFF": "末端投机升温",
    "MEGACAP_CONCENTRATION": "指数上涨但风险资本收缩",
    "EDGE_CONTRACTION": "风险从边缘资产撤退",
    "ACTIVE_DELEVERAGING": "放量去杠杆",
    "CONTRACTION": "风险偏好收缩",
    "MIXED": "扩散结构混合",
    "UNAVAILABLE": "数据不足",
}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_history(frame, report_date):
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=["date", "close", "volume"])
    aliases = {str(column).lower(): column for column in frame.columns}
    required = [aliases.get(name) for name in ("date", "close", "volume")]
    if any(column is None for column in required):
        return pd.DataFrame(columns=["date", "close", "volume"])
    normalized = frame[required].copy()
    normalized.columns = ["date", "close", "volume"]
    normalized["date"] = (pd.to_datetime(
        normalized["date"], errors="coerce", utc=True)
        .dt.tz_convert(None).dt.normalize())
    normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
    normalized["volume"] = pd.to_numeric(normalized["volume"], errors="coerce")
    cutoff = pd.Timestamp(report_date).normalize()
    return (normalized.dropna(subset=["date", "close"])
            .loc[lambda item: item["date"] <= cutoff]
            .sort_values("date").drop_duplicates("date", keep="last")
            .reset_index(drop=True))


def _return_pct(close, periods):
    if len(close) < periods + 1:
        return None
    previous = _finite(close.iloc[-(periods + 1)])
    current = _finite(close.iloc[-1])
    if previous in (None, 0) or current is None:
        return None
    return (current / previous - 1.0) * 100.0


def _robust_z(current, baseline):
    values = pd.to_numeric(baseline, errors="coerce").replace(
        [np.inf, -np.inf], np.nan).dropna()
    current = _finite(current)
    if current is None or len(values) < 20:
        return None
    median = float(values.median())
    mad = float((values - median).abs().median())
    scale = 1.4826 * mad
    if scale <= 1e-12:
        scale = float(values.std(ddof=1))
    if not math.isfinite(scale) or scale <= 1e-12:
        return 0.0
    return (current - median) / scale


def _benchmark_snapshot(frame, report_date):
    data = _normalize_history(frame, report_date)
    source_date = (data["date"].iloc[-1].strftime("%Y-%m-%d")
                   if not data.empty else None)
    fresh = source_date == str(report_date)
    return {
        "source_date": source_date,
        "sample_len": len(data),
        "return_1d_pct": _return_pct(data["close"], 1),
        "return_5d_pct": _return_pct(data["close"], 5),
        "quality": "OK" if fresh and len(data) >= 6 else (
            "STALE" if source_date else "MISSING"),
    }


def _empty_member(report_date, symbol, status, source_name):
    return {
        "report_date": str(report_date),
        "ticker": symbol,
        "layer": MEMBER_LAYER[symbol],
        "close": None,
        "return_1d_pct": None,
        "return_5d_pct": None,
        "qqq_return_1d_pct": None,
        "qqq_return_5d_pct": None,
        "relative_1d_pct": None,
        "relative_5d_pct": None,
        "dollar_volume_proxy": None,
        "turnover_ratio_20d": None,
        "abnormal_turnover_z60": None,
        "above_20d": None,
        "above_50d": None,
        "member_score": None,
        "active_direction": "NONE",
        "event_suspect": False,
        "source_date": None,
        "source_name": source_name,
        "sample_len": 0,
        "quality": status,
        "decision_eligible": False,
        "calc_version": CALC_VERSION,
    }


def compute_member_rows(histories, report_date, source_name="IBKR:TRADES:1day:RTH"):
    """Calculate member evidence from split-adjusted IBKR-style daily bars."""
    benchmark = _benchmark_snapshot(histories.get(BENCHMARK), report_date)
    rows = []
    for symbol in ALL_MEMBERS:
        data = _normalize_history(histories.get(symbol), report_date)
        if data.empty:
            rows.append(_empty_member(
                report_date, symbol, "MISSING", source_name))
            continue

        source_date = data["date"].iloc[-1].strftime("%Y-%m-%d")
        close = data["close"]
        volume = data["volume"]
        ret_1d = _return_pct(close, 1)
        ret_5d = _return_pct(close, 5)
        qqq_1d = benchmark.get("return_1d_pct")
        qqq_5d = benchmark.get("return_5d_pct")
        dollar_volume = close * volume
        current_dollar_volume = _finite(dollar_volume.iloc[-1])
        prior_20 = dollar_volume.iloc[-21:-1]
        prior_60_log = np.log(
            dollar_volume.iloc[-61:-1].where(dollar_volume.iloc[-61:-1] > 0))
        current_log = (math.log(current_dollar_volume)
                       if current_dollar_volume is not None and
                       current_dollar_volume > 0 else None)
        median_20 = _finite(prior_20.median()) if len(prior_20) >= 20 else None
        turnover_ratio = (current_dollar_volume / median_20
                          if current_dollar_volume is not None and
                          median_20 not in (None, 0) else None)
        abnormal_z = _robust_z(current_log, prior_60_log)

        quality = "OK"
        if source_date != str(report_date):
            quality = "STALE"
        elif len(data) < MIN_HISTORY:
            quality = "INSUFFICIENT_HISTORY"
        elif current_dollar_volume is None or current_dollar_volume <= 0:
            quality = "INVALID_VOLUME"
        elif benchmark.get("quality") != "OK":
            quality = "BENCHMARK_UNAVAILABLE"
        required_values = (ret_1d, ret_5d, qqq_1d, qqq_5d, turnover_ratio,
                           abnormal_z)
        eligible = quality == "OK" and all(
            value is not None for value in required_values)

        relative_1d = (ret_1d - qqq_1d
                       if ret_1d is not None and qqq_1d is not None else None)
        relative_5d = (ret_5d - qqq_5d
                       if ret_5d is not None and qqq_5d is not None else None)
        current_close = _finite(close.iloc[-1])
        sma20 = _finite(close.tail(20).mean()) if len(close) >= 20 else None
        sma50 = _finite(close.tail(50).mean()) if len(close) >= 50 else None
        above_20d = (current_close > sma20
                     if current_close is not None and sma20 is not None else None)
        above_50d = (current_close > sma50
                     if current_close is not None and sma50 is not None else None)
        score_inputs = [
            relative_1d is not None and relative_1d > 0,
            relative_5d is not None and relative_5d > 0,
            ret_5d is not None and ret_5d > 0,
            above_20d is True,
            above_50d is True,
        ]
        member_score = (sum(score_inputs) / len(score_inputs) * 100.0
                        if eligible else None)
        active_direction = "NORMAL"
        if eligible and abnormal_z >= ABNORMAL_TURNOVER_Z:
            active_direction = (
                "RISK_ON" if relative_1d >= ACTIVE_RELATIVE_MOVE_PCT else
                ("DELEVERAGING"
                 if relative_1d <= -ACTIVE_RELATIVE_MOVE_PCT else "NORMAL"))
        event_suspect = bool(
            eligible and (abs(ret_1d) >= 15.0 or abnormal_z >= 5.0))

        rows.append({
            "report_date": str(report_date),
            "ticker": symbol,
            "layer": MEMBER_LAYER[symbol],
            "close": round(current_close, 4) if current_close is not None else None,
            "return_1d_pct": round(ret_1d, 4) if ret_1d is not None else None,
            "return_5d_pct": round(ret_5d, 4) if ret_5d is not None else None,
            "qqq_return_1d_pct": round(qqq_1d, 4) if qqq_1d is not None else None,
            "qqq_return_5d_pct": round(qqq_5d, 4) if qqq_5d is not None else None,
            "relative_1d_pct": round(relative_1d, 4) if relative_1d is not None else None,
            "relative_5d_pct": round(relative_5d, 4) if relative_5d is not None else None,
            "dollar_volume_proxy": round(current_dollar_volume, 2)
            if current_dollar_volume is not None else None,
            "turnover_ratio_20d": round(turnover_ratio, 4)
            if turnover_ratio is not None else None,
            "abnormal_turnover_z60": round(abnormal_z, 4)
            if abnormal_z is not None else None,
            "above_20d": above_20d,
            "above_50d": above_50d,
            "member_score": round(member_score, 1)
            if member_score is not None else None,
            "active_direction": active_direction,
            "event_suspect": event_suspect,
            "source_date": source_date,
            "source_name": source_name,
            "sample_len": len(data),
            "quality": quality,
            "decision_eligible": eligible,
            "calc_version": CALC_VERSION,
        })
    return rows, benchmark


def _fraction(rows, predicate):
    return (sum(1 for row in rows if predicate(row)) / len(rows)
            if rows else None)


def aggregate_risk_capital(member_rows, benchmark, report_date):
    eligible = [row for row in member_rows if row.get("decision_eligible")]
    layer_details = {}
    for layer, members in LAYER_MEMBERS.items():
        rows = [row for row in eligible if row.get("ticker") in members]
        scores = [_finite(row.get("member_score")) for row in rows]
        scores = [score for score in scores if score is not None]
        layer_details[layer] = {
            "label": LAYER_LABELS[layer],
            "eligible_count": len(rows),
            "member_count": len(members),
            "coverage": round(len(rows) / len(members), 4),
            "score": round(float(np.mean(scores)), 1) if scores else None,
            "relative_breadth_1d": round(_fraction(
                rows, lambda row: row.get("relative_1d_pct", 0) > 0), 4)
            if rows else None,
            "relative_breadth_5d": round(_fraction(
                rows, lambda row: row.get("relative_5d_pct", 0) > 0), 4)
            if rows else None,
            "active_risk_on": [row["ticker"] for row in rows
                               if row.get("active_direction") == "RISK_ON"],
            "active_deleveraging": [row["ticker"] for row in rows
                                     if row.get("active_direction") == "DELEVERAGING"],
        }

    coverage = len(eligible) / len(ALL_MEMBERS)
    ready = (
        len(eligible) >= 7 and
        all(layer_details[layer]["eligible_count"] >= minimum
            for layer, minimum in MIN_LAYER_MEMBERS.items())
    )
    layer_scores = {
        layer: _finite(detail.get("score"))
        for layer, detail in layer_details.items()
    }
    score = (round(float(np.mean(list(layer_scores.values()))), 1)
             if ready and all(value is not None for value in layer_scores.values())
             else None)

    relative_1d = _fraction(
        eligible, lambda row: row.get("relative_1d_pct", 0) > 0)
    relative_5d = _fraction(
        eligible, lambda row: row.get("relative_5d_pct", 0) > 0)
    positive_1d = _fraction(
        eligible, lambda row: row.get("return_1d_pct", 0) > 0)
    positive_5d = _fraction(
        eligible, lambda row: row.get("return_5d_pct", 0) > 0)
    turnover_breadth = _fraction(
        eligible, lambda row: row.get("abnormal_turnover_z60", -99) >=
        ABNORMAL_TURNOVER_Z)
    active_up = [row["ticker"] for row in eligible
                 if row.get("active_direction") == "RISK_ON"]
    active_down = [row["ticker"] for row in eligible
                   if row.get("active_direction") == "DELEVERAGING"]
    suspect = [row["ticker"] for row in eligible if row.get("event_suspect")]

    state = "UNAVAILABLE"
    state_detail = "覆盖不足；不发布风险资本状态"
    if ready:
        institutional = layer_scores["institutional"]
        speculative = layer_scores["speculative"]
        extreme = layer_scores["extreme"]
        negative_layers = sum(
            value < 50 for value in layer_scores.values())
        qqq_1d = _finite(benchmark.get("return_1d_pct"))
        if len(active_down) >= 3 and negative_layers >= 2:
            state = "ACTIVE_DELEVERAGING"
            state_detail = "至少3只成分放量跑输QQQ，且至少两层处于收缩侧"
        elif (qqq_1d is not None and qqq_1d > 0 and
              relative_1d is not None and relative_1d <= 0.375 and
              relative_5d is not None and relative_5d <= 0.375):
            state = "MEGACAP_CONCENTRATION"
            state_detail = "QQQ上涨，但多数风险资本成分在1D与5D均跑输"
        elif (extreme >= 75 and institutional < 50 and
              len(layer_details["extreme"]["active_risk_on"]) >= 2):
            state = "SPECULATIVE_BLOWOFF"
            state_detail = "极端风险层两只成分均放量走强，但机构成长层未同步确认"
        elif (all(value >= 60 for value in layer_scores.values()) and
              positive_5d is not None and positive_5d >= 0.625):
            state = "BROAD_EXPANSION"
            state_detail = "三层风险资本同时扩散，且多数成分5日绝对收益为正"
        elif extreme < 40 and speculative < 50 and institutional >= 50:
            state = "EDGE_CONTRACTION"
            state_detail = "机构层尚可，但投机与极端风险层已先行撤退"
        elif (negative_layers >= 2 or
              (relative_5d is not None and relative_5d <= 0.375)):
            state = "CONTRACTION"
            state_detail = "至少两层偏弱或多数成分5日跑输QQQ"
        else:
            state = "MIXED"
            state_detail = "层间信号不一致，尚未形成明确扩散或收缩顺序"

    history_quality = (_fraction(
        eligible, lambda row: int(row.get("sample_len") or 0) >= 126) or 0.0)
    confidence = min(1.0, coverage * (0.7 + 0.3 * history_quality))
    now = datetime.now(timezone.utc).isoformat()
    return {
        "report_date": str(report_date),
        "state": state,
        "state_cn": STATE_LABELS[state],
        "state_detail": state_detail,
        "score": score,
        "coverage": round(coverage, 4),
        "confidence": round(confidence, 4),
        "qqq_return_1d_pct": benchmark.get("return_1d_pct"),
        "qqq_return_5d_pct": benchmark.get("return_5d_pct"),
        "relative_breadth_1d": round(relative_1d, 4) if relative_1d is not None else None,
        "relative_breadth_5d": round(relative_5d, 4) if relative_5d is not None else None,
        "positive_breadth_1d": round(positive_1d, 4) if positive_1d is not None else None,
        "positive_breadth_5d": round(positive_5d, 4) if positive_5d is not None else None,
        "turnover_breadth": round(turnover_breadth, 4)
        if turnover_breadth is not None else None,
        "active_risk_on": active_up,
        "active_deleveraging": active_down,
        "event_suspects": suspect,
        "layer_details": layer_details,
        "source_date": benchmark.get("source_date"),
        "source_name": "IBKR:TRADES:1day:RTH",
        "calc_version": CALC_VERSION,
        "shadow_mode": True,
        "computed_at": now,
        "members": member_rows,
    }


def compute_risk_capital_ladder(histories, report_date,
                                source_name="IBKR:TRADES:1day:RTH"):
    rows, benchmark = compute_member_rows(
        histories, report_date, source_name=source_name)
    result = aggregate_risk_capital(rows, benchmark, report_date)
    result["source_name"] = source_name
    return result


def persistence_payloads(result):
    summary = {key: value for key, value in result.items() if key != "members"}
    members = []
    now = datetime.now(timezone.utc).isoformat()
    for member in result.get("members", []):
        payload = dict(member)
        payload["created_at"] = now
        members.append(payload)
    summary["created_at"] = now
    return summary, members


def fetch_risk_capital_ladder(supabase, report_date):
    fallback = {
        "report_date": str(report_date),
        "state": "UNAVAILABLE",
        "state_cn": STATE_LABELS["UNAVAILABLE"],
        "state_detail": "风险资本阶梯数据尚未入库",
        "coverage": 0.0,
        "confidence": 0.0,
        "members": [],
        "calc_version": CALC_VERSION,
        "shadow_mode": True,
    }
    try:
        summary_response = (supabase.table("risk_capital_daily").select("*")
                            .eq("report_date", report_date).limit(1).execute())
        member_response = (supabase.table("risk_capital_member_daily").select("*")
                           .eq("report_date", report_date).order("ticker").execute())
        if not summary_response.data:
            return fallback
        summary = dict(summary_response.data[0])
        summary["members"] = list(member_response.data or [])
        return summary
    except Exception:
        return fallback


def _fmt(value, digits=1, suffix=""):
    number = _finite(value)
    return "NA" if number is None else f"{number:.{digits}f}{suffix}"


def _fmt_fraction(value):
    number = _finite(value)
    return "NA" if number is None else f"{number * 100:.0f}%"


def format_risk_capital_summary(result):
    """Render a compact but auditable standalone email section."""
    result = result or {}
    score = _finite(result.get("score"))
    coverage = _finite(result.get("coverage")) or 0.0
    confidence = _finite(result.get("confidence")) or 0.0
    lines = [
        "=== 风险资本阶梯（影子观察，不触发预警） ===",
        f"状态：{result.get('state_cn') or STATE_LABELS.get(result.get('state'), '数据不足')}",
        f"说明：{result.get('state_detail') or '暂无'}",
        f"扩散观察值：{_fmt(score) + '/100' if score is not None else '数据不足'}",
        f"有效覆盖：{coverage:.0%} | 置信度：{confidence:.0%} | "
        f"版本：{result.get('calc_version') or CALC_VERSION}",
        (f"QQQ：1D {_fmt(result.get('qqq_return_1d_pct'), 2, '%')} | "
         f"5D {_fmt(result.get('qqq_return_5d_pct'), 2, '%')}"),
        (f"成分广度：1D跑赢QQQ {_fmt_fraction(result.get('relative_breadth_1d'))} | "
         f"5D跑赢QQQ {_fmt_fraction(result.get('relative_breadth_5d'))} | "
         f"异常成交额覆盖 {_fmt_fraction(result.get('turnover_breadth'))}"),
        "分层：",
    ]
    details = result.get("layer_details") or {}
    for layer in LAYER_MEMBERS:
        item = details.get(layer) or {}
        lines.append(
            f"- {LAYER_LABELS[layer]}：{_fmt(item.get('score'))}/100 | "
            f"覆盖 {item.get('eligible_count', 0)}/{item.get('member_count', len(LAYER_MEMBERS[layer]))}")

    active_up = result.get("active_risk_on") or []
    active_down = result.get("active_deleveraging") or []
    suspects = result.get("event_suspects") or []
    lines.extend([
        "放量跑赢QQQ：" + ("、".join(active_up) if active_up else "无"),
        "放量跑输QQQ：" + ("、".join(active_down) if active_down else "无"),
    ])
    if suspects:
        lines.append(
            "个股事件污染提示：" + "、".join(suspects) +
            "（仅保留分项，不允许单独决定总状态）")

    members = {row.get("ticker"): row for row in result.get("members", [])}
    lines.append("成分明细（相对QQQ / 异常成交额）：")
    for symbol in ALL_MEMBERS:
        row = members.get(symbol, {})
        lines.append(
            f"- {symbol}: 1D {_fmt(row.get('relative_1d_pct'), 2, '%')} | "
            f"5D {_fmt(row.get('relative_5d_pct'), 2, '%')} | "
            f"量比 {_fmt(row.get('turnover_ratio_20d'), 2)}x | "
            f"稳健z {_fmt(row.get('abnormal_turnover_z60'), 2)} | "
            f"质量 {row.get('quality', 'MISSING')}")
    lines.append(
        "口径：成交额为收盘价×RTH成交量代理；本指标衡量风险偏好扩散，"
        "不等同于美元流动性、净资金流或未来收益预测。")
    return "\n".join(lines)
