# -*- coding: utf-8 -*-
"""Cross-panel temporal states and forward shadow evaluation.

This module does not change any source score or alert gate.  It normalizes the
existing shadow panels onto a common "higher = more pressure" scale, describes
their time behavior, and evaluates matured observations with future market
outcomes.  Historical replay and live shadow observations remain distinct.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import numpy as np
import pandas as pd


CALC_VERSION = "temporal_state_v1.0-shadow"
EVAL_VERSION = "temporal_eval_v1.0-shadow"
SHADOW_MODE = True
HORIZONS = (1, 5, 21)
MIN_LIVE_REVIEW_DAYS = 60
TARGET_LIVE_REVIEW_DAYS = 120
MIN_STATE_OUTCOMES = 20

HIGH_PRESSURE = 65.0
LOW_PRESSURE = 35.0
IMPULSE_THRESHOLD = 5.0
SHOCK_THRESHOLD = 10.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PanelSpec:
    key: str
    label: str
    table: str
    date_col: str
    value_col: Optional[str]
    level_label: str
    confirmation_group: str
    pressure_polarity: int = 1
    coverage_col: Optional[str] = "coverage"
    confidence_col: Optional[str] = None
    min_coverage: float = 0.0


PANEL_SPECS = {
    "environment": PanelSpec(
        "environment", "环境指数", "environment_daily", "report_date",
        "composite", "风险压力", "cross_asset_stress", min_coverage=0.0),
    "liquidity": PanelSpec(
        "liquidity", "流动性水位", "liquidity_daily", "report_date",
        "composite", "流动性支持", "system_liquidity",
        pressure_polarity=-1, min_coverage=0.60),
    "tactical_stress": PanelSpec(
        "tactical_stress", "盘后战术压力", "market_history", "record_date",
        "eod_stress_score", "压力观察值", "cross_asset_stress",
        coverage_col="stress_coverage",
        confidence_col="stress_confidence", min_coverage=0.70),
    "risk_capital": PanelSpec(
        "risk_capital", "风险资本扩散", "risk_capital_daily", "report_date",
        "score", "扩散观察值", "risk_appetite", pressure_polarity=-1,
        confidence_col="confidence", min_coverage=0.875),
    "event_pulse": PanelSpec(
        "event_pulse", "动态异动脉冲", "risk_event_pulse_daily",
        "report_date", None, "候选数量", "event_breadth",
        coverage_col="scan_coverage",
        confidence_col="candidate_coverage", min_coverage=0.50),
}


PRESSURE_CODES = {
    "NEW_PRESSURE", "PRESSURE_BUILDING", "PRESSURE_PERSISTENT",
    "HIGH_PRESSURE", "OVERHEAT",
}
RELIEF_CODES = {"RELIEF", "RECOVERY", "CALM"}

EVENT_RISK_OFF = {
    "RISK_OFF_CONTAGION", "THEME_RISK_OFF", "ISOLATED_EVENT_CRASH",
}
EVENT_RISK_ON = {
    "RISK_ON_DIFFUSION", "THEME_RISK_ON", "ISOLATED_EVENT_SPIKE",
}
CAPITAL_PRESSURE_STATES = {
    "ACTIVE_DELEVERAGING", "MEGACAP_CONCENTRATION",
    "EDGE_CONTRACTION", "CONTRACTION",
}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mapping(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _list_value(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return []


def _date_string(value):
    if value is None or pd.isna(value):
        return None
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _state_is_unavailable(panel, state):
    state = str(state or "")
    if not state:
        return panel != "tactical_stress"
    if panel == "environment":
        return state.startswith("数据不足")
    if panel == "liquidity":
        return state.startswith("数据不足")
    return state == "UNAVAILABLE"


def _nested_source_dates(value):
    dates = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_date":
                parsed = _date_string(item)
                if parsed:
                    dates.append(parsed)
            else:
                dates.extend(_nested_source_dates(item))
    elif isinstance(value, list):
        for item in value:
            dates.extend(_nested_source_dates(item))
    return dates


def _row_source_date(row, spec):
    direct = _date_string(row.get("source_date"))
    if direct:
        return direct
    nested = []
    for key in ("details", "stress_components"):
        nested.extend(_nested_source_dates(_mapping(row.get(key))))
    return max(nested) if nested else _date_string(row.get(spec.date_col))


def _source_fingerprint(row, spec):
    nested = []
    for key in ("details", "stress_components"):
        nested.extend(_nested_source_dates(_mapping(row.get(key))))
    if nested:
        return "|".join(sorted(set(nested)))
    return _row_source_date(row, spec)


def _confidence(row, spec, coverage):
    explicit = _finite(row.get(spec.confidence_col)) if spec.confidence_col else None
    if explicit is None:
        explicit = coverage
    if explicit is None:
        return 0.0
    return round(max(0.0, min(1.0, explicit)), 4)


def _normalize_history(rows, spec):
    records = []
    if isinstance(rows, pd.DataFrame):
        source_rows = rows.to_dict("records")
    else:
        source_rows = list(rows or [])
    for row in source_rows:
        report_date = _date_string(row.get(spec.date_col))
        if not report_date:
            continue
        raw_state = str(row.get("state") or "")
        state = (raw_state.split(" | ", 1)[0]
                 if spec.key == "environment" else raw_state)
        coverage = (_finite(row.get(spec.coverage_col))
                    if spec.coverage_col else 1.0)
        if coverage is None:
            coverage = 0.0
        raw_value = (_finite(row.get(spec.value_col))
                     if spec.value_col else None)
        eligible = (
            not _state_is_unavailable(spec.key, state)
            and coverage >= spec.min_coverage
            and (raw_value is not None or spec.key == "event_pulse")
        )
        risk_value = None
        if eligible and raw_value is not None:
            risk_value = raw_value if spec.pressure_polarity > 0 else 100.0 - raw_value
            risk_value = max(0.0, min(100.0, risk_value))
        confidence = _confidence(row, spec, coverage)
        quality = ("INSUFFICIENT" if not eligible else
                   ("OK" if confidence >= 0.75 else "PARTIAL"))
        details = {
            "source_state": state or None,
            "source_state_detail": raw_state or None,
            "level_label": spec.level_label,
            "pressure_polarity": spec.pressure_polarity,
            "confirmation_group": spec.confirmation_group,
            "source_calc_version": row.get("calc_version") or row.get(
                "stress_calc_version"),
            "source_fingerprint": _source_fingerprint(row, spec),
        }
        if spec.key == "event_pulse":
            details.update({
                "candidate_count": int(row.get("candidate_count") or 0),
                "eligible_count": int(row.get("eligible_count") or 0),
                "risk_on_count": len(_list_value(row.get("risk_on_tickers"))),
                "risk_off_count": len(_list_value(row.get("risk_off_tickers"))),
                "positive_industry_count": len(
                    _list_value(row.get("positive_industries"))),
                "negative_industry_count": len(
                    _list_value(row.get("negative_industries"))),
                "market_confirmation": bool(row.get("market_confirmation")),
            })
            raw_value = float(details["candidate_count"])
        records.append({
            "report_date": report_date,
            "level_value": raw_value,
            "risk_value": risk_value,
            "coverage": max(0.0, min(1.0, coverage)),
            "confidence": confidence,
            "quality_status": quality,
            "source_state": state,
            "source_date": _row_source_date(row, spec),
            "source_fingerprint": details["source_fingerprint"],
            "details": details,
        })
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    frame["_date"] = pd.to_datetime(frame["report_date"])
    return (frame.sort_values("_date")
            .drop_duplicates("report_date", keep="last")
            .reset_index(drop=True))


def _window_delta(values, index, periods, required_ratio=0.80):
    if index < periods:
        return None
    window = values.iloc[index - periods:index + 1]
    required = int(math.ceil((periods + 1) * required_ratio))
    if window.notna().sum() < required:
        return None
    current = _finite(window.iloc[-1])
    prior = _finite(window.iloc[0])
    return None if current is None or prior is None else current - prior


def _zone(value):
    value = _finite(value)
    if value is None:
        return "UNAVAILABLE"
    if value >= HIGH_PRESSURE:
        return "HIGH"
    if value <= LOW_PRESSURE:
        return "LOW"
    return "MID"


def _consecutive_zone(values, index, zone):
    count = 0
    for position in range(index, -1, -1):
        if _zone(values.iloc[position]) != zone:
            break
        count += 1
    return count


def _direction_streak(values, index, direction):
    count = 0
    for position in range(index, 0, -1):
        current = _finite(values.iloc[position])
        prior = _finite(values.iloc[position - 1])
        if current is None or prior is None:
            break
        delta = current - prior
        if (direction > 0 and delta < 0) or (direction < 0 and delta > 0):
            break
        count += 1
    return max(1, count + 1)


def _native_updates(frame, index, periods=5):
    start = max(0, index - periods + 1)
    values = [str(value) for value in
              frame.loc[start:index, "source_fingerprint"].tolist() if value]
    return len(set(values))


def _source_family_streak(frame, index, family):
    count = 0
    for position in range(index, -1, -1):
        state = frame.iloc[position]["source_state"]
        current_family = (
            "CAPITAL_PRESSURE" if state in CAPITAL_PRESSURE_STATES
            else ("CAPITAL_EXPANSION" if state == "BROAD_EXPANSION" else state)
        )
        if current_family != family:
            break
        count += 1
    return count


def _continuous_state(frame, index):
    risk = _finite(frame.at[index, "risk_value"])
    if risk is None:
        return "INSUFFICIENT_DATA", "数据不足", 0, False, None
    values = frame["risk_value"]
    change_1d = _window_delta(values, index, 1)
    change_5d = _window_delta(values, index, 5)
    previous_5d = _window_delta(values, index - 1, 5) if index else None
    current_zone = _zone(risk)
    previous = _finite(values.iloc[index - 1]) if index else None
    previous_zone = _zone(previous)

    if current_zone == "HIGH":
        persistence = _consecutive_zone(values, index, "HIGH")
    elif change_5d is not None and abs(change_5d) >= IMPULSE_THRESHOLD:
        persistence = _direction_streak(
            values, index, 1 if change_5d > 0 else -1)
    else:
        persistence = _consecutive_zone(values, index, current_zone)

    turn_direction = None
    turn_candidate = False
    if previous_5d is not None and change_5d is not None:
        reversed_direction = (
            previous_5d <= -IMPULSE_THRESHOLD
            and change_5d >= IMPULSE_THRESHOLD
        ) or (
            previous_5d >= IMPULSE_THRESHOLD
            and change_5d <= -IMPULSE_THRESHOLD
        )
        abrupt = change_1d is not None and abs(change_1d) >= SHOCK_THRESHOLD
        crossed_zone = previous_zone != current_zone
        turn_candidate = reversed_direction and (abrupt or crossed_zone)
        if turn_candidate:
            turn_direction = 1 if change_5d > 0 else -1

    if current_zone == "HIGH" and previous_zone != "HIGH":
        code, label = "NEW_PRESSURE", "新发压力"
    elif current_zone == "HIGH" and change_5d is not None and change_5d >= IMPULSE_THRESHOLD:
        code, label = "PRESSURE_BUILDING", "压力累积"
    elif current_zone == "HIGH" and change_5d is not None and change_5d <= -IMPULSE_THRESHOLD:
        code, label = "RELIEF", "高压下的边际缓解"
    elif current_zone == "HIGH" and persistence >= 3:
        code, label = "PRESSURE_PERSISTENT", "高位压力延续"
    elif current_zone == "HIGH":
        code, label = "HIGH_PRESSURE", "高压观察"
    elif change_5d is not None and change_5d >= IMPULSE_THRESHOLD:
        code, label = "PRESSURE_BUILDING", "压力正在累积"
    elif risk >= 50 and change_5d is not None and change_5d <= -IMPULSE_THRESHOLD:
        code, label = "RELIEF", "压力边际缓解"
    elif risk < 50 and change_5d is not None and change_5d <= -IMPULSE_THRESHOLD:
        code, label = "RECOVERY", "环境继续改善"
    elif current_zone == "LOW":
        code, label = "CALM", "低压平稳"
    else:
        code, label = "STABLE", "区间平稳"
    return code, label, persistence, turn_candidate, turn_direction


def _base_payload(spec, frame, index, observation_mode):
    row = frame.iloc[index]
    risk_values = frame["risk_value"]
    code, label, persistence, turn_candidate, turn_direction = (
        _continuous_state(frame, index))
    prior_state = frame.iloc[index - 1]["source_state"] if index else None
    current_state = row["source_state"] or None
    transition = None
    if prior_state and current_state and prior_state != current_state:
        transition = f"{prior_state} -> {current_state}"
    details = dict(row["details"])
    details.update({
        "native_updates_5d": _native_updates(frame, index),
        "turn_candidate": turn_candidate,
        "turn_direction": turn_direction,
        "high_pressure_threshold": HIGH_PRESSURE,
        "low_pressure_threshold": LOW_PRESSURE,
        "impulse_threshold": IMPULSE_THRESHOLD,
        "shock_threshold": SHOCK_THRESHOLD,
    })
    payload = {
        "report_date": row["report_date"],
        "panel": spec.key,
        "level_value": _finite(row["level_value"]),
        "risk_value": _finite(row["risk_value"]),
        "change_1d": _finite(_window_delta(risk_values, index, 1)),
        "change_5d": _finite(_window_delta(risk_values, index, 5)),
        "change_21d": _finite(_window_delta(risk_values, index, 21)),
        "persistence_days": int(persistence),
        "temporal_state": code,
        "temporal_state_cn": label,
        "transition_type": transition,
        "state_changed": bool(transition),
        "coverage": round(float(row["coverage"]), 4),
        "confidence": round(float(row["confidence"]), 4),
        "independent_confirmation_count": 0,
        "quality_status": row["quality_status"],
        "source_date": row["source_date"],
        "source_state": current_state,
        "observation_mode": observation_mode,
        "details": details,
        "shadow_mode": SHADOW_MODE,
        "calc_version": CALC_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    if spec.key == "risk_capital" and row["quality_status"] != "INSUFFICIENT":
        source_state = row["source_state"]
        if source_state in CAPITAL_PRESSURE_STATES:
            streak = _source_family_streak(frame, index, "CAPITAL_PRESSURE")
            payload["persistence_days"] = streak
            payload["temporal_state"] = (
                "PRESSURE_PERSISTENT" if streak >= 2 else "NEW_PRESSURE")
            labels = {
                "ACTIVE_DELEVERAGING": "主动去杠杆延续" if streak >= 2 else "新发主动去杠杆",
                "MEGACAP_CONCENTRATION": "风险偏好持续集中" if streak >= 2 else "风险偏好转向集中",
                "EDGE_CONTRACTION": "边缘风险层持续收缩" if streak >= 2 else "边缘风险层开始收缩",
                "CONTRACTION": "风险资本收缩延续" if streak >= 2 else "风险资本开始收缩",
            }
            payload["temporal_state_cn"] = labels[source_state]
        elif source_state == "BROAD_EXPANSION":
            streak = _source_family_streak(frame, index, "CAPITAL_EXPANSION")
            payload["persistence_days"] = streak
            payload["temporal_state"] = (
                "RISK_APPETITE_PERSISTENT" if streak >= 2
                else "RISK_APPETITE_EXPANDING")
            payload["temporal_state_cn"] = (
                "风险资本广泛扩散延续" if streak >= 2
                else "风险资本开始广泛扩散")
    return payload


def _event_family(state):
    if state in EVENT_RISK_OFF:
        return "RISK_OFF"
    if state in EVENT_RISK_ON:
        return "RISK_ON"
    return state or "UNAVAILABLE"


def _event_payload(spec, frame, index, observation_mode):
    row = frame.iloc[index]
    state = row["source_state"]
    quality = row["quality_status"]
    current_family = _event_family(state)
    prior_state = frame.iloc[index - 1]["source_state"] if index else None
    persistence = 0
    for position in range(index, -1, -1):
        if _event_family(frame.iloc[position]["source_state"]) != current_family:
            break
        persistence += 1

    start_5 = max(0, index - 4)
    start_20 = max(0, index - 19)
    states_5 = frame.loc[start_5:index, "source_state"].tolist()
    states_20 = frame.loc[start_20:index, "source_state"].tolist()
    details = dict(row["details"])
    details.update({
        "risk_off_days_5d": sum(item in EVENT_RISK_OFF for item in states_5),
        "risk_on_days_5d": sum(item in EVENT_RISK_ON for item in states_5),
        "confirmed_days_20d": sum(item in {
            "RISK_OFF_CONTAGION", "RISK_ON_DIFFUSION",
        } for item in states_20),
        "isolated_event_days_20d": sum(str(item).startswith(
            "ISOLATED_EVENT") for item in states_20),
        "native_updates_5d": _native_updates(frame, index),
    })

    if quality == "INSUFFICIENT":
        code, label, persistence = "INSUFFICIENT_DATA", "数据不足", 0
    elif state == "ISOLATED_EVENT_CRASH":
        code, label = "ISOLATED_EVENT", "孤立下跌事件脉冲"
    elif state == "ISOLATED_EVENT_SPIKE":
        code, label = "ISOLATED_EVENT", "孤立上涨事件脉冲"
    elif state in EVENT_RISK_OFF:
        code = "PRESSURE_PERSISTENT" if persistence >= 2 else "NEW_PRESSURE"
        label = "风险撤退延续" if persistence >= 2 else "新发风险撤退"
    elif state in EVENT_RISK_ON:
        code = "RISK_APPETITE_PERSISTENT" if persistence >= 2 else "RISK_APPETITE_EXPANDING"
        label = "风险偏好扩散延续" if persistence >= 2 else "风险偏好开始扩散"
    elif state == "TWO_WAY_SPECULATION":
        code, label = "OVERHEAT", "双向高波投机"
    elif state in {"THEME_RISK_ON", "THEME_RISK_OFF", "MIXED"}:
        code, label = "MIXED", "主题或方向分化"
    elif state == "QUIET" and any(item in EVENT_RISK_OFF for item in states_5[:-1]):
        code, label = "RELIEF", "事件压力暂时缓解"
    else:
        code, label = "STABLE", "未形成连续市场脉冲"

    transition = None
    if prior_state and state and prior_state != state:
        transition = f"{prior_state} -> {state}"
    return {
        "report_date": row["report_date"],
        "panel": spec.key,
        "level_value": _finite(row["level_value"]),
        "risk_value": None,
        "change_1d": None,
        "change_5d": None,
        "change_21d": None,
        "persistence_days": int(persistence),
        "temporal_state": code,
        "temporal_state_cn": label,
        "transition_type": transition,
        "state_changed": bool(transition),
        "coverage": round(float(row["coverage"]), 4),
        "confidence": round(float(row["confidence"]), 4),
        "independent_confirmation_count": 0,
        "quality_status": quality,
        "source_date": row["source_date"],
        "source_state": state or None,
        "observation_mode": observation_mode,
        "details": details,
        "shadow_mode": SHADOW_MODE,
        "calc_version": CALC_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _apply_turn_confirmation(rows):
    by_date = {}
    for row in rows:
        by_date.setdefault(row["report_date"], []).append(row)
    for row in rows:
        details = row.get("details") or {}
        if not details.get("turn_candidate"):
            continue
        direction = details.get("turn_direction")
        confirmations = 0
        for other in by_date.get(row["report_date"], []):
            if other["panel"] == row["panel"] or other.get("risk_value") is None:
                continue
            row_spec = PANEL_SPECS.get(row["panel"])
            other_spec = PANEL_SPECS.get(other["panel"])
            if (row_spec and other_spec and
                    row_spec.confirmation_group == other_spec.confirmation_group):
                continue
            change = _finite(other.get("change_5d"))
            if change is None or abs(change) < IMPULSE_THRESHOLD:
                continue
            if (change > 0 and direction > 0) or (change < 0 and direction < 0):
                confirmations += 1
        row["independent_confirmation_count"] = confirmations
        if confirmations:
            row["temporal_state"] = "SUDDEN_TURN"
            row["temporal_state_cn"] = (
                "突然转折：压力上升" if direction > 0
                else "突然转折：压力缓解")
        else:
            row["temporal_state"] = "TURN_WATCH"
            row["temporal_state_cn"] = (
                "转折观察：压力上升但未获跨板块确认" if direction > 0
                else "转折观察：压力缓解但未获跨板块确认")
    return rows


def build_temporal_history(histories, observation_mode="REPLAY"):
    """Build no-look-ahead temporal rows from existing daily panel histories."""
    rows = []
    for key, spec in PANEL_SPECS.items():
        frame = _normalize_history(histories.get(key), spec)
        if frame.empty:
            continue
        for index in range(len(frame)):
            if key == "event_pulse":
                payload = _event_payload(spec, frame, index, observation_mode)
            else:
                payload = _base_payload(spec, frame, index, observation_mode)
                if (key == "risk_capital" and
                        payload.get("source_state") == "SPECULATIVE_BLOWOFF" and
                        payload.get("quality_status") != "INSUFFICIENT"):
                    payload["temporal_state"] = "OVERHEAT"
                    payload["temporal_state_cn"] = "投机层过热但机构层未确认"
            rows.append(payload)
    return _apply_turn_confirmation(rows)


def _fetch_table_rows(supabase, table, date_col, start_date, end_date,
                      page_size=1000):
    rows, offset = [], 0
    while True:
        response = (supabase.table(table).select("*")
                    .gte(date_col, start_date).lte(date_col, end_date)
                    .order(date_col)
                    .range(offset, offset + page_size - 1).execute())
        batch = list(response.data or [])
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def fetch_panel_histories(supabase, start_date, end_date):
    histories = {}
    for key, spec in PANEL_SPECS.items():
        try:
            histories[key] = _fetch_table_rows(
                supabase, spec.table, spec.date_col, start_date, end_date)
        except Exception:
            logger.warning(
                "时间状态读取失败: table=%s, panel=%s", spec.table, key,
                exc_info=True)
            histories[key] = []
    return histories


def _price_frame(rows, ticker):
    prefix = ticker.lower()
    records = []
    for row in rows:
        report_date = _date_string(row.get("date"))
        close = _finite(row.get(f"{prefix}_close_price"))
        if not report_date or close is None:
            continue
        records.append({
            "report_date": report_date,
            "close": close,
            "open": _finite(row.get(f"{prefix}_open_price")),
            "previous_close": _finite(row.get(f"{prefix}_previous_close")),
        })
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records).drop_duplicates("report_date", keep="last")
    frame["_date"] = pd.to_datetime(frame["report_date"])
    return frame.sort_values("_date").reset_index(drop=True)


def fetch_benchmark_prices(supabase, start_date, end_date):
    try:
        rows = _fetch_table_rows(
            supabase, "macro_spot_daily", "date", start_date, end_date)
    except Exception:
        logger.warning("时间状态基准价格读取 macro_spot_daily 失败", exc_info=True)
        rows = []
    frames = {ticker: _price_frame(rows, ticker) for ticker in ("QQQ", "SPY")}

    # Older rows may predate the open/close acceptance columns.  A close-only
    # fallback is valid for return/drawdown evaluation but not for gap metrics.
    try:
        fallback = _fetch_table_rows(
            supabase, "stock_spot_post_close", "date", start_date, end_date)
    except Exception:
        logger.warning("时间状态基准价格读取 stock_spot_post_close 失败", exc_info=True)
        fallback = []
    for ticker in frames:
        close_rows = []
        for row in fallback:
            if str(row.get("ticker") or "").upper() != ticker:
                continue
            report_date = _date_string(row.get("date"))
            close = _finite(row.get("current_price") or row.get("close_price"))
            if report_date and close is not None:
                close_rows.append({
                    "report_date": report_date, "close": close,
                    "open": None, "previous_close": None,
                })
        if close_rows:
            fallback_frame = pd.DataFrame(close_rows)
            fallback_frame["_date"] = pd.to_datetime(
                fallback_frame["report_date"])
            if frames[ticker].empty:
                frames[ticker] = fallback_frame.sort_values(
                    "_date").reset_index(drop=True)
            else:
                merged = fallback_frame.merge(
                    frames[ticker].drop(columns=["_date"]),
                    on="report_date", how="outer", suffixes=("_fallback", ""))
                for column in ("close", "open", "previous_close"):
                    fallback_col = f"{column}_fallback"
                    merged[column] = merged[column].combine_first(
                        merged[fallback_col])
                merged["_date"] = pd.to_datetime(merged["report_date"])
                frames[ticker] = merged[[
                    "report_date", "close", "open", "previous_close", "_date",
                ]].sort_values("_date").reset_index(drop=True)
        if not frames[ticker].empty:
            derived_prior = frames[ticker]["close"].shift(1)
            frames[ticker]["previous_close"] = frames[ticker][
                "previous_close"].combine_first(derived_prior)
    return frames


def evaluate_temporal_states(temporal_rows, benchmark_frames,
                             horizons=HORIZONS):
    """Evaluate only signals whose full forward trading window already exists."""
    evaluations = []
    for ticker, source in benchmark_frames.items():
        if source is None or source.empty:
            continue
        frame = source.copy().reset_index(drop=True)
        positions = {str(date): index for index, date in enumerate(
            frame["report_date"].tolist())}
        for signal in temporal_rows:
            if signal.get("quality_status") == "INSUFFICIENT":
                continue
            signal_date = str(signal.get("report_date"))
            start = positions.get(signal_date)
            if start is None:
                continue
            start_close = _finite(frame.at[start, "close"])
            if start_close is None or start_close <= 0:
                continue
            for horizon in horizons:
                end = start + int(horizon)
                if end >= len(frame):
                    continue
                path = pd.to_numeric(
                    frame.loc[start:end, "close"], errors="coerce")
                if path.isna().any() or len(path) != horizon + 1:
                    continue
                end_close = float(path.iloc[-1])
                forward_return = (end_close / start_close - 1.0) * 100.0
                running_peak = path.cummax()
                max_drawdown = float((path / running_peak - 1.0).min() * 100.0)
                returns = path.pct_change().dropna()
                realized_vol = None
                if len(returns) >= 2:
                    realized_vol = float(
                        returns.std(ddof=1) * math.sqrt(252.0) * 100.0)

                gaps = []
                for position in range(start + 1, end + 1):
                    open_price = _finite(frame.at[position, "open"])
                    previous_close = _finite(
                        frame.at[position, "previous_close"])
                    if (open_price is not None and previous_close is not None
                            and previous_close > 0):
                        gaps.append((open_price / previous_close - 1.0) * 100.0)
                max_abs_gap = max((abs(value) for value in gaps), default=None)
                evaluations.append({
                    "signal_date": signal_date,
                    "panel": signal.get("panel"),
                    "benchmark": ticker,
                    "horizon_days": int(horizon),
                    "temporal_state": signal.get("temporal_state"),
                    "observation_mode": signal.get("observation_mode", "REPLAY"),
                    "risk_value": _finite(signal.get("risk_value")),
                    "benchmark_close": round(start_close, 6),
                    "outcome_end_date": str(frame.at[end, "report_date"]),
                    "forward_return_pct": round(forward_return, 6),
                    "max_drawdown_pct": round(max_drawdown, 6),
                    "realized_vol_pct": (
                        round(realized_vol, 6)
                        if realized_vol is not None and math.isfinite(realized_vol)
                        else None),
                    "max_abs_gap_pct": (
                        round(max_abs_gap, 6) if max_abs_gap is not None else None),
                    "downside_event": bool(forward_return < 0),
                    "signal_calc_version": signal.get("calc_version") or CALC_VERSION,
                    "eval_calc_version": EVAL_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
    return evaluations


def summarize_evaluations(temporal_rows, evaluations):
    progress = {}
    for key, spec in PANEL_SPECS.items():
        panel_rows = [row for row in temporal_rows if row.get("panel") == key]
        live_dates = {row.get("report_date") for row in panel_rows
                      if row.get("observation_mode") == "LIVE_SHADOW"}
        replay_dates = {row.get("report_date") for row in panel_rows
                        if row.get("observation_mode") == "REPLAY"}
        mature_21 = {
            row.get("signal_date") for row in evaluations
            if row.get("panel") == key and row.get("benchmark") == "QQQ"
            and int(row.get("horizon_days") or 0) == 21
        }
        progress[key] = {
            "label": spec.label,
            "live_days": len(live_dates),
            "replay_days": len(replay_dates),
            "mature_qqq_21d": len(mature_21),
            "stage_review_ready": len(live_dates) >= MIN_LIVE_REVIEW_DAYS,
            "full_cycle_ready": len(live_dates) >= TARGET_LIVE_REVIEW_DAYS,
        }

    publishable = []
    frame = pd.DataFrame(evaluations)
    if not frame.empty:
        live = frame[frame["observation_mode"] == "LIVE_SHADOW"]
        for keys, group in live.groupby(
                ["panel", "benchmark", "horizon_days", "temporal_state"]):
            if len(group) < MIN_STATE_OUTCOMES:
                continue
            panel, benchmark, horizon, state = keys
            if not progress.get(panel, {}).get("stage_review_ready"):
                continue
            publishable.append({
                "panel": panel,
                "benchmark": benchmark,
                "horizon_days": int(horizon),
                "temporal_state": state,
                "sample_count": len(group),
                "mean_forward_return_pct": round(
                    float(group["forward_return_pct"].mean()), 4),
                "downside_rate": round(
                    float(group["downside_event"].mean()), 4),
                "mean_max_drawdown_pct": round(
                    float(group["max_drawdown_pct"].mean()), 4),
                "mean_realized_vol_pct": (
                    round(float(group["realized_vol_pct"].dropna().mean()), 4)
                    if group["realized_vol_pct"].notna().any() else None),
            })
    return {"progress": progress, "publishable": publishable}


def _persist_chunks(supabase, table, rows, conflict_cols, chunk_size=500):
    if not rows:
        return True, 0
    from market_utils import safe_upsert

    written = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        if safe_upsert(
                supabase, table, chunk, conflict_cols=conflict_cols) is None:
            return False, written
        written += len(chunk)
    return True, written


def _fetch_persisted_temporal(supabase, start_date, end_date):
    try:
        return _fetch_table_rows(
            supabase, "temporal_state_daily", "report_date",
            start_date, end_date)
    except Exception:
        return []


def run_temporal_pipeline(supabase, report_date, lookback_days=180,
                          persist=True):
    """Compute current temporal states and refresh matured shadow outcomes."""
    from market_utils import log_data_quality, trading_days_back

    start_date = trading_days_back(lookback_days, end_date=report_date)
    histories = fetch_panel_histories(supabase, start_date, report_date)
    history_rows = build_temporal_history(histories, observation_mode="REPLAY")
    current = [dict(row) for row in history_rows
               if row.get("report_date") == str(report_date)]
    present = {row["panel"] for row in current}
    for key, spec in PANEL_SPECS.items():
        if key in present:
            continue
        current.append({
            "report_date": str(report_date), "panel": key,
            "level_value": None, "risk_value": None,
            "change_1d": None, "change_5d": None, "change_21d": None,
            "persistence_days": 0, "temporal_state": "INSUFFICIENT_DATA",
            "temporal_state_cn": "数据不足", "transition_type": None,
            "state_changed": False, "coverage": 0.0, "confidence": 0.0,
            "independent_confirmation_count": 0,
            "quality_status": "INSUFFICIENT", "source_date": None,
            "source_state": None, "observation_mode": "LIVE_SHADOW",
            "details": {"level_label": spec.level_label,
                        "missing_source_row": True},
            "shadow_mode": True, "calc_version": CALC_VERSION,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })
    for row in current:
        row["observation_mode"] = "LIVE_SHADOW"

    state_write_ok, state_rows_written = True, 0
    if persist:
        state_write_ok, state_rows_written = _persist_chunks(
            supabase, "temporal_state_daily", current,
            "report_date,panel")

    persisted = _fetch_persisted_temporal(
        supabase, start_date, report_date) if persist else []
    if not persisted:
        persisted = current
    price_frames = fetch_benchmark_prices(supabase, start_date, report_date)
    evaluations = evaluate_temporal_states(persisted, price_frames)
    eval_write_ok, eval_rows_written = True, 0
    if persist:
        eval_write_ok, eval_rows_written = _persist_chunks(
            supabase, "temporal_shadow_evaluation", evaluations,
            "signal_date,panel,benchmark,horizon_days")

    summary = summarize_evaluations(persisted, evaluations)
    status = ("ok" if state_write_ok and eval_write_ok else
              ("partial" if current else "failed"))
    if persist:
        log_data_quality(
            supabase, job_name="auto_analyst", table_name="temporal_state_daily",
            status=status, rows_written=state_rows_written + eval_rows_written,
            missing_fields=[row["panel"] for row in current
                            if row["quality_status"] == "INSUFFICIENT"] or None,
            notes=(f"state_rows={state_rows_written}; eval_rows={eval_rows_written}; "
                   f"calc={CALC_VERSION}; shadow=true"),
        )
    return {
        "report_date": str(report_date),
        "current": sorted(current, key=lambda row: list(PANEL_SPECS).index(
            row["panel"])),
        "history": history_rows,
        "evaluations": evaluations,
        "evaluation_summary": summary,
        "persistence_status": status if persist else "skipped",
        "calc_version": CALC_VERSION,
        "shadow_mode": True,
    }


def backfill_temporal_pipeline(supabase, start_date, end_date, persist=False,
                               lookback_buffer=30):
    """Replay temporal states without labeling them as prospective evidence."""
    try:
        from market_utils import trading_days_back
        fetch_start = trading_days_back(lookback_buffer, end_date=start_date)
    except Exception:
        # The buffer is only for prior observations.  A conservative calendar
        # fallback cannot introduce look-ahead and keeps offline replay usable.
        fetch_start = (pd.Timestamp(start_date) - pd.Timedelta(
            days=int(lookback_buffer * 1.7) + 10)).strftime("%Y-%m-%d")
    histories = fetch_panel_histories(supabase, fetch_start, end_date)
    all_rows = build_temporal_history(histories, observation_mode="REPLAY")
    rows = [row for row in all_rows
            if str(start_date) <= row["report_date"] <= str(end_date)]
    prices = fetch_benchmark_prices(supabase, fetch_start, end_date)
    evaluations = evaluate_temporal_states(rows, prices)
    state_ok = eval_ok = True
    protected_live_count = 0
    if persist:
        existing = _fetch_persisted_temporal(supabase, start_date, end_date)
        protected = {
            (str(row.get("report_date")), row.get("panel"))
            for row in existing
            if row.get("observation_mode") == "LIVE_SHADOW"
        }
        protected_live_count = len(protected)
        rows = [row for row in rows
                if (row["report_date"], row["panel"]) not in protected]
        evaluations = evaluate_temporal_states(rows, prices)
        state_ok, _ = _persist_chunks(
            supabase, "temporal_state_daily", rows, "report_date,panel")
        eval_ok, _ = _persist_chunks(
            supabase, "temporal_shadow_evaluation", evaluations,
            "signal_date,panel,benchmark,horizon_days")
    return {
        "states": rows,
        "evaluations": evaluations,
        "state_write_ok": state_ok,
        "evaluation_write_ok": eval_ok,
        "observation_mode": "REPLAY",
        "protected_live_count": protected_live_count,
    }


def _fmt(value, digits=1):
    number = _finite(value)
    return "数据不足" if number is None else f"{number:.{digits}f}"


def _risk_change_text(value, window):
    number = _finite(value)
    if number is None:
        return f"{window}日变化 数据不足"
    direction = "恶化" if number > 0 else ("改善" if number < 0 else "持平")
    return f"{window}日风险变化 {number:+.1f}（{direction}）"


def format_temporal_summary(result):
    result = result or {}
    current = {row.get("panel"): row for row in result.get("current") or []}
    lines = [
        "=== 核心板块时间状态（影子观察，不触发预警） ===",
        "统一口径：风险标准化后数值越高表示压力越大；曲线和状态不构成交易信号。",
    ]
    for key, spec in PANEL_SPECS.items():
        row = current.get(key) or {}
        if not row or row.get("quality_status") == "INSUFFICIENT":
            lines.append(
                f"- {spec.label}：数据不足 | 覆盖 {float(row.get('coverage') or 0):.0%} | "
                f"状态：{row.get('temporal_state_cn') or '数据不足'}")
            continue
        details = row.get("details") or {}
        quality = (
            f"覆盖 {float(row.get('coverage') or 0):.0%}/"
            f"置信 {float(row.get('confidence') or 0):.0%}")
        if key == "event_pulse":
            lines.append(
                f"- {spec.label}：候选 {int(row.get('level_value') or 0)} | "
                f"5日风险撤退 {details.get('risk_off_days_5d', 0)}日 / "
                f"风险扩散 {details.get('risk_on_days_5d', 0)}日 | "
                f"状态：{row.get('temporal_state_cn')} | 连续 {row.get('persistence_days', 0)}日 | "
                f"{quality}")
            continue
        level = _fmt(row.get("level_value"))
        lines.append(
            f"- {spec.label}：{spec.level_label} {level}/100 | "
            f"{_risk_change_text(row.get('change_5d'), 5)} | "
            f"状态：{row.get('temporal_state_cn')} | 连续 {row.get('persistence_days', 0)}日 | "
            f"{quality}")

    summary = result.get("evaluation_summary") or {}
    progress = summary.get("progress") or {}
    lines.append("影子验证进度（历史回放不等同于实盘观察）：")
    for key, spec in PANEL_SPECS.items():
        item = progress.get(key) or {}
        lines.append(
            f"- {spec.label}：实盘影子 {item.get('live_days', 0)}/{MIN_LIVE_REVIEW_DAYS}日 | "
            f"历史回放 {item.get('replay_days', 0)}日 | "
            f"QQQ未来21日成熟结果 {item.get('mature_qqq_21d', 0)}条")
    publishable = summary.get("publishable") or []
    qqq_21d = [item for item in publishable
               if item.get("benchmark") == "QQQ"
               and item.get("horizon_days") == 21]
    if qqq_21d:
        lines.append("已达阶段门槛的描述统计（仅表示关联，不代表预测或因果）：")
        for item in qqq_21d[:5]:
            spec = PANEL_SPECS.get(item.get("panel"))
            label = spec.label if spec else str(item.get("panel"))
            vol = item.get("mean_realized_vol_pct")
            vol_text = "NA" if vol is None else f"{vol:.1f}%"
            lines.append(
                f"- {label}/{item.get('temporal_state')}：n={item.get('sample_count')} | "
                f"QQQ后21日均值 {item.get('mean_forward_return_pct'):+.2f}% | "
                f"下跌率 {item.get('downside_rate'):.0%} | "
                f"平均最大回撤 {item.get('mean_max_drawdown_pct'):.2f}% | "
                f"实现波动 {vol_text}")
    lines.extend([
        f"阶段门槛：满{MIN_LIVE_REVIEW_DAYS}个交易日才做首次审阅，"
        f"满{TARGET_LIVE_REVIEW_DAYS}日才考虑校准；当前不接入告警或仓位。",
        f"计算版本：{result.get('calc_version') or CALC_VERSION} | "
        f"落库状态：{result.get('persistence_status') or '未知'}",
    ])
    return "\n".join(lines)
