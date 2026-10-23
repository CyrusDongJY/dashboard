"""Optional explanatory inputs. No input here can change the funding score."""
from __future__ import annotations

import time

import pandas as pd
import requests

from core_funding_index import _validated_rows, visible_series
from core_funding_sources import (CoreFundingSources, NY, SOURCE_VERSION,
                                  SourceSpec, digest_rows, utc_time)

SOURCES = {
    "loan_demand": SourceSpec("DRSDCILM", 1., "quarterly", "percent"),
    "sofr_volume": SourceSpec("SOFRVOL", 1., "daily", "USD billions"),
    "fed_securities": SourceSpec("WSHOSHO", .001, "weekly", "USD billions"),
    "primary_credit": SourceSpec("WLCFLPCL", .001, "weekly", "USD billions"),
}


def collect_context(api_key, cache_dir, refresh=False):
    if not api_key:
        return {"version": SOURCE_VERSION, "rows": [], "digest": digest_rows([]),
                "source_status": {key: "NOT_REQUESTED" for key in SOURCES}}
    with requests.Session() as session:
        source = CoreFundingSources(api_key, timeout=4, session=session,
                                    source_specs=SOURCES, deadline=time.monotonic() + 25)
        start = str((pd.Timestamp.now(tz=NY) - pd.DateOffset(years=3)).date())
        return source.collect(cache_dir, start_date=start, refresh=refresh)


def build_context(archive, core_rows, as_of):
    if archive.get("version") != SOURCE_VERSION or digest_rows(archive["rows"]) != archive.get("digest"):
        raise ValueError("Context archive contract mismatch")
    cutoff = utc_time(as_of)
    date = cutoff.tz_convert(NY).tz_localize(None).normalize()
    rows = _validated_rows(archive["rows"], source_specs=SOURCES)
    visible = [r for r in rows if utc_time(r["available_at"]) <= cutoff
               and pd.Timestamp(r["observation_date"]) <= date]
    native, _ = visible_series(core_rows, cutoff)
    values, observations = {}, {}
    for key, spec in SOURCES.items():
        selected = [r for r in visible if r["series"] == key]
        series = pd.Series({pd.Timestamp(r["observation_date"]): r["value"]
                            for r in selected}, dtype=float).sort_index()
        values[key] = series
        item = {"status": "MISSING", "value": None, "source_date": None,
                "source": spec.fred_id, "unit": spec.unit}
        if not series.empty:
            day = series.index[-1]
            age = (date - day).days
            item.update(status="OK" if age <= (150 if key == "loan_demand" else 7 if key == "sofr_volume" else 14) else "STALE",
                        value=float(series.iloc[-1]), source_date=str(day.date()), age_days=age)
        observations[key] = item
    composition = {"status": "MISSING_COMMON_WINDOW", "contributions_b": {}}
    total = native["fed_assets"].dropna()
    if not total.empty:
        day, before = total.index[-1], total.index[-1] - pd.Timedelta(weeks=1)
        legs = {"fed_assets": total, "securities": values["fed_securities"],
                "primary_credit": values["primary_credit"]}
        if (date - day).days <= 14 and all(day in s.index and before in s.index for s in legs.values()):
            changes = {k: float(s.loc[day] - s.loc[before]) for k, s in legs.items()}
            changes["other_assets"] = changes["fed_assets"] - changes["securities"] - changes["primary_credit"]
            composition.update(status="OK", from_date=str(before.date()), to_date=str(day.date()),
                               contributions_b=changes)
    month_end = date + pd.offsets.BMonthEnd(0)
    distance = len(pd.bdate_range(date, month_end)) - 1
    return {"status": "OK" if any(i["status"] == "OK" for i in observations.values()) else "UNAVAILABLE",
            "score_eligible": False, "decision_eligible": False,
            "archive_digest": archive["digest"], "observations": observations,
            "fed_asset_composition_1w": composition,
            "calendar": {"month_end_within_3_weekdays": distance <= 3,
                         "quarter_end_within_3_weekdays": distance <= 3 and date.month in (3, 6, 9, 12),
                         "tax_and_treasury_settlement": "NOT_VERIFIED",
                         "basis": "WEEKDAY_CALENDAR_NOT_SETTLEMENT_CALENDAR"}}


def format_context(context):
    lines = ["=== 补充观察（不计分） ==="]
    labels = {"loan_demand": "大中型企业净贷款需求", "sofr_volume": "SOFR成交规模",
              "fed_securities": "美联储持有证券", "primary_credit": "美联储一级信贷"}
    for key, item in context.get("observations", {}).items():
        value = f"{item['value']:.2f} {item['unit']}" if item["status"] == "OK" else item["status"]
        lines.append(f"{labels[key]}：{value} | 数据日 {item['source_date'] or 'NA'}")
    composition = context.get("fed_asset_composition_1w", {})
    if composition.get("status") == "OK":
        c = composition["contributions_b"]
        lines.append(f"Fed资产当周变化 {c['fed_assets']:+.2f}B：证券 {c['securities']:+.2f}B / 一级信贷 {c['primary_credit']:+.2f}B / 其他资产 {c['other_assets']:+.2f}B")
    if context.get("status") == "UNAVAILABLE":
        lines.append("补充数据不可用，核心分数不受影响")
    if context.get("calendar", {}).get("month_end_within_3_weekdays"):
        lines.append("接近月末工作日，关注融资季节性；不据此认定资金压力")
    lines.append("成交规模无固定宽松方向；贷款需求与贷款标准联合看；Fed资产增加不自动等于QE。税款与国债结算日未核验，不生成日历事件。")
    return "\n".join(lines)
