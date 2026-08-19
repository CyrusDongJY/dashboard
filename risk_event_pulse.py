"""Dynamic EOD event pulse for unusual US stock moves.

The pulse complements the fixed risk-capital ladder. It discovers changing
leaders each day, but an isolated company event never becomes a market-level
risk-appetite confirmation on its own.
"""
from datetime import datetime, timezone
import math

from risk_capital_ladder import (
    ALL_MEMBERS,
    BENCHMARK,
    benchmark_snapshot,
    compute_symbol_observation,
)


CALC_VERSION = "risk_event_pulse_v1.0-shadow"
SCAN_CODES = ("TOP_PERC_GAIN", "TOP_PERC_LOSE", "HOT_BY_VOLUME")
REQUIRED_SCAN_CODES = ("TOP_PERC_GAIN", "TOP_PERC_LOSE")
MAX_PER_SCAN = 8
MAX_CANDIDATES = 18
MIN_ABS_RETURN_PCT = 5.0
MIN_RELATIVE_RETURN_PCT = 3.0
MIN_TURNOVER_Z = 1.0
EXTREME_RETURN_PCT = 20.0
EXTREME_TURNOVER_Z = 5.0

STATE_LABELS = {
    "RISK_ON_DIFFUSION": "跨行业风险偏好扩散",
    "RISK_OFF_CONTAGION": "跨行业风险撤退",
    "TWO_WAY_SPECULATION": "双向高波动投机",
    "THEME_RISK_ON": "单一主题风险扩散",
    "THEME_RISK_OFF": "单一主题风险撤退",
    "ISOLATED_EVENT_SPIKE": "孤立事件型暴涨",
    "ISOLATED_EVENT_CRASH": "孤立事件型暴跌",
    "MIXED": "异动结构分化",
    "QUIET": "未形成有效异动脉冲",
    "UNAVAILABLE": "动态扫描数据不足",
}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ticker(value):
    symbol = str(value or "").strip().upper()
    return symbol if symbol and symbol.replace(".", "").isalnum() else None


def merge_scan_candidates(scan_results, core_members=ALL_MEMBERS,
                          max_per_scan=MAX_PER_SCAN,
                          max_candidates=MAX_CANDIDATES):
    """Merge scanner lists with per-scan quotas and remove fixed members."""
    excluded = set(core_members) | {BENCHMARK}
    merged = {}
    for scan_code in SCAN_CODES:
        rows = sorted(
            scan_results.get(scan_code) or [],
            key=lambda item: int(item.get("scanner_rank", 10_000)),
        )[:max_per_scan]
        for raw in rows:
            symbol = _ticker(raw.get("ticker"))
            if not symbol or symbol in excluded:
                continue
            currency = str(raw.get("currency") or "USD").upper()
            sec_type = str(raw.get("sec_type") or "STK").upper()
            if currency != "USD" or sec_type != "STK":
                continue
            item = merged.setdefault(symbol, {
                "ticker": symbol,
                "scanner_rank": int(raw.get("scanner_rank", 10_000)),
                "scan_codes": [],
                "industry": raw.get("industry"),
                "category": raw.get("category"),
                "subcategory": raw.get("subcategory"),
                "primary_exchange": raw.get("primary_exchange"),
                "currency": currency,
                "sec_type": sec_type,
            })
            item["scanner_rank"] = min(
                item["scanner_rank"], int(raw.get("scanner_rank", 10_000)))
            if scan_code not in item["scan_codes"]:
                item["scan_codes"].append(scan_code)
            for key in ("industry", "category", "subcategory",
                        "primary_exchange"):
                if not item.get(key) and raw.get(key):
                    item[key] = raw[key]

    candidates = list(merged.values())
    candidates.sort(key=lambda item: (
        item["scanner_rank"], -len(item["scan_codes"]), item["ticker"]))
    return candidates[:max_candidates]


