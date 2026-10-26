"""Point-in-time report assembly. Missing observations never imply low risk."""
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from copy import deepcopy
import math
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import AbstractHolidayCalendar, USFederalHolidayCalendar, nearest_workday, sunday_to_monday
import pandas_market_calendars as mcal

from . import VERSION

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# key: Chinese label, unit, native frequency
SPECS = {
    "fed_assets": ("美联储总资产", "十亿美元", "weekly"),
    "tga_weekly": ("财政部账户（周三）", "十亿美元", "weekly"),
    "rrp": ("隔夜逆回购余额", "十亿美元", "daily"),
    "reserves": ("银行准备金（周均）", "十亿美元", "weekly"),
    "tga_daily": ("财政部账户（日终）", "十亿美元", "daily"),
    "net_liquidity": ("同日净流动性代理", "十亿美元", "weekly"),
    "sofr": ("SOFR", "%", "daily"),
    "effr": ("EFFR", "%", "daily"),
    "iorb": ("IORB", "%", "daily"),
    "sofr99": ("SOFR 第99百分位", "%", "daily"),
    "sofr_iorb": ("SOFR - IORB", "基点", "daily"),
    "effr_iorb": ("EFFR - IORB", "基点", "daily"),
    "sofr_tail": ("SOFR 尾部利差", "基点", "daily"),
    "ig": ("投资级信用利差", "基点", "daily"),
    "hy": ("高收益信用利差", "基点", "daily"),
    "ccc": ("CCC及以下信用利差", "基点", "daily"),
    "ust2": ("美国2年期国债收益率", "%", "daily"),
    "real10": ("美国10年期实际收益率", "%", "daily"),
    "vix": ("VIX", "点", "daily"),
    "vix_ratio_contango_pct": ("VIX期限结构余量", "%", "daily"),
    "dxy": ("美元指数", "点", "daily"),
    "usdjpy_return_5d_pct": ("美元兑日元5日变化", "%", "daily"),
    "pct_200ma": ("站上200日均线比例", "%", "daily"),
    "pct_20ma": ("站上20日均线比例", "%", "daily"),
    "pct_50ma": ("站上50日均线比例", "%", "daily"),
    "spy_rsp_spread_pct": ("SPY相对等权表现差", "%", "daily"),
    "spy_vwap_time_acceptance_pct": ("SPY均价线上方时间占比", "%", "daily"),
    "qqq_vwap_time_acceptance_pct": ("QQQ均价线上方时间占比", "%", "daily"),
}
DB_METRICS = (
    "vix", "vix_ratio_contango_pct", "dxy", "usdjpy_return_5d_pct", "pct_200ma", "pct_20ma", "pct_50ma",
    "spy_rsp_spread_pct", "spy_vwap_time_acceptance_pct", "qqq_vwap_time_acceptance_pct",
)
STATUS_CN = {
    "OK": "已取得", "AS_OF": "错期参考，非昨日同步", "LAGGED": "观测滞后", "STALE": "已过期",
    "MISSING": "尚无可用数据", "FETCH_ERROR": "本次抓取失败",
    "NOT_CONFIGURED": "数据源未配置", "CACHED": "旧抓取缓存",
    "CACHED_AFTER_ERROR": "今晨缓存／刷新失败",
    "EMPTY": "源未返回数据", "DEADLINE": "截止前未完成",
}


def stamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return parsed.astimezone(UTC)


@lru_cache(maxsize=512)
def sessions(start, end):
    # Calendar failure must stop the job; never replace NYSE with weekdays.
    schedule = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)
    return [str(index.date()) for index in schedule.index]


class FederalReserveCalendar(AbstractHolidayCalendar):
    rules = deepcopy(USFederalHolidayCalendar.rules)
    # Reserve Banks remain open on Friday when a fixed holiday falls on Saturday.
    for rule in rules:
        if rule.observance is nearest_workday:
            rule.observance = sunday_to_monday


@lru_cache(maxsize=512)
def native_sessions(key, start, end):
    if key in ("sofr", "sofr99", "sofr_iorb", "sofr_tail", "rrp", "ust2", "real10"):
        return mcal.get_calendar("SIFMA_US").valid_days(start, end).strftime("%Y-%m-%d").tolist()
    if key in ("effr", "effr_iorb", "iorb", "tga_daily"):
        cal = USFederalHolidayCalendar() if key == "tga_daily" else FederalReserveCalendar()
        return pd.date_range(start, end, freq=pd.offsets.CustomBusinessDay(calendar=cal)).strftime("%Y-%m-%d").tolist()
    return sessions(start, end)


