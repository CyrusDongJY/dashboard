import os
import sys

# ================= 🔐 安全挂载全局金库 =================
CONFIG_DIR = os.path.expanduser('~/market_dashboard')
if CONFIG_DIR not in sys.path:
    sys.path.append(CONFIG_DIR)

try:
    import market_config as cfg
except ImportError:
    print("❌ 致命错误：找不到 market_config.py 密钥文件！程序终止。")
    sys.exit(1)

os.environ["PYTHONWARNINGS"] = "ignore" 
import warnings
warnings.filterwarnings("ignore")

import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import math
import json

from liquidity_sources import OfficialLiquiditySources
from data_contracts import (
    ETF_FLOW_TICKERS,
    classify_hyg_tlt,
    compute_etf_share_metrics,
)
from market_utils import lag_trading_days
from tactical_stress import StressInput, nullable_int, score_eod_stress

class UltimateDashboard:
    def __init__(self):
        # ✅ 从金库安全读取 FRED API 密钥
        self.fred_api_key = cfg.FRED_API_KEY
        self.lake_dir = os.path.expanduser('~/DataLake')
        os.makedirs(self.lake_dir, exist_ok=True)

        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json'
        }
        
        self.tickers_dict = {'QQQ': 'NDX 100 (QQQ)', 'SPY': 'S&P 500 (SPY)', 'DIA': 'Dow Jones (DIA)', 'IWM': 'Russell 2000 (IWM)'}
        self.sector_dict = {
            'XLK': '科技', 'XLF': '金融', 'XLV': '医疗', 'XLY': '可选消费', 
            'XLP': '日常消费', 'XLE': '能源', 'XLI': '工业', 'XLU': '公用事业', 
            'XLB': '材料', 'XLRE': '房地产', 'XLC': '通信'
        }
        
        self.data_cache, self.smf, self.liquidity, self.regime, self.score_details, self.macro_engines = {}, {}, {}, {}, {}, {}
        self.sector_rs, self.risk_scissors, self.vol_metrics = {}, {}, {}
        # 结构化数值缓存：与展示字符串平行存储原始数值，供直接落库（替代正则解析文本）
        self.num = {}
        self.fred_source_dates = {}
        self.liquidity_source_names = {}
        self.market_source_dates = {}
        self.stress_details = {}

    def log(self, msg):
        print(msg)

    def clean_trading_days(self, series):
        s = series.replace([np.inf, -np.inf], np.nan).dropna()
        s = s[s.index.dayofweek < 5]
        return s.loc[~s.index.duplicated(keep='last')]

    def calc_zscore(self, series, window=252):
        # min_periods 保证样本不足 window 时用可得历史计算，而不是整列 NaN 后静默返回 0
        s = self.clean_trading_days(series)
        if len(s) < 30: return 0.0
        min_p = max(30, min(window, len(s)) // 2)
        rolling_mean = s.rolling(window=window, min_periods=min_p).mean()
        rolling_std = s.rolling(window=window, min_periods=min_p).std()
        z = (s - rolling_mean) / rolling_std
        return float(z.iloc[-1]) if pd.notna(z.iloc[-1]) else 0.0

    def fetch_market_data(self):
        try:
            real_time_tickers = list(self.tickers_dict.keys()) + list(self.sector_dict.keys()) + [
                '^VIX', '^MOVE', '^VIX3M', '^VVIX', '^SKEW', 
                'DX-Y.NYB', 'JPY=X', 'BTC-USD', 'GC=F', 'CL=F', '^TNX', 
                'HYG', 'TLT', 'HG=F', 'QQQE', 'RSP', 'SOXX',
                'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA'
            ]
            # 2y 数据窗口：保证 252D z-score 基准真正生效（6mo 只有约126个交易日，rolling(252) 全 NaN）
            recent_df = yf.download(real_time_tickers, period="2y", interval="1d", progress=False)['Close']
            if not recent_df.empty:
                if isinstance(recent_df, pd.Series): recent_df = recent_df.to_frame(name=real_time_tickers[0])
                recent_df.index = pd.to_datetime(recent_df.index).normalize().tz_localize(None)
                self.data_cache['raw_close'] = recent_df
                self.data_cache['close'] = recent_df.ffill()
                for symbol in recent_df.columns:
                    native = recent_df[symbol].dropna()
                    if not native.empty:
                        self.market_source_dates[str(symbol)] = (
                            native.index.max().strftime('%Y-%m-%d'))
            
            vol_data = yf.download(['QQQ'], period="2y", interval="1d", progress=False).dropna(how='all')
            if not vol_data.empty:
                vol_data.index = pd.to_datetime(vol_data.index).normalize().tz_localize(None)
                self.data_cache['qqq_full'] = vol_data[~vol_data.index.duplicated(keep='last')]
                self.market_source_dates['QQQ_OHLCV'] = (
                    vol_data.index.max().strftime('%Y-%m-%d'))
        except Exception: pass

    def fetch_fred_data_direct(self):
        fred_map = {
            'WALCL': 'Fed_Assets', 'WTREGEN': 'TGA', 'RRPONTSYD': 'RRP',
            'BAMLH0A0HYM2': 'Credit_Spread', 'BAMLC0A0CM': 'IG_Spread',
            'M2SL': 'M2_Money_Supply', 'T10Y2Y': 'Spread_10Y2Y',
            'NFCI': 'NFCI', 'DGS10': 'US10Y', 'DFII10': 'TIPS10Y', 
            'SOFR': 'SOFR', 'WRESBAL': 'Reserves', 'ECBASSETSW': 'ECB_Assets',
            'JPNASSETS': 'BOJ_Assets', 'DEXUSEU': 'EUR_USD', 'DEXJPUS': 'USD_JPY'
        }
        
        session = requests.Session()
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry_strategy = Retry(
            total=5, 
            backoff_factor=1, 
            status_forcelist=[429, 500, 502, 503, 504]
        )
        session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
        
        series_dict = {}
        for series_id, col_name in fred_map.items():
            try:
                url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={self.fred_api_key}&file_type=json"
                res = session.get(url, headers=self.headers, timeout=30)
                if res.status_code == 200:
                    data = res.json().get('observations', [])
                    if data:
                        s = pd.DataFrame(data)
                        s['value'] = pd.to_numeric(s['value'], errors='coerce')
                        s['date'] = pd.to_datetime(s['date'])
                        native = s.dropna().set_index('date')['value']
                        series_dict[col_name] = native
                        if not native.empty:
                            self.fred_source_dates[col_name] = native.index.max().strftime('%Y-%m-%d')
                            self.liquidity_source_names[col_name] = f"FRED:{series_id}"
            except: continue
                
        if series_dict:
            df_fred = pd.DataFrame(series_dict).sort_index()
            all_days = pd.date_range(start=df_fred.index.min(), end=pd.Timestamp.now().normalize(), freq='B')
            df_fred = df_fred.reindex(df_fred.index.union(all_days)).ffill().reindex(all_days)
            self.data_cache['fred_historical'] = df_fred
            latest = df_fred.iloc[-1]
            
            self.liquidity.update({
                'assets': round(latest.get('Fed_Assets', 0)/1000, 2) if pd.notna(latest.get('Fed_Assets')) else '-',
                'tga': round(latest.get('TGA', 0)/1000, 2) if pd.notna(latest.get('TGA')) else '-',
                'rrp': round(latest.get('RRP', 0), 2) if pd.notna(latest.get('RRP')) else '-',
                'credit': round(latest.get('Credit_Spread', 0), 2) if pd.notna(latest.get('Credit_Spread')) else '-',
                'ig_spread': round(latest.get('IG_Spread', 0), 2) if pd.notna(latest.get('IG_Spread')) else '-',
                'nfci': round(latest.get('NFCI', 0), 3) if pd.notna(latest.get('NFCI')) else '-',
                'sofr': round(latest.get('SOFR', 0), 2) if pd.notna(latest.get('SOFR')) else '-',
                'y10': round(latest.get('US10Y', 0), 2) if pd.notna(latest.get('US10Y')) else '-',
                'tips': round(latest.get('TIPS10Y', 0), 2) if pd.notna(latest.get('TIPS10Y')) else '-',
                # WRESBAL单位为百万美元，转成万亿美元。
                'reserves': round(latest.get('Reserves', 0)/1_000_000, 3) if pd.notna(latest.get('Reserves')) else '-'
            })
            
            if self.liquidity['assets'] != '-' and self.liquidity['tga'] != '-' and self.liquidity['rrp'] != '-':
                self.liquidity['net_liq'] = round(self.liquidity['assets'] - self.liquidity['tga'] - self.liquidity['rrp'], 2)
            
            self.regime['spread'] = int(latest.get('Spread_10Y2Y', 0) * 100) if pd.notna(latest.get('Spread_10Y2Y')) else '-'
            
            try:
                fed = latest.get('Fed_Assets', 0) / 1000
                ecb_usd = (latest.get('ECB_Assets', 0) * latest.get('EUR_USD', 1)) / 1000
                boj_usd = (latest.get('BOJ_Assets', 0) / 10) / latest.get('USD_JPY', 150)
                if fed > 0: self.liquidity['g3_liq'] = round(fed + ecb_usd + boj_usd, 2)
            except: self.liquidity['g3_liq'] = '-'

    def refresh_daily_tga_official(self):
        """Use the same daily Treasury TGA contract as the Waterline monitor."""
        try:
            today = pd.Timestamp.now(tz="America/New_York").date()
            start = (pd.Timestamp(today) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
            source = OfficialLiquiditySources(self.fred_api_key)
            tga = source.fetch_tga(start, str(today))
            if tga.empty:
                return
            latest_date = pd.Timestamp(tga.index[-1]).strftime("%Y-%m-%d")
            self.liquidity['tga'] = round(float(tga.iloc[-1]), 2)
            self.fred_source_dates['TGA'] = latest_date
            self.liquidity_source_names['TGA'] = "TreasuryFiscalData:DTS"
            fred_history = self.data_cache.get('fred_historical')
            if fred_history is not None and not fred_history.empty and 'TGA' in fred_history:
                target = fred_history.index
                aligned_tga_m = (tga.reindex(target.union(tga.index)).sort_index()
                                 .ffill().reindex(target) * 1_000)
                first_official = pd.Timestamp(tga.index.min())
                mask = target >= first_official
                fred_history.loc[mask, 'TGA'] = aligned_tga_m.loc[mask]
            if all(self.liquidity.get(key, '-') != '-' for key in ('assets', 'tga', 'rrp')):
                self.liquidity['net_liq'] = round(
                    self.liquidity['assets'] - self.liquidity['tga'] - self.liquidity['rrp'], 2)
        except Exception as exc:
            self.log(f"⚠️ Treasury日度TGA获取失败，保留FRED周度口径: {exc}")

    def fetch_liquidity_and_smf(self):
        self.smf = {
            'dix': '-', 'gex': '-', 'dix_gex_source_date': None,
            'sp500_net': '-', 'nasdaq_net': '-', 'cot_vix': '-',
            'cot_report_date': None, 'cot_category': 'Leveraged Money',
            'cot_metadata': {}, 'spy_sh': '-', 'qqq_sh': '-', 'hyg_sh': '-',
            'jnk_sh': '-', 'hyg_px': '-', 'jnk_px': '-', 'junk_flow': '-',
            'etf_share_metrics': {},
        }
        self.score_details.update({
            'trin': '-', 'trin_scope': 'top500_nyse_nasdaq_by_mktcap',
            'trin_source': 'TradingViewScanner', 'trin_as_of': None,
            'trin_closing_auction_inclusion': 'UNVERIFIED',
            'breadth_sample_size': 0,
            'pct_adv': '-', 'up_down_volume_ratio': '-',
            'pct_20ma': '-', 'pct_50ma': '-', 'pct_200ma': '-',
            'nh': '-', 'nl': '-', 'net_nh_nl': '-',
            'breadth_thrust': '⚪ 未触发'
        })
        self.fetch_fred_data_direct()
        self.refresh_daily_tga_official()

        try:
            df_sm = pd.read_csv("https://squeezemetrics.com/monitor/static/DIX.csv")
            latest_sm = df_sm.iloc[-1]
            self.smf.update({
                'dix': round(latest_sm['dix'] * 100, 2),
                'gex': round(latest_sm['gex'] / 1e9, 2),
                'dix_gex_source_date': str(latest_sm.get('date'))[:10],
            })
        except: pass

        try:
            url_cftc = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
            date_res = requests.get(url_cftc, params={"$limit": "1", "$select": "report_date_as_yyyy_mm_dd", "$order": "report_date_as_yyyy_mm_dd DESC"}, headers=self.headers, timeout=10).json()
            if date_res:
                latest_date = date_res[0]['report_date_as_yyyy_mm_dd'][:10]
                self.smf['cot_report_date'] = latest_date
                latest_rows = requests.get(
                    url_cftc,
                    params={"report_date_as_yyyy_mm_dd": latest_date,
                            "$limit": "1500"},
                    headers=self.headers, timeout=15).json()

                def cot_net(row):
                    return float(row.get(
                        'lev_money_positions_long_all',
                        row.get('lev_money_positions_long', 0))) - float(row.get(
                            'lev_money_positions_short_all',
                            row.get('lev_money_positions_short', 0)))

                for item in latest_rows:
                    name = item.get('contract_market_name', '').upper()
                    key = None
                    output_key = None
                    if 'S&P 500' in name and 'MINI' in name and 'MICRO' not in name:
                        key, output_key = 'sp500', 'sp500_net'
                    elif 'NASDAQ' in name and '100' in name and 'MICRO' not in name:
                        key, output_key = 'nasdaq100', 'nasdaq_net'
                    elif 'VIX FUTURES' in name:
                        key, output_key = 'vix', 'cot_vix'
                    if key is None:
                        continue
                    net = cot_net(item)
                    self.smf[output_key] = f"{int(net):,}"
                    code = str(item.get('cftc_contract_market_code', ''))
                    history = []
                    if code.replace('-', '').isalnum():
                        history = requests.get(
                            url_cftc,
                            params={
                                "$where": f"cftc_contract_market_code='{code}'",
                                "$order": "report_date_as_yyyy_mm_dd DESC",
                                "$limit": "156",
                            }, headers=self.headers, timeout=15).json()
                    net_history_values = []
                    for row in history:
                        try:
                            net_history_values.append(cot_net(row))
                        except (TypeError, ValueError):
                            continue
                    net_history = pd.Series(net_history_values, dtype=float)
                    zscore = None
                    if len(net_history) >= 30 and net_history.std(ddof=1) > 0:
                        zscore = float(
                            (net - net_history.mean()) / net_history.std(ddof=1))
                    open_interest = item.get('open_interest_all')
                    try:
                        net_oi_pct = net / float(open_interest) * 100.0
                    except (TypeError, ValueError, ZeroDivisionError):
                        net_oi_pct = None
                    self.smf['cot_metadata'][key] = {
                        'report_date': latest_date,
                        'category': 'Leveraged Money',
                        'contract_market_name': item.get('contract_market_name'),
                        'contract_code': code or None,
                        'net_contracts': int(net),
                        'net_open_interest_pct': round(net_oi_pct, 4)
                        if net_oi_pct is not None else None,
                        'zscore_156w': round(zscore, 3)
                        if zscore is not None else None,
                        'sample_count': int(len(net_history)),
                    }
        except: pass

        try:
            url_scan = "https://scanner.tradingview.com/america/scan"
            payload = {
                "filter": [
                    {"left": "exchange", "operation": "in_range", "right": ["NYSE", "NASDAQ"]},
                    {"left": "is_primary", "operation": "equal", "right": True},
                    {"left": "type", "operation": "equal", "right": "stock"}
                ],
                "columns": ["name", "change", "close", "SMA20", "SMA50", "SMA200", "price_52_week_high", "price_52_week_low", "volume"],
                "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
                "range": [0, 500] 
            }
            stocks = requests.post(url_scan, json=payload, headers=self.headers, timeout=15).json().get("data", [])
            if stocks and len(stocks) >= 450:
                total = len(stocks)
                adv_i = dec_i = adv_v = dec_v = a20 = a50 = a200 = nh = nl = 0
                for item in stocks:
                    d = item['d']
                    chg, cl, s20, s50, s200, hi52, lo52, vol = d[1], d[2], d[3], d[4], d[5], d[6], d[7], d[8]
                    if chg and vol:
                        if chg > 0: adv_i += 1; adv_v += vol
                        elif chg < 0: dec_i += 1; dec_v += vol
                    if cl:
                        if s20 and cl > s20: a20 += 1
                        if s50 and cl > s50: a50 += 1
                        if s200 and cl > s200: a200 += 1
                    if cl and hi52 and cl >= hi52 * 0.99: nh += 1
                    if cl and lo52 and cl <= lo52 * 1.01: nl += 1
                
                trin = (adv_i / dec_i) / (adv_v / dec_v) if dec_i > 0 and dec_v > 0 and (adv_v/dec_v) > 0 else 0.0
                self.score_details.update({
                    'trin': round(trin, 2), 'pct_20ma': round((a20 / total) * 100, 1), 
                    'pct_50ma': round((a50 / total) * 100, 1), 'pct_200ma': round((a200 / total) * 100, 1), 
                    'net_nh_nl': nh - nl, 'nh': nh, 'nl': nl,
                    'pct_adv': round(
                        adv_i / (adv_i + dec_i) * 100, 1)
                    if adv_i + dec_i > 0 else None,
                    'up_down_volume_ratio': round(adv_v / dec_v, 3)
                    if dec_v > 0 else None,
                    'breadth_sample_size': total,
                    'trin_as_of': pd.Timestamp.now(
                        tz="America/New_York").isoformat(),
                })

                today_str = pd.Timestamp.now(
                    tz="America/New_York").strftime("%Y-%m-%d")
                cache_file = os.path.join(self.lake_dir, 'breadth_history.json')
                history = []
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, 'r') as f: history = json.load(f)
                    except: pass
                
                history = [h for h in history if h['date'] != today_str]
                pct_adv = (adv_i / (adv_i + dec_i)) * 100 if (adv_i + dec_i) > 0 else 0
                history.append({'date': today_str, 'pct_adv': pct_adv})
                # 保留约 2 年历史，供多窗口分位计算；推力判定仍只看最近几天
                history = history[-500:]
                
                with open(cache_file, 'w') as f: json.dump(history, f)
                
                thrust_alert = "⚪ 未触发"
                if len(history) >= 2:
                    if pct_adv >= 90:
                        for past in history[-4:-1]:
                            if past['pct_adv'] <= 10:
                                thrust_alert = "🚀【极致广度推力】90%抛售瞬间切为90%买盘！底部确立"
                                break
                    elif pct_adv <= 10:
                        thrust_alert = "🔴【极致恐慌抛售】市场内超90%股票处于下跌状态"
                self.score_details['breadth_thrust'] = thrust_alert

        except: pass

        try:
            new_records = []
            today_str = pd.Timestamp.now(
                tz="America/New_York").strftime("%Y-%m-%d")
            csv_file = os.path.expanduser("~/market_dashboard/ETF_Flows_Cache.csv")
            for ticker in ETF_FLOW_TICKERS:
                tk = yf.Ticker(ticker)
                shares = tk.info.get('sharesOutstanding', 0)
                if shares == 0 and hasattr(tk, 'fast_info'):
                    try: shares = tk.fast_info.get('shares', 0)
                    except: pass
                hist = tk.history(period="1d")
                if not hist.empty:
                    price = hist['Close'].iloc[-1]
                    if ticker == 'HYG': self.smf['hyg_px'] = round(price, 2)
                    if ticker == 'JNK': self.smf['jnk_px'] = round(price, 2)
                    if shares > 0:
                        new_records.append({'Date': today_str, 'Ticker': ticker, 'Shares': shares, 'Price': price})
                        if ticker == 'SPY': self.smf['spy_sh'] = f"{round(shares/1e6, 2)}M"
                        elif ticker == 'QQQ': self.smf['qqq_sh'] = f"{round(shares/1e6, 2)}M"
                        elif ticker == 'HYG': self.smf['hyg_sh'] = f"{round(shares/1e6, 2)}M"
                        elif ticker == 'JNK': self.smf['jnk_sh'] = f"{round(shares/1e6, 2)}M"

            if new_records:
                if os.path.exists(csv_file):
                    try:
                        df_hist = pd.read_csv(csv_file)
                    except Exception:
                        df_hist = pd.DataFrame(columns=['Date', 'Ticker', 'Shares', 'Price'])
                else:
                    df_hist = pd.DataFrame(columns=['Date', 'Ticker', 'Shares', 'Price'])
                combined = pd.concat(
                    [df_hist, pd.DataFrame(new_records)], ignore_index=True)
                combined['Date'] = pd.to_datetime(
                    combined['Date'], errors='coerce').dt.strftime('%Y-%m-%d')
                combined = (combined.dropna(subset=['Date', 'Ticker'])
                            .sort_values(['Date', 'Ticker'])
                            .drop_duplicates(['Date', 'Ticker'], keep='last'))
                os.makedirs(os.path.dirname(csv_file), exist_ok=True)
                temp_file = f"{csv_file}.tmp"
                combined.to_csv(temp_file, index=False)
                os.replace(temp_file, csv_file)

                metrics = compute_etf_share_metrics(combined)
                self.smf['etf_share_metrics'] = metrics
                debt_flows = []
                for ticker in ('HYG', 'JNK'):
                    one_day = metrics.get(ticker, {}).get('changes', {}).get('1d')
                    if one_day and one_day.get('dollar_flow_m') is not None:
                        debt_flows.append(one_day['dollar_flow_m'])
                self.smf['junk_flow'] = (
                    round(sum(debt_flows), 2) if len(debt_flows) == 2 else '-')
        except Exception as exc:
            self.log(f"⚠️ ETF份额历史更新失败: {exc}")

    def calculate_metrics(self):
        df, raw_df = self.data_cache.get('close', pd.DataFrame()), self.data_cache.get('raw_close', pd.DataFrame())
        fh = self.data_cache.get('fred_historical', pd.DataFrame())
        
        self.score_details.update({
            'macro_score': 0, 'micro_score': None,
            'eod_stress_score': None, 'vix': '-', 'move': '-',
            'pcr': '-', 'cmf': '-',
        })
        self.regime = {'desc': '不明朗', 'spread': self.regime.get('spread', '-'), 'cg_ratio': '-', 'hyg': '-', 'oil': '-', 'dxy': '-', 'btc': '-', 'gold': '-', 'jpy': '-'}

        mag7_symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA']
        one_day_returns = {}
        for symbol in ['QQQ', 'QQQE', 'SPY', 'RSP', *mag7_symbols]:
            if symbol not in df.columns:
                continue
            series = self.clean_trading_days(df[symbol])
            if len(series) >= 2:
                one_day_returns[symbol] = float(
                    (series.iloc[-1] / series.iloc[-2] - 1.0) * 100.0)
        mag7_values = [one_day_returns[symbol] for symbol in mag7_symbols
                       if symbol in one_day_returns]
        mag7_return = (sum(mag7_values) / len(mag7_values)
                       if len(mag7_values) == len(mag7_symbols) else None)
        qqq_qqqe = (one_day_returns['QQQ'] - one_day_returns['QQQE']
                    if all(key in one_day_returns for key in ('QQQ', 'QQQE'))
                    else None)
        spy_rsp = (one_day_returns['SPY'] - one_day_returns['RSP']
                   if all(key in one_day_returns for key in ('SPY', 'RSP'))
                   else None)
        mag7_rsp = (mag7_return - one_day_returns['RSP']
                    if mag7_return is not None and 'RSP' in one_day_returns
                    else None)
        self.num.update({
            'qqq_qqqe_spread_pct': round(qqq_qqqe, 4)
            if qqq_qqqe is not None else None,
            'spy_rsp_spread_pct': round(spy_rsp, 4)
            if spy_rsp is not None else None,
            'mag7_rsp_spread_pct': round(mag7_rsp, 4)
            if mag7_rsp is not None else None,
            'concentration_quality': 'OK'
            if None not in (qqq_qqqe, spy_rsp, mag7_rsp) else 'PARTIAL',
            'contribution_attribution_status': 'POINT_IN_TIME_WEIGHTS_UNAVAILABLE',
        })
        
        for k, t in {'dxy':'DX-Y.NYB', 'jpy':'JPY=X', 'oil':'CL=F', 'gold':'GC=F'}.items(): 
            self.regime[k] = round(df[t].iloc[-1], 2) if t in df.columns else '-'
        if 'BTC-USD' in df.columns: self.regime['btc'] = f"{int(df['BTC-USD'].iloc[-1]):,}"
        
        if self.liquidity.get('y10', '-') == '-' and '^TNX' in df.columns:
            s_tnx = self.clean_trading_days(df['^TNX'])
            if not s_tnx.empty:
                self.liquidity['y10'] = round(s_tnx.iloc[-1], 2)

        micro_add_score = 0
        try:
            h = self.data_cache.get('qqq_full', pd.DataFrame())
            if not h.empty:
                cl, hi, lo, vo = (h['Close']['QQQ'], h['High']['QQQ'], h['Low']['QQQ'], h['Volume']['QQQ']) if isinstance(h.columns, pd.MultiIndex) else (h['Close'], h['High'], h['Low'], h['Volume'])
                range_hl = (hi - lo).replace(0, 0.01)
                mfv = (((cl - lo) - (hi - cl)) / range_hl) * vo
                cmf_series = (mfv.rolling(21).sum() / vo.rolling(21).sum())
                cmf_series = cmf_series.replace([np.inf, -np.inf], np.nan).dropna()
                if not cmf_series.empty:
                    self.score_details['cmf'] = round(cmf_series.iloc[-1], 2)
                    self.num['cmf_source_date'] = pd.Timestamp(
                        cmf_series.index[-1]).strftime('%Y-%m-%d')
        except: pass

        try:
            tk_qqq = yf.Ticker("QQQ")
            opts = tk_qqq.options
            if opts: 
                valid_exps = [exp for exp in opts if datetime.strptime(exp, '%Y-%m-%d').date() >= datetime.now().date()]
                if valid_exps:
                    chain = tk_qqq.option_chain(valid_exps[0])
                    c_vol = pd.to_numeric(chain.calls['volume'], errors='coerce').fillna(0).sum()
                    p_vol = pd.to_numeric(chain.puts['volume'], errors='coerce').fillna(0).sum()
                    if c_vol > 0:
                        self.score_details['pcr'] = round(p_vol / c_vol, 2)
                        self.num['pcr_source_date'] = pd.Timestamp.now(
                            tz="America/New_York").strftime('%Y-%m-%d')
        except: pass

        self.vol_metrics = {'vix': '-', 'move': '-', 'vix_term': '-', 'vrp': '-', 'vvix': '-', 'skew': '-'}
        if '^VVIX' in df.columns: self.vol_metrics['vvix'] = round(df['^VVIX'].iloc[-1], 2)
        if '^SKEW' in df.columns: self.vol_metrics['skew'] = round(df['^SKEW'].iloc[-1], 2)
        if '^MOVE' in df.columns: 
            self.vol_metrics['move'] = round(df['^MOVE'].iloc[-1], 2)
            self.score_details['move'] = self.vol_metrics['move']
        
        if '^VIX' in df.columns:
            vix_cur = df['^VIX'].replace([np.inf, -np.inf], np.nan).dropna().iloc[-1]
            self.vol_metrics['vix'] = round(vix_cur, 2)
            self.score_details['vix'] = self.vol_metrics['vix']
            
            if '^VIX3M' in df.columns:
                vix3m_cur = df['^VIX3M'].replace([np.inf, -np.inf], np.nan).dropna().iloc[-1]
                ratio = vix_cur / vix3m_cur
                state = "🔴倒挂极限(Backwardation)" if ratio >= 1.0 else "🟢正常升水(Contango)"
                self.vol_metrics['vix_term'] = f"{ratio:.2f} [{state}]"
                self.num['vix_term_ratio'] = round(float(ratio), 4)
                
            if 'SPY' in df.columns:
                spy_s = self.clean_trading_days(df['SPY'])
                if len(spy_s) >= 21:
                    log_ret = np.log(spy_s / spy_s.shift(1)).dropna()
                    hv20 = log_ret.tail(20).std() * np.sqrt(252) * 100
                    vrp = vix_cur - hv20
                    vrp_state = "🔥极度恐慌(做市商Sell Put底牌)" if vrp > 10 else ("🧊情绪麻木" if vrp < 0 else "⚪情绪正常")
                    self.vol_metrics['vrp'] = f"{vrp:+.2f} [{vrp_state}] (HV20: {hv20:.1f})"
                    self.num['vrp'] = round(float(vrp), 2)
                    self.num['hv20'] = round(float(hv20), 2)

        self.risk_scissors = {
            'ratio': '-', 'roc_21d': '-', 'hyg_return_21d': '-',
            'tlt_return_21d': '-', 'state': '-',
        }
        if 'HYG' in df.columns and 'TLT' in df.columns:
            s_hyg, s_tlt = self.clean_trading_days(df['HYG']), self.clean_trading_days(df['TLT'])
            common_idx = s_hyg.index.intersection(s_tlt.index)
            if len(common_idx) >= 21:
                ratio_s = s_hyg.loc[common_idx] / s_tlt.loc[common_idx]
                cur_r, past_r = ratio_s.iloc[-1], ratio_s.iloc[-21]
                roc = (cur_r / past_r - 1) * 100
                hyg_return = (s_hyg.loc[common_idx].iloc[-1]
                              / s_hyg.loc[common_idx].iloc[-21] - 1) * 100
                tlt_return = (s_tlt.loc[common_idx].iloc[-1]
                              / s_tlt.loc[common_idx].iloc[-21] - 1) * 100
                state = classify_hyg_tlt(hyg_return, tlt_return, roc)
                self.risk_scissors = {
                    'ratio': f"{cur_r:.3f}",
                    'roc_21d': f"{roc:+.2f}%",
                    'hyg_return_21d': f"{hyg_return:+.2f}%",
                    'tlt_return_21d': f"{tlt_return:+.2f}%",
                    'state': state,
                }
                self.num['hyg_tlt_ratio'] = round(float(cur_r), 4)
                self.num['hyg_tlt_roc21'] = round(float(roc), 2)
                self.num['hyg_return_21d'] = round(float(hyg_return), 2)
                self.num['tlt_return_21d'] = round(float(tlt_return), 2)
                self.num['hyg_tlt_state'] = state

        cg_val = cg_z = '-'
        if 'HG=F' in df.columns and 'GC=F' in df.columns:
            cg_series = self.clean_trading_days((df['HG=F'] * 100) / df['GC=F'])
            if not cg_series.empty:
                cg_val = round(cg_series.iloc[-1], 2)
                cg_z = self.calc_zscore(cg_series)
        self.regime['cg_ratio'] = cg_val

        self.sector_rs = {}
        if 'SPY' in df.columns:
            spy_s = self.clean_trading_days(df['SPY'])
            for sym, name in self.sector_dict.items():
                if sym in df.columns:
                    sec_s = self.clean_trading_days(df[sym])
                    c_idx = sec_s.index.intersection(spy_s.index)
                    if len(c_idx) > 63:
                        sec_c, spy_c = sec_s.loc[c_idx], spy_s.loc[c_idx]
                        a_1w = ((sec_c.iloc[-1]/sec_c.iloc[-6]) - (spy_c.iloc[-1]/spy_c.iloc[-6])) * 100
                        a_1m = ((sec_c.iloc[-1]/sec_c.iloc[-22]) - (spy_c.iloc[-1]/spy_c.iloc[-22])) * 100
                        a_3m = ((sec_c.iloc[-1]/sec_c.iloc[-64]) - (spy_c.iloc[-1]/spy_c.iloc[-64])) * 100
                        self.sector_rs[name] = f"1周: {a_1w:+.2f}% | 1月: {a_1m:+.2f}% | 3月: {a_3m:+.2f}%"
                    else:
                        self.sector_rs[name] = "数据不足"

        # ==================== 修复的宏观打分引擎 (Macro Score) ====================
        m_score = 0
        
        # 1. 10Y美债急升度 (TNX)
        if '^TNX' in df.columns:
            z = self.calc_zscore(df['^TNX'], 63)
            self.macro_engines['tnx'] = f"Z: {z:+.2f}"
            self.num['tnx_z'] = round(z, 2)
            m_score += 15 if z > 2.0 else (5 if z > 1.0 else 0)
        
        # 2. 原油暴涨率 (OIL)
        if 'CL=F' in df.columns:
            oil = self.clean_trading_days(df['CL=F'])
            if len(oil) >= 21:
                oil_roc = (oil.iloc[-1] / oil.iloc[-21] - 1) * 100
                self.macro_engines['oil'] = f"ROC: {oil_roc:+.2f}%"
                self.num['oil_roc'] = round(float(oil_roc), 2)
                m_score += 15 if oil_roc > 15.0 else (10 if oil_roc > 10.0 else 0)
            else: self.macro_engines['oil'] = "-"
        else: self.macro_engines['oil'] = "-"

        if not fh.empty:
            # 3. 10Y-2Y利差变动 (Yield Curve)
            if 'Spread_10Y2Y' in fh.columns and len(fh['Spread_10Y2Y']) >= 21:
                yc_cur = fh['Spread_10Y2Y'].iloc[-1]
                yc_1m = fh['Spread_10Y2Y'].iloc[-21]
                yc_chg = yc_cur - yc_1m
                self.macro_engines['yc'] = f"{yc_chg:+.2f}%"
                self.num['yc_chg'] = round(float(yc_chg), 3)
                # 只有在倒挂期恶化时才计算宏观压力
                if yc_cur < 0: 
                    m_score += 15 if yc_chg > 0.2 else (5 if yc_chg > 0.0 else 0)
            else: self.macro_engines['yc'] = "-"

            # 4. 短期净流动性抽水 (Liquidity)
            nl = (fh['Fed_Assets']/1000) - (fh['TGA']/1000) - fh['RRP']
            if len(nl) >= 63:
                l_roc = (nl.iloc[-1] / nl.iloc[-63] - 1) * 100
                self.macro_engines['liq'] = f"63业务日ROC: {l_roc:+.2f}%"
                self.num['liq_roc'] = round(float(l_roc), 2)
                self.num['liq_roc_window'] = 63
                m_score += 15 if l_roc < -5.0 else (8 if l_roc < -2.0 else 0)
            
            # 5. M2 货币供应 (M2)
            if len(fh['M2_Money_Supply']) >= 252:
                m2_yoy = (fh['M2_Money_Supply'].iloc[-1] / fh['M2_Money_Supply'].iloc[-252] - 1) * 100
                self.macro_engines['m2'] = f"YoY: {m2_yoy:+.2f}%"
                self.num['m2_yoy'] = round(float(m2_yoy), 2)
                m_score += 10 if m2_yoy < 0 else (5 if m2_yoy < 2.0 else 0)
            
            # 6. 金融摩擦压力 (NFCI)
            if 'NFCI' in fh.columns:
                nfci_val = fh['NFCI'].iloc[-1]
                self.macro_engines['stress'] = f"{nfci_val:.2f} (NFCI)"
                self.num['nfci'] = round(float(nfci_val), 3)
                m_score += 10 if nfci_val > 1.0 else (5 if nfci_val > 0.5 else 0)
                
            # 7. 信用利差 (Credit Spread)
            cs_z = self.calc_zscore(fh['Credit_Spread'])
            self.macro_engines['credit'] = f"Z: {cs_z:+.2f}"
            self.num['credit_z'] = round(cs_z, 2)
            m_score += 10 if cs_z > 2.0 else (5 if cs_z > 1.0 else 0)
            
            # 8. 铜金比避险偏离 (Copper/Gold)
            self.macro_engines['cg'] = f"Z: {cg_z:+.2f}" if cg_z != '-' else '-'
            if cg_z != '-':
                self.num['cg_z'] = round(float(cg_z), 2)
                m_score += 10 if float(cg_z) < -2.0 else (5 if float(cg_z) < -1.0 else 0)
            
        self.score_details['macro_score'] = min(100, m_score)
        # ==================== 修复的宏观打分引擎结束 ====================

        # ==================== EOD跨资产战术压力（V8.6公式可靠性加固） ====================
        try:
            report_date = pd.Timestamp.now(
                tz="America/New_York").strftime('%Y-%m-%d')

            def component_lag(source_date):
                return (lag_trading_days(source_date, report_date)
                        if source_date else None)

            # 只有当日 SPY 截面有效时，才允许 TRIN 获得下跌确认加分。
            spy_down = False
            spy_source_date = self.market_source_dates.get('SPY')
            spy_down_available = component_lag(spy_source_date) == 0
            if spy_down_available and 'SPY' in df.columns:
                spy_s = self.clean_trading_days(df['SPY'])
                if len(spy_s) >= 2 and spy_s.iloc[-1] < spy_s.iloc[-2]:
                    spy_down = True

            trin_as_of = self.score_details.get('trin_as_of')
            trin_source_date = str(trin_as_of)[:10] if trin_as_of else None
            credit_source_date = self.fred_source_dates.get('Credit_Spread')
            inputs = {
                'vix': StressInput(
                    self.vol_metrics.get('vix'),
                    self.market_source_dates.get('^VIX'),
                    component_lag(self.market_source_dates.get('^VIX')),
                    'yfinance:^VIX'),
                'move': StressInput(
                    self.score_details.get('move'),
                    self.market_source_dates.get('^MOVE'),
                    component_lag(self.market_source_dates.get('^MOVE')),
                    'yfinance:^MOVE'),
                'credit_spread': StressInput(
                    self.liquidity.get('credit'), credit_source_date,
                    component_lag(credit_source_date),
                    self.liquidity_source_names.get('Credit_Spread')),
                'trin': StressInput(
                    self.score_details.get('trin'), trin_source_date,
                    component_lag(trin_source_date),
                    self.score_details.get('trin_source')),
                'pcr': StressInput(
                    self.score_details.get('pcr'),
                    self.num.get('pcr_source_date'),
                    component_lag(self.num.get('pcr_source_date')),
                    'yfinance:QQQ nearest-expiry option volume'),
                'cmf': StressInput(
                    self.score_details.get('cmf'),
                    self.num.get('cmf_source_date'),
                    component_lag(self.num.get('cmf_source_date')),
                    'yfinance:QQQ OHLCV'),
            }

            raw_vix = (self.clean_trading_days(raw_df['^VIX'])
                       if '^VIX' in raw_df.columns
                       else pd.Series(dtype=float))
            vix_chg_5d = (float(raw_vix.iloc[-1] - raw_vix.iloc[-6])
                          if len(raw_vix) >= 6 else None)
            vix_history = raw_vix.tail(252)
            vix_percentile = (float(
                (vix_history <= vix_history.iloc[-1]).mean() * 100.0)
                if len(vix_history) >= 60 else None)

            result = score_eod_stress(
                inputs, spy_down=spy_down,
                context={
                    'spy_down_available': spy_down_available,
                    'spy_source_date': spy_source_date,
                    'trin_scope': self.score_details.get('trin_scope'),
                    'trin_closing_auction_inclusion': self.score_details.get(
                        'trin_closing_auction_inclusion'),
                    'vix_5d_change': round(vix_chg_5d, 4)
                    if vix_chg_5d is not None else None,
                    'vix_percentile_252': round(vix_percentile, 2)
                    if vix_percentile is not None else None,
                    'vix_term_ratio': self.num.get('vix_term_ratio'),
                },
                computed_at=pd.Timestamp.now(tz='UTC').isoformat(),
            )
            self.stress_details = result.to_dict()
            self.score_details['eod_stress_score'] = result.score
            # 兼容旧列；不可用时同样保持 NULL，禁止回退成 0。
            self.score_details['micro_score'] = result.score
            if result.score is None:
                self.log(
                    "⚠️ 盘后跨资产战术压力不可用: "
                    + ", ".join(result.reasons))

        except Exception as e:
            self.log(f"⚠️ 盘后跨资产战术压力引擎异常: {e}")
            self.score_details['micro_score'] = None
            self.score_details['eod_stress_score'] = None
            self.stress_details = {
                'score': None, 'coverage': 0.0, 'confidence': 0.0,
                'status': 'UNAVAILABLE', 'reasons': ['INTERNAL_ERROR'],
                'components': {}, 'calc_version': 'eod_stress_v1.1-shadow',
                'computed_at': pd.Timestamp.now(tz='UTC').isoformat(),
            }
        # ==================== EOD跨资产战术压力结束 ====================

        p200 = self.score_details.get('pct_200ma', 0)
        if p200 != '-':
            if p200 > 60: self.score_details['skeleton'] = "🛡️ 结构牛市"
            elif p200 < 40: self.score_details['skeleton'] = "🧊 结构熊市"
            else: self.score_details['skeleton'] = "🔄 宽幅震荡"

        y10_val = self.liquidity['y10'] if self.liquidity['y10'] != '-' else (df['^TNX'].iloc[-1] if '^TNX' in df.columns else '-')
        oil_val = self.regime['oil']
        hyg_val = round(df['HYG'].iloc[-1], 2) if 'HYG' in df.columns else '-'
        
        if y10_val != '-' and oil_val != '-' and cg_val != '-':
            if y10_val > 4.3 and oil_val > 80 and cg_val > 20: self.regime['desc'] = "🔥 再通胀/不着陆"
            elif y10_val < 3.8 and hyg_val != '-' and hyg_val < 75 and oil_val < 70: self.regime['desc'] = "🧊 硬着陆/衰退"
            elif y10_val < 4.2 and hyg_val != '-' and hyg_val > 77 and oil_val < 75: self.regime['desc'] = "🌤️ 金发女孩"
            elif y10_val > 4.5 and cg_val < 18 and oil_val > 85: self.regime['desc'] = "🌪️ 滞胀"

        dix_div_alert = "⚪ 未触发"
        if 'SPY' in df.columns and self.smf.get('dix') != '-':
            spy_c = self.clean_trading_days(df['SPY'])
            if len(spy_c) >= 20:
                min_20d = spy_c.iloc[-21:-1].min()
                curr_px = spy_c.iloc[-1]
                dix_val = float(self.smf['dix'])
                if curr_px < min_20d and dix_val > 45.0:
                    dix_div_alert = (
                        f"⚠️【DIX价格背离观察】现价跌破20日低点，DIX为{dix_val}%；"
                        "DIX仅是FINRA短售量代理，不能据此归因为机构吸筹"
                    )
        self.score_details['dix_divergence'] = dix_div_alert

        self.heatmap_df, self.current_prices = pd.DataFrame(), {}
        for sym, name in self.tickers_dict.items():
            if sym in df.columns:
                s_data = self.clean_trading_days(df[sym])
                if len(s_data) > 64:
                    self.current_prices[name] = round(s_data.iloc[-1], 2)
                    self.heatmap_df[name] = {
                        '1_Day': round((s_data.iloc[-1]/s_data.iloc[-2]-1)*100, 2),
                        '1_Week': round((s_data.iloc[-1]/s_data.iloc[-6]-1)*100, 2),
                        '1_Month': round((s_data.iloc[-1]/s_data.iloc[-22]-1)*100, 2),
                        '3_Months': round((s_data.iloc[-1]/s_data.iloc[-64]-1)*100, 2)
                    }
        self.heatmap_df = self.heatmap_df.T

    def generate_outputs(self):
        mac_s, mic_s = self.score_details.get('macro_score', 0), self.score_details.get('micro_score', 0)
        stress_score_text = "N/A" if mic_s is None else str(mic_s)
        stress_coverage = self.stress_details.get('coverage')
        stress_confidence = self.stress_details.get('confidence')
        stress_quality_text = (
            f"status={self.stress_details.get('status', 'UNAVAILABLE')} | "
            f"coverage={stress_coverage:.0%} | confidence={stress_confidence:.0%} | "
            f"version={self.stress_details.get('calc_version', 'NA')}"
            if stress_coverage is not None and stress_confidence is not None
            else "status=UNAVAILABLE")
        m_val = lambda k: self.macro_engines.get(k, '-')
        etf_lines = []
        for ticker in ETF_FLOW_TICKERS:
            metric = self.smf.get('etf_share_metrics', {}).get(ticker, {})
            changes = metric.get('changes', {})
            parts = []
            for window in ('1d', '5d', '20d'):
                change = changes.get(window)
                if not change:
                    parts.append(f"{window}=NA")
                    continue
                pct = change.get('share_change_pct')
                flow = change.get('dollar_flow_m')
                pct_text = f"{pct:+.3f}%" if pct is not None else "NA"
                flow_text = f"${flow:+.2f}M" if flow is not None else "NA"
                parts.append(
                    f"{window} {pct_text} / {flow_text}")
            etf_lines.append(
                f"[{ticker}份额变化]: " + " | ".join(parts)
                + f" (source={metric.get('source', 'NA')}, context-only)")
        etf_detail_text = "\n".join(etf_lines) if etf_lines else "[ETF份额变化]: 历史不足"
        def cot_detail(key, output_key):
            meta = self.smf.get('cot_metadata', {}).get(key, {})
            return (
                f"净仓={self.smf.get(output_key, '-')} | "
                f"报告日={meta.get('report_date', 'NA')} | "
                f"类别={meta.get('category', 'NA')} | "
                f"净仓/OI={meta.get('net_open_interest_pct', 'NA')}% | "
                f"156周Z={meta.get('zscore_156w', 'NA')} | "
                f"样本={meta.get('sample_count', 0)}"
            )
        cot_vix_detail = cot_detail('vix', 'cot_vix')
        cot_sp500_detail = cot_detail('sp500', 'sp500_net')
        cot_nasdaq_detail = cot_detail('nasdaq100', 'nasdaq_net')
        
        text_output = f"""
[净新高−新低]: {self.score_details.get('net_nh_nl', '-')} (正值代表正向广度)
[深度广度推力信号]: {self.score_details.get('breadth_thrust', '-')}
[机构暗池背离信号]: {self.score_details.get('dix_divergence', '-')}

==== 🌊 全球宏观流动性 ====
[G3净流动性]: {self.liquidity.get('g3_liq', '-')}
[美联储总资产]: {self.liquidity.get('assets', '-')}
[真实净流动性]: {self.liquidity.get('net_liq', '-')}
[TGA账户余额]: {self.liquidity.get('tga', '-')}
[隔夜逆回购RRP]: {self.liquidity.get('rrp', '-')}
[银行准备金]: {self.liquidity.get('reserves', '-')}
[流动性来源日期]: Fed资产 {self.fred_source_dates.get('Fed_Assets', 'NA')} / TGA {self.fred_source_dates.get('TGA', 'NA')} / RRP {self.fred_source_dates.get('RRP', 'NA')} / 准备金 {self.fred_source_dates.get('Reserves', 'NA')}
[SOFR利率]: {self.liquidity.get('sofr', '-')}
[Chicago Fed NFCI]: {self.liquidity.get('nfci', '-')}

==== 📈 利率收益率结构 ====
[10年期美债]: {self.liquidity.get('y10', '-')}
[实际收益率TIPS]: {self.liquidity.get('tips', '-')}
[10Y-2Y利差]: {self.regime.get('spread', '-')}
[投资级信用利差]: {self.liquidity.get('ig_spread', '-')}
[高收益债信用利差]: {self.liquidity.get('credit', '-')}
[垃圾债资金流入]: {self.smf.get('junk_flow', '-')}
[铜金比]: {self.regime.get('cg_ratio', '-')}

==== 🌪️ 资产波动情绪 ====
[标普恐慌VIX]: {self.vol_metrics.get('vix', '-')}
[美债恐慌MOVE]: {self.vol_metrics.get('move', '-')}
[波率之波VVIX]: {self.vol_metrics.get('vvix', '-')}
[黑天鹅SKEW]: {self.vol_metrics.get('skew', '-')}
[期权看跌比PCR]: {self.score_details.get('pcr', '-')}
[VIX倒挂比]: {self.vol_metrics.get('vix_term', '-')}
[VRP波动率风险溢价]: {self.vol_metrics.get('vrp', '-')}
[VIX COT]: {cot_vix_detail}

==== 🦅 股市结构与广度 (样本: 前500大市值股) ====
[大盘骨架评级]: {self.score_details.get('skeleton', '-')}
[20MA占比]: {self.score_details.get('pct_20ma', '-')}
[50MA占比]: {self.score_details.get('pct_50ma', '-')}
[200MA占比]: {self.score_details.get('pct_200ma', '-')}
[净新高新低]: 创新高 {self.score_details.get('nh', '-')} / 创新低 {self.score_details.get('nl', '-')}
[当日上涨家数占比]: {self.score_details.get('pct_adv', '-')}%
[当日上涨/下跌成交量比]: {self.score_details.get('up_down_volume_ratio', '-')}
[前500大市值样本TRIN]: {self.score_details.get('trin', '-')} (NYSE+Nasdaq, 样本数={self.score_details.get('breadth_sample_size', 0)}, 截面={self.score_details.get('trin_as_of', 'NA')}, 收盘竞价=UNVERIFIED)
[做市商敞口GEX代理]: {self.smf.get('gex', '-')} (源日期={self.smf.get('dix_gex_source_date') or 'NA'}, context-only)
[DIX场外短售代理]: {self.smf.get('dix', '-')} (源日期={self.smf.get('dix_gex_source_date') or 'NA'}, context-only)

==== 指数集中度相对表现 ====
[QQQ−QQQE]: {self.num.get('qqq_qqqe_spread_pct', 'NA')}%
[SPY−RSP]: {self.num.get('spy_rsp_spread_pct', 'NA')}%
[Mag7−RSP]: {self.num.get('mag7_rsp_spread_pct', 'NA')}%
[集中度质量]: {self.num.get('concentration_quality', 'PARTIAL')}；正值仅表示市值权重相对占优
[成分贡献分解]: {self.num.get('contribution_attribution_status')}；缺少点时权重，不生成伪精确归因

==== ⚖️ 聪明钱避险剪刀差 (HYG/TLT) ====
[垃圾债/长债比值]: {self.risk_scissors.get('ratio', '-')}
[HYG 21日收益]: {self.risk_scissors.get('hyg_return_21d', '-')}
[TLT 21日收益]: {self.risk_scissors.get('tlt_return_21d', '-')}
[HYG/TLT 21日相对变化]: {self.risk_scissors.get('roc_21d', '-')} -> {self.risk_scissors.get('state', '-')}

==== 🏭 11 大 GICS 行业轮动 (相对 SPY 超额收益) ====
"""
        for name, data in self.sector_rs.items():
            text_output += f"[{name}]: {data}\n"

        text_output += f"""
==== 🌍 资产定价与仓位 ====
[S&P500 COT]: {cot_sp500_detail}
[Nasdaq100 COT]: {cot_nasdaq_detail}
[美元指数DXY]: {self.regime.get('dxy', '-')}
[日元汇率JPY]: {self.regime.get('jpy', '-')}
[WTI原油Oil]: {self.regime.get('oil', '-')}
[黄金XAU]: {self.regime.get('gold', '-')}
[比特币BTC]: {self.regime.get('btc', '-')}
[资金流向CMF]: {self.score_details.get('cmf', '-')}

==== 附加数据 ====
[宏观压力得分]: {mac_s}
[盘后跨资产战术压力观察值]: {stress_score_text}
[压力评分质量]: {stress_quality_text}
【判定象限】: {self.regime.get('desc', '-')}
[SPY份额]: {self.smf.get('spy_sh', '-')}
[QQQ份额]: {self.smf.get('qqq_sh', '-')}
[HYG份额]: {self.smf.get('hyg_sh', '-')}
[JNK份额]: {self.smf.get('jnk_sh', '-')}
[ETF份额来源声明]: yfinance sharesOutstanding/fast_info，未验证原生发布日期，仅作上下文，不参与Waterline评分
{etf_detail_text}
[HYG价格]: {self.smf.get('hyg_px', '-')}
[JNK价格]: {self.smf.get('jnk_px', '-')}

==== 🏛️ 宏观八大引擎数据 ====
[宏观]10Y急升度Z: {m_val('tnx')}
[宏观]原油20日ROC: {m_val('oil')}
[宏观]10Y-2Y利差变动: {m_val('yc')}
[宏观]净流动性ROC: {m_val('liq')}
[宏观]M2同比YoY: {m_val('m2')}
[宏观]STLFSI压力: {m_val('stress')}
[宏观]信用利差Z: {m_val('credit')}
[宏观]铜金比Z: {m_val('cg')}

==== 四大指数动能矩阵 ====
"""
        for name in self.heatmap_df.index:
            try:
                chg_1d = self.heatmap_df.loc[name, '1_Day']
                chg_1w = self.heatmap_df.loc[name, '1_Week']
                chg_1m = self.heatmap_df.loc[name, '1_Month']
                chg_3m = self.heatmap_df.loc[name, '3_Months']
                text_output += f"[{name}] 最新价: ${self.current_prices.get(name, 0)} | 1日: {chg_1d:+.2f}% | 1周: {chg_1w:+.2f}% | 1月: {chg_1m:+.2f}% | 3月: {chg_3m:+.2f}%\n"
            except:
                text_output += f"[{name}] 最新价: ${self.current_prices.get(name, 0)}\n"

        print(text_output)
        return text_output

    # ================= 结构化落库出口 =================
    def get_db_payload(self, date_str=None):
        """直接从内部状态组装 market_history 的结构化 payload。
        替代 auto_review 旧有的『打印文本 -> 50条正则抠数字』链路：
        文案/emoji 变动不再影响入库数据。返回 (payload, cn_row_dict)。"""
        date_str = date_str or pd.Timestamp.now(
            tz="America/New_York").strftime('%Y-%m-%d')

        def nz(v):
            # '-' / NaN / 带逗号字符串 统一转 float 或 None
            if v is None or v == '-': return None
            try:
                f = float(str(v).replace(',', '').replace('%', ''))
                return f if not math.isnan(f) else None
            except (ValueError, TypeError):
                return None

        liq, sd, sm, rg, vm, num = self.liquidity, self.score_details, self.smf, self.regime, self.vol_metrics, self.num
        stress = self.stress_details
        net_nh_nl = sd.get('net_nh_nl', '-')
        stress_components = dict(stress.get('components') or {})
        stress_components['_meta'] = {
            'status': stress.get('status', 'UNAVAILABLE'),
            'reasons': stress.get('reasons', []),
        }

        payload = {
            "record_date": date_str,
            "macro_score": int(sd.get('macro_score', 0) or 0),
            "micro_score": nullable_int(sd.get('micro_score')),
            "eod_stress_score": nullable_int(sd.get('eod_stress_score')),
            "stress_components": stress_components,
            "stress_coverage": stress.get('coverage'),
            "stress_confidence": stress.get('confidence'),
            "stress_calc_version": stress.get('calc_version'),
            "stress_computed_at": stress.get('computed_at'),
            "regime": rg.get('desc'),
            "skeleton": sd.get('skeleton', '未知'),
            "breadth_thrust": sd.get('breadth_thrust'),
            "dix_div": sd.get('dix_divergence'),
            "g3_liq": nz(liq.get('g3_liq')), "fed_assets": nz(liq.get('assets')),
            "net_liq": nz(liq.get('net_liq')), "tga": nz(liq.get('tga')),
            "rrp": nz(liq.get('rrp')), "reserves": nz(liq.get('reserves')),
            "sofr": nz(liq.get('sofr')), "nfci": nz(liq.get('nfci')),
            "us10y": nz(liq.get('y10')), "tips": nz(liq.get('tips')),
            "spread_10y2y": nz(rg.get('spread')),
            "ig_spread": nz(liq.get('ig_spread')), "credit_spread": nz(liq.get('credit')),
            "junk_flow": nz(sm.get('junk_flow')), "cg_ratio": nz(rg.get('cg_ratio')),
            "vix": nz(vm.get('vix')), "vix_term": vm.get('vix_term'),
            "move": nz(vm.get('move')), "vvix": nz(vm.get('vvix')),
            "skew": nz(vm.get('skew')), "pcr": nz(sd.get('pcr')),
            "cot_vix": nz(sm.get('cot_vix')), "vrp": vm.get('vrp'),
            "pct_20ma": nz(sd.get('pct_20ma')), "pct_50ma": nz(sd.get('pct_50ma')),
            "pct_200ma": nz(sd.get('pct_200ma')),
            "pct_adv": nz(sd.get('pct_adv')),
            "up_down_volume_ratio": nz(sd.get('up_down_volume_ratio')),
            "new_highs": nz(sd.get('nh')), "new_lows": nz(sd.get('nl')),
            "net_nh_nl": nz(sd.get('net_nh_nl')),
            "nh_nl": f"创新高 {sd.get('nh', '-')} / 创新低 {sd.get('nl', '-')}",
            "trin": nz(sd.get('trin')), "gex": nz(sm.get('gex')), "dix": nz(sm.get('dix')),
            "trin_scope": sd.get('trin_scope'),
            "trin_source": sd.get('trin_source'),
            "trin_as_of": sd.get('trin_as_of'),
            "trin_closing_auction_inclusion": sd.get(
                'trin_closing_auction_inclusion'),
            "breadth_sample_size": int(sd.get('breadth_sample_size', 0) or 0),
            "qqq_qqqe_spread_pct": num.get('qqq_qqqe_spread_pct'),
            "spy_rsp_spread_pct": num.get('spy_rsp_spread_pct'),
            "mag7_rsp_spread_pct": num.get('mag7_rsp_spread_pct'),
            "breadth_diff_pct": num.get('mag7_rsp_spread_pct'),
            "concentration_quality": num.get('concentration_quality'),
            "sp500_net": nz(sm.get('sp500_net')), "nasdaq_net": nz(sm.get('nasdaq_net')),
            "dxy": nz(rg.get('dxy')), "jpy": nz(rg.get('jpy')), "oil": nz(rg.get('oil')),
            "gold": nz(rg.get('gold')), "btc": nz(rg.get('btc')), "cmf": nz(sd.get('cmf')),
            "spy_sh": str(sm.get('spy_sh', '-')), "qqq_sh": str(sm.get('qqq_sh', '-')),
            "hyg_sh": str(sm.get('hyg_sh', '-')), "jnk_sh": str(sm.get('jnk_sh', '-')),
            "hyg_px": nz(sm.get('hyg_px')), "jnk_px": nz(sm.get('jnk_px')),
            "hyg_tlt_ratio": num.get('hyg_tlt_ratio'),
            "hyg_tlt_trend": f"{self.risk_scissors.get('roc_21d', '-')} -> {self.risk_scissors.get('state', '-')}",
            "hyg_return_21d": num.get('hyg_return_21d'),
            "tlt_return_21d": num.get('tlt_return_21d'),
            "hyg_tlt_state": num.get('hyg_tlt_state'),
            "tnx_z": num.get('tnx_z'), "oil_roc": num.get('oil_roc'),
            "yc_chg": num.get('yc_chg'), "liq_roc": num.get('liq_roc'),
            "liq_roc_window": num.get('liq_roc_window'),
            "m2_yoy": num.get('m2_yoy'), "credit_z": num.get('credit_z'),
            "cg_z": num.get('cg_z'),
            # 旧列仅为兼容；不再冒充 Hindenburg/黑天鹅指标。
            "hindenburg": f"净新高-新低: {net_nh_nl}",
            "dix_gex_source_date": sm.get('dix_gex_source_date'),
            "cot_report_date": sm.get('cot_report_date'),
            "cot_category": sm.get('cot_category'),
            "cot_metadata": sm.get('cot_metadata', {}),
        }
        # 结构化数值附录（含 vrp 数值、vix_term 比值），供异常引擎读取
        payload["full_metrics"] = {
            **{k: v for k, v in payload.items() if k != 'full_metrics'},
            "vrp_num": num.get('vrp'), "hv20": num.get('hv20'),
            "vix_term_ratio": num.get('vix_term_ratio'),
            "hyg_tlt_roc21": num.get('hyg_tlt_roc21'),
            "breadth_sample": "top500_by_mktcap",
            "fred_source_dates": self.fred_source_dates,
            "liquidity_source_names": self.liquidity_source_names,
            "liquidity_source_contract": "official_liquidity_v2",
            "etf_share_metrics": self.smf.get('etf_share_metrics', {}),
            "contribution_attribution_status": num.get(
                'contribution_attribution_status'),
            "net_liq_source_date": min(
                [self.fred_source_dates.get(key) for key in ('Fed_Assets', 'TGA', 'RRP')
                 if self.fred_source_dates.get(key)],
                default=None,
            ),
        }

        # 本地 CSV 备份用的中文表头行（保持既有格式）
        cn_row = {
            '日期': date_str, '[总评]宏观象限': payload['regime'],
            '[总评]宏观压力得分': payload['macro_score'],
            '[总评]盘后跨资产战术压力观察值': payload['eod_stress_score'],
            '[总评]压力评分覆盖率': payload['stress_coverage'],
            '[总评]压力评分置信度': payload['stress_confidence'],
            '[总评]压力评分版本': payload['stress_calc_version'],
            '[价格]QQQ': self.current_prices.get('NDX 100 (QQQ)', '-'),
            '[价格]SPY': self.current_prices.get('S&P 500 (SPY)', '-'),
            '[总评]大盘骨架': payload['skeleton'], '[广度]净新高-新低': payload['net_nh_nl'],
            '[信号]广度推力': payload['breadth_thrust'], '[信号]暗池背离': payload['dix_div'],
            '[波动]VRP风险溢价': payload['vrp'], '[波动]VIX期限结构': payload['vix_term'],
            '[避险]垃圾债/长债比': payload['hyg_tlt_ratio'], '[避险]21天动能': payload['hyg_tlt_trend'],
            '[引擎]10Y急升度Z': payload['tnx_z'], '[引擎]原油20日ROC': payload['oil_roc'],
            '[引擎]10Y-2Y变动': payload['yc_chg'], '[引擎]净流动性ROC': payload['liq_roc'],
            '[引擎]M2同比YoY': payload['m2_yoy'], '[引擎]压力指数(NFCI)': payload['nfci'],
            '[引擎]信用利差Z': payload['credit_z'], '[引擎]铜金比Z': payload['cg_z'],
            '[流动性]G3净流动(B)': payload['g3_liq'], '[流动性]美联储资产(B)': payload['fed_assets'],
            '[流动性]真实净流动(B)': payload['net_liq'], '[流动性]TGA(B)': payload['tga'],
            '[流动性]逆回购(B)': payload['rrp'], '[流动性]准备金(T)': payload['reserves'],
            '[流动性]SOFR利率(%)': payload['sofr'], '[流动性]Chicago Fed NFCI': payload['nfci'],
            '[利率]10Y美债(%)': payload['us10y'], '[利率]TIPS(%)': payload['tips'],
            '[利率]10Y-2Y利差(bps)': payload['spread_10y2y'], '[利率]投资级利差(%)': payload['ig_spread'],
            '[利率]信用利差(%)': payload['credit_spread'], '[利率]垃圾债流向(M)': payload['junk_flow'],
            '[利率]铜金比': payload['cg_ratio'],
            '[波动]VIX': payload['vix'], '[波动]MOVE': payload['move'], '[波动]VVIX': payload['vvix'],
            '[波动]SKEW': payload['skew'], '[波动]PCR': payload['pcr'], '[波动]VIX净持仓': payload['cot_vix'],
            '[股市]20MA占比(%)': payload['pct_20ma'], '[股市]50MA占比(%)': payload['pct_50ma'],
            '[股市]200MA占比(%)': payload['pct_200ma'], '[股市]净新高新低': payload['nh_nl'],
            '[股市]前500大市值样本TRIN': payload['trin'], '[股市]GEX敞口(B)': payload['gex'],
            '[股市]DIX暗池(%)': payload['dix'],
            '[宏观]SP500净持仓': payload['sp500_net'], '[宏观]纳指净持仓': payload['nasdaq_net'],
            '[宏观]美元DXY': payload['dxy'], '[宏观]日元JPY': payload['jpy'],
            '[宏观]原油Oil': payload['oil'], '[宏观]黄金XAU': payload['gold'],
            '[宏观]比特币BTC': payload['btc'], '[宏观]CMF流向': payload['cmf'],
            '[资金]SPY份额': payload['spy_sh'], '[资金]QQQ份额': payload['qqq_sh'],
            '[资金]HYG份额': payload['hyg_sh'], '[资金]JNK份额': payload['jnk_sh'],
        }
        return payload, cn_row

if __name__ == "__main__":
    app = UltimateDashboard()
    app.fetch_market_data()
    app.fetch_liquidity_and_smf()
    app.calculate_metrics()
    app.generate_outputs()
