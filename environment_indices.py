# -*- coding: utf-8 -*-
"""环境指数影子模型。

指数仅用于观察和回测，不进入 ALERT_GATE，也不产生交易指令。每个分量按
有效样本数与数据滞后加权；覆盖不足时明确返回“数据不足”，绝不默认健康。
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from anomaly_engine import BASELINE, CALC_VERSION, METRIC_REGISTRY

SHADOW_MODE = True
MIN_EFFECTIVE_OBS = 60
MIN_INDEX_COVERAGE = 0.50
MIN_STATE_COVERAGE = 0.65

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
    return len(keys), fake


def classify_state(idx, coverage, resonance_hits=0, fake_reversal_present=False):
    """影子状态分类。覆盖不足优先返回 UNKNOWN，不输出虚假安慰。"""
    key_cov = [coverage.get(k, 0.0) for k in
               ("idx_risk_pressure", "idx_liquidity_stress", "idx_breadth_decay")]
    overall = float(np.mean(key_cov)) if key_cov else 0.0
    if overall < MIN_STATE_COVERAGE or min(key_cov) < MIN_INDEX_COVERAGE:
        return "数据不足", "核心维度覆盖不足，继续积累；本日不作环境分类"

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


def _component_observations(df, metric, scope):
    rows = df[df["metric"] == metric]
    if scope == "STOCK_ANY":
        rows = rows[~rows["scope"].isin(["MACRO", "COMPOSITE"])]
    else:
        rows = rows[rows["scope"] == (scope or "MACRO")]
    observations = []
    for _, row in rows.iterrows():
        danger = _danger_pctile(metric, row.get("percentile"))
        quality = _quality_weight(row)
        if danger is not None and quality > 0:
            observations.append((danger, quality))
    return observations


def score_frame(df):
    """对单个交易日的 metric_daily DataFrame 计算五个影子指数。"""
    result, coverage = {}, {}
    for idx_key, spec in INDEX_DEFS.items():
        weighted_sum = 0.0
        available_weight = 0.0
        total_weight = sum(weight for _, _, weight in spec["components"])
        for metric, scope, base_weight in spec["components"]:
            observations = _component_observations(df, metric, scope) if not df.empty else []
            if not observations:
                continue
            danger = float(np.average(
                [x[0] for x in observations], weights=[x[1] for x in observations]))
            quality = float(np.mean([x[1] for x in observations]))
            effective_weight = base_weight * quality
            weighted_sum += danger * effective_weight
            available_weight += effective_weight
        coverage[idx_key] = round(available_weight / total_weight, 3) if total_weight else 0.0
        result[idx_key] = (round(weighted_sum / available_weight, 1)
                           if available_weight else None)
    return result, coverage


def composite_score(scores, coverage):
    eligible_keys = [key for key in INDEX_DEFS
                     if scores.get(key) is not None and
                     coverage.get(key, 0) >= MIN_INDEX_COVERAGE]
    if not eligible_keys:
        return None
    return round(float(np.average(
        [scores[key] for key in eligible_keys],
        weights=[coverage[key] for key in eligible_keys])), 1)


def compute_indices(supabase, report_date=None, persist=True, events=None, session="EOD"):
    """计算影子指数；返回值只用于报告与回测，不接入预警门控。"""
    report_date = report_date or datetime.now().strftime("%Y-%m-%d")
    df = _fetch_day(supabase, report_date, session=session)
    result, coverage = score_frame(df)
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
                "calc_version": CALC_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

    res_hits, fake_present = _resonance_hits(supabase, report_date, events=events)
    state, desc = classify_state(result, coverage, res_hits, fake_present)

    composite = composite_score(result, coverage)
    overall_cov = round(float(np.mean(list(coverage.values()))), 3) if coverage else 0.0
    result.update({
        "resonance_count": res_hits,
        "state": state,
        "state_desc": desc,
        "coverage": coverage,
        "composite": composite,
        "overall_coverage": overall_cov,
        "shadow_mode": SHADOW_MODE,
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
            "calc_version": CALC_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        safe_upsert(supabase, "environment_daily", env_row, conflict_cols="report_date")
    return result


def format_env_summary(idx):
    lines = ["=== 环境指数（影子观察，不触发预警） ===",
             f"状态：{idx.get('state', '数据不足')}",
             f"说明：{idx.get('state_desc', '')}"]
    labels = {
        "idx_risk_pressure": "风险压强",
        "idx_liquidity_stress": "信用条件",
        "idx_breadth_decay": "广度衰退",
        "idx_options_fragility": "期权脆弱",
        "idx_flow_behavior": "资金代理",
    }
    coverage = idx.get("coverage", {})
    for key, label in labels.items():
        value, cov = idx.get(key), coverage.get(key, 0.0)
        if value is None:
            lines.append(f"{label}: 数据不足（覆盖 {cov:.0%}）")
        else:
            lines.append(f"{label}: {value:5.1f}/100（质量覆盖 {cov:.0%}）")
    comp = idx.get("composite")
    lines.append(f"综合观察值: {comp if comp is not None else '数据不足'}")
    lines.append(f"共振主题数: {idx.get('resonance_count', 0)}")
    return "\n".join(lines)