def _industry_group(row):
    for key in ("industry", "category", "subcategory"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "UNKNOWN"


def _known_groups(rows):
    return sorted({row["industry_group"] for row in rows
                   if row.get("industry_group") != "UNKNOWN"})


def _direction(row):
    if not row.get("decision_eligible"):
        return "NONE"
    ret_1d = _finite(row.get("return_1d_pct"))
    relative_1d = _finite(row.get("relative_1d_pct"))
    turnover_z = _finite(row.get("abnormal_turnover_z60"))
    if None in (ret_1d, relative_1d, turnover_z):
        return "NONE"
    if turnover_z < MIN_TURNOVER_Z:
        return "NORMAL"
    if ret_1d >= MIN_ABS_RETURN_PCT and relative_1d >= MIN_RELATIVE_RETURN_PCT:
        return "RISK_ON"
    if ret_1d <= -MIN_ABS_RETURN_PCT and relative_1d <= -MIN_RELATIVE_RETURN_PCT:
        return "RISK_OFF"
    return "NORMAL"


def _candidate_row(histories, benchmark, discovery, report_date, source_name):
    symbol = discovery["ticker"]
    row = compute_symbol_observation(
        histories.get(symbol), benchmark, report_date, symbol, "dynamic",
        source_name=source_name, calc_version=CALC_VERSION)
    row.update({
        "scan_codes": list(discovery.get("scan_codes") or []),
        "scanner_rank": int(discovery.get("scanner_rank", 10_000)),
        "industry": discovery.get("industry"),
        "category": discovery.get("category"),
        "subcategory": discovery.get("subcategory"),
        "primary_exchange": discovery.get("primary_exchange"),
    })
    row["industry_group"] = _industry_group(row)
    row["pulse_direction"] = _direction(row)
    ret_1d = _finite(row.get("return_1d_pct"))
    turnover_z = _finite(row.get("abnormal_turnover_z60"))
    row["single_name_extreme"] = bool(
        row.get("decision_eligible") and (
            (ret_1d is not None and abs(ret_1d) >= EXTREME_RETURN_PCT) or
            (ret_1d is not None and abs(ret_1d) >= MIN_ABS_RETURN_PCT and
             turnover_z is not None and turnover_z >= EXTREME_TURNOVER_Z)))
    row["catalyst_status"] = "UNVERIFIED"
    return row


def aggregate_event_pulse(candidate_rows, benchmark, report_date,
                          discovered_count=None, scans_completed=None,
                          source_name="IBKR_SCANNER+TRADES:1day:RTH"):
    scans_completed = sorted(set(scans_completed or []))
    discovered_count = (len(candidate_rows) if discovered_count is None
                        else int(discovered_count))
    eligible = [row for row in candidate_rows
                if row.get("decision_eligible")]
    risk_on = [row for row in eligible
               if row.get("pulse_direction") == "RISK_ON"]
    risk_off = [row for row in eligible
                if row.get("pulse_direction") == "RISK_OFF"]
    extreme_up = [row for row in eligible
                  if row.get("single_name_extreme") and
                  (_finite(row.get("return_1d_pct")) or 0) > 0]
    extreme_down = [row for row in eligible
                    if row.get("single_name_extreme") and
                    (_finite(row.get("return_1d_pct")) or 0) < 0]
    positive_groups = _known_groups(risk_on)
    negative_groups = _known_groups(risk_off)
    scan_ready = all(code in scans_completed for code in REQUIRED_SCAN_CODES)
    ready = scan_ready and discovered_count >= 3 and len(eligible) >= 3
    positive_confirmed = len(risk_on) >= 3 and len(positive_groups) >= 2
    negative_confirmed = len(risk_off) >= 3 and len(negative_groups) >= 2

    state = "UNAVAILABLE"
    detail = "涨幅榜与跌幅榜扫描或历史验证覆盖不足"
    market_confirmation = False
    if ready:
        if positive_confirmed and negative_confirmed:
            state = "TWO_WAY_SPECULATION"
            detail = "涨跌两端均有跨行业放量异动，方向分歧但投机活跃度很高"
        elif positive_confirmed:
            state = "RISK_ON_DIFFUSION"
            detail = "至少3只跨两个行业放量跑赢QQQ，风险偏好扩散获得确认"
            market_confirmation = True
        elif negative_confirmed:
            state = "RISK_OFF_CONTAGION"
            detail = "至少3只跨两个行业放量跑输QQQ，风险撤退获得确认"
            market_confirmation = True
        elif len(risk_on) >= 3 and len(positive_groups) == 1:
            state = "THEME_RISK_ON"
            detail = "放量上涨集中在单一行业，只确认主题扩散，不升级为市场信号"
        elif len(risk_off) >= 3 and len(negative_groups) == 1:
            state = "THEME_RISK_OFF"
            detail = "放量下跌集中在单一行业，只确认主题撤退，不升级为市场信号"
        elif extreme_up:
            state = "ISOLATED_EVENT_SPIKE"
            detail = "存在极端上涨个股，但缺少跨行业跟随，按孤立事件处理"
        elif extreme_down:
            state = "ISOLATED_EVENT_CRASH"
            detail = "存在极端下跌个股，但缺少跨行业传染，按孤立事件处理"
        elif risk_on or risk_off:
            state = "MIXED"
            detail = "存在经价量验证的异动，但数量或行业广度不足"
        else:
            state = "QUIET"
            detail = "扫描候选未同时满足绝对涨跌、相对QQQ和异常成交门槛"

    coverage = (len(eligible) / discovered_count
                if discovered_count else 0.0)
    scan_coverage = len(scans_completed) / len(SCAN_CODES)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "report_date": str(report_date),
        "state": state,
        "state_cn": STATE_LABELS[state],
        "state_detail": detail,
        "market_confirmation": market_confirmation,
        "candidate_count": discovered_count,
        "eligible_count": len(eligible),
        "candidate_coverage": round(coverage, 4),
        "scan_coverage": round(scan_coverage, 4),
        "scans_completed": scans_completed,
        "risk_on_tickers": [row["ticker"] for row in risk_on],
        "risk_off_tickers": [row["ticker"] for row in risk_off],
        "extreme_tickers": [row["ticker"] for row in extreme_up + extreme_down],
        "positive_industries": positive_groups,
        "negative_industries": negative_groups,
        "qqq_return_1d_pct": benchmark.get("return_1d_pct"),
        "source_date": benchmark.get("source_date"),
        "source_name": source_name,
        "calc_version": CALC_VERSION,
        "shadow_mode": True,
        "computed_at": now,
        "candidates": candidate_rows,
    }


