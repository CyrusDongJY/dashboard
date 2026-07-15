# -*- coding: utf-8 -*-
"""
anomaly_engine.py — 盘后多窗口异常矩阵引擎

设计三层：
  第一层 single      单指标绝对阈值 / 252D z-score / 分位异常
  第二层 multiwindow 同一指标 1D 冲击 + 5D 延续 + 21D 分位共同确认 -> 升级
  第三层 resonance   跨指标共振主题（高危下跌 / 假摔反转 / 抱团脆弱）

核心原则：
  - 252D 作为统计基准，1D/5D/21D/63D 只是观察窗口
  - 每个指标的方向语义（direction_sign）集中在 METRIC_REGISTRY，杜绝口径冲突
  - 置信度 = 样本充分度 × 数据新鲜度，滞后数据自动降权
  - 输出结构化 AnomalyEvent，可回测、可调参、可落库
"""
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from market_utils import trading_days_back, lag_trading_days

# ============================================================
# 指标注册表：唯一的"方向语义"真相源
#   bad_dir = +1  表示"数值越高越危险"（如 MOVE、credit_spread）
#   bad_dir = -1  表示"数值越低越危险"（如 vix_contango、breadth、hyg_tlt_ratio）
#   abs_alert     绝对阈值触发 (op, threshold, severity)，可为 None
#   z_alert       z-score 阈值 (severity2, severity3)，作用于 bad 方向
#   invert_signal 该指标极值可能是反向信号（如 DPSV 高=机构吸筹=利好），
#                 用于共振层区分"风险"与"托底"
# ============================================================
METRIC_REGISTRY = {
    # 波动率结构
    "vix_contango_pct": {"cn": "VIX期限结构", "bad_dir": -1, "scope": "MACRO",
                          "abs": [("<", 0, 3), ("<", 3, 1)], "z": (2.0, 3.0)},
    "move":            {"cn": "MOVE债市波动", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">", 120, 2), (">", 100, 1)], "z": (2.0, 3.0)},
    "vix":             {"cn": "VIX", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">", 30, 2), (">", 22, 1)], "z": (2.0, 3.0)},
    "vvix":            {"cn": "VVIX", "bad_dir": +1, "scope": "MACRO", "abs": None, "z": (2.0, 3.0)},
    "vrp_num":         {"cn": "VRP风险溢价", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">", 10, 1)], "z": (2.0, 3.0)},
    "vix_term_ratio":  {"cn": "VIX/VIX3M", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">=", 1.0, 2)], "z": (2.0, 3.0)},
    # 信用与流动性
    "credit_spread":   {"cn": "高收益债信用利差", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">", 5.0, 2)], "z": (2.0, 3.0)},
    "credit_z":        {"cn": "信用利差Z", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">", 2.0, 2), (">", 1.0, 1)], "z": (2.0, 3.0)},
    "nfci":            {"cn": "NFCI金融压力", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">", 0, 2), (">", -0.3, 1)], "z": (2.0, 3.0)},
    "hyg_tlt_ratio":   {"cn": "垃圾债/长债比", "bad_dir": -1, "scope": "MACRO", "abs": None, "z": (2.0, 3.0)},
    "net_liq":         {"cn": "真实净流动性", "bad_dir": -1, "scope": "MACRO", "abs": None, "z": (2.0, 3.0)},
    # 广度与结构
    "pct_200ma":       {"cn": "200MA占比", "bad_dir": -1, "scope": "MACRO",
                          "abs": [("<", 30, 2), ("<", 40, 1)], "z": (2.0, 3.0)},
    "pct_20ma":        {"cn": "20MA占比", "bad_dir": -1, "scope": "MACRO", "abs": None, "z": (2.0, 3.0)},
    "breadth_diff_pct":{"cn": "Mag7-RSP广度差", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">", 1.5, 1)], "z": (2.0, 3.0)},
    "trin":            {"cn": "TRIN", "bad_dir": +1, "scope": "MACRO",
                          "abs": [(">", 2.0, 1)], "z": (2.5, 3.5)},
    # 跨资产
    "dxy":             {"cn": "美元指数", "bad_dir": +1, "scope": "MACRO", "abs": None, "z": (2.0, 3.0)},
    "cg_z":            {"cn": "铜金比Z", "bad_dir": -1, "scope": "MACRO",
                          "abs": [("<", -2.0, 2), ("<", -1.0, 1)], "z": (2.0, 3.0)},
    "tqqq_drag_pct":   {"cn": "TQQQ损耗", "bad_dir": -1, "scope": "MACRO",
                          "abs": [("<", -2.0, 1)], "z": (2.0, 3.0)},
    # 微观（个股，反向信号指标）
    "dpsv_pct":        {"cn": "暗池做空比", "bad_dir": -1, "scope": "STOCK",
                          "abs": [("<", 40, 1)], "z": (2.0, 3.0), "invert_signal": True},
    "ivr_pct":         {"cn": "IVR波动率百分位", "bad_dir": -1, "scope": "STOCK",
                          "abs": [("<", 10, 1)], "z": (2.0, 3.0)},
    "charm_m":         {"cn": "Charm", "bad_dir": 0, "scope": "STOCK", "abs": None, "z": (2.5, 3.5)},
}

