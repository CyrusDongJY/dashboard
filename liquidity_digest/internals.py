"""Descriptive market internals, with no additional score or alert vote."""
from datetime import date, timedelta

from market_internals import (OBSERVATION_VERSION, RECONSTRUCTION_VERSION, PERIODS,
                              SECTORS, eod_time, reconstruct_sectors)
from .model import NY, finite, sessions, stamp


VERSION = "market_internals_v1.1-shadow"
# Display heuristics, not calibrated predictors. Keep them versioned and explicit.
RULES = {"broad_share_pct": 65, "cross_sector_count": 8, "concentration_gap_pp": .5,
         "rotation_excess_pp": .5, "trend_noise_pp": 1.0, "minimum_coverage": .9}
SCOPE_NOTE = "前500大市值股票样本 + 标普500行业ETF，不代表美股全市场。"
SHADOW_NOTE = "描述性影子观察；不新增总分、不重复投票、不触发交易预警。"


def _observed(observation, day, cutoff):
    if not isinstance(observation, dict) or observation.get("source_date") != day:
        return False
    try:
        captured = stamp(observation["captured_at"])
        return (observation.get("session") == "EOD" and captured <= stamp(cutoff)
                and bool(observation.get("source")) and eod_time(captured, day)[1])
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def _breadth(observation, day, cutoff):
    result = {"state": "UNKNOWN", "label": "证据不足", "quality": "UNAVAILABLE",
              "reason": "缺少合格的当日结构化样本；不以旧上涨比例反推平盘数。",
              "sample_size": None, "valid_count": None, "expected_size": 500,
              "advancing": None, "declining": None, "unchanged": None,
              "advancing_pct": None, "declining_pct": None, "unchanged_pct": None,
              "source_date": None, "captured_at": None, "coverage": None}
    if not _observed(observation, day, cutoff):
        return result
    if (observation.get("date_basis") != "CAPTURE_SESSION"
            or str(stamp(observation["captured_at"]).astimezone(NY).date()) != day):
        return result
    result.update({k: observation.get(k) for k in ("sample_size", "valid_count", "coverage",
                   "source_date", "captured_at", "source", "date_basis", "provider_timestamp_status")})
    counts = [finite(observation.get(k)) for k in ("advancing", "declining", "unchanged")]
    valid, sample = finite(observation.get("valid_count")), finite(observation.get("sample_size"))
    if (observation.get("status") != "OK" or observation.get("scope") != "top500_nyse_nasdaq_by_mktcap"
            or observation.get("expected_size") != 500 or observation.get("duplicate_count") != 0
            or any(v is None or v < 0 or not v.is_integer() for v in counts)
            or valid is None or sample is None or not 450 <= valid <= sample <= 500
            or sum(counts) != valid):
        result["reason"] = "有效样本不足90%或计数契约异常；缺失不归入平盘。"
        return result
    for name, value in zip(("advancing", "declining", "unchanged"), counts):
        result[name], result[name + "_pct"] = int(value), value / valid * 100
    result["coverage"] = valid / 500
    if result["advancing_pct"] >= RULES["broad_share_pct"]:
        state, label = "BROAD_UP", "广泛上涨（样本）"
    elif result["declining_pct"] >= RULES["broad_share_pct"]:
        state, label = "BROAD_DOWN", "广泛下跌（样本）"
    else:
        state, label = "MIXED", "涨跌分化（样本）"
    result.update(state=state, label=label, quality="OK",
                  reason="涨跌占比以有效价格样本为分母，包含平盘；与旧pct_adv口径不拼接。")
    return result


