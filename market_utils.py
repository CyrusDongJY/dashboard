# -*- coding: utf-8 -*-
"""
market_utils.py — 全系统共享的数据契约层
提供：交易日窗口计算 / 数据新鲜度元数据 / 数据质量记录 / 带重试的 upsert

设计约定（数据契约）：
  每条落库记录必须携带三个时间字段：
    source_date : 数据本身所属的日期（如 FINRA 文件日期），滞后数据用真实日期
    as_of_time  : 该数据在数据源侧的截面时间（ISO8601，UTC）
    ingested_at : 本系统抓取入库的时间（ISO8601，UTC）
  由此下游异常引擎可以计算 lag_days = 报告交易日 - source_date，
  并按新鲜度对告警置信度降权。
"""
import time
import logging
from datetime import datetime, timezone

import pytz
import pandas as pd
import pandas_market_calendars as mcal

from data_contracts import (
    ETF_FLOW_TICKERS, calculate_iv_rank_percentile,
    compute_etf_share_metrics, up_down_volume_ratio,
)

NY_TZ = pytz.timezone('America/New_York')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# 交易日窗口：统一用 NYSE 交易日行数，不用自然日 timedelta
# ---------------------------------------------------------------
_nyse = None

def _get_nyse():
    global _nyse
    if _nyse is None:
        _nyse = mcal.get_calendar('NYSE')
    return _nyse

def trading_days_back(n, end_date=None):
    """返回距 end_date（默认纽约今天）向前第 n 个交易日的日期字符串 'YYYY-MM-DD'。
    n=1 表示上一交易日。失败时回退到自然日近似（n*1.6 天）。"""
    end = end_date or datetime.now(NY_TZ).strftime('%Y-%m-%d')
    try:
        # 向前多取一些自然日以覆盖节假日
        start = (pd.Timestamp(end) - pd.Timedelta(days=int(n * 2.2) + 10)).strftime('%Y-%m-%d')
        sched = _get_nyse().schedule(start_date=start, end_date=end)
        days = sched.index
        if len(days) >= n + 1:
            return days[-(n + 1)].strftime('%Y-%m-%d')
        return days[0].strftime('%Y-%m-%d')
    except Exception as e:
        logger.warning(f"交易日历计算失败，回退自然日近似: {e}")
        return (pd.Timestamp(end) - pd.Timedelta(days=int(n * 1.6) + 3)).strftime('%Y-%m-%d')

def is_trading_day(date_str=None):
    d = date_str or datetime.now(NY_TZ).strftime('%Y-%m-%d')
    try:
        sched = _get_nyse().schedule(start_date=d, end_date=d)
        return not sched.empty
    except Exception as e:
        logger.warning(f"日历检测异常: {e}")
        return False

def lag_trading_days(source_date, ref_date=None):
    """source_date 相对 ref_date（默认今天）滞后了几个交易日。0 = 当日数据。"""
    ref = ref_date or datetime.now(NY_TZ).strftime('%Y-%m-%d')
    try:
        if str(source_date) >= str(ref):
            return 0
        sched = _get_nyse().schedule(start_date=str(source_date), end_date=str(ref))
        return max(0, len(sched) - 1)
    except Exception:
        return max(0, (pd.Timestamp(ref) - pd.Timestamp(str(source_date))).days)

# ---------------------------------------------------------------
# 数据新鲜度元数据
# ---------------------------------------------------------------
def attach_metadata(payload, source_date=None):
    """给落库 payload 附加 source_date / as_of_time / ingested_at。
    source_date 缺省时取 payload 里的 date/record_date（即认为是当日数据）。"""
    now_utc = datetime.now(timezone.utc).isoformat()
    sd = source_date or payload.get('date') or payload.get('record_date')
    payload.setdefault('source_date', str(sd) if sd else None)
    payload.setdefault('as_of_time', now_utc)
    payload['ingested_at'] = now_utc
    return payload

# ---------------------------------------------------------------
# 带指数退避重试的 upsert / insert
# ---------------------------------------------------------------
def safe_upsert(supabase, table_name, data, conflict_cols='date', max_retries=3, sleep_fn=time.sleep):
    """通用容灾 upsert。data 可为 dict 或 list[dict]。失败最终返回 None（不抛出）。"""
    for attempt in range(max_retries):
        try:
            return supabase.table(table_name).upsert(data, on_conflict=conflict_cols).execute()
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⚠️ 数据库写入异常 ({table_name})，{wait}秒后重试: {e}")
                sleep_fn(wait)
            else:
                logger.error(f"❌ 数据库写入最终失败 ({table_name}): {e}")
                return None

# ---------------------------------------------------------------
# 数据质量记录：每个抓取任务、每张表一条
# ---------------------------------------------------------------
def log_data_quality(supabase, job_name, table_name, status,
                     rows_written=0, missing_fields=None, lag_days=0,
                     anomaly_count=0, notes=''):
    """写入 data_quality 表。status: ok / partial / failed。
    该函数永不抛出——质量记录失败不能拖垮主流程。"""
    try:
        record = {
            "run_date": datetime.now(NY_TZ).strftime('%Y-%m-%d'),
            "job_name": job_name,
            "table_name": table_name,
            "status": status,
            "rows_written": int(rows_written),
            "missing_fields": ','.join(missing_fields) if missing_fields else None,
            "lag_days": int(lag_days),
            "anomaly_count": int(anomaly_count),
            "notes": str(notes)[:500] if notes else None,
            "logged_at": datetime.now(timezone.utc).isoformat(),
        }
        supabase.table('data_quality').insert(record).execute()
    except Exception as e:
        logger.warning(f"⚠️ data_quality 记录失败（不影响主流程）: {e}")

def find_missing_fields(payload, expected_fields):
    """返回 payload 中值为 None/NaN/'-' 的期望字段列表。"""
    missing = []
    for f in expected_fields:
        v = payload.get(f)
        if v is None or v == '-' or (isinstance(v, float) and pd.isna(v)):
            missing.append(f)
    return missing