WINDOWS = {"1D": 1, "5D": 5, "21D": 21, "63D": 63}
BASELINE = 252  # 统计基准窗口（交易日）


@dataclass
class AnomalyEvent:
    report_date: str
    metric: str
    scope: str
    window: str
    value: Optional[float] = None
    change: Optional[float] = None
    zscore: Optional[float] = None
    percentile: Optional[float] = None
    direction: Optional[str] = None
    severity: int = 0
    confidence: float = 0.0
    source_date: Optional[str] = None
    lag_days: int = 0
    layer: str = "single"
    resonance_key: Optional[str] = None
    explanation: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_row(self):
        d = asdict(self)
        # DB 列名用 window_scope（window 是 PostgreSQL 保留字）
        d['window_scope'] = d.pop('window')
        return d


# ============================================================
# 统计工具
# ============================================================
def _clean(series):
    s = pd.to_numeric(series, errors='coerce').replace([np.inf, -np.inf], np.nan).dropna()
    return s

def zscore_252(series):
    s = _clean(series)
    if len(s) < 30:
        return None, len(s)
    win = min(BASELINE, len(s))
    ref = s.iloc[-win:]
    mu, sd = ref.mean(), ref.std()
    if sd == 0 or pd.isna(sd):
        return 0.0, len(s)
    return float((s.iloc[-1] - mu) / sd), len(s)

def percentile_252(series):
    s = _clean(series)
    if len(s) < 30:
        return None
    win = min(BASELINE, len(s))
    ref = s.iloc[-win:]
    return float((ref < s.iloc[-1]).mean() * 100)

def window_change(series, n):
    s = _clean(series)
    if len(s) < n + 1:
        return None
    return float(s.iloc[-1] - s.iloc[-(n + 1)])

def freshness_confidence(sample_len, lag_days, needs_baseline=True):
    """置信度 = 样本充分度 × 新鲜度。
    needs_baseline=True（z-score/分位类）：252 样本满分，样本不足线性降权；
    needs_baseline=False（绝对阈值类）：数值本身即有含义，样本惩罚下限 0.5。
    滞后每交易日打 0.85 折。"""
    sample_factor = min(1.0, sample_len / BASELINE) if sample_len else 0.0
    if not needs_baseline:
        sample_factor = max(0.5, sample_factor)
    fresh_factor = 0.85 ** max(0, lag_days)
    return round(sample_factor * fresh_factor, 3)

def _cmp(op, a, b):
    return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


