"""Auditable report diagnostics, isolated from the core score and trading gates."""
from __future__ import annotations

from pathlib import Path
import time

import pandas as pd
import requests

from core_funding_index import COMPONENTS, GROUPS, _validated_rows, build_features, visible_series
from core_funding_sources import (CoreFundingSources, NY, SOURCE_VERSION, SOURCES,
                                  SourceSpec, digest_rows, utc_time)

DIAGNOSTICS_VERSION = "core_funding_diagnostics_v1"
CREDIT_SOURCES = {
    "hy_oas": SourceSpec("BAMLH0A0HYM2", 1., "daily", "percent"),
    "ig_oas": SourceSpec("BAMLC0A0CM", 1., "daily", "percent"),
    "bbb_oas": SourceSpec("BAMLC0A4CBBB", 1., "daily", "percent"),
}
CREDIT_GUARDS = {"hy_oas": (500., 30.), "ig_oas": (150., 10.), "bbb_oas": (200., 15.)}
COMPONENT_LABELS = {
    "reserve_ratio": "准备金/银行资产", "m2_yoy": "美国M2同比(延后3个月)",
    "net_flow_4w": "净流动性4周变化", "net_flow_13w": "净流动性13周变化",
    "sofr_iorb": "SOFR-IORB", "effr_iorb": "EFFR-IORB", "sofr_tail": "SOFR尾部",
    "loans_13w": "银行贷款13周增长", "loan_standards": "贷款标准",
}


def empty_credit_archive():
    return {"version": SOURCE_VERSION, "rows": [], "digest": digest_rows([]),
            "source_status": {key: "NOT_REQUESTED" for key in CREDIT_SOURCES}}


def collect_credit_context(api_key, cache_dir, refresh=False):
    if not api_key:
        return empty_credit_archive()
    # Separate cache and a best-effort 25-second I/O budget; the job also has its hard timeout.
    with requests.Session() as session:
        source = CoreFundingSources(api_key, timeout=4, session=session,
                                    source_specs=CREDIT_SOURCES, deadline=time.monotonic() + 25)
        start = str((pd.Timestamp.now(tz=NY) - pd.DateOffset(years=3)).date())
        return source.collect(Path(cache_dir), start_date=start, refresh=refresh)


def session_dates(end_date, count=300):
    import pandas_market_calendars as mcal
    end = pd.Timestamp(end_date)
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=end - pd.Timedelta(days=count * 2 + 30), end_date=end)
    return list(schedule.index[-count:])


def score_dynamics(result, history):
    dates = [str(day.date()) for day in session_dates(result["report_date"], 64)]
    prior = {}
    conflict = set()
    for row in history:
        if (row["report_date"] >= result["report_date"] or row["calc_version"] != result["calc_version"]
                or row["observation_mode"] != result["observation_mode"]
                or utc_time(row["as_of"]) > utc_time(result["as_of"])):
            continue
        day = row["report_date"]
        if day in prior and prior[day] != row:
            conflict.add(day)
        prior[day] = row
    output = {}
    for count in (1, 5, 20, 63):
        window = dates[-count - 1:-1]
        item = {"status": "INSUFFICIENT", "delta": None, "sessions": count,
                "from_date": window[0] if len(window) == count else None,
                "to_date": result["report_date"], "direction": "UNAVAILABLE",
                "component_contributions": {}, "group_contributions": {}}
        complete = (dates and dates[-1] == result["report_date"] and len(window) == count
                    and result["score"] is not None and all(
                        day not in conflict and day in prior and prior[day]["score"] is not None
                        and prior[day]["coverage"] == 1 for day in window) and result["coverage"] == 1)
        if complete:
            before = prior[window[0]]
            delta = round(result["score"] - before["score"], 2)
            item.update(status="OK", delta=delta,
                        direction="IMPROVING" if delta >= .5 else "TIGHTENING" if delta <= -.5 else "LITTLE_CHANGE")
            contributions = {}
            for spec in COMPONENTS:
                old = before.get("components", {}).get(spec.key, {}).get("score")
                new = result.get("components", {}).get(spec.key, {}).get("score")
                if old is not None and new is not None:
                    contributions[spec.key] = round((new - old) * spec.weight * GROUPS[spec.group][1], 6)
            if len(contributions) == len(COMPONENTS):
                residual = round(delta - sum(contributions.values()), 6)
                if abs(residual) <= .02:
                    item["component_contributions"] = contributions
                    item["group_contributions"] = {
                        key: round(sum(contributions[c.key] for c in COMPONENTS if c.group == key), 6)
                        for key in GROUPS}
                    item["rounding_residual"] = residual
                    item["attribution_status"] = "RECONCILED"
                else:
                    item["attribution_status"] = "RECONCILIATION_FAILED"
            else:
                item["attribution_status"] = "MISSING_COMPONENT_SCORES"
        output[str(count)] = item
    return output