def expected_daily_date(key, report_plan):
    cutoff = stamp(report_plan["cutoff"]).astimezone(NY)
    start = cutoff.date() - timedelta(days=40)
    end = str(cutoff.date() - timedelta(days=1)) if key not in DB_METRICS else report_plan["market_date"]
    days = native_sessions(key, start, end)
    if key == "tga_daily":
        # DTS is due at 16:00 on the next federal business day, not on trade date.
        releases = native_sessions(key, start, cutoff.date())
        due = [d for d in releases if datetime.combine(date.fromisoformat(d), time(16), NY) <= cutoff]
        latest_release = due[-1]
        return max(d for d in days if d < latest_release)
    return days[-1]


AS_OF_KEYS = {"ig", "hy", "ccc", "ust2", "real10"}


def prior_publication_date(key, report_plan):
    end = expected_daily_date(key, report_plan)
    return native_sessions(key, date.fromisoformat(end) - timedelta(days=40), end)[-2]


def plan(day):
    day = date.fromisoformat(str(day))
    days = sessions(day - timedelta(days=40), day)
    if str(day) not in days:
        raise ValueError("Not an NYSE trading day")
    return {
        "report_date": str(day), "market_date": days[-2],
        "cutoff": datetime.combine(day, time(9, 15), NY).isoformat(),
        "send_at": datetime.combine(day, time(9, 20), NY).isoformat(),
    }


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None and math.isfinite(float(value)) else None
    except (TypeError, ValueError, OverflowError):
        return None


def clean_points(points, end):
    result = {}
    for row in points:
        try:
            d = str(date.fromisoformat(row["date"]))
            v = finite(row["value"])
            if d <= end and v is not None:
                result[d] = v
        except (KeyError, ValueError, TypeError):
            continue
    return [{"date": d, "value": v} for d, v in sorted(result.items())]


def aligned(points_a, points_b, scale=1):
    other = {p["date"]: p["value"] for p in points_b}
    return [{"date": p["date"], "value": (p["value"] - other[p["date"]]) * scale}
            for p in points_a if p["date"] in other]


def summarize(key, points, report_plan, acquisition="OK", source="", fetched_at=None):
    label, unit, frequency = SPECS[key]
    end = (report_plan["market_date"] if key in DB_METRICS else
           str(stamp(report_plan["cutoff"]).astimezone(NY).date() - timedelta(days=1)))
    points = clean_points(points, end)
    result = {
        "key": key, "label": label, "unit": unit, "frequency": frequency,
        "value": None, "source_date": None, "status": "MISSING",
        "acquisition": acquisition, "source": source, "fetched_at": fetched_at,
        "expected_date": None, "publication_note": "",
        "changes": {}, "history": points, "trend": "样本不足",
    }
    if not points:
        return result
    last = points[-1]
    result.update(value=last["value"], source_date=last["date"])
    age = (date.fromisoformat(report_plan["market_date"]) - date.fromisoformat(last["date"])).days
    if frequency == "weekly":
        cutoff = stamp(report_plan["cutoff"]).astimezone(NY)
        wednesday = cutoff.date() - timedelta(days=(cutoff.weekday() - 2) % 7)
        release = datetime.combine(wednesday + timedelta(days=1), time(16, 30), NY)
        if cutoff < release:
            wednesday -= timedelta(days=7)
        result["status"] = "OK" if last["date"] >= str(wednesday) else "LAGGED" if age <= 17 else "STALE"
    else:
        expected = expected_daily_date(key, report_plan)
        result["expected_date"] = expected
        result["status"] = "OK" if last["date"] >= expected else "LAGGED" if age <= 5 else "STALE"
        if key == "tga_daily" and result["status"] == "OK" and last["date"] < report_plan["market_date"]:
            result["publication_note"] = "按DTS发布节奏；次业务日16:00前更新"
        elif key in AS_OF_KEYS and last["date"] == prior_publication_date(key, report_plan):
            result["status"] = "AS_OF"
            result["publication_note"] = "截止前所获已发布值；仅限落后1个原生业务日，不代表昨日收盘"
    values = {p["date"]: p["value"] for p in points}
    if frequency == "weekly":
        # Compare actual weekly dates, not padded daily copies or arbitrary row offsets.
        for name, days in (("1周", 7), ("4周", 28)):
            prior = str(date.fromisoformat(last["date"]) - timedelta(days=days))
            result["changes"][name] = last["value"] - values[prior] if prior in values else None
        wanted = [str(date.fromisoformat(last["date"]) - timedelta(days=7 * i))
                  for i in (3, 2, 1, 0)]
    else:
        calendar = native_sessions(key, date.fromisoformat(last["date"]) - timedelta(days=70), last["date"])
        for name, count in (("1日", 1), ("5日", 5), ("21日", 21)):
            prior = calendar[-count - 1] if len(calendar) > count else None
            result["changes"][name] = last["value"] - values[prior] if prior in values else None
        wanted = calendar[-4:]
    recent = [values.get(d) for d in wanted]
    if len(recent) == 4 and all(v is not None for v in recent):
        # Match display precision; sub-display noise must not create a reversal headline.
        diffs = [round(b - a, 2) for a, b in zip(recent, recent[1:])]
        if all(d == 0 for d in diffs):
            trend = "连续持平"
        elif all(d > 0 for d in diffs):
            trend = "连续上行"
        elif all(d < 0 for d in diffs):
            trend = "连续下行"
        elif diffs[-1] * diffs[-2] < 0 and diffs[-2] * diffs[-3] > 0:
            trend = "方向转折待确认"
        else:
            trend = "方向混合"
        result["trend"] = trend
    return result


