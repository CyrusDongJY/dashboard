"""Fail-closed EOD cross-asset tactical stress score.

The score intentionally preserves the legacy V8.6 thresholds. This module adds
only the data contract around that formula: component freshness, critical-group
gates, weighted coverage, explainable contributions, and versioned output.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from typing import Optional


CALC_VERSION = "eod_stress_v1.1-shadow"
MIN_WEIGHTED_COVERAGE = 0.70


@dataclass(frozen=True)
class ComponentSpec:
    weight: float
    max_lag_days: int
    group: str


COMPONENT_SPECS = {
    "vix": ComponentSpec(60.0, 0, "core"),
    "move": ComponentSpec(15.0, 1, "cross_asset"),
    "credit_spread": ComponentSpec(15.0, 3, "cross_asset"),
    "trin": ComponentSpec(15.0, 0, "market_internal"),
    "pcr": ComponentSpec(10.0, 0, "market_internal"),
    "cmf": ComponentSpec(10.0, 0, "market_internal"),
}


@dataclass(frozen=True)
class StressInput:
    value: object
    source_date: Optional[str]
    lag_days: Optional[int]
    source_name: Optional[str] = None


@dataclass
class StressResult:
    score: Optional[int]
    coverage: float
    confidence: float
    status: str
    reasons: list[str]
    components: dict
    calc_version: str = CALC_VERSION
    computed_at: str = ""

    def to_dict(self):
        return asdict(self)


def nullable_int(value):
    """Convert a score to int without turning missing data into zero."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) else None


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


def _display_number(value, digits=2, suffix=""):
    number = _finite_number(value)
    if number is None:
        return "数据缺失"
    return f"{number:.{digits}f}{suffix}"


def format_tactical_stress_summary(row):
    """Render the persisted stress contract as a standalone email section."""
    row = _mapping(row)
    components = _mapping(row.get("stress_components"))
    meta = _mapping(components.get("_meta"))
    context = _mapping(components.get("_context"))

    score_value = (row.get("eod_stress_score")
                   if "eod_stress_score" in row else row.get("micro_score"))
    score = nullable_int(score_value)
    status = meta.get("status") or ("UNAVAILABLE" if score is None else "OK")
    status_cn = {
        "OK": "完整可用",
        "PARTIAL": "部分覆盖，可用",
        "UNAVAILABLE": "不可计算",
    }.get(str(status), str(status))
    coverage = _finite_number(row.get("stress_coverage"))
    confidence = _finite_number(row.get("stress_confidence"))

    lines = [
        "=== 盘后跨资产战术压力观察（影子观察，不触发预警） ===",
        f"状态：{status_cn}",
        f"观察值：{f'{score}/100' if score is not None else '数据不足'}",
        "有效覆盖：" + (f"{coverage:.0%}" if coverage is not None else "数据缺失"),
        "数据置信度：" + (f"{confidence:.0%}" if confidence is not None else "数据缺失"),
        f"计算版本：{row.get('stress_calc_version') or '未记录'}",
    ]

    reasons = meta.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    reason_labels = {
        "VIX_CORE_UNAVAILABLE": "VIX 核心分量不可用",
        "NO_CROSS_ASSET_CONFIRMATION": "缺少跨资产确认",
        "NO_MARKET_INTERNAL_CONFIRMATION": "缺少市场内部确认",
        "INSUFFICIENT_WEIGHTED_COVERAGE": "有效加权覆盖不足",
        "INTERNAL_ERROR": "计算过程异常",
    }
    if reasons:
        lines.append("不可用原因：" + "；".join(
            reason_labels.get(reason, str(reason)) for reason in reasons))

    context_parts = [
        f"VIX 5日变化 {_display_number(context.get('vix_5d_change'))}",
        f"VIX 252日分位 {_display_number(context.get('vix_percentile_252'), 1, '%')}",
        f"VIX/VIX3M {_display_number(context.get('vix_term_ratio'), 3)}",
    ]
    if context.get("spy_down_available") is False:
        context_parts.append("SPY方向 数据缺失")
    elif "spy_down" in context:
        context_parts.append("SPY当日下跌" if context.get("spy_down") else "SPY当日未下跌")
    lines.append("VIX与价格背景：" + " | ".join(context_parts))

    component_labels = {
        "vix": "VIX",
        "move": "MOVE",
        "credit_spread": "信用利差",
        "trin": "TRIN",
        "pcr": "Put/Call 成交量比",
        "cmf": "QQQ CMF",
    }
    status_labels = {
        "OK": "有效",
        "MISSING_VALUE": "数值缺失",
        "OUT_OF_DOMAIN": "数值越界",
        "MISSING_SOURCE_DATE": "来源日期缺失",
        "MISSING_LAG": "滞后信息缺失",
        "STALE_SOURCE": "来源过期",
    }
    lines.append("分项数据与贡献：")
    for name in COMPONENT_SPECS:
        item = _mapping(components.get(name))
        state = status_labels.get(item.get("status"), item.get("status") or "未记录")
        contribution = _finite_number(item.get("contribution"))
        contribution_text = (f"{contribution:+.1f}分"
                             if contribution is not None else "不计分")
        lag = item.get("lag_days")
        lag_text = f"滞后{lag}个交易日" if lag is not None else "滞后未知"
        source = item.get("source_name") or "来源未记录"
        source_date = item.get("source_date") or "日期未记录"
        lines.append(
            f"- {component_labels[name]}：{_display_number(item.get('value'))} | "
            f"{contribution_text} | {state} | {source_date}，{lag_text} | {source}")

    lines.append(
        "口径：这是 EOD 跨资产压力证据汇总；不等同于市场流动性、趋势方向或反转确认。")
    return "\n".join(lines)