def _sectors(observation, day, cutoff):
    eligible = (_observed(observation, day, cutoff) and observation.get("scope") == "sp500_sector_etfs"
                and observation.get("status") in ("OK", "PARTIAL"))
    raw = observation.get("sectors", []) if eligible else []
    raw = raw if isinstance(raw, list) else []
    by_symbol = {r.get("symbol"): r for r in raw if isinstance(r, dict)}
    benchmarks = observation.get("benchmarks", {}) if eligible else {}
    benchmarks = benchmarks if isinstance(benchmarks, dict) else {}

    def returns(row):
        row = row if isinstance(row, dict) else {}
        values, quality = row.get("returns", {}), row.get("window_quality", {})
        if not isinstance(values, dict) or not isinstance(quality, dict):
            return {p: None for p in PERIODS}
        return {p: finite(values.get(p)) if row.get("source_date") == day
                and quality.get(p) == "OK" else None for p in PERIODS}

    base = {s: returns(benchmarks.get(s)) for s in ("SPY", "RSP", "QQQ", "QQQE")}
    rows = []
    for symbol, label in SECTORS.items():
        values = returns(by_symbol.get(symbol))
        excess = {p: round(values[p] - base["SPY"][p], 6) if values[p] is not None
                  and base["SPY"][p] is not None else None for p in PERIODS}
        rows.append({"symbol": symbol, "label": label, "returns": values, "excess": excess})
    daily = [r["returns"]["1d"] for r in rows if r["returns"]["1d"] is not None]
    return {"rows": rows, "benchmarks": base, "valid_count": len(daily), "expected_size": 11,
            "positive_count": sum(v > 0 for v in daily), "negative_count": sum(v < 0 for v in daily),
            "flat_count": sum(v == 0 for v in daily), "source_date": day if eligible else None,
            "captured_at": observation.get("captured_at") if eligible else None,
            "history_kind": observation.get("history_kind", "RECORDED") if eligible else None,
            "quality": "OK" if len(daily) == 11 else "PARTIAL" if daily else "UNAVAILABLE"}


def _leadership(breadth, sectors):
    base = sectors["benchmarks"]
    spreads = {}
    for title, equal, cap in (("RSP-SPY", "RSP", "SPY"), ("QQQE-QQQ", "QQQE", "QQQ")):
        spreads[title] = {p: round(base[equal][p] - base[cap][p], 6)
                          if base[equal][p] is not None and base[cap][p] is not None else None
                          for p in PERIODS}
    result = {"state": "UNKNOWN", "label": "领导结构待确认", "spreads": spreads,
              "reason": "需要同日合格广度、11个行业及SPY/RSP；不把缺失解释为分化。"}
    gap, spy = spreads["RSP-SPY"]["1d"], base["SPY"]["1d"]
    if breadth["quality"] != "OK" or sectors["quality"] != "OK" or gap is None:
        return result
    if spy > 0 and gap <= -RULES["concentration_gap_pp"] and breadth["advancing_pct"] < 60:
        state, label = "NARROW_UP", "权重集中上涨"
    elif breadth["state"] == "BROAD_DOWN" and sectors["negative_count"] >= RULES["cross_sector_count"]:
        state, label = "DECLINE_SPREAD", "下跌跨行业扩散"
    elif breadth["state"] == "BROAD_UP" and sectors["positive_count"] >= RULES["cross_sector_count"]:
        state, label = "ADVANCE_SPREAD", "上涨跨行业扩散"
    elif gap >= RULES["concentration_gap_pp"] and breadth["advancing_pct"] > 50:
        state, label = "EQUAL_WEIGHT_LEADS", "等权相对占优"
    elif sectors["positive_count"] and sectors["negative_count"]:
        state, label = "SECTOR_MIXED", "行业涨跌分化"
    else:
        state, label = "MIXED", "结构混合"
    result.update(state=state, label=label,
                  reason="等权差为相对表现代理，含规模与行业暴露，不是精确权重贡献或资金流。")
    return result


def _rotation(sector_history, grid):
    wanted = grid[-6:]
    result = {"state": "UNKNOWN", "label": "轮动观察：历史不足", "from_sectors": [], "to_sectors": [],
              "reason": "需连续6个交易日的完整行业5日相对表现；不由单日排名推断轮动。"}
    available = [d for d in wanted if d in sector_history
                 and len(sector_history[d]["rows"]) == 11
                 and all(r["excess"]["5d"] is not None for r in sector_history[d]["rows"])]
    rebuilt = [d for d in available if sector_history[d].get("history_kind") == "RECONSTRUCTED"]
    result.update(available_dates=available, reconstructed_dates=rebuilt,
                  history_note=f"完整行业窗口 {len(available)}/6；其中历史价格重建 {len(rebuilt)} 日。"
                  + ("重建仅用于本期观察，不代表当时可得信号。" if rebuilt else ""))
    if len(wanted) < 6 or any(d not in sector_history for d in wanted):
        return result
    ordered = []
    for day in wanted:
        rows = sector_history[day]["rows"]
        if len(rows) != 11 or any(r["excess"]["5d"] is None for r in rows):
            return result
        ordered.append(sorted(rows, key=lambda r: (-r["excess"]["5d"], r["symbol"])))
    result.update(state="UNCONFIRMED", label="未确认行业轮动",
                  reason="未满足多个行业持续接替领涨的影子条件；分化不等于轮动。")
    # A tied third/fourth rank must not manufacture a leadership change.
    if any(round(rows[2]["excess"]["5d"] - rows[3]["excess"]["5d"], 6) < .1
           for rows in (ordered[0], *ordered[-3:])):
        return result
    old = {r["symbol"] for r in ordered[0][:3]}
    persistent = set.intersection(*[{r["symbol"] for r in rows[:3]
                                   if r["excess"]["5d"] >= RULES["rotation_excess_pp"]}
                                  for rows in ordered[-3:]])
    entering, leaving = persistent - old, old - {r["symbol"] for r in ordered[-1][:3]}
    if len(entering) >= 2 and len(leaving) >= 2:
        result.update(state="CANDIDATE", label="出现行业轮动迹象",
                      from_sectors=[SECTORS[s] for s in SECTORS if s in leaving],
                      to_sectors=[SECTORS[s] for s in SECTORS if s in entering],
                      reason="至少2个行业相对5日前进入前三，连续3日领先且5日超额≥0.5个百分点；非资金流确认。")
    return result


