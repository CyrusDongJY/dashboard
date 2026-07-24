# -*- coding: utf-8 -*-
"""环境指数影子模型。

指数仅用于观察和回测，不进入 ALERT_GATE，也不产生交易指令。每个分量按
有效样本数与数据滞后加权；覆盖不足时明确返回“数据不足”，绝不默认健康。
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from anomaly_engine import BASELINE, METRIC_REGISTRY, RESONANCE_THEMES

SHADOW_MODE = True
ENV_CALC_VERSION = "env_v2.1"
MIN_EFFECTIVE_OBS = 60
MIN_INDEX_COVERAGE = 0.50
MIN_STATE_COVERAGE = 0.65
MIN_INDEX_MATURITY = 0.35
MIN_STATE_MATURITY = 0.50
HISTORY_CALENDAR_DAYS = 1200

# 非平稳水平值和方向依赖情境的 Greeks 暂不进入评分，只保留原始曲线。
# components: (metric, scope, weight); STOCK_ANY 表示对股票横截面质量加权汇总。
INDEX_DEFS = {
    "idx_risk_pressure": {
        "cn": "风险压强指数",
        "components": [
            ("vix", None, 1.0),
            ("vvix", None, 0.6),
            ("move", None, 0.8),
            ("vix_contango_pct", None, 1.0),
        ],
    },
    "idx_liquidity_stress": {
        "cn": "信用与金融条件指数",
        # credit_z 与 credit_spread 同源，不重复计权；net_liq/HYG-TLT 水平值先只画曲线。
        "components": [
            ("credit_spread", None, 1.0),
            ("nfci", None, 1.0),
        ],
    },
    "idx_breadth_decay": {
        "cn": "广度衰退指数",
        "components": [
            ("pct_200ma", None, 1.0),
            ("pct_20ma", None, 0.7),
            ("trin", None, 0.5),
            ("breadth_diff_pct", None, 0.8),
        ],
    },
    "idx_options_fragility": {
        "cn": "期权脆弱性观察",
        "components": [
            ("ivr_pct", "STOCK_ANY", 1.0),
            ("expected_move_pct", "STOCK_ANY", 0.8),
            ("short_gamma_m", "STOCK_ANY", 0.6),
        ],
    },
    "idx_flow_behavior": {
        "cn": "资金行为代理观察",
        "components": [
            ("dix_pct", None, 1.0),
            ("gex_billions", None, 0.8),
            ("tqqq_drag_pct", None, 0.5),
        ],
    },
}


def _danger_pctile(metric, percentile):
    """将历史分位折算为危险方向分位；方向未定义的指标不参与评分。"""
    if percentile is None or pd.isna(percentile):
        return None
    reg = METRIC_REGISTRY.get(metric)
    if reg is None or reg["bad_dir"] == 0:
        return None
    p = float(percentile)
    return p if reg["bad_dir"] > 0 else 100.0 - p


def _quality_weight(row):
    """有效样本与新鲜度共同决定分量质量，返回 0-1。"""
    n = row.get("effective_obs_count")
    if n is None or pd.isna(n):
        n = row.get("sample_len")
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 0.0
    if n < MIN_EFFECTIVE_OBS:
        return 0.0
    try:
        lag = max(0, int(row.get("lag_days") or 0))
    except (TypeError, ValueError):
        lag = 0
    sample_factor = min(1.0, n / BASELINE)
    freshness_factor = 0.85 ** lag
    return float(sample_factor * freshness_factor)


def _fetch_day(supabase, report_date, session="EOD"):
    res = (supabase.table("metric_daily")
           .select("*")
           .eq("report_date", report_date)
           .eq("session", session)
           .neq("scope", "COMPOSITE")
           .execute())
    return pd.DataFrame(res.data) if res.data else pd.DataFrame()


def _fetch_history(supabase, report_date, session="EOD", page_size=1000):
    """Fetch enough persisted observations to repair a sparse current snapshot."""
    start_date = (pd.Timestamp(report_date) -
                  pd.Timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    metrics = sorted({metric for spec in INDEX_DEFS.values()
                      for metric, _scope, _weight in spec["components"]})
    rows, offset = [], 0
    while True:
        result = (supabase.table("metric_daily")
                  .select("report_date,metric,scope,value,percentile,sample_len,"
                          "effective_obs_count,source_date,source_name,lag_days")
                  .eq("session", session)
                  .neq("scope", "COMPOSITE")
                  .in_("metric", metrics)
                  .gte("report_date", start_date)
                  .lte("report_date", report_date)
                  .order("report_date").order("metric").order("scope")
                  .range(offset, offset + page_size - 1)
                  .execute())
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return pd.DataFrame(rows)


def rebuild_latest_statistics(history, report_date):
    """Rebuild today's percentile from persisted native observations.

    Daily engine snapshots can have a short in-memory source history even after
    metric_daily was backfilled. Native source dates are de-duplicated so weekly
    series carried across trading days do not pretend to be daily observations.
    """
    if history.empty or "report_date" not in history:
        return pd.DataFrame()
    frame = history.copy()
    report_ts = pd.Timestamp(report_date).normalize()
    report_dates = pd.to_datetime(frame["report_date"], errors="coerce").dt.normalize()
    current = frame[report_dates == report_ts].copy()
    if current.empty:
        return current

    for row_index, row in current.iterrows():
        same_series = frame[
            (frame.get("metric") == row.get("metric")) &
            (frame.get("scope") == row.get("scope")) &
            (report_dates <= report_ts)
        ].copy()
        if same_series.empty or "source_date" not in same_series:
            continue
        same_series["_native_date"] = pd.to_datetime(
            same_series["source_date"], errors="coerce").dt.normalize()
        same_series["_value"] = pd.to_numeric(
            same_series.get("value"), errors="coerce")
        same_series["_report_date"] = pd.to_datetime(
            same_series["report_date"], errors="coerce").dt.normalize()
        same_series = (same_series.dropna(subset=["_native_date", "_value"])
                       .sort_values(["_native_date", "_report_date"])
                       .drop_duplicates("_native_date", keep="last"))
        reference = same_series.tail(BASELINE)
        effective_n = len(reference)
        existing_n = row.get("effective_obs_count")
        if existing_n is None or pd.isna(existing_n):
            existing_n = row.get("sample_len")
        try:
            existing_n = int(existing_n)
        except (TypeError, ValueError):
            existing_n = 0
        existing_percentile = row.get("percentile")
        if (effective_n < 30 or effective_n < existing_n or
                (effective_n == existing_n and pd.notna(existing_percentile))):
            continue
        current_value = pd.to_numeric(
            pd.Series([row.get("value")]), errors="coerce").iloc[0]
        if pd.isna(current_value):
            continue
        percentile = float((reference["_value"] < current_value).mean() * 100.0)
        current.at[row_index, "percentile"] = round(percentile, 1)
        current.at[row_index, "sample_len"] = max(existing_n, effective_n)
        current.at[row_index, "effective_obs_count"] = max(existing_n, effective_n)
    return current.reset_index(drop=True)


def _resonance_hits(supabase, report_date, events=None):
    if events is not None:
        keys = {e.resonance_key for e in events
                if getattr(e, "layer", None) == "resonance" and e.resonance_key}
    else:
        try:
            res = (supabase.table("anomaly_events")
                   .select("resonance_key")
                   .eq("report_date", report_date)
                   .eq("layer", "resonance")
                   .execute())
            keys = {r.get("resonance_key") for r in (res.data or []) if r.get("resonance_key")}
        except Exception:
            keys = set()
    fake = "fake_selloff_reversal" in keys
    return len(keys), fake, sorted(keys)


def classify_state(idx, coverage, resonance_hits=0, fake_reversal_present=False,
                   maturity=None):
    """影子状态分类。覆盖不足优先返回 UNKNOWN，不输出虚假安慰。"""
    key_cov = [coverage.get(k, 0.0) for k in
               ("idx_risk_pressure", "idx_liquidity_stress", "idx_breadth_decay")]
    overall = float(np.mean(key_cov)) if key_cov else 0.0
    if overall < MIN_STATE_COVERAGE or min(key_cov) < MIN_INDEX_COVERAGE:
        return "数据不足", "核心维度覆盖不足，继续积累；本日不作环境分类"
    if maturity is not None:
        key_maturity = [maturity.get(k, 0.0) for k in
                        ("idx_risk_pressure", "idx_liquidity_stress",
                         "idx_breadth_decay")]
        maturity_mean = float(np.mean(key_maturity)) if key_maturity else 0.0
        if (maturity_mean < MIN_STATE_MATURITY or
                min(key_maturity) < MIN_INDEX_MATURITY):
            return ("数据不足",
                    "核心维度样本成熟度不足，继续积累；本日不作环境分类")

    rp = idx.get("idx_risk_pressure")
    ls = idx.get("idx_liquidity_stress")
    bd = idx.get("idx_breadth_decay")
    of = idx.get("idx_options_fragility")

    if fake_reversal_present and rp is not None and rp >= 85:
        return "反转观察", "极端压力伴随反转代理共振，仅作为待验证观察，不构成底部判断"
    if rp is not None and bd is not None and rp >= 90 and bd >= 80:
        return "恐慌观察", "波动率压力与市场广度同时进入历史尾部"
    if (rp is not None and rp >= 75) or (ls is not None and ls >= 75):
        return "脆弱观察", "风险压强或金融条件进入历史高分位"
    if resonance_hits >= 2:
        return "共振观察", f"{resonance_hits} 个跨资产主题同时命中"
    if (rp is not None and rp <= 25 and of is not None and
            coverage.get("idx_options_fragility", 0) >= MIN_INDEX_COVERAGE and of >= 80):
        return "低波动拥挤观察", "波动率受抑且期权代理进入尾部，需关注拥挤反转风险"
    return "常态观察", "核心维度覆盖达标，当前未进入预设历史尾部"


def _component_rows(df, metric, scope):
    rows = df[df["metric"] == metric]
    if scope == "STOCK_ANY":
        rows = rows[~rows["scope"].isin(["MACRO", "COMPOSITE"])]
    else:
        rows = rows[rows["scope"] == (scope or "MACRO")]
    return rows


def _sample_count(row):
    n = row.get("effective_obs_count")
    if n is None or pd.isna(n):
        n = row.get("sample_len")
    try:
        return int(n)
    except (TypeError, ValueError):
        return 0


def _component_observations(df, metric, scope):
    rows = _component_rows(df, metric, scope)
    observations = []
    for _, row in rows.iterrows():
        danger = _danger_pctile(metric, row.get("percentile"))
        quality = _quality_weight(row)
        if danger is not None:
            observations.append({
                "danger": danger,
                "quality": quality,
                "effective_obs_count": _sample_count(row),
                "lag_days": row.get("lag_days"),
            })
    return observations


def score_frame_details(df):
    """Return scores plus separate availability, maturity and blockers."""
    result, coverage, maturity, diagnostics = {}, {}, {}, {}
    for idx_key, spec in INDEX_DEFS.items():
        weighted_sum = 0.0
        eligible_weight = 0.0
        quality_weight = 0.0
        component_diagnostics = []
        total_weight = sum(weight for _, _, weight in spec["components"])
        for metric, scope, base_weight in spec["components"]:
            observations = _component_observations(df, metric, scope) if not df.empty else []
            if not observations:
                rows = _component_rows(df, metric, scope) if not df.empty else pd.DataFrame()
                component_diagnostics.append({
                    "metric": metric,
                    "scope": scope or "MACRO",
                    "status": "MISSING_COMPONENT" if rows.empty else "MISSING_PERCENTILE",
                    "effective_obs_count": max(
                        [_sample_count(row) for _, row in rows.iterrows()], default=0),
                    "required_obs_count": MIN_EFFECTIVE_OBS,
                    "quality": 0.0,
                })
                continue
            eligible = [item for item in observations if item["quality"] > 0]
            if not eligible:
                component_diagnostics.append({
                    "metric": metric,
                    "scope": scope or "MACRO",
                    "status": "INSUFFICIENT_SAMPLE",
                    "effective_obs_count": max(
                        item["effective_obs_count"] for item in observations),
                    "required_obs_count": MIN_EFFECTIVE_OBS,
                    "quality": 0.0,
                })
                continue
            danger = float(np.average(
                [item["danger"] for item in eligible],
                weights=[item["quality"] for item in eligible]))
            quality = float(np.mean([item["quality"] for item in eligible]))
            effective_weight = base_weight * quality
            weighted_sum += danger * effective_weight
            eligible_weight += base_weight
            quality_weight += effective_weight
            component_diagnostics.append({
                "metric": metric,
                "scope": scope or "MACRO",
                "status": "OK",
                "effective_obs_count": max(
                    item["effective_obs_count"] for item in eligible),
                "required_obs_count": MIN_EFFECTIVE_OBS,
                "quality": round(quality, 3),
                "observation_count": len(eligible),
            })
        coverage[idx_key] = round(eligible_weight / total_weight, 3) if total_weight else 0.0
        maturity[idx_key] = (round(quality_weight / eligible_weight, 3)
                             if eligible_weight else 0.0)
        result[idx_key] = (round(weighted_sum / quality_weight, 1)
                           if quality_weight else None)
        diagnostics[idx_key] = component_diagnostics
    return result, coverage, maturity, diagnostics


def score_frame(df):
    """对单个交易日的 metric_daily DataFrame 计算五个影子指数。"""
    result, coverage, _maturity, _diagnostics = score_frame_details(df)
    return result, coverage


def composite_score(scores, coverage, maturity=None):
    eligible_keys = [key for key in INDEX_DEFS
                     if scores.get(key) is not None and
                     coverage.get(key, 0) >= MIN_INDEX_COVERAGE and
                     (maturity is None or
                      maturity.get(key, 0) >= MIN_INDEX_MATURITY)]
    if not eligible_keys:
        return None
    return round(float(np.average(
        [scores[key] for key in eligible_keys],
        weights=[coverage[key] * (maturity.get(key, 1.0) if maturity else 1.0)
                 for key in eligible_keys])), 1)


def compute_indices(supabase, report_date=None, persist=True, events=None, session="EOD"):
    """计算影子指数；返回值只用于报告与回测，不接入预警门控。"""
    report_date = report_date or datetime.now().strftime("%Y-%m-%d")
    history = _fetch_history(supabase, report_date, session=session)
    df = rebuild_latest_statistics(history, report_date)
    result, coverage, maturity, diagnostics = score_frame_details(df)
    rows_to_persist = []
    for idx_key, spec in INDEX_DEFS.items():
        score = result.get(idx_key)
        if score is not None:
            rows_to_persist.append({
                "report_date": report_date,
                "metric": idx_key,
                "scope": "COMPOSITE",
                "session": session,
                "cn_name": spec["cn"],
                "value": score,
                "percentile": None,
                "severity": 0,  # 影子模式禁止把实验指数伪装成正式告警。
                "sample_len": None,
                "effective_obs_count": None,
                "source_date": report_date,
                "source_name": "environment_indices",
                "is_filled": False,
                "is_final": session == "EOD",
                "lag_days": 0,
                "calc_version": ENV_CALC_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

    res_hits, fake_present, resonance_themes = _resonance_hits(
        supabase, report_date, events=events)
    state, desc = classify_state(
        result, coverage, res_hits, fake_present, maturity=maturity)

    composite = composite_score(result, coverage, maturity=maturity)
    overall_cov = round(float(np.mean(list(coverage.values()))), 3) if coverage else 0.0
    overall_maturity = (round(float(np.mean(list(maturity.values()))), 3)
                        if maturity else 0.0)
    result.update({
        "resonance_count": res_hits,
        "resonance_themes": resonance_themes,
        "state": state,
        "state_desc": desc,
        "coverage": coverage,
        "maturity": maturity,
        "diagnostics": diagnostics,
        "composite": composite,
        "overall_coverage": overall_cov,
        "overall_maturity": overall_maturity,
        "shadow_mode": SHADOW_MODE,
        "calc_version": ENV_CALC_VERSION,
    })

    if persist:
        from market_utils import safe_upsert
        if rows_to_persist:
            safe_upsert(supabase, "metric_daily", rows_to_persist,
                        conflict_cols="report_date,metric,scope,session")
        env_row = {
            "report_date": report_date,
            "vol_pressure": result.get("idx_risk_pressure"),
            "credit_stress": result.get("idx_liquidity_stress"),
            "breadth_decay": result.get("idx_breadth_decay"),
            "options_fragility": result.get("idx_options_fragility"),
            "flow_risk": result.get("idx_flow_behavior"),
            "composite": composite,
            "resonance_count": res_hits,
            "state": f"{state} | {desc}",
            "coverage": overall_cov,
            "shadow_mode": True,
            "calc_version": ENV_CALC_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        safe_upsert(supabase, "environment_daily", env_row, conflict_cols="report_date")
    return result


def format_env_summary(idx):
    state = idx.get("state", "数据不足")
    if state == "数据不足":
        state_text = "暂不分类（核心面板未达到发布条件）"
    else:
        state_text = state
    lines = ["=== 环境指数（影子观察，不触发预警） ===",
             f"正式状态：{state_text}",
             f"说明：{idx.get('state_desc', '')}"]
    labels = {
        "idx_risk_pressure": "风险压强",
        "idx_liquidity_stress": "信用条件",
        "idx_breadth_decay": "广度衰退",
        "idx_options_fragility": "期权脆弱",
        "idx_flow_behavior": "资金代理",
    }
    coverage = idx.get("coverage", {})
    maturity = idx.get("maturity", {})
    for key, label in labels.items():
        value = idx.get(key)
        cov = coverage.get(key, 0.0)
        mature = maturity.get(key, 0.0)
        if value is None:
            lines.append(
                f"{label}: 数据不足 | 分量覆盖 {cov:.0%} | 样本成熟度 {mature:.0%}")
        else:
            lines.append(
                f"{label}: {value:5.1f}/100 | 分量覆盖 {cov:.0%} | "
                f"样本成熟度 {mature:.0%}")

    diagnostics = idx.get("diagnostics", {})
    diagnostic_labels = {
        "MISSING_COMPONENT": "缺少当日分量",
        "MISSING_PERCENTILE": "历史分位不可用",
        "INSUFFICIENT_SAMPLE": "有效样本不足",
    }
    blockers = []
    for key in ("idx_risk_pressure", "idx_liquidity_stress", "idx_breadth_decay"):
        for item in diagnostics.get(key, []):
            if item.get("status") == "OK":
                continue
            metric = item.get("metric", "unknown")
            metric_label = METRIC_REGISTRY.get(metric, {}).get("cn", metric)
            status_text = diagnostic_labels.get(
                item.get("status"), item.get("status", "未知原因"))
            sample = item.get("effective_obs_count", 0)
            required = item.get("required_obs_count", MIN_EFFECTIVE_OBS)
            if item.get("status") == "INSUFFICIENT_SAMPLE":
                status_text += f" {sample}/{required}"
            blockers.append(f"{metric_label}：{status_text}")
    if blockers:
        lines.append("分类阻塞项：" + "；".join(blockers))

    comp = idx.get("composite")
    if comp is None:
        lines.append("综合观察值：暂不发布（暂无达到门槛的成熟子指数）")
    elif state == "数据不足":
        lines.append(f"可用面板参考值：{comp:.1f}/100（不代表整体环境）")
    else:
        lines.append(f"综合观察值：{comp:.1f}/100")

    theme_keys = idx.get("resonance_themes") or []
    theme_names = [RESONANCE_THEMES.get(key, {}).get("cn", key)
                   for key in theme_keys]
    if theme_names:
        lines.append(
            f"跨资产共振：触发 {len(theme_names)} 个正式主题（{'、'.join(theme_names)}）")
    else:
        lines.append("跨资产共振：未触发正式主题（0个；不代表环境健康）")
    lines.append(f"计算版本：{idx.get('calc_version', ENV_CALC_VERSION)}")
    return "\n".join(lines)