def balance_ledger(rows, as_of):
    cutoff = utc_time(as_of)
    date = cutoff.tz_convert(NY).tz_localize(None).normalize()
    native, visible = visible_series(rows, cutoff)
    _, balances = build_features(native)
    by_key = {(row["series"], row["observation_date"]): row for row in visible}
    latest = {}
    for key in ("fed_assets", "tga", "rrp", "reserves"):
        values = native[key].dropna()
        item = {"status": "MISSING", "value_b": None, "source_date": None,
                "source": SOURCES[key].fred_id, "unit": "USD billions"}
        if not values.empty:
            day = values.index[-1]
            age = (date - day).days
            item.update(status="STALE" if age > (7 if key == "rrp" else 14) else "OK",
                        value_b=float(values.iloc[-1]), source_date=str(day.date()), age_days=age,
                        available_at=by_key[(key, str(day.date()))]["available_at"])
        latest[key] = item
    result = {"status": "MISSING", "unit": "USD billions", "latest_inputs": latest,
              "source_date": None, "net_b": None, "legs_b": {}, "changes": {},
              "percentile_52w": None, "percentile_observations": 0,
              "formula": "WALCL - WDTGAL - RRPONTSYD; SAME_OBSERVATION_DATE"}
    if balances.empty:
        return result
    day = balances.index[-1]
    if any(latest[key]["source_date"] != str(day.date()) for key in ("fed_assets", "tga")):
        result["status"] = "NO_COMMON_LATEST_DATE"
        return result
    if (date - day).days > 14:
        result["status"] = "STALE"
        return result
    legs = {key: float(balances.loc[day, key]) for key in ("fed_assets", "tga", "rrp")}
    result.update(status="OK", source_date=str(day.date()), legs_b=legs,
                  net_b=legs["fed_assets"] - legs["tga"] - legs["rrp"],
                  quarter_end=day.is_quarter_end)
    for weeks in (1, 4):
        previous = day - pd.Timedelta(weeks=weeks)
        change = {"status": "MISSING_WINDOW", "from_date": str(previous.date()),
                  "to_date": str(day.date()), "net_change_b": None, "contributions_b": {}}
        if previous in balances.index:
            before = balances.loc[previous]
            parts = {"fed_assets": legs["fed_assets"] - float(before.fed_assets),
                     "tga": float(before.tga) - legs["tga"], "rrp": float(before.rrp) - legs["rrp"]}
            change.update(status="OK", contributions_b=parts, net_change_b=sum(parts.values()))
        result["changes"][str(weeks)] = change
    previous_weeks = pd.date_range(day - pd.Timedelta(weeks=52), periods=52, freq="W-WED")
    reference = balances.net.reindex(previous_weeks)
    result["percentile_observations"] = int(reference.notna().sum())
    if reference.notna().all():
        result["percentile_52w"] = float(100 * (
            (reference < result["net_b"]).sum() + .5 * (reference == result["net_b"]).sum()) / 52)
    return result