def _persistence(metric, grid):
    history = {r["date"]: finite(r["value"]) for r in metric.get("history", [])}
    history = {d: v if v is not None and 0 <= v <= 100 else None for d, v in history.items()}
    result = {"state": "UNKNOWN", "label": "持续性：历史不足", "streak": 0,
              "basis": "站上20日均线比例", "changes": {"5d": None, "21d": None},
              "reason": "需要连续合格观测；缺口断线，不以前值填充。"}
    if (metric.get("status") != "OK" or metric.get("acquisition") != "OK"
            or metric.get("source_date") != grid[-1]):
        return result, history
    valid = lambda ds: all(history.get(d) is not None and 0 <= history[d] <= 100 for d in ds)
    for period, size in (("5d", 5), ("21d", 21)):
        days = grid[-size - 1:]
        if len(days) == size + 1 and valid(days):
            result["changes"][period] = history[days[-1]] - history[days[0]]
    if not valid(grid[-4:]) or len(grid[-4:]) < 4:
        return result, history
    diffs = []
    for i in range(len(grid) - 1, 0, -1):
        if not valid(grid[i - 1:i + 1]):
            break
        diffs.append(history[grid[i]] - history[grid[i - 1]])
    signs = [1 if round(d, 6) >= RULES["trend_noise_pp"] else -1 if round(d, 6) <= -RULES["trend_noise_pp"] else 0 for d in diffs]
    streak = 0
    if signs[0]:
        for sign in signs:
            if sign != signs[0]:
                break
            streak += 1
    if streak >= 3:
        state, label = ("IMPROVING", f"连续{streak}日改善") if signs[0] > 0 else ("DETERIORATING", f"连续{streak}日恶化")
    elif signs[0] * signs[1] == -1 and signs[1] == signs[2]:
        state, label = "TURN_PENDING", "方向转折待确认"
    elif all(s == 0 for s in signs[:3]):
        state, label = "NO_CLEAR_CHANGE", "近期变化不明显"
    else:
        state, label = "MIXED", "近期方向混合"
    result.update(state=state, label=label, streak=streak,
                  reason="以20日均线广度衡量持续性，单日1个百分点为影子噪声线；不代表低风险。")
    return result, history


