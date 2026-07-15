# -*- coding: utf-8 -*-
"""
backfill_history.py — 宏观指标历史回补（一次性运行）

目的：曲线化的地基 metric_daily 只能从部署日往前积累，但宏观类指标
（FRED 信用/流动性、yfinance 波动率/汇率/商品）在源头就有完整历史，
可以一次性回补 2 年，让波动率面板、信用面板部署第一天就有丰满曲线。

不可回补（本脚本不碰）：
  - squeezemetrics DIX/GEX、FINRA DPSV（源头无历史）
  - IB 手算希腊值 ZGL/Charm/Vanna/Gamma/Wall（从未存过）
  - TradingView 广度快照（无历史）
  这些只能靠 metric_daily 从今天起向前积累。

方法：对每个可回补指标重建 2 年日频序列，然后【逐历史交易日重放
metric_snapshot】——用与线上引擎完全相同的统计口径回算 z/分位/窗口变化，
保证回补数据与今后每日实时写入的数据同源同口径，曲线不会有"接缝"。

用法：
  python3 backfill_history.py            # 回补并写库
  python3 backfill_history.py dry        # 只算不写，打印每指标覆盖天数
  python3 backfill_history.py 3y         # 自定义回补时长（默认 2y）
"""
import os
import sys

CONFIG_DIR = os.path.expanduser('~/market_dashboard')
if CONFIG_DIR not in sys.path:
    sys.path.append(CONFIG_DIR)

os.environ["PYTHONWARNINGS"] = "ignore"
import warnings
warnings.filterwarnings("ignore")

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

try:
    import market_config as cfg
    import requests
    import yfinance as yf
    from supabase import create_client, Client
    from anomaly_engine import metric_snapshot, METRIC_REGISTRY
    from market_utils import safe_upsert
except ImportError as e:
    print(f"❌ 依赖缺失 ({e})。确认在 ~/market_dashboard/ 下、且环境含 yfinance/requests/supabase。")
    sys.exit(1)

supabase: Client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)

logger = logging.getLogger("backfill")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(ch)

HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}

# 只有引擎注册表里存在、且源头可回补的指标才回补
YF_TICKERS = ['^VIX', '^MOVE', '^VVIX', '^VIX3M', 'DX-Y.NYB', 'HYG', 'TLT', 'HG=F', 'GC=F']
FRED_MAP = {
    'BAMLH0A0HYM2': 'credit_spread',   # 高收益债信用利差
    'NFCI': 'nfci',                    # 芝加哥联储金融状况
    'WALCL': 'Fed_Assets', 'WTREGEN': 'TGA', 'RRPONTSYD': 'RRP',  # 净流动性三件套
}


def fetch_yf(period):
    logger.info(f"拉取 yfinance {len(YF_TICKERS)} 个序列（{period}）...")
    df = yf.download(YF_TICKERS, period=period, interval="1d", progress=False)['Close']
    if isinstance(df, pd.Series):
        df = df.to_frame()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df.ffill()


def fetch_fred(period_days):
    logger.info(f"拉取 FRED {len(FRED_MAP)} 个序列...")
    out = {}
    sess = requests.Session()
    for sid, col in FRED_MAP.items():
        try:
            url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
                   f"&api_key={cfg.FRED_API_KEY}&file_type=json")
            r = sess.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                obs = r.json().get('observations', [])
                if obs:
                    s = pd.DataFrame(obs)
                    s['value'] = pd.to_numeric(s['value'], errors='coerce')
                    s['date'] = pd.to_datetime(s['date'])
                    out[col] = s.dropna().set_index('date')['value']
        except Exception as e:
            logger.warning(f"FRED {sid} 拉取失败: {e}")
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out).sort_index()
    # FRED 是低频/发布滞后的，向前填充到营业日
    bdays = pd.date_range(df.index.min(), pd.Timestamp.now().normalize(), freq='B')
    return df.reindex(df.index.union(bdays)).ffill().reindex(bdays)