def compute_event_pulse(histories, discoveries, report_date,
                        scans_completed=None,
                        source_name="IBKR_SCANNER+TRADES:1day:RTH"):
    benchmark = benchmark_snapshot(histories.get(BENCHMARK), report_date)
    rows = [_candidate_row(
        histories, benchmark, discovery, report_date, source_name)
        for discovery in discoveries]
    return aggregate_event_pulse(
        rows, benchmark, report_date, discovered_count=len(discoveries),
        scans_completed=scans_completed, source_name=source_name)


def persistence_payloads(result):
    summary = {key: value for key, value in result.items()
               if key != "candidates"}
    now = datetime.now(timezone.utc).isoformat()
    candidates = []
    for candidate in result.get("candidates", []):
        payload = dict(candidate)
        payload["created_at"] = now
        candidates.append(payload)
    summary["created_at"] = now
    return summary, candidates


def fetch_event_pulse(supabase, report_date):
    fallback = {
        "report_date": str(report_date),
        "state": "UNAVAILABLE",
        "state_cn": STATE_LABELS["UNAVAILABLE"],
        "state_detail": "动态异动脉冲尚未入库",
        "market_confirmation": False,
        "candidate_count": 0,
        "eligible_count": 0,
        "candidate_coverage": 0.0,
        "scan_coverage": 0.0,
        "candidates": [],
        "calc_version": CALC_VERSION,
        "shadow_mode": True,
    }
    try:
        summary_response = (supabase.table("risk_event_pulse_daily")
                            .select("*").eq("report_date", report_date)
                            .limit(1).execute())
        candidate_response = (supabase.table("risk_event_candidate_daily")
                              .select("*").eq("report_date", report_date)
                              .order("scanner_rank").execute())
        if not summary_response.data:
            return fallback
        result = dict(summary_response.data[0])
        result["candidates"] = list(candidate_response.data or [])
        return result
    except Exception:
        return fallback


def _fmt(value, digits=1, suffix=""):
    number = _finite(value)
    return "NA" if number is None else f"{number:.{digits}f}{suffix}"


def format_event_pulse_summary(result):
    result = result or {}
    lines = [
        "=== 当日动态异动脉冲（影子观察，不触发预警） ===",
        f"状态：{result.get('state_cn') or STATE_LABELS['UNAVAILABLE']}",
        f"说明：{result.get('state_detail') or '暂无'}",
        "市场级确认：" + ("是" if result.get("market_confirmation") else "否"),
        (f"扫描完整性：{_fmt((_finite(result.get('scan_coverage')) or 0) * 100, 0, '%')} | "
         f"历史验证：{result.get('eligible_count', 0)}/{result.get('candidate_count', 0)} | "
         f"版本：{result.get('calc_version') or CALC_VERSION}"),
        "放量上涨：" + ("、".join(result.get("risk_on_tickers") or []) or "无"),
        "放量下跌：" + ("、".join(result.get("risk_off_tickers") or []) or "无"),
        "极端单股事件候选：" + ("、".join(result.get("extreme_tickers") or []) or "无"),
        "重点异动（仅依据价量，新闻催化状态均为未核验）：",
    ]
    candidates = list(result.get("candidates") or [])
    candidates.sort(key=lambda row: abs(_finite(row.get("return_1d_pct")) or 0),
                    reverse=True)
    shown = [row for row in candidates
             if row.get("pulse_direction") in ("RISK_ON", "RISK_OFF") or
             row.get("single_name_extreme")][:8]
    if not shown:
        lines.append("- 无满足价量联合门槛的动态异动")
    for row in shown:
        tag = "极端事件" if row.get("single_name_extreme") else row.get(
            "pulse_direction", "NORMAL")
        lines.append(
            f"- {row.get('ticker')}: {_fmt(row.get('return_1d_pct'), 2, '%')} | "
            f"相对QQQ {_fmt(row.get('relative_1d_pct'), 2, '%')} | "
            f"量比 {_fmt(row.get('turnover_ratio_20d'), 2)}x | "
            f"稳健z {_fmt(row.get('abnormal_turnover_z60'), 2)} | "
            f"{row.get('industry_group') or 'UNKNOWN'} | {tag}")
    lines.append(
        "口径：动态榜单存在选择偏差；单只事件股只作情绪脉冲，至少3只、"
        "跨2个行业的同向价量确认才升级为市场扩散/撤退证据。")
    return "\n".join(lines)
