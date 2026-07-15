# -*- coding: utf-8 -*-
"""
environment_indices.py — 复合环境指数（环境评估仪表盘的"状态曲线"）

把 metric_daily 里几十条单指标，聚合成 5 个可长期观察的复合指数 + 共振计数，
再映射成一句话环境状态判定（健康 / 过热 / 脆弱 / 恐慌 / 反转临界）。

设计纪律（与 ALERT_GATE 同源，杜绝黑盒）：
  1. 归一化：复用 metric_daily 里已算好的 252D percentile。每个分量按 bad_dir
     折算成"危险分位"0-100（越高越危险），再加权平均。量纲天然统一。
  2. 权重集中在 INDEX_DEFS，可调、可回测。
  3. 覆盖度透明：分量缺数据（历史不足）时跳过并降低 coverage，不假装满分。
  4. 复合指数本身也写回 metric_daily（scope=COMPOSITE），于是它自己也能画曲线、
     算自己的历史分位——"状态曲线"就是这么来的。

依赖：只读 metric_daily / anomaly_events，写回 metric_daily。不碰行情源。
"""
from datetime import datetime, timezone

import pandas as pd
import numpy as np

from anomaly_engine import METRIC_REGISTRY

# ============================================================
#  复合指数定义：唯一的"权重真相源"
#  components: [(metric, scope, weight), ...]
#    scope=None 表示 MACRO；个股类用具体 ticker 或 "STOCK_ANY"(取该指标所有ticker均值)
#  danger 分位 = percentile（bad_dir>0）或 100-percentile（bad_dir<0）
# ============================================================
INDEX_DEFS = {
    "idx_risk_pressure": {
        "cn": "风险压强指数",
        "components": [
            ("vix", None, 1.0),
            ("vvix", None, 0.6),
            ("move", None, 0.8),
            ("vix_contango_pct", None, 1.0),   # bad_dir=-1，低/倒挂=危险
        ],
    },
    "idx_liquidity_stress": {
        "cn": "流动性恶化指数",
        "components": [
            ("credit_spread", None, 1.0),
            ("credit_z", None, 0.8),
            ("nfci", None, 1.0),
            ("net_liq", None, 0.8),            # bad_dir=-1，净流动性下降=危险
            ("hyg_tlt_ratio", None, 0.8),      # bad_dir=-1
        ],
    },
    "idx_breadth_decay": {
        "cn": "广度衰退指数",
        "components": [
            ("pct_200ma", None, 1.0),          # bad_dir=-1，占比下降=危险
            ("pct_20ma", None, 0.6),
            ("trin", None, 0.6),
            ("breadth_diff_pct", None, 0.8),   # Mag7-RSP 分化扩大=抱团=脆弱
        ],
    },
    "idx_options_fragility": {
        "cn": "期权脆弱性指数",
        # ⚠️ 历史无法回补，上线约1季度内 coverage 低、置信度低（会如实标注）
        "components": [
            ("ivr_pct", "STOCK_ANY", 1.0),     # bad_dir=-1，IVR 极低=麻木=易爆
            ("dpsv_pct", "STOCK_ANY", 0.6),
        ],
    },
    "idx_flow_behavior": {
        "cn": "资金行为异常指数",
        # ⚠️ DIX/GEX 同样无法回补历史
        "components": [
            ("dpsv_pct", "STOCK_ANY", 1.0),
            ("tqqq_drag_pct", None, 0.6),      # bad_dir=-1
        ],
    },
}

# 环境状态判定：可调映射（阈值基于复合指数 0-100）
# 顺序判定，命中即返回。
def classify_state(idx, resonance_hits, fake_reversal_present):
    rp = idx.get("idx_risk_pressure")
    ls = idx.get("idx_liquidity_stress")
    bd = idx.get("idx_breadth_decay")
    of = idx.get("idx_options_fragility")

    def hi(v, t): return v is not None and v >= t

    # 反转临界：恐慌级压强 + 出现"假摔反转"共振
    if fake_reversal_present and hi(rp, 75):
        return "🔄 反转临界", "恐慌读数伴随假摔/吸筹共振，可能接近短期底部拐点"
    # 恐慌：压强极高 + 广度崩塌
    if hi(rp, 85) and hi(bd, 70):
        return "🩸 恐慌区", "波动率压强与广度衰退同时极端，处于风险释放阶段"
    # 脆弱：压强/流动性抬升但广度尚未跟随（表面稳、底层变差），或共振≥2
    if (hi(rp, 60) or hi(ls, 60)) and not hi(bd, 70):
        return "⚠️ 脆弱区", "压强或信用已抬升而广度尚稳，底层先于价格转弱"
    if resonance_hits >= 2:
        return "⚠️ 脆弱区", f"{resonance_hits} 个跨资产共振主题并存，环境脆弱"
    # 过热：压强低但广度极度伸展 + 期权麻木（IVR 极低）
    if (rp is not None and rp < 35) and hi(of, 70):
        return "🔥 过热区", "波动率压抑而期权情绪麻木，拥挤度偏高"
    # 健康
    return "🟢 健康区", "各环境维度未见系统性压力"


def _danger_pctile(metric, percentile):
    """按 bad_dir 把 252D 分位折算成'危险分位'（越高越危险）。"""
    if percentile is None:
        return None
    reg = METRIC_REGISTRY.get(metric)
    if reg is None:
        return None
    bad = reg["bad_dir"]
    if bad >= 0:
        return float(percentile)
    return float(100.0 - percentile)