# ============================================================
# 第一层 + 第二层：逐指标扫描
# ============================================================
def scan_metric(metric, series, report_date, scope=None, source_date=None):
    """对单指标的时间序列（升序）跑一层+二层，返回 [AnomalyEvent]。"""
    reg = METRIC_REGISTRY.get(metric)
    if reg is None:
        return []
    s = _clean(series)
    if s.empty:
        return []

    scope = scope or reg["scope"]
    lag = lag_trading_days(source_date, report_date) if source_date else 0
    z, n = zscore_252(s)
    pct = percentile_252(s)
    conf_baseline = freshness_confidence(n, lag, needs_baseline=True)   # z/分位类
    conf_abs = freshness_confidence(n, lag, needs_baseline=False)       # 绝对阈值类
    cur = float(s.iloc[-1])
    bad_dir = reg["bad_dir"]
    events = []

    # ---- 绝对阈值 ----
    hit_sev = 0
    if reg.get("abs"):
        for op, thr, sev in reg["abs"]:
            if _cmp(op, cur, thr):
                hit_sev = max(hit_sev, sev)
    if hit_sev > 0:
        events.append(AnomalyEvent(
            report_date=report_date, metric=metric, scope=scope, window="1D",
            value=round(cur, 4), zscore=round(z, 2) if z is not None else None,
            percentile=round(pct, 1) if pct is not None else None,
            direction="up" if bad_dir >= 0 else "down",
            severity=hit_sev, confidence=conf_abs, source_date=source_date, lag_days=lag,
            layer="single",
            explanation=f"{reg['cn']} 绝对阈值触发：现值 {cur:.2f}",
        ))

    # ---- z-score 异常（作用于危险方向）----
    if z is not None and bad_dir != 0:
        z_dir = z * bad_dir  # 折算成"越大越危险"
        s2, s3 = reg["z"]
        z_sev = 3 if z_dir >= s3 else (2 if z_dir >= s2 else 0)
        if z_sev > 0:
            events.append(AnomalyEvent(
                report_date=report_date, metric=metric, scope=scope, window="252D",
                value=round(cur, 4), zscore=round(z, 2),
                percentile=round(pct, 1) if pct is not None else None,
                direction="up" if z > 0 else "down",
                severity=z_sev, confidence=conf_baseline, source_date=source_date, lag_days=lag,
                layer="single",
                explanation=f"{reg['cn']} 偏离252日基准 {z:+.2f}σ (分位 {pct:.0f}%)" if pct is not None else f"{reg['cn']} 偏离基准 {z:+.2f}σ",
            ))

    # ---- 多窗口确认（第二层）----
    chg = {w: window_change(s, k) for w, k in WINDOWS.items()}
    c1, c5, c21 = chg.get("1D"), chg.get("5D"), chg.get("21D")
    if bad_dir != 0 and None not in (c1, c5) and pct is not None:
        d1, d5 = c1 * bad_dir, c5 * bad_dir  # 折算成危险方向的变化
        extreme_pct = (pct >= 90) if bad_dir > 0 else (pct <= 10)
        # 当日冲击 + 5日延续 + 21日已进入分位异常区
        if d1 > 0 and d5 > 0 and extreme_pct:
            events.append(AnomalyEvent(
                report_date=report_date, metric=metric, scope=scope, window="1D+5D+21D",
                value=round(cur, 4), change=round(c1, 4), zscore=round(z, 2) if z is not None else None,
                percentile=round(pct, 1), direction="up" if bad_dir > 0 else "down",
                severity=3, confidence=conf_baseline, source_date=source_date, lag_days=lag,
                layer="multiwindow",
                explanation=(f"{reg['cn']} 多窗口确认：当日{c1:+.2f}、5日{c5:+.2f} 同向恶化，"
                             f"21日分位达 {pct:.0f}%（危险区）"),
            ))
    return events


# ============================================================
#  每日全量快照：不管越不越线，都算出完整派生向量
#  这是"曲线化 / 环境评估"的地基——scan_metric 每天已经算了这些，
#  以前没越线就扔掉，现在无条件持久化到 metric_daily。
# ============================================================
def _dist_to_threshold(reg, cur):
    """当前值到"入场阈值"（该指标 abs 列表里最不严重的那条）的带符号距离。
    正=尚有安全边际，负=已越线。无 abs 阈值的指标返回 None。"""
    absr = reg.get("abs")
    if not absr:
        return None
    # 取 severity 最小的那条作为"入场线"
    op, thr, _sev = min(absr, key=lambda x: x[2])
    # 危险方向：> 阈值危险 → 距离 = thr - cur（cur 越过 thr 后为负）
    #           < 阈值危险 → 距离 = cur - thr
    if op in (">", ">="):
        return round(float(thr - cur), 4)
    else:
        return round(float(cur - thr), 4)