def credit_observation(archive, as_of):
    if archive.get("version") != SOURCE_VERSION or digest_rows(archive["rows"]) != archive.get("digest"):
        raise ValueError("Credit archive contract mismatch")
    cutoff = utc_time(as_of)
    date = cutoff.tz_convert(NY).tz_localize(None).normalize()
    records = _validated_rows(archive["rows"], source_specs=CREDIT_SOURCES)
    visible = [row for row in records if utc_time(row["available_at"]) <= cutoff
               and pd.Timestamp(row["observation_date"]) <= date]
    if any(row["value"] < 0 for row in visible):
        raise ValueError("Negative credit spread")
    result = {"score_eligible": False, "decision_eligible": False, "status": "UNAVAILABLE",
              "coverage": 0., "series": {}, "archive_digest": archive["digest"],
              "calibration": "PROVISIONAL_SHADOW_ONLY", "partial": True}
    for key, spec in CREDIT_SOURCES.items():
        values = pd.Series({pd.Timestamp(row["observation_date"]): float(row["value"]) * 100
                            for row in visible if row["series"] == key}, dtype=float).sort_index()
        item = {"status": "MISSING", "value_bp": None, "source_date": None,
                "source": spec.fred_id, "unit": "bp", "change_5_sessions_bp": None,
                "change_status": "MISSING_WINDOW", "percentile_252": None,
                "flags": [], "source_status": archive.get("source_status", {}).get(key, "ARCHIVED")}
        high, widening = CREDIT_GUARDS[key]
        item["provisional_guards"] = {"level_bp": high, "change_5_sessions_bp": widening}
        if not values.empty:
            day = values.index[-1]
            dates = session_dates(date, 280)
            lag = sum(d > day for d in dates)
            item.update(value_bp=float(values.iloc[-1]), source_date=str(day.date()),
                        age_days=(date - day).days, session_lag=lag)
            row = next(row for row in visible if row["series"] == key and row["observation_date"] == str(day.date()))
            item["available_at"] = row["available_at"]
            item["status"] = "STALE" if lag > 2 or (date - day).days > 7 else "OK"
            if item["status"] == "OK":
                calendar = session_dates(day, 253)
                window = values.reindex(calendar[-6:])
                if len(window) == 6 and calendar[-1] == day and window.notna().all():
                    item.update(change_5_sessions_bp=float(window.iloc[-1] - window.iloc[0]), change_status="OK",
                                change_from_date=str(calendar[-6].date()), change_to_date=str(day.date()))
                prior = values.reindex(calendar[:-1]).dropna()
                item["percentile_observations"] = len(prior)
                if len(prior) >= 126 and calendar[-1] == day:
                    item["percentile_252"] = float(100 * ((prior < item["value_bp"]).sum()
                                                         + .5 * (prior == item["value_bp"]).sum()) / len(prior))
                if item["value_bp"] >= high:
                    item["flags"].append("HIGH_LEVEL")
                if item["change_5_sessions_bp"] is not None and item["change_5_sessions_bp"] >= widening:
                    item["flags"].append("FAST_WIDENING")
        result["series"][key] = item
    valid = [item for item in result["series"].values() if item["status"] == "OK"]
    complete = len(valid) == 3 and all(item["change_status"] == "OK" for item in valid)
    flagged = any(item["flags"] for item in valid)
    result.update(coverage=len(valid) / 3, partial=not complete,
                  status=("WATCH" if complete else "WATCH_PARTIAL") if flagged else
                  "NOT_TRIGGERED" if complete else "INCOMPLETE" if valid else "UNAVAILABLE")
    return result


def build_diagnostics(result, history, rows, credit_archive=None):
    dynamics = score_dynamics(result, history)
    ledger = balance_ledger(rows, result["as_of"])
    credit = credit_observation(credit_archive or empty_credit_archive(), result["as_of"])
    week = dynamics["5"]
    divergence = "INSUFFICIENT"
    if week["status"] == "OK" and credit["coverage"] > 0:
        divergence = "INSUFFICIENT" if credit["partial"] else "NOT_TRIGGERED"
        if week["direction"] == "IMPROVING":
            if any("FAST_WIDENING" in item["flags"] for item in credit["series"].values()):
                divergence = "FUNDING_IMPROVES_CREDIT_WORSENS"
            elif any("HIGH_LEVEL" in item["flags"] for item in credit["series"].values()):
                divergence = "FUNDING_IMPROVES_CREDIT_STILL_ELEVATED"
    return {"version": DIAGNOSTICS_VERSION, "as_of": result["as_of"],
            "calc_version": result["calc_version"], "observation_mode": result["observation_mode"],
            "score_eligible": False, "decision_eligible": False,
            "dynamics": dynamics, "balance_ledger": ledger, "credit": credit,
            "divergence": divergence,
            "interpretation": "Score level, changes and independent market stress are different quantities"}