def valid_rows(rows, date_field, end, cutoff):
    result = []
    for row in rows:
        d = row.get(date_field)
        if not d or d > end:
            continue
        times = [row.get(k) for k in ("computed_at", "created_at", "ingested_at", "stress_computed_at")]
        try:
            if any(stamp(t) > stamp(cutoff) for t in times if t):
                continue
        except (ValueError, TypeError):
            continue
        result.append(row)
    return result


def assemble(report_plan, inputs, demo=False):
    metrics = {}
    for key in SPECS:
        if key in DB_METRICS or key in ("net_liquidity", "sofr_iorb", "effr_iorb", "sofr_tail"):
            continue
        capture = inputs.get(key, {})
        metrics[key] = summarize(
            key, capture.get("data", []), report_plan, capture.get("status", "MISSING"),
            capture.get("source", ""), capture.get("captured_at"))

    rows = valid_rows(inputs.get("db_metric_daily", {}).get("data", []),
                      "report_date", report_plan["market_date"], report_plan["cutoff"])
    for key in DB_METRICS:
        candidates = [r for r in rows if r.get("metric") == key and r.get("scope") == "MACRO"
                      and r.get("session") == "EOD" and r.get("is_final") is True
                      and r.get("is_filled") is False and r.get("source_date") == r["report_date"]
                      and r.get("calc_version")]
        version = max(candidates, key=lambda r: r["report_date"])["calc_version"] if candidates else None
        points = [{"date": r["report_date"], "value": r.get("value")}
                  for r in candidates if r["calc_version"] == version]
        capture = inputs.get("db_metric_daily", {})
        metrics[key] = summarize(key, points, report_plan, capture.get("status", "MISSING"),
                                 "metric_daily / " + str(version or "无版本"), capture.get("captured_at"))

    # Only unchanged raw price definitions may bridge a failed snapshot write.
    # Recovered values are observed now, never backdated into an archived report.
    raw_capture = inputs.get("db_market_history", {})
    raw_rows = valid_rows(raw_capture.get("data", []), "record_date", report_plan["market_date"], report_plan["cutoff"])
    for key in ("vix", "dxy"):
        if metrics[key]["status"] == "OK" or raw_capture.get("status") != "OK":
            continue
        for row in raw_rows:
            full = row.get("full_metrics")
            lineages = full.get("market_metric_lineage", {}) if isinstance(full, dict) else {}
            lineage = lineages.get(key, {}) if isinstance(lineages, dict) else {}
            value = finite(row.get(key))
            observed_at = row.get("ingested_at")
            if (not isinstance(lineage, dict) or row["record_date"] != report_plan["market_date"]
                    or value is None or value <= 0 or lineage.get("source_date") != row["record_date"]
                    or not lineage.get("source_name") or not observed_at):
                continue
            times = [observed_at, lineage.get("reconstructed_at"), lineage.get("restored_at")]
            try:
                if any(stamp(t) > stamp(report_plan["cutoff"]) for t in times if t):
                    continue
            except (ValueError, TypeError):
                continue
            history = metrics[key]["history"] + [{"date": row["record_date"], "value": value}]
            source = metrics[key]["source"] + "；当日原始值：" + lineage["source_name"]
            metrics[key] = summarize(key, history, report_plan, "OK", source, raw_capture.get("captured_at"))
            metrics[key]["publication_note"] = "原始观测补缺；不恢复当时评分"
            metrics[key]["raw_fallback"] = {"source_date": row["record_date"], "ingested_at": observed_at,
                                           "lineage": lineage}
            break

    for key, a, b, multiplier in (
        ("sofr_iorb", "sofr", "iorb", 100), ("effr_iorb", "effr", "iorb", 100),
        ("sofr_tail", "sofr99", "sofr", 100),
        ("net_liquidity", "fed_assets", "tga_weekly", 1),
    ):
        parts = [a, b] + (["rrp"] if key == "net_liquidity" else [])
        points = aligned(metrics[a]["history"], metrics[b]["history"], multiplier)
        if key == "net_liquidity":
            points = aligned(points, metrics["rrp"]["history"])
        acquisitions = [metrics[p]["acquisition"] for p in parts]
        acquisition = next((v for v in acquisitions if v != "OK"), "OK")
        times = [metrics[p]["fetched_at"] for p in parts if metrics[p]["fetched_at"]]
        metrics[key] = summarize(key, points, report_plan, acquisition,
                                 "同观测日对齐：" + " / ".join(parts),
                                 max(times, key=stamp) if len(times) == len(parts) else None)

    panels = {}
    table_fields = {
        "tactical": ("market_history", "record_date"),
        "risk_capital": ("risk_capital_daily", "report_date"),
        "event_pulse": ("risk_event_pulse_daily", "report_date"),
        "review": ("daily_review", "report_date"),
    }
    for key, (table, field) in table_fields.items():
        capture = inputs.get("db_" + table, {})
        candidates = valid_rows(capture.get("data", []), field,
                                report_plan["market_date"], report_plan["cutoff"])
        panels[key] = next((r for r in candidates if r[field] == report_plan["market_date"]), None)
        if capture.get("status") != "OK":
            panels[key] = None
    temporal = valid_rows(inputs.get("db_temporal_state_daily", {}).get("data", []),
                          "report_date", report_plan["market_date"], report_plan["cutoff"])
    temporal = [r for r in temporal if r.get("observation_mode") == "LIVE_SHADOW"
                and r.get("quality_status") == "OK" and finite(r.get("risk_value")) is not None
                and r.get("source_date") == r.get("report_date") and r.get("calc_version")]
    history = {}
    def source_version(row):
        details = row.get("details")
        return details.get("source_calc_version") if isinstance(details, dict) else None
    for name in ("environment", "liquidity", "tactical_stress", "risk_capital", "event_pulse"):
        records = sorted([r for r in temporal if r.get("panel") == name], key=lambda r: r["report_date"])
        version = records[-1]["calc_version"] if records else None
        source = source_version(records[-1]) if records else None
        history[name] = [r for r in records if r["calc_version"] == version and source_version(r) == source]
    panels["tactical_temporal"] = next(
        (r for r in history["tactical_stress"] if r["report_date"] == report_plan["market_date"]), None)
    # Prioritize evidence, not a newly invented total score.
    watch = []
    for key in ("sofr_iorb", "sofr_tail", "hy", "ccc", "net_liquidity",
                "usdjpy_return_5d_pct", "pct_200ma"):
        metric = metrics[key]
        if metric["status"] == "OK" and metric["acquisition"] == "OK" and (
                metric["trend"] in ("连续上行", "连续下行", "方向转折待确认")):
            watch.append(f'{metric["label"]}：{metric["trend"]}，截至 {metric["source_date"]}。')
    gaps = [m["label"] for m in metrics.values()
            if m["status"] not in ("OK", "AS_OF") or m["acquisition"] != "OK"]
    report = {
        "version": VERSION, **report_plan, "demo": demo, "metrics": metrics,
        "panels": panels, "temporal": history, "focus": watch[:3],
        "gaps": gaps,
        "publication_lags": [m["label"] + "截至" + m["source_date"]
                             for m in metrics.values() if m["status"] == "AS_OF"],
        "source_audit": {k: {p: v.get(p) for p in ("status", "source", "captured_at", "attempt_at")}
                         for k, v in inputs.items()},
    }
    from .states import build_states
    report["states"] = build_states(report)
    from .domains import build_domains
    report["domains"] = build_domains(report)
    from .internals import build_internals
    report["internals"] = build_internals(report_plan, inputs.get("db_market_history", {}), metrics)
    from .frontpage import build_frontpage
    report["frontpage"] = build_frontpage(report, inputs)
    from .capital import build_capital
    report["capital"] = build_capital(report, inputs)
    return report
