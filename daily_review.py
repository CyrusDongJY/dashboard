# -*- coding: utf-8 -*-
"""Deterministic end-of-day review states and compact email rendering."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import math

RULE_VERSION = "daily_review_v1.0-shadow"

DISPLAY_LABELS = {
    "quality": {
        "HIGH": "高",
        "MEDIUM": "中",
        "LOW": "低",
    },
    "cash": {
        "Accepted": "价格已获接受",
        "Rejected": "价格未获接受",
        "Partial": "部分接受",
        "UNAVAILABLE": "数据不可用",
    },
    "breadth": {
        "Broad": "广泛扩散",
        "Deterioration": "广度恶化",
        "Rotation": "轮动",
        "Concentrated": "集中",
        "UNAVAILABLE": "数据不可用",
    },
    "volatility": {
        "Stress": "承压",
        "Suppressed": "低波压制",
        "Normal": "常态",
        "UNAVAILABLE": "数据不可用",
    },
    "credit": {
        "Stress": "承压",
        "Warning": "预警",
        "Stable": "稳定",
        "UNAVAILABLE": "数据不可用",
    },
    "rates": {
        "Headwind": "逆风",
        "Tailwind": "顺风",
        "Neutral": "中性",
        "UNAVAILABLE": "数据不可用",
    },
    "liquidity": {
        "Inflow": "扩张",
        "Drain": "收缩",
        "Flat": "平稳",
        "UNAVAILABLE": "数据不可用",
    },
    "risk": {
        "Risk-off": "风险规避",
        "UNAVAILABLE": "数据不可用",
    },
    "vix_curve": {
        "BACKWARDATION": "倒挂",
        "CONTANGO": "正向升水",
        "FLAT": "平坦",
        "MISSING": "数据缺失",
    },
}


def _display_label(category, value):
    """Translate report labels without changing persisted rule enums."""
    label = str(value) if value is not None else "UNAVAILABLE"
    return DISPLAY_LABELS.get(category, {}).get(label, label)


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
        except (TypeError, ValueError):
            return {}
    return {}


def _date_value(row, key):
    value = (row or {}).get(key)
    return str(value)[:10] if value else None


def _module(state, quality, evidence, *, decision_eligible=None, values=None):
    return {
        "state": state,
        "quality": quality,
        "decision_eligible": (
            quality == "HIGH" if decision_eligible is None else bool(decision_eligible)
        ),
        "evidence": [str(item) for item in evidence if item],
        "values": values or {},
    }


def _fmt(value, digits=1, suffix=""):
    number = _finite(value)
    return "NA" if number is None else f"{number:.{digits}f}{suffix}"


def _fetch_rows(supabase, table, date_col, report_date, limit=2):
    try:
        response = (
            supabase.table(table).select("*")
            .lte(date_col, report_date)
            .order(date_col, desc=True).limit(limit).execute()
        )
        return list(response.data or [])
    except Exception:
        return []


def fetch_review_inputs(supabase, report_date):
    """Fetch the final persisted rows used by the review engine."""
    market_rows = _fetch_rows(
        supabase, "market_history", "record_date", report_date, limit=2)
    spot_rows = _fetch_rows(
        supabase, "macro_spot_daily", "date", report_date, limit=2)
    prior_review_rows = _fetch_rows(
        supabase, "daily_review", "report_date", report_date, limit=2)

    try:
        quality_response = (
            supabase.table("data_quality").select("*")
            .eq("run_date", report_date).execute()
        )
        quality_rows = list(quality_response.data or [])
    except Exception:
        quality_rows = []

    try:
        metric_response = (
            supabase.table("metric_daily")
            .select("report_date,metric,scope,value,percentile,zscore,source_date,lag_days")
            .eq("report_date", report_date).eq("session", "EOD").execute()
        )
        metric_rows = list(metric_response.data or [])
    except Exception:
        metric_rows = []

    return {
        "market_row": market_rows[0] if market_rows else {},
        "prior_market_row": market_rows[1] if len(market_rows) > 1 else {},
        "spot_row": spot_rows[0] if spot_rows else {},
        "prior_spot_row": spot_rows[1] if len(spot_rows) > 1 else {},
        "prior_review": (
            (prior_review_rows[1] if len(prior_review_rows) > 1 else {})
            if prior_review_rows
            and str(prior_review_rows[0].get("report_date")) == report_date
            else (prior_review_rows[0] if prior_review_rows else {})
        ),
        "quality_rows": quality_rows,
        "metric_rows": metric_rows,
    }


def _structure_module(market, prior_review, fresh):
    p20 = _finite(market.get("pct_20ma"))
    p50 = _finite(market.get("pct_50ma"))
    p200 = _finite(market.get("pct_200ma"))
    available = sum(value is not None for value in (p20, p50, p200))
    quality = "HIGH" if fresh and available == 3 else (
        "MEDIUM" if fresh and p200 is not None and available >= 2 else "LOW")
    if quality == "LOW":
        return _module("UNAVAILABLE", quality, ["20/50/200MA广度不足"])

    previous = (prior_review or {}).get("structure_state")
    if previous == "结构牛市" and p200 >= 60 and (p50 is None or p50 >= 50):
        state = "结构牛市"
    elif previous == "结构转弱" and p200 <= 58:
        state = "结构转弱"
    elif p200 > 65 and (p50 is None or p50 >= 55):
        state = "结构牛市"
    elif p200 < 55:
        state = "结构转弱"
    else:
        state = "中性震荡"
    evidence = [
        f"20/50/200MA广度={_fmt(p20)}%/{_fmt(p50)}%/{_fmt(p200)}%",
        f"净新高-新低={_fmt(market.get('net_nh_nl'), 0)}",
    ]
    return _module(state, quality, evidence, values={
        "pct_20ma": p20, "pct_50ma": p50, "pct_200ma": p200,
        "net_nh_nl": _finite(market.get("net_nh_nl")),
    })


def _cash_symbol(spot, symbol):
    prefix = symbol.lower()
    time_pct = _finite(spot.get(f"{prefix}_vwap_time_acceptance_pct"))
    volume_pct = _finite(spot.get(f"{prefix}_vwap_volume_acceptance_pct"))
    gap_ratio = _finite(spot.get(f"{prefix}_gap_acceptance"))
    o2c = _finite(spot.get(f"{prefix}_open_to_close_pct"))
    close_vs_vwap = _finite(spot.get(f"{prefix}_close_vs_vwap_pct"))
    vwap_quality = str(spot.get(f"{prefix}_vwap_quality") or "MISSING")
    gap_quality = str(spot.get(f"{prefix}_gap_quality") or "MISSING")
    eligible = (
        time_pct is not None and volume_pct is not None
        and vwap_quality in {"OK", "PARTIAL_COVERAGE"}
    )
    if not eligible:
        state = "UNAVAILABLE"
    else:
        acceptance = (time_pct + volume_pct) / 2.0
        rejected = (
            acceptance < 40 or (gap_quality == "OK" and gap_ratio is not None
                                and gap_ratio < 0)
            or (close_vs_vwap is not None and close_vs_vwap < 0 and acceptance < 50)
        )
        accepted = (
            acceptance > 60
            and not (gap_quality == "OK" and gap_ratio is not None and gap_ratio < 0)
            and (close_vs_vwap is None or close_vs_vwap >= 0)
        )
        state = "Accepted" if accepted else ("Rejected" if rejected else "Partial")
    return {
        "state": state,
        "eligible": eligible,
        "vwap_quality": vwap_quality,
        "gap_quality": gap_quality,
        "time_pct": time_pct,
        "volume_pct": volume_pct,
        "gap_ratio": gap_ratio,
        "open_to_close_pct": o2c,
        "close_vs_vwap_pct": close_vs_vwap,
        "last_vwap": _finite(spot.get(f"{prefix}_last_vwap")),
    }


def _cash_module(spot, fresh):
    symbols = {symbol: _cash_symbol(spot, symbol) for symbol in ("SPY", "QQQ")}
    eligible_count = sum(item["eligible"] for item in symbols.values())
    full_quality = all(
        item["vwap_quality"] == "OK"
        and item["gap_quality"] in {"OK", "SMALL_GAP"}
        for item in symbols.values()
    )
    quality = "HIGH" if fresh and eligible_count == 2 and full_quality else (
        "MEDIUM" if fresh and eligible_count >= 1 else "LOW")
    states = [item["state"] for item in symbols.values() if item["eligible"]]
    if quality == "LOW":
        state = "UNAVAILABLE"
    elif quality != "HIGH":
        state = "Partial"
    elif states and all(value == "Accepted" for value in states):
        state = "Accepted"
    elif states and all(value == "Rejected" for value in states):
        state = "Rejected"
    elif "Rejected" in states and "Accepted" not in states:
        state = "Rejected"
    else:
        state = "Partial"
    evidence = []
    for symbol, item in symbols.items():
        evidence.append(
            f"{symbol} O→C {_fmt(item['open_to_close_pct'], 2, '%')} | "
            f"VWAP时间/成交量 {_fmt(item['time_pct'])}%/{_fmt(item['volume_pct'])}% | "
            f"收盘-VWAP {_fmt(item['close_vs_vwap_pct'], 2, '%')} | "
            f"{_display_label('cash', item['state'])}"
        )
    return _module(
        state, quality, evidence, decision_eligible=quality == "HIGH",
        values=symbols)


def _breadth_module(market, fresh):
    pct_adv = _finite(market.get("pct_adv"))
    trin = _finite(market.get("trin"))
    ud = _finite(market.get("up_down_volume_ratio"))
    spreads = {
        "QQQ-QQQE": _finite(market.get("qqq_qqqe_spread_pct")),
        "SPY-RSP": _finite(market.get("spy_rsp_spread_pct")),
        "Mag7-RSP": _finite(market.get("mag7_rsp_spread_pct")),
    }
    concentration_quality = str(market.get("concentration_quality") or "MISSING")
    quality = "HIGH" if (
        fresh and pct_adv is not None and trin is not None
        and concentration_quality == "OK"
    ) else ("MEDIUM" if fresh and pct_adv is not None else "LOW")
    usable_spreads = [value for value in spreads.values() if value is not None]
    max_spread = max(usable_spreads) if usable_spreads else None
    if quality == "LOW":
        state = "UNAVAILABLE"
    elif pct_adv < 40 and (trin is None or trin > 1.2) and (ud is None or ud < 0.8):
        state = "Deterioration"
    elif max_spread is not None and max_spread >= 0.75 and pct_adv < 60:
        state = "Concentrated"
    elif pct_adv >= 60 and (max_spread is None or max_spread < 0.75) and (trin is None or trin <= 1.2):
        state = "Broad"
    else:
        state = "Rotation"
    evidence = [
        f"上涨家数={_fmt(pct_adv)}% | Top500 TRIN={_fmt(trin, 2)}",
        "集中度 " + " | ".join(f"{key}={_fmt(value, 2)}%" for key, value in spreads.items()),
        (f"Top500 U/D={_fmt(ud, 2)}" if ud is not None
         else "U/D=NA（不以中性值替代）"),
    ]
    return _module(
        state, quality, evidence, decision_eligible=quality == "HIGH",
        values={"pct_adv": pct_adv, "trin": trin, "up_down_volume_ratio": ud,
                **spreads})


def _volatility_module(market, spot, metric_rows, fresh):
    vix = _finite(market.get("vix"))
    vvix = _finite(market.get("vvix"))
    contango = _finite(spot.get("vix_contango_pct"))
    curve = str(spot.get("vix_curve_state") or "MISSING")
    quality = "HIGH" if fresh and vix is not None and curve != "MISSING" else (
        "MEDIUM" if fresh and (vix is not None or curve != "MISSING") else "LOW")
    if quality == "LOW":
        state = "UNAVAILABLE"
    elif curve == "BACKWARDATION" or (vix is not None and vix >= 30) or (vvix is not None and vvix >= 130):
        state = "Stress"
    elif curve == "CONTANGO" and vix is not None and vix < 18:
        state = "Suppressed"
    else:
        state = "Normal"

    gex = next((row for row in metric_rows
                if row.get("metric") == "gex_billions"), {})
    gex_context = {
        "value": _finite(gex.get("value")),
        "percentile": _finite(gex.get("percentile")),
        "zscore": _finite(gex.get("zscore")),
        "source_date": gex.get("source_date"),
    }
    evidence = [
        f"VIX={_fmt(vix, 2)} | VVIX={_fmt(vvix, 2)} | "
        f"VIX期货={_display_label('vix_curve', curve)} ({_fmt(contango, 2)}%)",
        (f"GEX={_fmt(gex_context['value'], 2)} | 分位={_fmt(gex_context['percentile'])}% | "
         f"Z={_fmt(gex_context['zscore'], 2)} | 仅作背景"),
    ]
    return _module(state, quality, evidence, decision_eligible=quality == "HIGH",
                   values={"vix": vix, "vvix": vvix, "contango_pct": contango,
                           "curve_state": curve, "gex": gex_context})


def _macro_modules(market, fresh):
    tnx_z = _finite(market.get("tnx_z"))
    credit_z = _finite(market.get("credit_z"))
    credit_spread = _finite(market.get("credit_spread"))
    liq_roc = _finite(market.get("liq_roc"))
    liq_window = market.get("liq_roc_window")

    rate_quality = "HIGH" if fresh and tnx_z is not None else "LOW"
    rate_state = "UNAVAILABLE" if tnx_z is None else (
        "Headwind" if tnx_z > 1 else ("Tailwind" if tnx_z < -1 else "Neutral"))
    rates = _module(rate_state, rate_quality,
                    [f"10Y 63日Z={_fmt(tnx_z, 2)}"], values={"tnx_z": tnx_z})

    credit_quality = "HIGH" if fresh and credit_z is not None else (
        "MEDIUM" if fresh and credit_spread is not None else "LOW")
    credit_state = "UNAVAILABLE" if credit_quality == "LOW" else (
        "Stress" if credit_z is not None and credit_z > 2 else
        ("Warning" if credit_z is not None and credit_z > 1 else "Stable"))
    credit = _module(
        credit_state, credit_quality,
        [f"HY OAS={_fmt(credit_spread, 2)} | 历史Z={_fmt(credit_z, 2)}"],
        decision_eligible=credit_quality == "HIGH",
        values={"credit_spread": credit_spread, "credit_z": credit_z})

    liquidity_quality = "HIGH" if fresh and liq_roc is not None and liq_window else "LOW"
    liquidity_state = "UNAVAILABLE" if liquidity_quality == "LOW" else (
        "Inflow" if liq_roc > 2 else ("Drain" if liq_roc < -2 else "Flat"))
    liquidity = _module(
        liquidity_state, liquidity_quality,
        [f"真实净流动性{liq_window or 'NA'}日ROC={_fmt(liq_roc, 2)}%"],
        values={"liq_roc": liq_roc, "window": liq_window})
    return rates, credit, liquidity


def _rank_anomalies(events):
    rows = []
    for event in events or []:
        if is_dataclass(event):
            row = asdict(event)
        elif isinstance(event, dict):
            row = dict(event)
        elif hasattr(event, "to_row"):
            row = dict(event.to_row())
        else:
            continue
        severity = int(_finite(row.get("severity")) or 0)
        confidence = _finite(row.get("confidence")) or 0.0
        lag_days = int(_finite(row.get("lag_days")) or 0)
        if severity <= 0:
            continue
        row["severity"] = severity
        row["confidence"] = confidence
        row["lag_days"] = lag_days
        rows.append(row)
    rows.sort(key=lambda row: (-row["severity"], -row["confidence"], row["lag_days"]))
    result = []
    seen = set()
    for row in rows:
        key = (row.get("metric"), row.get("scope"), row.get("resonance_key"))
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "metric": row.get("metric"), "scope": row.get("scope"),
            "severity": row["severity"], "confidence": row["confidence"],
            "lag_days": row["lag_days"], "explanation": row.get("explanation"),
            "window": row.get("window") or row.get("window_scope"),
        })
        if len(result) == 3:
            break
    return result


def _meaningful_changes(market, prior_market, spot, prior_spot):
    specs = [
        ("200MA广度", market, prior_market, "pct_200ma", 2.0, "%"),
        ("50MA广度", market, prior_market, "pct_50ma", 3.0, "%"),
        ("上涨家数", market, prior_market, "pct_adv", 8.0, "%"),
        ("QQQ-QQQE", market, prior_market, "qqq_qqqe_spread_pct", 0.4, "%"),
        ("SPY-RSP", market, prior_market, "spy_rsp_spread_pct", 0.4, "%"),
        ("VIX", market, prior_market, "vix", 1.5, ""),
        ("信用Z", market, prior_market, "credit_z", 0.4, ""),
        ("10Y Z", market, prior_market, "tnx_z", 0.4, ""),
        ("VIX期货升贴水", spot, prior_spot, "vix_contango_pct", 1.5, "%"),
    ]
    changes = []
    for label, current_row, prior_row, key, threshold, suffix in specs:
        current = _finite(current_row.get(key))
        previous = _finite(prior_row.get(key))
        if current is None or previous is None:
            continue
        delta = current - previous
        if abs(delta) >= threshold:
            changes.append((abs(delta) / threshold,
                            f"{label}: {_fmt(previous, 2)}{suffix} → {_fmt(current, 2)}{suffix} "
                            f"({delta:+.2f}{suffix})"))
    changes.sort(key=lambda item: item[0], reverse=True)
    return [text for _, text in changes[:5]]


def _gamma_eligible(row):
    explicit = (row or {}).get("gamma_decision_eligible")
    decision_eligible = explicit is True or str(explicit).lower() == "true"
    quality = _mapping((row or {}).get("gamma_quality"))
    return decision_eligible and quality.get("ALL") == "OK" and (row or {}).get("gamma_flip_quality") in {
        "OK", "VALID_NO_CROSS", "NO_CROSSING", "BASELINE_RESET", None,
    }


def _price_map(micro_rows, cash_values):
    indexed = {str(row.get("ticker")): row for row in (micro_rows or [])}
    result = {}
    for symbol in ("SPY", "QQQ"):
        row = indexed.get(symbol, {})
        gamma_ok = _gamma_eligible(row)
        cash = cash_values.get(symbol, {})
        result[symbol] = {
            "current_price": _finite(row.get("current_price")),
            "breakout_confirmation": _finite(row.get("call_wall")) if gamma_ok else None,
            "midpoint": cash.get("last_vwap"),
            "first_risk": _finite(row.get("put_wall")) if gamma_ok else None,
            "regime_change": None,
            "gamma_eligible": gamma_ok,
            "note": ("Gamma位通过质量闸门" if gamma_ok else
                     "Gamma质量不足；不生成精确突破/风险位"),
        }
    return result


def build_daily_review(report_date, *, market_row, spot_row,
                       prior_market_row=None, prior_spot_row=None,
                       prior_review=None, quality_rows=None, metric_rows=None,
                       anomaly_events=None, micro_rows=None):
    market_row = market_row or {}
    spot_row = spot_row or {}
    prior_market_row = prior_market_row or {}
    prior_spot_row = prior_spot_row or {}
    prior_review = prior_review or {}
    quality_rows = quality_rows or []
    metric_rows = metric_rows or []

    market_date = _date_value(market_row, "record_date")
    spot_date = _date_value(spot_row, "date")
    market_fresh = market_date == report_date
    spot_fresh = spot_date == report_date
    failed_jobs = sorted({
        str(row.get("job_name")) for row in quality_rows
        if str(row.get("status", "")).lower() == "failed"
    })

    structure = _structure_module(market_row, prior_review, market_fresh)
    cash = _cash_module(spot_row, spot_fresh)
    breadth = _breadth_module(market_row, market_fresh)
    volatility = _volatility_module(
        market_row, spot_row, metric_rows, market_fresh and spot_fresh)
    rates, credit, liquidity = _macro_modules(market_row, market_fresh)

    core_levels = [
        structure["quality"], cash["quality"], breadth["quality"],
        volatility["quality"],
    ]
    low_core = core_levels.count("LOW")
    if failed_jobs or not market_fresh or not spot_fresh or low_core >= 2:
        overall_quality = "LOW"
    elif all(level == "HIGH" for level in core_levels) and credit["quality"] != "LOW":
        overall_quality = "HIGH"
    else:
        overall_quality = "MEDIUM"

    cash_eligible = cash["decision_eligible"]
    breadth_eligible = breadth["decision_eligible"]
    qqq_return = _finite(spot_row.get("qqq_mom_pct"))
    if not cash_eligible:
        tactical = "战术状态不可用"
    elif breadth_eligible and cash["state"] == "Accepted" and breadth["state"] == "Broad":
        tactical = "趋势上涨"
    elif cash["state"] == "Rejected" and (
            breadth["state"] == "Deterioration" or (qqq_return is not None and qqq_return < 0)):
        tactical = "下跌扩散"
    elif breadth_eligible and breadth["state"] in {"Rotation", "Concentrated"}:
        tactical = "资金轮动"
    elif cash_eligible:
        tactical = "高位消化"
    else:
        tactical = "战术状态不可用"

    vol_state = volatility["state"] if volatility["decision_eligible"] else "UNAVAILABLE"
    credit_state = credit["state"] if credit["decision_eligible"] else "UNAVAILABLE"
    if vol_state == "Stress" and credit_state == "Stress":
        risk = "Risk-off"
    elif "Stress" in {vol_state, credit_state} or credit_state == "Warning":
        risk = "压力上升"
    elif vol_state == "Suppressed" and tactical not in {"趋势上涨", "战术状态不可用"}:
        risk = "脆弱低波"
    elif vol_state != "UNAVAILABLE" or credit_state != "UNAVAILABLE":
        risk = "低风险"
    else:
        risk = "风险状态不可用"

    structure_state = structure["state"]
    summary = f"{structure_state}｜{tactical}｜{_display_label('risk', risk)}"
    changes = _meaningful_changes(
        market_row, prior_market_row, spot_row, prior_spot_row)
    if not changes:
        changes = ["没有达到预设变化阈值的核心变量。"]
    anomalies = _rank_anomalies(anomaly_events)
    price_map = _price_map(micro_rows, cash["values"])

    scenarios = {
        "bull": [
            "SPY与QQQ现金市场均转为价格已获接受",
            "广度维持广泛扩散，或由集中改善为广泛扩散",
            "VIX期限结构不进入倒挂，信用保持稳定",
        ],
        "base": [
            f"当前基准：{summary}",
            "关键质量字段不降级，价格围绕有效VWAP中轴消化",
        ],
        "bear": [
            "SPY与QQQ动态VWAP连续未获价格接受",
            "上涨家数与Top500 TRIN共同确认广度恶化",
            "VIX结构转为承压；若信用同步承压则升级为环境切换",
        ],
    }
    exposure = {
        "mode": "SHADOW_CONTEXT_ONLY",
        "beta": ("降低" if risk in {"压力上升", "Risk-off"} else "保持"),
        "theta": ("停止" if risk == "Risk-off" else
                  ("减少" if risk in {"压力上升", "脆弱低波"} else "正常")),
        "convexity": ("增加" if risk in {"压力上升", "Risk-off"} else
                      ("正常" if risk == "脆弱低波" else "低")),
        "disclaimer": "仅为环境风险暴露观察，不构成交易指令。",
    }

    return {
        "report_date": report_date,
        "rule_version": RULE_VERSION,
        "data_quality": overall_quality,
        "quality_details": {
            "market_date": market_date, "spot_date": spot_date,
            "failed_jobs": failed_jobs,
            "module_quality": {
                "structure": structure["quality"], "cash": cash["quality"],
                "breadth": breadth["quality"], "volatility": volatility["quality"],
                "rates": rates["quality"], "credit": credit["quality"],
                "liquidity": liquidity["quality"],
            },
        },
        "structure_state": structure_state,
        "tactical_state": tactical,
        "risk_state": risk,
        "summary": summary,
        "modules": {
            "structure": structure, "cash": cash, "breadth": breadth,
            "volatility": volatility, "rates": rates, "credit": credit,
            "liquidity": liquidity,
        },
        "changes": changes,
        "anomalies": anomalies,
        "price_map": price_map,
        "scenarios": scenarios,
        "exposure_context": exposure,
        "input_dates": {"market_history": market_date, "macro_spot_daily": spot_date},
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _state_change(current, previous):
    if not previous:
        return "→"
    return "→" if str(current) == str(previous) else "变"


def format_daily_review_email(review, prior_review=None):
    prior_review = prior_review or {}
    modules = review.get("modules", {})
    quality = review.get("data_quality", "LOW")
    lines = [
        f"美股日度复盘 | {review.get('report_date')}",
        "=" * 52,
        "① 今日一句话结论",
        review.get("summary", "状态不可用"),
        f"数据质量：{_display_label('quality', quality)} | "
        f"规则版本：{review.get('rule_version')}",
        "",
        "② 状态总表",
        (f"市场结构：{review.get('structure_state')} "
         f"[{_state_change(review.get('structure_state'), prior_review.get('structure_state'))}]"),
        (f"现金市场：{_display_label('cash', modules.get('cash', {}).get('state'))} | "
         f"广度：{_display_label('breadth', modules.get('breadth', {}).get('state'))}"),
        (f"波动率：{_display_label('volatility', modules.get('volatility', {}).get('state'))} | "
         f"信用：{_display_label('credit', modules.get('credit', {}).get('state'))}"),
        (f"利率：{_display_label('rates', modules.get('rates', {}).get('state'))} | "
         f"流动性：{_display_label('liquidity', modules.get('liquidity', {}).get('state'))}"),
        "",
        "③ 较昨日的重要变化",
    ]
    lines.extend(f"- {item}" for item in review.get("changes", [])[:5])

    for number, key, title in (
        ("④", "cash", "现金市场"),
        ("⑤", "breadth", "广度与集中度"),
        ("⑥", "volatility", "波动率与Gamma/GEX背景"),
        ("⑦", "rates", "利率、信用与流动性"),
    ):
        lines.extend(["", f"{number} {title}"])
        if key == "rates":
            evidence = []
            for macro_key in ("rates", "credit", "liquidity"):
                evidence.extend(modules.get(macro_key, {}).get("evidence", []))
        else:
            evidence = modules.get(key, {}).get("evidence", [])
        lines.extend(f"- {item}" for item in evidence)

    lines.extend(["", "⑧ 最高优先级异常（最多3项）"])
    anomalies = review.get("anomalies", [])
    if anomalies:
        for item in anomalies:
            lines.append(
                f"- S{item.get('severity')} {item.get('metric')} | "
                f"置信度{_fmt((item.get('confidence') or 0) * 100)}% | "
                f"滞后{item.get('lag_days', 0)}日 | "
                f"{item.get('explanation') or '无附加解释'}"
            )
    else:
        lines.append("- 今日无达到展示门槛的客观异常。")

    lines.extend(["", "⑨ 价格地图"])
    for symbol, item in review.get("price_map", {}).items():
        lines.append(
            f"- {symbol}: 突破确认 {_fmt(item.get('breakout_confirmation'), 2)} | "
            f"VWAP中轴 {_fmt(item.get('midpoint'), 2)} | "
            f"第一风险 {_fmt(item.get('first_risk'), 2)} | {item.get('note')}"
        )

    lines.extend(["", "⑩ 三情景（只列触发条件）"])
    for key, label in (("bull", "乐观"), ("base", "基准"), ("bear", "悲观")):
        lines.append(f"{label}: " + "；".join(review.get("scenarios", {}).get(key, [])))

    exposure = review.get("exposure_context", {})
    lines.extend([
        "", "⑪ 风险暴露观察（影子）",
        (f"方向暴露={exposure.get('beta', '数据不可用')} | "
         f"时间价值暴露={exposure.get('theta', '数据不可用')} | "
         f"凸性暴露={exposure.get('convexity', '数据不可用')}"),
        exposure.get("disclaimer", ""),
        "", "数据归档：market_history / macro_spot_daily / "
        "stock_options_pre_market / stock_spot_post_close / anomaly_events / daily_review",
        (f"数据截止：market_history={review.get('input_dates', {}).get('market_history') or 'NA'} | "
         f"macro_spot_daily={review.get('input_dates', {}).get('macro_spot_daily') or 'NA'}"),
        "完整原始快照已入库，不在邮件正文重复展示。",
    ])
    return "\n".join(line for line in lines if line is not None)


def persist_daily_review(supabase, review):
    from market_utils import attach_metadata, log_data_quality, safe_upsert

    payload = {
        "report_date": review.get("report_date"),
        "rule_version": review.get("rule_version"),
        "data_quality": review.get("data_quality"),
        "structure_state": review.get("structure_state"),
        "tactical_state": review.get("tactical_state"),
        "risk_state": review.get("risk_state"),
        "summary": review.get("summary"),
        "quality_details": review.get("quality_details"),
        "modules": review.get("modules"),
        "changes": review.get("changes"),
        "anomalies": review.get("anomalies"),
        "price_map": review.get("price_map"),
        "scenarios": review.get("scenarios"),
        "exposure_context": review.get("exposure_context"),
        "input_dates": review.get("input_dates"),
        "computed_at": review.get("computed_at"),
    }
    attach_metadata(payload, source_date=review.get("report_date"))
    result = safe_upsert(
        supabase, "daily_review", payload, conflict_cols="report_date")
    log_data_quality(
        supabase, job_name="auto_analyst", table_name="daily_review",
        status=("failed" if result is None else
                ("partial" if review.get("data_quality") != "HIGH" else "ok")),
        rows_written=0 if result is None else 1,
        missing_fields=None,
        notes=f"rule={review.get('rule_version')}; quality={review.get('data_quality')}",
    )
    return result