def _days_in_risk(series, reg):
    """连续多少个交易日处于风险区（按 abs 入场阈值判定，末尾往前数）。
    无 abs 阈值的指标返回 None（避免用滚动 z 造成误导性数字）。"""
    absr = reg.get("abs")
    if not absr:
        return None
    op, thr, _sev = min(absr, key=lambda x: x[2])
    s = _clean(series)
    if s.empty:
        return 0
    count = 0
    for v in reversed(s.values):
        if _cmp(op, float(v), thr):
            count += 1
        else:
            break
    return count


def metric_snapshot(metric, series, report_date, scope=None, source_date=None):
    """输出单指标的每日派生向量（dict），供 metric_daily 落库画曲线。
    与 scan_metric 用同一套统计口径，但无条件产出（不依赖是否越线）。"""
    reg = METRIC_REGISTRY.get(metric)
    if reg is None:
        return None
    s = _clean(series)
    if s.empty:
        return None

    scope = scope or reg["scope"]
    lag = lag_trading_days(source_date, report_date) if source_date else 0
    z, n = zscore_252(s)
    pct = percentile_252(s)
    chg = {w: window_change(s, k) for w, k in WINDOWS.items()}
    cur = float(s.iloc[-1])

    # 该指标当日触发的最高 severity（与 scan_metric 一致，供面板着色）
    sev = 0
    if reg.get("abs"):
        for op, thr, sv in reg["abs"]:
            if _cmp(op, cur, thr):
                sev = max(sev, sv)
    if z is not None and reg["bad_dir"] != 0:
        z_dir = z * reg["bad_dir"]
        s2, s3 = reg["z"]
        sev = max(sev, 3 if z_dir >= s3 else (2 if z_dir >= s2 else 0))

    return {
        "report_date": report_date,
        "metric": metric,
        "scope": scope,
        "cn_name": reg["cn"],
        "value": round(cur, 4),
        "chg_1d": round(chg["1D"], 4) if chg["1D"] is not None else None,
        "chg_5d": round(chg["5D"], 4) if chg["5D"] is not None else None,
        "chg_21d": round(chg["21D"], 4) if chg["21D"] is not None else None,
        "chg_63d": round(chg["63D"], 4) if chg["63D"] is not None else None,
        "zscore": round(z, 3) if z is not None else None,
        "percentile": round(pct, 1) if pct is not None else None,
        "severity": sev,
        "dist_to_thr": _dist_to_threshold(reg, cur),
        "days_in_risk": _days_in_risk(s, reg),
        "sample_len": n,
        "source_date": source_date,
        "lag_days": lag,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# 第三层：跨资产共振
# ============================================================
RESONANCE_THEMES = {
    "high_risk_selloff": {
        "cn": "高危下跌共振", "min_hits": 3, "base_severity": 3,
        "conditions": {
            "vix_contango_pct": lambda v: v is not None and v < 3,
            "move": lambda v: v is not None and v > 110,
            "hyg_tlt_ratio_roc": lambda v: v is not None and v < -2.0,
            "pct_200ma": lambda v: v is not None and v < 45,
            "credit_z": lambda v: v is not None and v > 1.0,
        },
    },
    "fake_selloff_reversal": {
        "cn": "假摔/反转共振", "min_hits": 3, "base_severity": 2,
        "conditions": {
            "trin": lambda v: v is not None and v > 2.0,
            "vrp_num": lambda v: v is not None and v > 10,
            "dpsv_high": lambda v: v is not None and v > 50,       # 机构暗池吸筹
            "pct_20ma": lambda v: v is not None and v < 15,        # 广度恐慌
        },
    },
    "crowding_fragility": {
        "cn": "抱团脆弱共振", "min_hits": 3, "base_severity": 2,
        "conditions": {
            "breadth_diff_pct": lambda v: v is not None and v > 1.5,
            "pct_20ma": lambda v: v is not None and v < 40,
            "ivr_low": lambda v: v is not None and v < 15,         # 情绪麻木
            "pct_200ma": lambda v: v is not None and v < 55,
        },
    },
}

def scan_resonance(report_date, snapshot):
    """snapshot: dict of 已计算好的指标值（含派生量 hyg_tlt_ratio_roc / dpsv_high / ivr_low）。
    返回共振 AnomalyEvent 列表。"""
    events = []
    for key, theme in RESONANCE_THEMES.items():
        hits, reasons = 0, []
        for cond_name, fn in theme["conditions"].items():
            val = snapshot.get(cond_name)
            try:
                if fn(val):
                    hits += 1
                    reasons.append(cond_name)
            except Exception:
                pass
        if hits >= theme["min_hits"]:
            # 命中越多严重度越高，封顶 3
            sev = min(3, theme["base_severity"] + (hits - theme["min_hits"]))
            conf = round(min(1.0, 0.5 + 0.12 * hits), 3)
            events.append(AnomalyEvent(
                report_date=report_date, metric="RESONANCE", scope="MACRO",
                window="multi", severity=sev, confidence=conf,
                layer="resonance", resonance_key=key,
                explanation=f"{theme['cn']}：{hits}/{len(theme['conditions'])} 维度共振 [{', '.join(reasons)}]",
            ))
    return events


# ============================================================
# 顶层编排：从 Supabase 拉历史 -> 扫描 -> 落库
# ============================================================
MICRO_SYMBOLS = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'ORCL']

