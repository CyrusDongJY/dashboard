"""Unvalidated M2 transmission clock, separate from the availability score."""
from __future__ import annotations

import math

import pandas as pd

from core_funding_sources import NY, SOURCE_VERSION, utc_time

M2_CLOCK_VERSION = "m2_transmission_shadow_v1"
M2_LAG_MONTHS = 3


def impact_date(observation_month, lag_months=M2_LAG_MONTHS):
    if not isinstance(lag_months, int) or isinstance(lag_months, bool) or not 0 <= lag_months <= 12:
        raise ValueError("lag_months must be an integer from 0 to 12")
    period = pd.Period(observation_month, freq="M") + lag_months
    return period.to_timestamp(how="end").normalize().tz_localize(NY)


def build_m2_transmission(rows, as_of, lag_months=M2_LAG_MONTHS):
    cutoff = utc_time(as_of)
    impact_date(cutoff.tz_convert(NY).strftime("%Y-%m"), lag_months)
    known = {}
    for row in rows:
        if row["series"] != "m2":
            continue
        if row["source"] != "M2SL" or row["source_version"] != SOURCE_VERSION:
            raise ValueError("Transmission clock requires the US M2 first-release contract")
        date = pd.Timestamp(row["observation_date"])
        release = pd.Timestamp(row["release_date"])
        available = utc_time(row["available_at"])
        month = date.to_period("M")
        value = float(row["value"])
        if (date.tzinfo is not None or date.day != 1 or date != date.normalize()
                or not math.isfinite(value) or value <= 0
                or release.tzinfo is not None or release != release.normalize()
                or available.tz_convert(NY).date() < release.date()
                or available < impact_date(str(month), 0).tz_convert("UTC")):
            raise ValueError("Invalid monthly M2 observation or publication timing")
        if available > cutoff:
            continue
        item = (value, available, row["observation_date"])
        if month in known and known[month] != item:
            raise ValueError("Conflicting M2 first releases")
        known[month] = item
    observations = []
    for month in sorted(known):
        if month - 12 not in known:
            continue
        value, available, source_date = known[month]
        previous, prior_available, _ = known[month - 12]
        available = max(available, prior_available)
        impact = impact_date(str(month), lag_months)
        observations.append({
            "observation_month": str(month), "source_date": source_date,
            "source_available_at": available.isoformat(),
            "yoy_pct": round((value / previous - 1) * 100, 4),
            "assumed_impact_month": str(month + lag_months),
            "assumed_impact_date": str(impact.date()),
            "eligible_from": max(impact.tz_convert("UTC"), available).isoformat(),
        })
    current = observations[-1] if observations else None
    if current and (cutoff.tz_convert(NY).normalize() - impact_date(current["observation_month"], 0)).days > 100:
        current = None
    eligible = [row for row in observations if utc_time(row["eligible_from"]) <= cutoff]
    lagged = eligible[-1] if eligible else None
    if lagged and (cutoff.tz_convert(NY).normalize() - pd.Timestamp(
            lagged["assumed_impact_date"], tz=NY)).days > 45:
        lagged = None
    return {
        "version": M2_CLOCK_VERSION, "as_of": cutoff.isoformat(), "scope": "US_M2_ONLY",
        "source": "M2SL", "lag_months": lag_months, "score_eligible": False,
        "validation": "UNVALIDATED_HYPOTHESIS", "status": "OK" if current and lagged else "INSUFFICIENT",
        "clock": "OBSERVATION_MONTH_END_PLUS_CALENDAR_MONTHS_AND_ACTUAL_AVAILABILITY",
        "current": current, "lagged": lagged, "observations": observations,
    }


def format_m2_transmission(context, scoring_component=None):
    lag = context.get("lag_months", M2_LAG_MONTHS)
    period_label = "三个月" if lag == 3 else f"{lag}个月"
    scoring = scoring_component is not None and scoring_component.get("lag_months") == lag
    use_label = "计分口径见下文" if scoring else "不计入总分"
    lines = [f"=== M2传导时钟（美国M2，非全球；{period_label}假设待验证，{use_label}） ==="]
    if context.get("status") == "UNAVAILABLE":
        return "\n".join(lines + ["时间契约检查失败，本次不输出传导观察值。"])
    for key, label in (("current", "当前已公布"), ("lagged", f"{period_label}滞后观察")):
        if key == "lagged" and scoring:
            value = scoring_component["value"]
            if scoring_component["status"] != "OK":
                lines.append(f"本版M2计分：不可用（{scoring_component['status']}）；不使用其他月份替代。")
            else:
                lines.append(f"本版M2计分：同比 {value:+.2f}% | 原始观测月 {scoring_component['observation_month']}"
                             f" | 对齐日期 {scoring_component['effective_date']}（已包含在总分，不重复加分）")
            continue
        row = context.get(key)
        if row is None:
            lines.append(f"{label}：数据不足")
        else:
            text = f"{label}：M2同比 {row['yoy_pct']:+.2f}% | 原始观测月 {row['observation_month']}"
            if key == "lagged":
                text += f" | 假设影响月 {row['assumed_impact_month']}"
            lines.append(text)
    lines.append(f"按观测月末加{lag}个自然月向右对齐，且不得早于实际可用时间；不改原始日期，不保证{period_label}后股价上涨。")
    return "\n".join(lines)