def _finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _in_domain(name, value):
    if name in {"vix", "move", "trin"}:
        return value > 0
    if name in {"credit_spread", "pcr"}:
        return value >= 0
    if name == "cmf":
        return -1 <= value <= 1
    return False


def _contribution(name, value, spy_down):
    if name == "vix":
        if value >= 35:
            return 60.0
        if value >= 25:
            return 40.0 + ((value - 25.0) / 10.0) * 20.0
        if value >= 18:
            return 20.0 + ((value - 18.0) / 7.0) * 20.0
        if value >= 12:
            return ((value - 12.0) / 6.0) * 20.0
        return 0.0
    if name == "move":
        return 15.0 if value >= 120 else (10.0 if value >= 100 else 0.0)
    if name == "credit_spread":
        return 15.0 if value > 5.0 else 0.0
    if name == "trin":
        if value > 1.5 and spy_down:
            return 15.0
        if value > 1.2:
            return 10.0
        if value < 0.7 and spy_down:
            return -10.0
        return 0.0
    if name == "pcr":
        return 10.0 if value > 1.0 else 0.0
    if name == "cmf":
        return 10.0 if value < -0.05 else 0.0
    return 0.0


def score_eod_stress(inputs, *, spy_down=False, context=None, computed_at=None):
    """Score valid evidence only; never normalize missing components upward."""
    computed_at = computed_at or datetime.now(timezone.utc).isoformat()
    total_weight = sum(spec.weight for spec in COMPONENT_SPECS.values())
    components = {}
    valid_weight = 0.0
    confidence_weight = 0.0

    for name, spec in COMPONENT_SPECS.items():
        item = inputs.get(name, StressInput(None, None, None))
        value = _finite_number(item.value)
        lag = item.lag_days
        valid = True
        reason = "OK"
        if value is None:
            valid, reason = False, "MISSING_VALUE"
        elif not _in_domain(name, value):
            valid, reason = False, "OUT_OF_DOMAIN"
        elif not item.source_date:
            valid, reason = False, "MISSING_SOURCE_DATE"
        elif lag is None:
            valid, reason = False, "MISSING_LAG"
        elif lag < 0 or lag > spec.max_lag_days:
            valid, reason = False, "STALE_SOURCE"

        contribution = None
        if valid:
            lag = int(lag)
            contribution = _contribution(name, value, spy_down)
            valid_weight += spec.weight
            confidence_weight += spec.weight * (0.85 ** lag)

        components[name] = {
            "value": value,
            "source_name": item.source_name,
            "source_date": item.source_date,
            "lag_days": lag,
            "valid": valid,
            "status": reason,
            "group": spec.group,
            "weight": spec.weight,
            "max_lag_days": spec.max_lag_days,
            "contribution": round(contribution, 4)
            if contribution is not None else None,
        }

    coverage = valid_weight / total_weight
    confidence = confidence_weight / total_weight
    valid_names = {name for name, item in components.items() if item["valid"]}
    valid_groups = {COMPONENT_SPECS[name].group for name in valid_names}
    reasons = []
    if "vix" not in valid_names:
        reasons.append("VIX_CORE_UNAVAILABLE")
    if "cross_asset" not in valid_groups:
        reasons.append("NO_CROSS_ASSET_CONFIRMATION")
    if "market_internal" not in valid_groups:
        reasons.append("NO_MARKET_INTERNAL_CONFIRMATION")
    if coverage < MIN_WEIGHTED_COVERAGE:
        reasons.append("INSUFFICIENT_WEIGHTED_COVERAGE")

    score = None
    if not reasons:
        raw_score = sum(
            item["contribution"] for item in components.values()
            if item["contribution"] is not None
        )
        score = max(0, min(int(raw_score), 100))

    payload_context = dict(context or {})
    payload_context["spy_down"] = bool(spy_down)
    components["_context"] = payload_context
    status = "UNAVAILABLE" if score is None else (
        "OK" if coverage == 1.0 else "PARTIAL")
    return StressResult(
        score=score,
        coverage=round(coverage, 4),
        confidence=round(confidence, 4),
        status=status,
        reasons=reasons,
        components=components,
        computed_at=computed_at,
    )