def _fetch_history(supabase, table, date_col, days=400, ticker=None):
    """倒序取最近 N 行再翻转为升序。
    PostgREST 默认单次最多返回 1000 行，升序查询被截断时丢的是【最新】数据，
    倒序保证截断只影响最旧的历史。"""
    cutoff = trading_days_back(days)
    q = supabase.table(table).select("*").gte(date_col, cutoff)
    if ticker is not None:
        q = q.eq('ticker', ticker)
    res = q.order(date_col, desc=True).limit(1000).execute()
    if not res.data:
        return pd.DataFrame()
    return pd.DataFrame(res.data).sort_values(date_col).reset_index(drop=True)

def run_engine(supabase, report_date=None, persist=True):
    """主入口：扫描 market_history / macro_spot_daily / 个股表，产出异常矩阵。
    返回 (events: list[AnomalyEvent], snapshot: dict)。"""
    report_date = report_date or datetime.now().strftime('%Y-%m-%d')
    events = []
    snapshot = {}
    snap_rows = []   # metric_daily：每日无条件全指标派生向量（不论越没越线）

    # ---- 宏观：market_history ----
    mh = _fetch_history(supabase, 'market_history', 'record_date')
    if not mh.empty:
        latest = mh.iloc[-1]
        src = latest.get('source_date') or latest.get('record_date')
        macro_metrics = ['vix', 'move', 'vvix', 'credit_spread', 'credit_z', 'nfci',
                         'hyg_tlt_ratio', 'net_liq', 'pct_200ma', 'pct_20ma', 'trin',
                         'dxy', 'cg_z']
        for m in macro_metrics:
            if m in mh.columns:
                events += scan_metric(m, mh[m], report_date, scope="MACRO", source_date=src)
                row = metric_snapshot(m, mh[m], report_date, scope="MACRO", source_date=src)
                if row: snap_rows.append(row)
                snapshot[m] = pd.to_numeric(latest.get(m), errors='coerce')
        # 派生量：hyg_tlt 21日 ROC
        if 'hyg_tlt_ratio' in mh.columns:
            s = _clean(mh['hyg_tlt_ratio'])
            if len(s) >= 22:
                snapshot['hyg_tlt_ratio_roc'] = float((s.iloc[-1] / s.iloc[-22] - 1) * 100)

    # ---- 现货波动率：macro_spot_daily ----
    ms = _fetch_history(supabase, 'macro_spot_daily', 'date')
    if not ms.empty:
        latest = ms.iloc[-1]
        src = latest.get('source_date') or latest.get('date')
        for m in ['vix_contango_pct', 'breadth_diff_pct', 'tqqq_drag_pct']:
            if m in ms.columns:
                events += scan_metric(m, ms[m], report_date, scope="MACRO", source_date=src)
                row = metric_snapshot(m, ms[m], report_date, scope="MACRO", source_date=src)
                if row: snap_rows.append(row)
                snapshot[m] = pd.to_numeric(latest.get(m), errors='coerce')

    # ---- 个股微观：盘前 DPSV + 盘后 IVR（逐标的拉取，规避1000行截断）----
    dpsv_vals, ivr_vals = [], []
    for tkr in MICRO_SYMBOLS:
        try:
            g = _fetch_history(supabase, 'stock_options_pre_market', 'date', ticker=tkr)
            if not g.empty and 'dpsv_pct' in g.columns:
                src = g.iloc[-1].get('dpsv_source_date') or g.iloc[-1].get('source_date') or g.iloc[-1].get('date')
                events += scan_metric('dpsv_pct', g['dpsv_pct'], report_date, scope=tkr, source_date=src)
                row = metric_snapshot('dpsv_pct', g['dpsv_pct'], report_date, scope=tkr, source_date=src)
                if row: snap_rows.append(row)
                dv = pd.to_numeric(g.iloc[-1].get('dpsv_pct'), errors='coerce')
                if pd.notna(dv): dpsv_vals.append(dv)
        except Exception:
            pass
        try:
            g = _fetch_history(supabase, 'stock_spot_post_close', 'date', ticker=tkr)
            if not g.empty and 'ivr_pct' in g.columns:
                src = g.iloc[-1].get('source_date') or g.iloc[-1].get('date')
                events += scan_metric('ivr_pct', g['ivr_pct'], report_date, scope=tkr, source_date=src)
                row = metric_snapshot('ivr_pct', g['ivr_pct'], report_date, scope=tkr, source_date=src)
                if row: snap_rows.append(row)
                iv = pd.to_numeric(g.iloc[-1].get('ivr_pct'), errors='coerce')
                if pd.notna(iv): ivr_vals.append(iv)
        except Exception:
            pass

    # 共振派生量
    snapshot['dpsv_high'] = max(dpsv_vals) if dpsv_vals else None
    snapshot['ivr_low'] = min(ivr_vals) if ivr_vals else None

    # ---- 第三层共振 ----
    events += scan_resonance(report_date, snapshot)

    # 排序：severity desc, confidence desc
    events.sort(key=lambda e: (e.severity, e.confidence), reverse=True)

    if persist:
        from market_utils import safe_upsert
        if events:
            rows = [e.to_row() for e in events]
            # 唯一键 (report_date, metric, scope, window, layer)，重复运行覆盖
            safe_upsert(supabase, 'anomaly_events', rows,
                        conflict_cols='report_date,metric,scope,window_scope,layer')
        # 每日全量快照：不管越不越线都落库，供曲线/环境评估（唯一键去当日重复）
        if snap_rows:
            safe_upsert(supabase, 'metric_daily', snap_rows,
                        conflict_cols='report_date,metric,scope')

    return events, snapshot


def format_matrix(events):
    """把异常矩阵渲染成给 AI / 邮件的紧凑文本。"""
    if not events:
        return "【异常矩阵】本交易日未检出显著异常。"
    lines = ["=== 📊 多窗口异常矩阵 (severity↓ / confidence↓) ==="]
    layer_cn = {"single": "单指标", "multiwindow": "多窗口确认", "resonance": "跨资产共振"}
    for e in events:
        tag = "🔴" if e.severity >= 3 else ("🟠" if e.severity == 2 else "🟡")
        scope = "" if e.scope in ("MACRO", None) else f"[{e.scope}] "
        conf_pct = int(e.confidence * 100)
        lag_note = f" ⏳滞后{e.lag_days}d" if e.lag_days else ""
        lines.append(f"{tag} [{layer_cn.get(e.layer, e.layer)}|{e.window}] {scope}{e.explanation} "
                     f"(sev {e.severity}/3, 置信 {conf_pct}%{lag_note})")
    return "\n".join(lines)