def format_diagnostics(diagnostics):
    if diagnostics.get("status") == "UNAVAILABLE":
        return "=== 资金变化与信用观察 ===\n附加诊断不可用；不影响核心评分，也不代表无风险。"
    direction = {"IMPROVING": "改善", "TIGHTENING": "收紧", "LITTLE_CHANGE": "变化有限", "UNAVAILABLE": "不足"}
    lines = ["=== 资金变化与信用观察（不额外计分） ==="]
    for count, item in diagnostics["dynamics"].items():
        value = "NA" if item["delta"] is None else f"{item['delta']:+.2f}点"
        lines.append(f"{count}交易日变化：{value} | {direction[item['direction']]} | {item['from_date']} 至 {item['to_date']}")
    week = diagnostics["dynamics"]["5"]
    contributions = week["component_contributions"]
    if contributions:
        for label, positive in (("5日主要支撑", True), ("5日主要拖累", False)):
            selected = sorted(((key, value) for key, value in contributions.items() if (value > 0 if positive else value < 0)),
                              key=lambda pair: abs(pair[1]), reverse=True)[:3]
            lines.append(label + "：" + ("；".join(f"{COMPONENT_LABELS[key]} {value:+.2f}点" for key, value in selected) or "无"))
        lines.append(f"全部分量贡献合计 {sum(contributions.values()):+.4f}点；舍入差 {week['rounding_residual']:+.4f}点。")
    ledger = diagnostics["balance_ledger"]
    if ledger["status"] == "OK":
        legs = ledger["legs_b"]
        lines.append(f"同日净流动性：{ledger['net_b']:.3f}B = Fed {legs['fed_assets']:.3f}B - "
                     f"TGA {legs['tga']:.3f}B - RRP {legs['rrp']:.3f}B；观测日 {ledger['source_date']}。")
        for weeks, item in ledger["changes"].items():
            if item["status"] == "OK":
                parts = item["contributions_b"]
                lines.append(f"{weeks}个日历周净变化 {item['net_change_b']:+.3f}B：Fed {parts['fed_assets']:+.3f}B / "
                             f"TGA {parts['tga']:+.3f}B / RRP {parts['rrp']:+.3f}B；{item['from_date']} 至 {item['to_date']}。")
            else:
                lines.append(f"{weeks}个日历周净变化：NA（{item['status']}）。")
        percentile = ledger["percentile_52w"]
        lines.append("净流动性存量近52周分位：" + (f"{percentile:.1f}%" if percentile is not None else
                     f"NA（{ledger['percentile_observations']}/52周）") + "；不是投放量或现金可投资比例。")
    else:
        lines.append(f"同日净流动性：NA（{ledger['status']}）；禁止用异日数据或周均TGA凑数。")
    for key, label in (("fed_assets", "Fed资产"), ("tga", "TGA周三余额"), ("rrp", "RRP最新值"), ("reserves", "准备金周均")):
        item = ledger["latest_inputs"][key]
        value = "NA" if item["value_b"] is None else f"{item['value_b']:.3f}B"
        lines.append(f"{label}：{value} | 数据日 {item['source_date']} | {item['source']} | {item['status']}")
    credit = diagnostics["credit"]
    lines.append(f"信用传导观察：{credit['status']} | 新鲜覆盖 {credit['coverage']:.0%}（不计入核心指数）。")
    for key, label in (("hy_oas", "HY OAS"), ("ig_oas", "IG OAS"), ("bbb_oas", "BBB OAS")):
        item = credit["series"][key]
        value = "NA" if item["value_bp"] is None else f"{item['value_bp']:.1f}bp"
        change = "NA" if item["change_5_sessions_bp"] is None else f"{item['change_5_sessions_bp']:+.1f}bp"
        lines.append(f"{label}：{value} | 5交易日变化 {change} | 数据日 {item['source_date']} | "
                     f"{item['status']} | 标记 {','.join(item['flags']) or '无已确认标记'}")
    if diagnostics["divergence"] == "FUNDING_IMPROVES_CREDIT_WORSENS":
        lines.append("分歧候选：核心资金条件改善，但信用利差快速走阔；不能只看总分判断环境。")
    elif diagnostics["divergence"] == "FUNDING_IMPROVES_CREDIT_STILL_ELEVATED":
        lines.append("结构提示：核心资金条件改善，但信用利差仍处高位；高位不等于正在恶化。")
    lines.append("信用阈值为未校准的影子观察规则；缺数不等于无风险。分量贡献包含百分位基线变化，不是因果归因。")
    return "\n".join(lines)