def build_metric_series(yf_df, fred_df):
    """把原始源序列映射成引擎注册表里的 metric 名 -> 日频序列。"""
    series = {}
    col = lambda t: yf_df[t] if t in yf_df.columns else None

    if col('^VIX') is not None: series['vix'] = yf_df['^VIX']
    if col('^MOVE') is not None: series['move'] = yf_df['^MOVE']
    if col('^VVIX') is not None: series['vvix'] = yf_df['^VVIX']
    if col('DX-Y.NYB') is not None: series['dxy'] = yf_df['DX-Y.NYB']

    # VIX 期限结构：contango% = (VIX3M/VIX - 1)*100（与盘后口径一致：升水为正）
    if col('^VIX') is not None and col('^VIX3M') is not None:
        v, v3 = yf_df['^VIX'], yf_df['^VIX3M']
        series['vix_contango_pct'] = ((v3 / v) - 1.0) * 100.0

    # HYG/TLT 比值
    if col('HYG') is not None and col('TLT') is not None:
        series['hyg_tlt_ratio'] = yf_df['HYG'] / yf_df['TLT']

    # 铜金比 z：注册表里的 cg_z 是"已 z 化"的量，这里直接给铜金比原值，
    # metric_snapshot 会算它自己的 z——为避免二次 z 化语义混乱，回补铜金比原值到 cg_z 不合适。
    # 因此铜金比这轮不回补为 cg_z（留待线上积累），只回补能直接对应的原值指标。

    if not fred_df.empty:
        if 'credit_spread' in fred_df.columns: series['credit_spread'] = fred_df['credit_spread']
        if 'nfci' in fred_df.columns: series['nfci'] = fred_df['nfci']
        # 真实净流动性（万亿）：Fed - TGA - RRP，单位对齐盘后（/1000 到十亿再到万亿口径按盘后）
        if {'Fed_Assets', 'TGA', 'RRP'}.issubset(fred_df.columns):
            series['net_liq'] = (fred_df['Fed_Assets'] / 1000) - (fred_df['TGA'] / 1000) - fred_df['RRP']

    # 只保留注册表里认识的指标
    return {m: s.dropna() for m, s in series.items() if m in METRIC_REGISTRY and not s.dropna().empty}


def replay(metric, series):
    """逐历史交易日重放 metric_snapshot：第 i 天只用前 i 行，口径与线上完全一致。"""
    rows = []
    idx = series.index
    vals = series
    n = len(vals)
    # 从有足够样本起步（>=30，与 zscore_252 的下限一致），太早的点 z/分位为 None 但仍留 value 曲线
    start = 0
    for i in range(start, n):
        window = vals.iloc[:i + 1]
        report_date = idx[i].strftime('%Y-%m-%d')
        row = metric_snapshot(metric, window, report_date,
                              scope="MACRO", source_date=report_date)
        if row:
            rows.append(row)
    return rows


def main():
    args = [a.lower() for a in sys.argv[1:]]
    dry = 'dry' in args
    period = next((a for a in args if a.endswith('y') or a.endswith('mo')), '2y')
    period_days = 730

    yf_df = fetch_yf(period)
    fred_df = fetch_fred(period_days)
    metric_series = build_metric_series(yf_df, fred_df)

    if not metric_series:
        logger.error("没有可回补的指标序列，退出。")
        return

    logger.info(f"可回补指标：{list(metric_series.keys())}")
    all_rows = []
    for metric, s in metric_series.items():
        rows = replay(metric, s)
        all_rows += rows
        logger.info(f"  {metric:20} 覆盖 {len(rows)} 个交易日 "
                    f"({s.index.min().date()} → {s.index.max().date()})")

    logger.info(f"合计 {len(all_rows)} 行 metric_daily。")

    if dry:
        logger.info("DRY-RUN：不写库。")
        return

    # 分批写入，(report_date, metric, scope) 唯一，重复运行覆盖不重复
    BATCH = 500
    for i in range(0, len(all_rows), BATCH):
        chunk = all_rows[i:i + BATCH]
        safe_upsert(supabase, 'metric_daily', chunk,
                    conflict_cols='report_date,metric,scope')
        logger.info(f"  写入 {i + len(chunk)}/{len(all_rows)}")
    logger.info("✅ 历史回补完成。波动率/信用/流动性面板现在应有丰满曲线。")


if __name__ == "__main__":
    main()