def build_internals(report_plan, capture, metrics):
    end, cutoff = report_plan["market_date"], report_plan["cutoff"]
    grid = sessions(date.fromisoformat(end) - timedelta(days=70), end)
    accepted, rejections, price_vintages = {}, 0, []
    try:
        capture_ok = capture.get("status") == "OK" and stamp(capture["captured_at"]) <= stamp(cutoff)
    except (KeyError, ValueError, TypeError):
        capture_ok = False
    for row in capture.get("data", []) if capture_ok else []:
        day = row.get("record_date")
        full = row.get("full_metrics")
        observation = full.get("market_internals") if isinstance(full, dict) else None
        recovery = full.get("sector_history_reconstruction") if isinstance(full, dict) else None
        if day in grid and isinstance(recovery, dict) and recovery.get("version") == RECONSTRUCTION_VERSION:
            try:
                if (stamp(row["ingested_at"]) <= stamp(cutoff)
                        and stamp(recovery["computed_at"]) <= stamp(cutoff)
                        and recovery.get("point_in_time_eligible") is False
                        and recovery["sectors"]["source_date"] == day):
                    price_vintages.append(recovery["sectors"])
            except (KeyError, TypeError, ValueError):
                rejections += 1
        if day not in grid or not isinstance(observation, dict) or observation.get("version") != OBSERVATION_VERSION:
            continue
        try:
            times = [stamp(row["ingested_at"]), stamp(observation["computed_at"])]
            if any(t > stamp(cutoff) for t in times):
                raise ValueError("After cutoff")
            if day not in accepted or times[0] > accepted[day][0]:
                accepted[day] = (times[0], observation)
        except (KeyError, TypeError, ValueError):
            rejections += 1
    breadth_history, sector_history = {}, {}
    for day, (_, observation) in sorted(accepted.items()):
        breadth_history[day] = _breadth(observation.get("breadth"), day, cutoff)
        sector_history[day] = _sectors(observation.get("sectors"), day, cutoff)
        raw_sector = observation.get("sectors")
        if _observed(raw_sector, day, cutoff):
            price_vintages.append(raw_sector)
    # Current-report reconstruction never changes archived observations or breadth.
    for vintage in sorted(price_vintages, key=lambda v: str(v.get("captured_at", ""))):
        for day, rebuilt in reconstruct_sectors(vintage, cutoff).items():
            if day not in grid:
                continue
            current = sector_history.get(day)
            if current and current.get("history_kind") == "RECORDED":
                continue
            sector_history[day] = _sectors(rebuilt, day, cutoff)
    breadth = breadth_history.get(end) or _breadth(None, end, cutoff)
    sectors = sector_history.get(end) or _sectors(None, end, cutoff)
    leadership = _leadership(breadth, sectors)
    persistence, ma_history = _persistence(metrics.get("pct_20ma", {}), grid)
    rotation = _rotation(sector_history, grid)
    interpretation = "参与广度与行业方向需分开观察；行业相对强弱不能解释为净资金流。"
    if breadth["state"] == "UNKNOWN" or leadership["state"] == "UNKNOWN":
        interpretation = "市场内部证据尚未齐备；保留可用分项，不作确定性结构判断。"
    elif breadth["state"] == "BROAD_UP" and persistence["state"] == "DETERIORATING":
        interpretation = "当日上涨范围较广，但20日均线广度持续走弱；单日上涨尚未扭转参与趋势。"
    elif breadth["state"] == "BROAD_DOWN" and persistence["state"] == "IMPROVING":
        interpretation = "当日下跌范围较广，但20日均线广度仍在改善；两个窗口存在分歧，暂不确认反转。"
    elif breadth["state"] == "BROAD_DOWN" and persistence["state"] == "DETERIORATING":
        interpretation = "当日下跌扩散与20日均线广度恶化同向，内部压力呈延续迹象。"
    elif breadth["state"] == "BROAD_UP" and persistence["state"] == "IMPROVING":
        interpretation = "当日上涨扩散与20日均线广度改善同向，但不能据此认定资金条件宽松。"
    elif leadership["state"] == "NARROW_UP":
        interpretation = "指数上涨与市值加权相对占优并存，尚未获得多数股票参与确认。"
    gaps = []
    if breadth["quality"] != "OK":
        gaps.append(breadth["reason"])
    if sectors["quality"] != "OK":
        gaps.append(f'行业当日有效{sectors["valid_count"]}/11；旧报告的行业文字不作为原始数据回填。')
    if persistence["state"] == "UNKNOWN":
        gaps.append(persistence["reason"])
    if rotation["state"] == "UNKNOWN":
        gaps.append(rotation["reason"])
    history = [{"date": day,
                "advancing_pct": breadth_history.get(day, {}).get("advancing_pct")
                if breadth_history.get(day, {}).get("quality") == "OK" else None,
                "pct_20ma": ma_history.get(day)} for day in grid[-21:]]
    return {"version": VERSION, "rules": dict(RULES), "market_date": end,
            "headline": breadth["label"] + "｜" + leadership["label"],
            "scope_note": SCOPE_NOTE, "shadow_note": SHADOW_NOTE, "interpretation": interpretation,
            "breadth": breadth, "sectors": sectors, "leadership": leadership,
            "rotation": rotation, "persistence": persistence, "history": history, "gaps": gaps,
            "audit": {"observation_version": OBSERVATION_VERSION, "accepted_dates": sorted(accepted),
                      "rejected_rows": rejections, "capture_status": capture.get("status", "MISSING")}}
