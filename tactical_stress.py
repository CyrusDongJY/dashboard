"""Fail-closed EOD cross-asset tactical stress score.

The score intentionally preserves the legacy V8.6 thresholds. This module adds
only the data contract around that formula: component freshness, critical-group
gates, weighted coverage, explainable contributions, and versioned output.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