def _fetch_day(supabase, report_date):
    """取某交易日 metric_daily 全部单指标行（不含 COMPOSITE）。"""
    res = (supabase.table("metric_daily")
           .select("*")
           .eq("report_date", report_date)
           .neq("scope", "COMPOSITE")
           .execute())
    return pd.DataFrame(res.data) if res.data else pd.DataFrame()


def _resonance_hits(supabase, report_date):
    """当日 anomaly_events 里的共振主题命中情况。"""
    try:
        res = (supabase.table("anomaly_events")
               .select("resonance_key,explanation")
               .eq("report_date", report_date)
               .eq("layer", "resonance")
               .execute())
        keys = [r.get("resonance_key") for r in (res.data or []) if r.get("resonance_key")]
        fake = any("fake" in (k or "") or "reversal" in (k or "") for k in keys)
        return len(set(keys)), fake
    except Exception:
        return 0, False


def compute_indices(supabase, report_date=None, persist=True):
    """计算某交易日的复合环境指数，写回 metric_daily(scope=COMPOSITE)。
    返回 dict：{index_key: value 或 None, ...} + 'state' + 'state_desc' + 'coverage'。"""
    report_date = report_date or datetime.now().strftime("%Y-%m-%d")
    df = _fetch_day(supabase, report_date)

    result = {}
    coverage = {}
    rows_to_persist = []

    # 建 (metric, scope) -> percentile 查找表
    pct_lookup = {}
    stock_any = {}  # metric -> [percentile,...] 跨 ticker
    if not df.empty:
        for _, r in df.iterrows():
            m, sc, p = r.get("metric"), r.get("scope"), r.get("percentile")
            if p is None or (isinstance(p, float) and pd.isna(p)):
                continue
            pct_lookup[(m, sc)] = float(p)
            if sc not in (None, "MACRO", "COMPOSITE"):
                stock_any.setdefault(m, []).append(float(p))

    for idx_key, spec in INDEX_DEFS.items():
        num, wsum, have, total = 0.0, 0.0, 0, 0
        for metric, scope, weight in spec["components"]:
            total += 1
            # 解析分位
            if scope == "STOCK_ANY":
                vals = stock_any.get(metric)
                raw_pct = float(np.mean(vals)) if vals else None
            else:
                raw_pct = pct_lookup.get((metric, scope if scope else "MACRO"))
                if raw_pct is None:  # 宏观有时 scope 存成 MACRO
                    raw_pct = pct_lookup.get((metric, "MACRO"))
            dp = _danger_pctile(metric, raw_pct)
            if dp is not None:
                num += dp * weight
                wsum += weight
                have += 1
        if wsum > 0:
            val = round(num / wsum, 1)
            cov = round(have / total, 2)
            result[idx_key] = val
            coverage[idx_key] = cov
            rows_to_persist.append({
                "report_date": report_date,
                "metric": idx_key,
                "scope": "COMPOSITE",
                "cn_name": spec["cn"],
                "value": val,
                "percentile": None,      # 复合指数的历史分位随天数积累后另算
                "severity": 3 if val >= 85 else (2 if val >= 60 else (1 if val >= 40 else 0)),
                "sample_len": have,       # 借用字段记录有效分量数
                "days_in_risk": None,
                "source_date": report_date,
                "lag_days": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        else:
            result[idx_key] = None
            coverage[idx_key] = 0.0

    # 共振计数
    res_hits, fake_present = _resonance_hits(supabase, report_date)
    result["resonance_count"] = res_hits

    # 状态判定
    state, desc = classify_state(result, res_hits, fake_present)
    result["state"] = state
    result["state_desc"] = desc
    result["coverage"] = coverage

    # 综合环境压强 = 各有效指数的均值（缺的不计）
    vals = [result.get(k) for k in INDEX_DEFS if result.get(k) is not None]
    composite = round(float(np.mean(vals)), 1) if vals else None
    result["composite"] = composite
    cov_overall = round(float(np.mean(list(coverage.values()))), 2) if coverage else 0.0

    if persist:
        from market_utils import safe_upsert
        # (a) 复合指数作为 metric_daily 行 —— 供画曲线、算自身历史分位
        if rows_to_persist:
            safe_upsert(supabase, "metric_daily", rows_to_persist,
                        conflict_cols="report_date,metric,scope")
        # (b) 宽表 environment_daily —— 一天一行的状态台账，便于快速查询/回测
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
            "coverage": cov_overall,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        safe_upsert(supabase, "environment_daily", env_row,
                    conflict_cols="report_date")

    return result


def format_env_summary(idx):
    """复合指数摘要文本（给邮件正文头部）。"""
    lines = [f"=== 🌡️ 环境状态：{idx.get('state','-')} ===",
             f"    {idx.get('state_desc','')}", ""]
    label = {
        "idx_risk_pressure": "风险压强",
        "idx_liquidity_stress": "流动性恶化",
        "idx_breadth_decay": "广度衰退",
        "idx_options_fragility": "期权脆弱",
        "idx_flow_behavior": "资金行为",
    }
    cov = idx.get("coverage", {})
    for k, cn in label.items():
        v = idx.get(k)
        if v is None:
            lines.append(f"    {cn:6}: 数据不足（历史积累中）")
        else:
            bar = "█" * int(v / 10) + "░" * (10 - int(v / 10))
            c = cov.get(k, 0)
            cov_note = "" if c >= 0.75 else f"  (覆盖{int(c*100)}%,置信偏低)"
            lines.append(f"    {cn:6}: {bar} {v:5.1f}/100{cov_note}")
    lines.append(f"    共振主题数: {idx.get('resonance_count', 0)}")
    return "\n".join(lines)
