"""Independent funding availability index; never an execution or alert signal."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

import numpy as np
import pandas as pd

from core_funding_sources import NY, SOURCE_VERSION, SOURCES, digest_rows, utc_time
from core_funding_m2 import M2_LAG_MONTHS, impact_date

CALC_VERSION = "core_funding_v1.2.1_m2lag3"
GROUPS = {
    "stock": ("存量与缓冲", .25),
    "flow": ("边际净供给", .25),
    "funding": ("融资松紧", .25),
    "credit": ("银行信用活动（供需混合）", .25),
}


@dataclass(frozen=True)
class Component:
    key: str
    group: str
    weight: float
    direction: int
    minimum: int
    baseline: int
    max_age_days: int
    frequency: str


COMPONENTS = (
    Component("reserve_ratio", "stock", .6, 1, 104, 260, 23, "weekly"),
    Component("m2_yoy", "stock", .4, 1, 36, 120, 45, "monthly"),
    Component("net_flow_4w", "flow", .7, 1, 104, 260, 14, "weekly"),
    Component("net_flow_13w", "flow", .3, 1, 104, 260, 14, "weekly"),
    Component("sofr_iorb", "funding", .5, -1, 252, 1260, 7, "daily"),
    Component("effr_iorb", "funding", .3, -1, 252, 1260, 7, "daily"),
    Component("sofr_tail", "funding", .2, -1, 252, 1260, 7, "daily"),
    Component("loans_13w", "credit", .6, 1, 104, 260, 23, "weekly"),
    Component("loan_standards", "credit", .4, -1, 20, 40, 150, "quarterly"),
)


class ObservationStore:
    """Validate once before replaying many cutoffs; retain the native row contract."""

    def __init__(self, rows):
        self.rows = _validated_rows(rows)
        self.available = pd.DatetimeIndex([utc_time(row["available_at"]) for row in self.rows])
        self.dates = pd.DatetimeIndex([pd.Timestamp(row["observation_date"]) for row in self.rows])


def _validated_rows(rows, source_specs=None):
    specs = SOURCES if source_specs is None else source_specs
    records = {}
    for row in rows:
        if row["series"] not in specs or row.get("source_version") != SOURCE_VERSION:
            raise ValueError("Unsupported funding input contract")
        if row.get("source") != specs[row["series"]].fred_id:
            raise ValueError("Funding input source mismatch")
        observation = pd.Timestamp(row["observation_date"])
        release = pd.Timestamp(row["release_date"])
        available = utc_time(row["available_at"])
        if (pd.isna(observation) or pd.isna(release) or observation.tzinfo or release.tzinfo
                or observation != observation.normalize() or release != release.normalize()):
            raise ValueError("Invalid native observation/release date")
        if available.tz_convert(NY).date() < release.date():
            raise ValueError("Input available before its release")
        value = float(row["value"])
        if not math.isfinite(value):
            raise ValueError("Non-finite funding observation")
        if specs[row["series"]].unit == "USD billions" and value < 0:
            raise ValueError("Negative funding balance")
        frequency = specs[row["series"]].frequency
        if frequency == "weekly" and observation.dayofweek != 2:
            raise ValueError("Weekly funding inputs require native Wednesday observations")
        if frequency in {"monthly", "quarterly"} and observation.day != 1:
            raise ValueError("Repeated daily copies are not native observations")
        if frequency == "quarterly" and observation.month not in {1, 4, 7, 10}:
            raise ValueError("Invalid native quarter")
        key = (row["series"], row["observation_date"])
        old = records.get(key)
        if old and old != row:
            raise ValueError("Conflicting duplicate observation")
        records[key] = row
    return list(records.values())


def visible_series(rows, as_of):
    cutoff = utc_time(as_of)
    last_date = cutoff.tz_convert(NY).tz_localize(None).normalize()
    store = rows if isinstance(rows, ObservationStore) else ObservationStore(rows)
    if not store.rows:
        return {key: pd.Series(dtype=float, index=pd.DatetimeIndex([])) for key in SOURCES}, []
    visible = [store.rows[index] for index in np.flatnonzero(
        (store.available <= cutoff) & (store.dates <= last_date))]
    native = {}
    for key in SOURCES:
        selected = [r for r in visible if r["series"] == key]
        series = pd.Series({pd.Timestamp(r["observation_date"]): float(r["value"])
                            for r in selected}, dtype=float).sort_index()
        if SOURCES[key].unit == "USD billions":
            series = series.where(series >= 0)
        native[key] = series
    return native, visible


def _lag(series, days=None, months=None):
    if series.empty:
        return series
    dates = (series.index - pd.DateOffset(months=months) if months is not None
             else series.index - pd.Timedelta(days=days))
    return pd.Series(series.reindex(dates).to_numpy(), index=series.index)


def _growth(series, days=None, months=None):
    prior = _lag(series, days=days, months=months)
    return 100 * (series / prior.where(prior > 0) - 1)


def build_features(native):
    # Exact-date joins prevent mixed Wednesday/week-average TGA and asynchronous legs.
    balances = pd.concat([native[key].rename(key) for key in
                          ("fed_assets", "tga", "rrp")], axis=1).dropna()
    balances["net"] = balances["fed_assets"] - balances["tga"] - balances["rrp"]
    reserves = pd.concat([native[key].rename(key) for key in
                         ("reserves", "bank_assets")], axis=1).dropna()
    rates = {}
    for key, left, right in (("sofr_iorb", "sofr", "iorb"),
                             ("effr_iorb", "effr", "iorb"),
                             ("sofr_tail", "sofr99", "sofr")):
        pair = pd.concat([native[left].rename("left"),
                          native[right].rename("right")], axis=1).dropna()
        rates[key] = 100 * (pair["left"] - pair["right"])
    features = {
        "reserve_ratio": 100 * reserves["reserves"] / reserves["bank_assets"].where(
            reserves["bank_assets"] > 0),
        "m2_yoy": _growth(native["m2"], months=12),
        "loans_13w": _growth(native["loans"], days=91),
        "loan_standards": native["loan_standards"],
        **rates,
    }
    for weeks in (4, 13):
        denominator = _lag(balances["fed_assets"], days=7 * weeks)
        features[f"net_flow_{weeks}w"] = 100 * (
            balances["net"] - _lag(balances["net"], days=7 * weeks)
        ) / denominator.where(denominator > 0)
    return features, balances


def _component_score(series, spec, report_date):
    series = series.replace([np.inf, -np.inf], np.nan).sort_index()
    valid = series.dropna()
    value = float(valid.iloc[-1]) if not valid.empty else None
    source_date = valid.index[-1] if not valid.empty else None
    prior = valid.iloc[:-1].tail(spec.baseline)
    age = (report_date - source_date).days if source_date is not None else None
    status = "OK"
    if value is None:
        status = "MISSING"
    elif not series.empty and source_date != series.index[-1]:
        status = "MISSING_WINDOW"
    elif age > spec.max_age_days:
        status = "STALE"
    elif len(prior) < spec.minimum:
        status = "WARMUP"
    rank = None
    if status == "OK":
        rank = 100 * (float((prior < value).sum()) + .5 * float((prior == value).sum())) / len(prior)
        if spec.direction < 0:
            rank = 100 - rank
    return {
        "value": value, "score": None if rank is None else round(rank, 4),
        "status": status, "source_date": str(source_date.date()) if source_date is not None else None,
        "age_days": age, "prior_native_observations": len(prior),
        "minimum": spec.minimum, "frequency": spec.frequency,
        "weight": spec.weight, "group": spec.group,
    }


def _m2_component_score(series, spec, report_date, visible):
    # Shift only the M2 signal, before selecting the point and its historical baseline.
    effective_month = report_date.to_period("M") - (0 if report_date.is_month_end else 1)
    observation_month = effective_month - M2_LAG_MONTHS
    expected_source_date = observation_month.start_time
    start = min(series.index.min(), expected_source_date) if not series.empty else expected_source_date
    calendar = pd.date_range(start, expected_source_date, freq="MS")
    shifted = series.reindex(calendar)
    shifted.index = (calendar.to_period("M") + M2_LAG_MONTHS).to_timestamp(how="end").normalize()
    item = _component_score(shifted, spec, report_date)
    effective_date = item["source_date"]
    source_month = pd.Period(effective_date, freq="M") - M2_LAG_MONTHS if effective_date else None
    source_date = str(source_month.start_time.date()) if source_month is not None else None
    known = {row["observation_date"]: row for row in visible if row["series"] == "m2"}
    available = None
    if source_month is not None:
        current = known[source_date]
        prior = known[str((source_month - 12).start_time.date())]
        available = max(utc_time(current["available_at"]), utc_time(prior["available_at"]))
    item.update({
        "source_date": source_date,
        "age_days": (report_date - pd.Timestamp(source_date)).days if source_date else None,
        "effective_date": effective_date, "effective_age_days": item["age_days"],
        "observation_month": str(source_month) if source_month is not None else None,
        "expected_observation_month": str(observation_month),
        "expected_effective_date": str(effective_month.end_time.date()),
        "source_available_at": available.isoformat() if available is not None else None,
        "eligible_from": max(available, impact_date(str(source_month)).tz_convert("UTC")).isoformat()
        if available is not None else None,
        "lag_months": M2_LAG_MONTHS, "growth_basis": "YOY", "scope": "US_M2_ONLY",
        "timing_rule": "OBSERVATION_MONTH_END_PLUS_CALENDAR_MONTHS_AND_ACTUAL_AVAILABILITY",
        "validation": "USER_SPECIFIED_UNVALIDATED_HYPOTHESIS",
    })
    return item


def _pressure(components):
    guards = {"sofr_iorb": (5., 50.), "effr_iorb": (5., 25.), "sofr_tail": (25., 75.)}
    reasons, available = [], 0
    acute = False
    for key, (watch, hard) in guards.items():
        item = components[key]
        if item["status"] not in {"OK", "WARMUP"}:
            continue
        available += 1
        if item["value"] >= hard:
            acute = True
            reasons.append(key)
        elif item["value"] >= watch and item["score"] is not None and item["score"] <= 20:
            reasons.append(key)
    state = ("ACUTE" if acute else "WATCH" if reasons else
             "NOT_TRIGGERED" if available == len(guards) else "UNAVAILABLE")
    return {"state": state, "drivers": reasons, "coverage": available / len(guards),
            "calibration": "PROVISIONAL_SHADOW_ONLY"}


def _attribution(balances, report_date):
    output = {}
    if balances.empty or (report_date - balances.index[-1]).days > 14:
        return output
    last_date = balances.index[-1]
    for weeks in (1, 4, 13):
        previous = last_date - pd.Timedelta(weeks=weeks)
        if previous not in balances.index:
            continue
        now, before = balances.loc[last_date], balances.loc[previous]
        parts = {"fed_assets_b": float(now["fed_assets"] - before["fed_assets"]),
                 "tga_b": float(before["tga"] - now["tga"]),
                 "rrp_b": float(before["rrp"] - now["rrp"])}
        output[f"{weeks}w"] = {**parts, "net_change_b": sum(parts.values()),
                                "from_date": str(previous.date()),
                                "to_date": str(last_date.date())}
    return output


def rolling_base_effect(balances, report_date):
    """Bridge a rolling change using exact Wednesdays, never row offsets."""
    output = {}
    if balances.empty or (report_date - balances.index[-1]).days > 14:
        return output
    end = balances.index[-1]
    for weeks in (4, 13):
        prior = end - pd.Timedelta(weeks=1)
        base = end - pd.Timedelta(weeks=weeks)
        old_base = base - pd.Timedelta(weeks=1)
        item = {"status": "MISSING_WINDOW", "to_date": str(end.date()),
                "current_week_b": None, "base_rolloff_b": None,
                "rolling_change_delta_b": None, "base_effect_dominant": None}
        if all(day in balances.index for day in (end, prior, base, old_base)):
            net = balances["net"]
            recent = float(net.loc[end] - net.loc[prior])
            rolloff = float(net.loc[old_base] - net.loc[base])
            delta = recent + rolloff
            item.update(status="OK", current_week_b=recent, base_rolloff_b=rolloff,
                        rolling_change_delta_b=delta,
                        previous_window_b=float(net.loc[prior] - net.loc[old_base]),
                        current_window_b=float(net.loc[end] - net.loc[base]),
                        from_date=str(prior.date()), base_date=str(base.date()),
                        old_base_date=str(old_base.date()),
                        base_effect_dominant=abs(rolloff) > abs(recent))
        output[f"{weeks}w"] = item
    return output


def bank_activity_context(native, features, components, report_date):
    deposits = native["deposits"].dropna()
    growth = _growth(deposits, days=91).dropna()
    item = {"score_eligible": False, "deposit_growth_13w_pct": None,
            "deposit_source_date": None, "status": "INSUFFICIENT", "notes": []}
    if not growth.empty and (report_date - growth.index[-1]).days <= 23:
        item.update(status="OK", deposit_growth_13w_pct=float(growth.iloc[-1]),
                    deposit_source_date=str(growth.index[-1].date()))
    loans, standards = components["loans_13w"], components["loan_standards"]
    if loans["status"] == "OK" and loans["value"] > 0:
        if standards["status"] == "OK" and standards["value"] > 0:
            item["notes"].append("贷款增长但净收紧标准仍为正，不等同于信用供给宽松")
        if item["deposit_growth_13w_pct"] is not None and item["deposit_growth_13w_pct"] < 0:
            item["notes"].append("贷款增长与存款收缩并存，需关注资金基础")
    return item


def compute_core_funding(rows, as_of, observation_mode="PIT_REPLAY"):
    if observation_mode not in {"PIT_REPLAY", "LIVE_SHADOW"}:
        raise ValueError("Unsupported observation mode")
    cutoff = utc_time(as_of)
    report_date = cutoff.tz_convert(NY).tz_localize(None).normalize()
    native, visible = visible_series(rows, cutoff)
    features, balances = build_features(native)
    components = {spec.key: (_m2_component_score(features[spec.key], spec, report_date, visible)
                             if spec.key == "m2_yoy" else
                             _component_score(features[spec.key], spec, report_date))
                  for spec in COMPONENTS}
    groups = {}
    for key, (label, weight) in GROUPS.items():
        selected = [item for item in components.values() if item["group"] == key]
        coverage = sum(item["weight"] for item in selected if item["score"] is not None)
        complete = all(item["score"] is not None for item in selected)
        groups[key] = {"label": label, "weight": weight, "coverage": coverage,
                       "score": round(sum(item["weight"] * item["score"] for item in selected), 4)
                       if complete else None}
    coverage = sum(group["weight"] * group["coverage"] for group in groups.values())
    complete = all(group["score"] is not None for group in groups.values())
    score = round(sum(group["weight"] * group["score"] for group in groups.values()), 2) if complete else None
    state = ("INSUFFICIENT" if score is None else "LOW" if score < 20 else
             "TIGHT" if score < 40 else "MIDDLE" if score < 60 else
             "SUPPORTIVE" if score < 80 else "HIGH")
    context = {}
    for key in ("rrp", "deposits"):
        values = native[key].dropna()
        context[key] = ({"value": float(values.iloc[-1]),
                         "source_date": str(values.index[-1].date())} if not values.empty else None)
    contemporary_series = features["m2_yoy"].copy()
    contemporary_series.index = pd.DatetimeIndex(contemporary_series.index).to_period("M").to_timestamp(how="end").normalize()
    contemporary_m2 = _component_score(contemporary_series,
                                      replace(next(c for c in COMPONENTS if c.key == "m2_yoy"), max_age_days=75), report_date)
    if contemporary_m2["source_date"]:
        month = pd.Period(contemporary_m2["source_date"], freq="M")
        contemporary_m2.update(source_date=str(month.start_time.date()), observation_month=str(month),
                               freshness_basis="OBSERVATION_MONTH_END_MAX_75_CALENDAR_DAYS")
    comparison = None
    if score is not None and contemporary_m2["score"] is not None:
        comparison = round(score + .1 * (contemporary_m2["score"] - components["m2_yoy"]["score"]), 2)
    funding_score = groups["funding"]["score"]
    interpretation = ("数据不足" if score is None else
                      "综合分数偏高，但融资分量偏弱；不是全面宽松" if score >= 60 and funding_score < 40
                      else "分组分化，需同时查看融资压力与当周净变化")
    return {"report_date": str(report_date.date()), "as_of": cutoff.isoformat(),
            "calc_version": CALC_VERSION, "source_version": SOURCE_VERSION,
            "observation_mode": observation_mode, "shadow_mode": True,
            "decision_eligible": False, "score": score, "state": state,
            "coverage": round(coverage, 6), "groups": groups, "components": components,
            "funding_pressure": _pressure(components),
            "net_liquidity_attribution": _attribution(balances, report_date),
            "rolling_base_effect": rolling_base_effect(balances, report_date),
            "interpretation": interpretation,
            "bank_activity_context": bank_activity_context(native, features, components, report_date),
            "m2_unshifted_reference": {"score": comparison, "m2_component": contemporary_m2,
                                       "score_eligible": False, "label": "同一模型取消M2移位的研究对照"},
            "context": context, "input_digest": digest_rows(visible),
            "change_20_sessions": None, "historical_percentile": None,
            "trend": "INSUFFICIENT", "persistence_status": "NOT_REQUESTED"}


def build_core_history(rows, dates, cutoff_time="16:45"):
    rows = rows if isinstance(rows, ObservationStore) else ObservationStore(rows)
    results = []
    dates = sorted(set(str(pd.Timestamp(day).date()) for day in dates))
    for date in dates:
        result = compute_core_funding(rows, pd.Timestamp(f"{date} {cutoff_time}", tz=NY))
        add_trend(result, results, session_dates=dates)
        results.append(result)
    return results


def add_trend(result, history, session_dates=None):
    prior = sorted([row for row in history
                    if row["report_date"] < result["report_date"]
                    and row["calc_version"] == result["calc_version"]
                    and row["observation_mode"] == result["observation_mode"]],
                   key=lambda row: row["report_date"])
    if result["score"] is None:
        return
    if session_dates is None:
        import pandas_market_calendars as mcal
        start = pd.Timestamp(result["report_date"]) - pd.Timedelta(days=60)
        session_dates = mcal.get_calendar("NYSE").schedule(
            start_date=start, end_date=result["report_date"]).index
    dates = sorted(set(str(pd.Timestamp(day).date()) for day in session_dates
                       if str(pd.Timestamp(day).date()) <= result["report_date"]))
    by_date = {row["report_date"]: row for row in prior}
    window = dates[-21:-1] if dates and dates[-1] == result["report_date"] else []
    if len(window) == 20 and all(day in by_date and by_date[day]["score"] is not None for day in window):
        earlier = by_date[window[0]]
        result["change_20_sessions"] = round(result["score"] - earlier["score"], 2)
        differences = {key: round(result["groups"][key]["score"] - earlier["groups"][key]["score"], 4)
                       for key in GROUPS}
        result["score_change_attribution"] = {
            key: round(value * GROUPS[key][1], 4) for key, value in differences.items()}
        change = result["change_20_sessions"]
        direction = 1 if change > 0 else -1
        confirmations = sum(value * direction >= 5 for value in differences.values())
        result["trend"] = ("IMPROVING_CANDIDATE" if change >= 5 and confirmations >= 2 else
                           "TIGHTENING_CANDIDATE" if change <= -5 and confirmations >= 2 else "MIXED_OR_STABLE")
    # Weekly samples keep unchanged slow variables from pretending to be daily evidence.
    scored = [(row["report_date"], row["score"]) for row in prior if row["score"] is not None]
    if scored:
        series = pd.Series([value for _, value in scored], index=pd.to_datetime([day for day, _ in scored]))
        weekly = series.resample("W-FRI").last().dropna()
        weekly = weekly[weekly.index < pd.Timestamp(result["report_date"])].tail(260)
        if len(weekly) >= 52:
            result["historical_percentile"] = float(round(100 * (
                (weekly < result["score"]).sum() + .5 * (weekly == result["score"]).sum()) / len(weekly), 1))
