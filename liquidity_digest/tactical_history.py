"""Net score changes from original observations, separate from persistence."""
from datetime import date, timedelta

from tactical_stress import CALC_VERSION, COMPONENT_SPECS, StressInput, score_eod_stress
from temporal_monitor import IMPULSE_THRESHOLD
from .model import NY, finite, sessions, stamp, valid_rows


def verified_score(row):
    if row.get("stress_calc_version") != CALC_VERSION:
        return None
    components = row.get("stress_components")
    if not isinstance(components, dict):
        return None
    context = components.get("_context") or {}
    if not isinstance(context.get("spy_down"), bool):
        return None
    day = row["record_date"]
    inputs = {}
    for key, spec in COMPONENT_SPECS.items():
        item = components.get(key) or {}
        if item.get("weight") != spec.weight:
            return None
        if item.get("status") == "OK":
            source = item.get("source_date")
            if (not source or not item.get("source_name") or source > day
                    or (key == "vix" and source != day)):
                return None
            date.fromisoformat(source)
            if item.get("lag_days") != max(0, len(sessions(source, day)) - 1):
                return None
        inputs[key] = StressInput(item.get("value"), item.get("source_date"),
                                  item.get("lag_days"), item.get("source_name"))
    rebuilt = score_eod_stress(inputs, spy_down=context["spy_down"])
    if any(components[k].get("status") != rebuilt.components[k]["status"]
           or components[k].get("valid") != rebuilt.components[k]["valid"] for k in COMPONENT_SPECS):
        return None
    if (rebuilt.score is None or rebuilt.score != finite(row.get("eod_stress_score"))
            or rebuilt.coverage != finite(row.get("stress_coverage"))
            or rebuilt.confidence != finite(row.get("stress_confidence"))
            or (components.get("_meta") or {}).get("status") != rebuilt.status):
        return None
    signature = tuple((k, v["source_name"], v["weight"]) for k, v in rebuilt.components.items()
                      if k in COMPONENT_SPECS and v["status"] == "OK")
    return {"score": rebuilt.score, "signature": signature, "version": CALC_VERSION}


def build_tactical_history(report, capture):
    result = {"changes": {"1d": None, "5d": None, "21d": None}, "windows": {},
              "net_direction": "UNKNOWN", "direction_cn": "区间净变化待确认", "history": [],
              "basis": "ARCHIVED_SCORE_ENDPOINTS", "alert_eligible": False,
              "reason": "昨日及基准日原始同口径分数未齐备"}
    try:
        received = stamp(capture["captured_at"])
        if (capture.get("status") != "OK" or received > stamp(report["cutoff"])
                or str(received.astimezone(NY).date()) != report["report_date"]):
            return result
    except (KeyError, TypeError, ValueError):
        return result
    raw = valid_rows(capture.get("data", []), "record_date", report["market_date"], report["cutoff"])
    indexed = {}
    for row in sorted(raw, key=lambda r: (r["record_date"], str(r.get("ingested_at") or ""))):
        try:
            if stamp(row["stress_computed_at"]) > stamp(report["cutoff"]):
                raise ValueError("Future calculation")
            indexed[row["record_date"]] = verified_score(row)
        except (KeyError, TypeError, ValueError):
            indexed[row["record_date"]] = None
    current = indexed.get(report["market_date"])
    if not current:
        return result
    days = sessions(date.fromisoformat(report["market_date"]) - timedelta(days=100), report["market_date"])
    comparable = {d: r for d, r in indexed.items() if r and r["signature"] == current["signature"]}
    result["history"] = [{"date": d, "value": comparable[d]["score"] if d in comparable else None} for d in days]
    for count in (1, 5, 21):
        window = days[-count - 1:]
        baseline = comparable.get(window[0]) if len(window) == count + 1 else None
        key = str(count) + "d"
        result["windows"][key] = {"start": window[0], "end": days[-1], "required": count + 1,
                                  "available": sum(d in comparable for d in window),
                                  "missing_dates": [d for d in window if d not in comparable],
                                  "start_score": baseline["score"] if baseline else None,
                                  "end_score": current["score"]}
        if baseline:
            result["changes"][key] = current["score"] - baseline["score"]
    delta = result["changes"]["5d"]
    if delta is not None:
        direction = "UP" if delta >= IMPULSE_THRESHOLD else "DOWN" if delta <= -IMPULSE_THRESHOLD else "FLAT"
        result.update(net_direction=direction, direction_cn={
            "UP": "近5日净压力增加", "DOWN": "近5日净压力减轻", "FLAT": "近5日净变化不明显"}[direction])
    missing = result["windows"].get("5d", {}).get("missing_dates", [])
    result["reason"] = ("原始同版本分数的端点差值，不是逐日连续趋势；"
                        + ("区间缺测 " + "、".join(missing) + "，不确认连续性" if missing
                           else "近5日窗口完整，连续状态仍按原影子规则单列"))
    return result
