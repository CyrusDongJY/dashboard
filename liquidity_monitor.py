# -*- coding: utf-8 -*-
"""Market liquidity water-level monitor.

Scores are shadow observations, not trading signals. A higher score always
means more liquidity support / healthier transmission. Coverage and freshness
are first-class outputs so missing funding data cannot silently look healthy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import math
import re
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)

CALC_VERSION = "liquidity_v2"
SHADOW_MODE = True
MIN_PILLAR_COVERAGE = 0.35
MIN_COMPOSITE_COVERAGE = 0.50

# A relative tail alone cannot promote an observation to a headline stress
# state. At least one economically meaningful absolute guard must also fire.
FUNDING_STRESS_GUARDS = {
    "sofr_iorb_bps": 5.0,
    "tgcr_iorb_bps": 5.0,
    "effr_iorb_bps": 5.0,
    "sofr_75_iorb_bps": 10.0,
    "sofr_dispersion_bps": 25.0,
    "nfci": 0.25,
    "move": 130.0,
}
CREDIT_STRESS_GUARDS = {"hy_spread": 5.0, "ig_spread": 1.5}

PILLARS = {
    "water_stock": ("基础水量", "System Stock", 1.25),
    "flow_pulse": ("边际水流", "Flow Pulse", 1.15),
    "funding_health": ("融资管道", "Funding Pipes", 1.25),
    "credit_transmission": ("信用传导", "Credit Transfer", 1.00),
    "market_distribution": ("市场分配", "Market Breadth", 0.85),
    "tail_resilience": ("尾部韧性", "Tail Resilience", 0.75),
}


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    pillar: str
    label: str
    column: str
    weight: float
    direction: int
    mode: str = "level"
    window: int = 0
    min_obs: int = 30
    baseline_obs: int = 252
    freshness_tolerance: int = 3


COMPONENTS = [
    # Stock: quantity and buffer, not short-term market calm.
    ComponentSpec("net_liq_level", "water_stock", "Net liquidity level",
                  "net_liq_b", 1.25, +1, min_obs=52, baseline_obs=156,
                  freshness_tolerance=8),
    ComponentSpec("reserves_buffer", "water_stock", "Reserve buffer",
                  "reserves_t", 1.10, +1, mode="reserves_buffer",
                  min_obs=1, baseline_obs=1, freshness_tolerance=8),
    ComponentSpec("m2_growth", "water_stock", "M2 YoY growth",
                  "m2_yoy", 0.65, +1, min_obs=12, baseline_obs=36,
                  freshness_tolerance=40),

    # Pulse: marginal additions/drains.
    ComponentSpec("net_liq_20d", "flow_pulse", "Net liquidity 20D change",
                  "net_liq_b", 1.25, +1, mode="change", window=20,
                  min_obs=40, baseline_obs=156, freshness_tolerance=8),
    ComponentSpec("tga_5d", "flow_pulse", "TGA 5D change",
                  "tga_b", 1.00, -1, mode="change", window=5,
                  min_obs=40, baseline_obs=252, freshness_tolerance=3),
    ComponentSpec("fed_assets_20d", "flow_pulse", "Fed assets 20D change",
                  "fed_assets_b", 0.75, +1, mode="change", window=20,
                  min_obs=40, baseline_obs=156, freshness_tolerance=8),

    # Pipes: raw SOFR is deliberately excluded. Only spreads/distribution count.
    ComponentSpec("sofr_iorb", "funding_health", "SOFR - IORB",
                  "sofr_iorb_bps", 0.90, -1, min_obs=40,
                  freshness_tolerance=3),
    ComponentSpec("tgcr_iorb", "funding_health", "TGCR - IORB",
                  "tgcr_iorb_bps", 0.80, -1, min_obs=40,
                  freshness_tolerance=3),
    ComponentSpec("effr_iorb", "funding_health", "EFFR - IORB",
                  "effr_iorb_bps", 0.80, -1, min_obs=40,
                  freshness_tolerance=3),
    ComponentSpec("sofr_75_iorb", "funding_health", "SOFR 75th - IORB",
                  "sofr_75_iorb_bps", 0.65, -1, min_obs=40,
                  freshness_tolerance=3),
    ComponentSpec("sofr_dispersion", "funding_health", "SOFR 99th - median",
                  "sofr_dispersion_bps", 0.65, -1, min_obs=40,
                  freshness_tolerance=3),
    ComponentSpec("nfci", "funding_health", "Chicago Fed NFCI",
                  "nfci", 1.10, -1, min_obs=26, baseline_obs=104,
                  freshness_tolerance=8),
    ComponentSpec("move", "funding_health", "MOVE",
                  "move", 0.55, -1, min_obs=60),

    # Credit willingness and risk-asset transmission.
    ComponentSpec("hy_spread", "credit_transmission", "HY OAS",
                  "hy_spread", 1.20, -1, min_obs=60),
    ComponentSpec("ig_spread", "credit_transmission", "IG OAS",
                  "ig_spread", 0.80, -1, min_obs=60),
    ComponentSpec("hyg_tlt_21d", "credit_transmission", "HYG/TLT 21D",
                  "hyg_tlt_ratio", 0.85, +1, mode="pct_change", window=21,
                  min_obs=60),
    # ETF shares from yfinance are retained as context only. They are not scored
    # until an issuer-grade daily shares source with a trustworthy as-of date is used.

    # Distribution: broad participation, kept separate from system funding.
    ComponentSpec("breadth_20", "market_distribution", "Above 20DMA",
                  "pct_20ma", 0.90, +1, min_obs=60),
    ComponentSpec("breadth_50", "market_distribution", "Above 50DMA",
                  "pct_50ma", 0.80, +1, min_obs=60),
    ComponentSpec("breadth_200", "market_distribution", "Above 200DMA",
                  "pct_200ma", 1.00, +1, min_obs=60),
    ComponentSpec("breadth_daily", "market_distribution", "Daily advancers",
                  "pct_adv", 0.55, +1, min_obs=60),
    ComponentSpec("concentration", "market_distribution", "Cap-weight gap",
                  "breadth_diff_pct", 0.85, -1, min_obs=60),

    # Dealer/tail context. DIX and GEX remain context-only until direction is validated.
    ComponentSpec("vix_futures_curve", "tail_resilience",
                  "VIX futures contango", "vix_futures_contango_pct",
                  0.80, +1, min_obs=60),
    ComponentSpec("vix_spot_curve", "tail_resilience",
                  "VIX/VIX3M contango", "vix_ratio_contango_pct",
                  0.40, +1, min_obs=60),
    ComponentSpec("vvix", "tail_resilience", "VVIX",
                  "vvix", 0.70, -1, min_obs=60),
    ComponentSpec("skew", "tail_resilience", "SKEW",
                  "skew", 0.55, -1, min_obs=60),
]


@dataclass
class ComponentScore:
    key: str
    pillar: str
    label: str
    score: Optional[float]
    quality: float
    raw_value: Optional[float]
    signal_value: Optional[float]
    source_date: Optional[str]
    lag_days: Optional[int]
    note: str = ""
    valid_obs_count: int = 0
    required_obs_count: int = 0


@dataclass
class PillarScore:
    key: str
    label: str
    chart_label: str
    score: Optional[float]
    coverage: float


@dataclass
class LiquidityResult:
    report_date: str
    composite: Optional[float]
    coverage: float
    state: str
    state_detail: str
    pillars: Dict[str, PillarScore]
    components: List[ComponentScore]
    supports: List[str] = field(default_factory=list)
    drags: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    context: Dict[str, Optional[float]] = field(default_factory=dict)
    shadow_mode: bool = SHADOW_MODE
    calc_version: str = CALC_VERSION
    persistence_status: str = "not_attempted"

    def to_row(self):
        pillar = lambda key: (
            self.pillars[key].score if key in self.pillars else None)
        details = {
            "pillars": {key: asdict(value) for key, value in self.pillars.items()},
            "components": [asdict(value) for value in self.components],
            "supports": self.supports,
            "drags": self.drags,
            "warnings": self.warnings,
            "context": self.context,
        }
        return {
            "report_date": self.report_date,
            "water_stock": pillar("water_stock"),
            "flow_pulse": pillar("flow_pulse"),
            "funding_health": pillar("funding_health"),
            "credit_transmission": pillar("credit_transmission"),
            "market_distribution": pillar("market_distribution"),
            "tail_resilience": pillar("tail_resilience"),
            "composite": self.composite,
            "coverage": self.coverage,
            "state": self.state,
            "state_detail": self.state_detail,
            "supports": " | ".join(self.supports) or None,
            "drags": " | ".join(self.drags) or None,
            "warnings": " | ".join(self.warnings) or None,
            "details": details,
            "shadow_mode": self.shadow_mode,
            "calc_version": self.calc_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def _fetch_rows(supabase, table: str, date_col: str, start_date: str,
                end_date: str, page_size: int = 1000) -> pd.DataFrame:
    rows, offset = [], 0
    while True:
        result = (supabase.table(table).select("*")
                  .gte(date_col, start_date).lte(date_col, end_date)
                  .order(date_col)
                  .range(offset, offset + page_size - 1).execute())
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return pd.DataFrame(rows)


def _shares_to_millions(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return np.nan
    text = str(value).strip().replace(",", "")
    match = re.match(r"^([-+]?\d*\.?\d+)\s*([KMBT]?)$", text, re.I)
    if not match:
        return np.nan
    number = float(match.group(1))
    unit = match.group(2).upper()
    return number * {"": 1 / 1_000_000, "K": 1 / 1_000,
                     "M": 1.0, "B": 1_000.0, "T": 1_000_000.0}[unit]


def _normalize_history(frame: pd.DataFrame, date_col: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    frame = frame.dropna(subset=[date_col]).set_index(date_col).sort_index()
    frame.index = frame.index.tz_localize(None)
    return frame.loc[~frame.index.duplicated(keep="last")]


def _expand_full_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    if "full_metrics" not in frame:
        return frame
    for key in ("vrp_num", "vix_term_ratio", "hyg_tlt_roc21"):
        frame[key] = frame["full_metrics"].map(
            lambda value: value.get(key) if isinstance(value, dict) else None)
    return frame


def build_database_frame(supabase, start_date: str,
                         end_date: str) -> pd.DataFrame:
    """Build a normalized frame from the existing production tables."""
    try:
        history = _normalize_history(
            _fetch_rows(supabase, "market_history", "record_date",
                        start_date, end_date),
            "record_date",
        )
    except Exception as exc:
        logger.warning("market_history unavailable: %s", exc)
        history = pd.DataFrame()

    frame = _expand_full_metrics(history)
    if "vix_term_ratio" in frame:
        frame["vix_ratio_contango_pct"] = (
            1.0 - pd.to_numeric(frame["vix_term_ratio"], errors="coerce")) * 100
    renames = {
        "fed_assets": "fed_assets_b",
        "net_liq": "net_liq_b",
        "tga": "tga_b",
        "rrp": "rrp_b",
        "reserves": "reserves_t",
        "credit_spread": "hy_spread",
        "sofr": "sofr_rate",
        "dix": "dix_pct",
        "gex": "gex_billions",
    }
    frame = frame.rename(columns=renames)
    for old, new in (
        ("spy_sh", "spy_sh_m"), ("qqq_sh", "qqq_sh_m"),
        ("hyg_sh", "hyg_sh_m"), ("jnk_sh", "jnk_sh_m"),
    ):
        if old in frame:
            frame[new] = frame[old].map(_shares_to_millions)

    fallback_source = (
        pd.to_datetime(frame["source_date"], errors="coerce")
        if "source_date" in frame else pd.Series(frame.index, index=frame.index)
    )
    for column in list(frame.columns):
        if column.endswith("_source_date") or column == "source_date":
            continue
        frame[f"{column}_source_date"] = fallback_source

    # Native FRED dates override ingestion dates. Missing provenance stays
    # missing so an official-source outage lowers coverage instead of making
    # repeated weekly/monthly values look fresh and daily.
    fred_source_keys = {
        "fed_assets_b": "Fed_Assets",
        "tga_b": "TGA",
        "rrp_b": "RRP",
        "reserves_t": "Reserves",
        "m2_yoy": "M2_Money_Supply",
        "nfci": "NFCI",
        "hy_spread": "Credit_Spread",
        "ig_spread": "IG_Spread",
        "sofr_rate": "SOFR",
    }
    if "full_metrics" in frame:
        for column, native_key in fred_source_keys.items():
            if column not in frame:
                continue
            native_dates = frame["full_metrics"].map(
                lambda value: (value.get("fred_source_dates") or {}).get(native_key)
                if isinstance(value, dict) else None)
            frame[f"{column}_source_date"] = pd.to_datetime(
                native_dates, errors="coerce")
    net_sources = [
        "fed_assets_b_source_date", "tga_b_source_date", "rrp_b_source_date"]
    if "net_liq_b" in frame and all(column in frame for column in net_sources):
        frame["net_liq_b_source_date"] = frame[net_sources].min(axis=1)

    for table, date_col in (
        ("macro_spot_daily", "date"),
        ("macro_options_daily", "date"),
    ):
        try:
            extra = _normalize_history(
                _fetch_rows(supabase, table, date_col, start_date, end_date),
                date_col,
            )
        except Exception as exc:
            logger.warning("%s unavailable: %s", table, exc)
            continue
        if extra.empty:
            continue
        if table == "macro_spot_daily" and "vix_contango_pct" in extra:
            extra = extra.rename(columns={
                "vix_contango_pct": "vix_futures_contango_pct"})
        union_index = frame.index.union(extra.index).sort_values()
        frame = frame.reindex(union_index)
        extra_source = (
            pd.to_datetime(extra["source_date"], errors="coerce")
            if "source_date" in extra else pd.Series(extra.index, index=extra.index)
        )
        for column in list(extra.columns):
            if column in ("source_date", "as_of_time", "ingested_at", "id"):
                continue
            if column in frame:
                frame[column] = extra[column].combine_first(frame[column])
            else:
                frame[column] = extra[column]
            source_col = f"{column}_source_date"
            if source_col in frame:
                frame[source_col] = extra_source.combine_first(frame[source_col])
            else:
                frame[source_col] = extra_source

    frame.index.name = "report_date"
    return frame.sort_index()


def merge_official_frame(database_frame: pd.DataFrame,
                         official_frame: Optional[pd.DataFrame]) -> pd.DataFrame:
    if official_frame is None or official_frame.empty:
        return database_frame.copy()
    official = official_frame.copy()
    official.index = pd.to_datetime(official.index).tz_localize(None)
    index = database_frame.index.union(official.index).sort_values()
    result = database_frame.reindex(index)
    for column in official.columns:
        if column in result:
            result[column] = official[column].combine_first(result[column])
        else:
            result[column] = official[column]
    result.index.name = "report_date"
    return result


def _lag_business_days(source_date, report_date) -> Optional[int]:
    if source_date is None or pd.isna(source_date):
        return None
    start = pd.Timestamp(source_date).date()
    end = pd.Timestamp(report_date).date()
    if start >= end:
        return 0
    return int(np.busday_count(start, end))


def _percentile_rank(series: pd.Series) -> Optional[float]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    if clean.nunique() <= 1:
        return 50.0
    current = clean.iloc[-1]
    return float((clean <= current).mean() * 100)


def _reserve_buffer_score(value: float) -> float:
    # Monitoring bands, not calibrated policy thresholds.
    if value >= 3.0:
        return min(100.0, 80.0 + (value - 3.0) * 40.0)
    if value >= 2.85:
        return 55.0 + (value - 2.85) / 0.15 * 25.0
    if value >= 2.65:
        return 25.0 + (value - 2.65) / 0.20 * 30.0
    return max(0.0, 25.0 - (2.65 - value) * 50.0)


def _score_component(frame: pd.DataFrame, spec: ComponentSpec,
                     report_date: pd.Timestamp) -> ComponentScore:
    empty = ComponentScore(
        spec.key, spec.pillar, spec.label, None, 0.0,
        None, None, None, None, "missing",
        valid_obs_count=0, required_obs_count=spec.min_obs,
    )
    if spec.column not in frame:
        return ComponentScore(
            spec.key, spec.pillar, spec.label, None, 0.0,
            None, None, None, None, "missing column",
            valid_obs_count=0, required_obs_count=spec.min_obs,
        )
    raw = pd.to_numeric(frame[spec.column], errors="coerce").loc[:report_date]
    raw = raw.dropna()
    if raw.empty:
        return ComponentScore(
            spec.key, spec.pillar, spec.label, None, 0.0,
            None, None, None, None, "missing values",
            valid_obs_count=0, required_obs_count=spec.min_obs,
        )

    transformed = raw
    if spec.mode == "change":
        transformed = raw.diff(spec.window)
    elif spec.mode == "pct_change":
        transformed = raw.pct_change(spec.window, fill_method=None) * 100

    transformed = transformed.replace([np.inf, -np.inf], np.nan).dropna()
    source_col = f"{spec.column}_source_date"
    if source_col in frame:
        aligned_sources = pd.to_datetime(
            frame.loc[transformed.index, source_col], errors="coerce")
        valid_count = int(aligned_sources.dropna().nunique())
    else:
        valid_count = len(transformed)
    if valid_count < spec.min_obs:
        return ComponentScore(
            spec.key, spec.pillar, spec.label, None, 0.0,
            float(raw.iloc[-1]), None, None, None,
            f"insufficient observations ({valid_count}/{spec.min_obs})",
            valid_obs_count=valid_count, required_obs_count=spec.min_obs,
        )

    source_date = None
    if source_col in frame:
        source_values = pd.to_datetime(
            frame.loc[raw.index, source_col], errors="coerce").dropna()
        if not source_values.empty:
            source_date = source_values.iloc[-1]
    lag = _lag_business_days(source_date, report_date)
    sample_factor = min(1.0, valid_count / max(1, spec.baseline_obs))
    freshness = 0.75 if lag is None else (
        0.85 ** max(0, lag - spec.freshness_tolerance))
    quality = float(np.clip(sample_factor * freshness, 0, 1))

    raw_value = float(raw.iloc[-1])
    if spec.mode == "reserves_buffer":
        score = _reserve_buffer_score(raw_value)
        signal_value = raw_value
    else:
        baseline = transformed.tail(max(252, spec.baseline_obs))
        percentile = _percentile_rank(baseline)
        if percentile is None:
            return empty
        score = percentile if spec.direction > 0 else 100.0 - percentile
        signal_value = float(transformed.iloc[-1])

    source_text = (
        pd.Timestamp(source_date).strftime("%Y-%m-%d")
        if source_date is not None and not pd.isna(source_date) else None
    )
    return ComponentScore(
        key=spec.key,
        pillar=spec.pillar,
        label=spec.label,
        score=round(float(np.clip(score, 0, 100)), 1),
        quality=round(quality, 3),
        raw_value=round(raw_value, 4),
        signal_value=round(signal_value, 4),
        source_date=source_text,
        lag_days=lag,
        note="ok" if quality >= 0.5 else "low confidence",
        valid_obs_count=valid_count,
        required_obs_count=spec.min_obs,
    )


def _absolute_guard_triggered(context: Dict[str, Optional[float]],
                              guards: Dict[str, float]) -> bool:
    return any(
        context.get(column) is not None and context[column] >= threshold
        for column, threshold in guards.items()
    )


def _classify(pillars: Dict[str, PillarScore],
              coverage: float,
              context: Optional[Dict[str, Optional[float]]] = None
              ) -> Tuple[str, str]:
    context = context or {}
    stock = pillars.get("water_stock")
    pulse = pillars.get("flow_pulse")
    funding = pillars.get("funding_health")
    credit = pillars.get("credit_transmission")
    distribution = pillars.get("market_distribution")
    if (coverage < MIN_COMPOSITE_COVERAGE or stock is None or pulse is None or
            stock.score is None or pulse.score is None):
        return "数据不足", "核心水位或边际流量覆盖不足，不作状态判断"

    if (funding and funding.score is not None and funding.score < 30 and
            _absolute_guard_triggered(context, FUNDING_STRESS_GUARDS)):
        return ("融资管道承压",
                "融资指标同时触发历史弱分位与绝对压力护栏")
    if (credit and credit.score is not None and credit.score < 25 and
            _absolute_guard_triggered(context, CREDIT_STRESS_GUARDS)):
        return ("信用收缩",
                "信用指标同时触发历史弱分位与绝对利差护栏")

    level = "high" if stock.score >= 60 else ("low" if stock.score < 40 else "mid")
    flow = "positive" if pulse.score >= 60 else ("negative" if pulse.score < 40 else "flat")
    states = {
        ("high", "positive"): ("宽松扩张", "存量充裕且边际继续补水"),
        ("high", "flat"): ("高水位平流", "存量充裕，边际流量接近中性"),
        ("high", "negative"): ("高水位抽水", "存量尚充裕，但边际流量正在转弱"),
        ("mid", "positive"): ("边际补水", "存量中性，新增流动性改善"),
        ("mid", "flat"): ("平衡水位", "存量与边际流量均接近历史中位"),
        ("mid", "negative"): ("温和收紧", "存量尚可，但边际抽水值得跟踪"),
        ("low", "positive"): ("低位修复", "水位偏低但边际开始补充"),
        ("low", "flat"): ("低水位观察", "流动性存量偏低且缺少明显补水"),
        ("low", "negative"): ("系统性收紧", "低水位与边际抽水同时出现"),
    }
    state, detail = states[(level, flow)]
    if distribution and distribution.score is not None and distribution.score < 45:
        state = f"{state}·分配集中"
        detail += "；资金分配集中，指数水位好于普通成分股"
    return state, detail


def _latest_value(frame: pd.DataFrame, column: str,
                  report_date: pd.Timestamp) -> Optional[float]:
    if column not in frame:
        return None
    values = pd.to_numeric(
        frame.loc[:report_date, column], errors="coerce").dropna()
    return float(values.iloc[-1]) if not values.empty else None


def _business_day_change(frame: pd.DataFrame, column: str,
                         report_date: pd.Timestamp,
                         business_days: int) -> Optional[float]:
    if column not in frame:
        return None
    values = pd.to_numeric(
        frame.loc[:report_date, column], errors="coerce").dropna()
    baseline_date = report_date - pd.offsets.BDay(business_days)
    baseline = values.loc[:baseline_date]
    if values.empty or baseline.empty:
        return None
    return float(values.iloc[-1] - baseline.iloc[-1])


def score_liquidity_frame(frame: pd.DataFrame,
                          report_date: Optional[str] = None) -> LiquidityResult:
    if frame.empty:
        date_text = report_date or datetime.now().strftime("%Y-%m-%d")
        return LiquidityResult(
            date_text, None, 0.0, "数据不足",
            "未读取到有效流动性历史", {}, [],
        )
    frame = frame.sort_index()
    as_of = pd.Timestamp(report_date or frame.index.max()).normalize()
    frame = frame.loc[:as_of]
    components = [_score_component(frame, spec, as_of) for spec in COMPONENTS]

    pillars = {}
    for key, (label, chart_label, _pillar_weight) in PILLARS.items():
        specs = [spec for spec in COMPONENTS if spec.pillar == key]
        by_key = {component.key: component for component in components}
        total_weight = sum(spec.weight for spec in specs)
        available = 0.0
        weighted = 0.0
        for spec in specs:
            component = by_key[spec.key]
            if component.score is None or component.quality <= 0:
                continue
            effective = spec.weight * component.quality
            available += effective
            weighted += component.score * effective
        coverage = available / total_weight if total_weight else 0.0
        score = weighted / available if (
            available and coverage >= MIN_PILLAR_COVERAGE) else None
        pillars[key] = PillarScore(
            key, label, chart_label,
            round(score, 1) if score is not None else None,
            round(coverage, 3),
        )

    total_pillar_weight = sum(value[2] for value in PILLARS.values())
    available_pillar_weight = 0.0
    composite_sum = 0.0
    for key, (_label, _chart, weight) in PILLARS.items():
        pillar = pillars[key]
        if pillar.score is None:
            continue
        effective = weight * pillar.coverage
        available_pillar_weight += effective
        composite_sum += pillar.score * effective
    coverage = available_pillar_weight / total_pillar_weight
    composite = (composite_sum / available_pillar_weight
                 if coverage >= MIN_COMPOSITE_COVERAGE and available_pillar_weight
                 else None)

    context_columns = (
        "rrp_b", "tga_b", "reserves_t", "sofr_rate", "sofr_volume_b",
        "dix_pct", "gex_billions", "sofr_iorb_bps", "tgcr_iorb_bps",
        "effr_iorb_bps", "sofr_75_iorb_bps", "sofr_dispersion_bps",
        "nfci", "move", "hy_spread", "ig_spread",
    )
    context = {
        key: _latest_value(frame, key, as_of) for key in context_columns
    }
    state, detail = _classify(pillars, coverage, context)
    eligible = [
        component for component in components
        if component.score is not None and component.quality >= 0.35
    ]
    supports = [
        f"{component.label} {component.score:.0f}"
        for component in sorted(
            eligible, key=lambda item: item.score or 0, reverse=True)[:3]
        if component.score is not None and component.score >= 60
    ]
    drags = [
        f"{component.label} {component.score:.0f}"
        for component in sorted(
            eligible, key=lambda item: item.score if item.score is not None else 101)[:3]
        if component.score is not None and component.score <= 40
    ]

    warnings = []
    if context["rrp_b"] is not None and context["rrp_b"] < 50:
        warnings.append("RRP缓冲接近耗尽，TGA与国债结算冲击将更直接")
    if context["reserves_t"] is not None and context["reserves_t"] < 2.85:
        warnings.append("准备金进入黄色敏感区，需联合观察回购利差")
    tga_change = _business_day_change(frame, "tga_b", as_of, 5)
    if tga_change is not None and tga_change >= 100:
        warnings.append("TGA五个工作日增加超过1000亿美元")
    distribution = pillars.get("market_distribution")
    if distribution and distribution.score is not None and distribution.score < 45:
        warnings.append("资金分配集中，指数表现可能高估全市场流动性")
    funding = pillars.get("funding_health")
    if funding and funding.coverage < 0.60:
        warnings.append("融资管道覆盖不足，优先补齐SOFR/TGCR/EFFR相对IORB利差")
    if (funding and funding.score is not None and funding.score < 30 and
            not _absolute_guard_triggered(context, FUNDING_STRESS_GUARDS)):
        warnings.append("融资指标处于历史弱分位，但未触发绝对压力护栏")
    credit = pillars.get("credit_transmission")
    if (credit and credit.score is not None and credit.score < 25 and
            not _absolute_guard_triggered(context, CREDIT_STRESS_GUARDS)):
        warnings.append("信用指标处于历史弱分位，但未触发绝对利差护栏")

    return LiquidityResult(
        report_date=as_of.strftime("%Y-%m-%d"),
        composite=round(composite, 1) if composite is not None else None,
        coverage=round(float(coverage), 3),
        state=state,
        state_detail=detail,
        pillars=pillars,
        components=components,
        supports=supports,
        drags=drags,
        warnings=warnings,
        context=context,
    )


def compute_liquidity_monitor(supabase, report_date: str,
                              official_frame: Optional[pd.DataFrame] = None,
                              persist: bool = True):
    start = (pd.Timestamp(report_date) - timedelta(days=1200)).strftime("%Y-%m-%d")
    database = build_database_frame(supabase, start, report_date)
    frame = merge_official_frame(database, official_frame)
    result = score_liquidity_frame(frame, report_date)
    if persist:
        from market_utils import log_data_quality, safe_upsert
        result.persistence_status = "ok"
        write_result = safe_upsert(
            supabase, "liquidity_daily", result.to_row(),
            conflict_cols="report_date",
        )
        if write_result is None:
            result.persistence_status = "failed"
            result.warnings.append("流动性水位计算完成，但liquidity_daily写入失败")
        log_data_quality(
            supabase, job_name="liquidity_monitor",
            table_name="liquidity_daily",
            status="ok" if write_result is not None else "failed",
            rows_written=1 if write_result is not None else 0,
            notes=f"state={result.state}; coverage={result.coverage:.1%}",
        )
    else:
        result.persistence_status = "skipped"
    return result, frame


def format_liquidity_summary(result: LiquidityResult) -> str:
    lines = [
        "=== 市场流动性水位仪（影子观察，不触发预警） ===",
        f"状态：{result.state}",
        f"说明：{result.state_detail}",
        f"综合水位：{result.composite if result.composite is not None else '数据不足'}",
        f"有效覆盖：{result.coverage:.0%}",
        f"落库状态：{result.persistence_status}",
    ]
    for key in PILLARS:
        pillar = result.pillars.get(key)
        if not pillar or pillar.score is None:
            blockers = [
                component for component in result.components
                if component.pillar == key and component.score is None
            ]
            diagnostic = "；".join(
                f"{item.label} {item.valid_obs_count}/{item.required_obs_count}"
                for item in blockers[:4]
            )
            coverage = pillar.coverage if pillar else 0.0
            suffix = f"（可评分覆盖 {coverage:.0%}"
            if diagnostic:
                suffix += f"；有效观测 {diagnostic}"
            suffix += "）"
            lines.append(f"{PILLARS[key][0]}：数据不足{suffix}")
        else:
            lines.append(
                f"{pillar.label}：{pillar.score:.1f}/100 "
                f"（覆盖 {pillar.coverage:.0%}）")
    if result.supports:
        lines.append("主要支撑：" + "；".join(result.supports))
    if result.drags:
        lines.append("主要拖累：" + "；".join(result.drags))
    if result.warnings:
        lines.append("结构提示：" + "；".join(result.warnings))
    lines.append("注：DIX/FINRA/GEX仅展示上下文，方向未经验证，不直接计分。")
    return "\n".join(lines)
